"""Events tab: edit the robot's events.ini without touching the text (Orchestron 2.31.0+).

What every transmitter control does is a rule in events.ini (see eventsini.py). This tab
reads the file from the robot's SD card, shows each rule in plain words, edits them with
dropdowns (with Learn: move a control and the editor picks the channel and position), and
writes the file back. The robot then reloads it and reports any line it didn't accept,
which shows on that row. While connected, a row lights up when its rule fires.

Files can also be opened and saved on the PC, with or without a robot.

Threading as app.py: robot I/O runs as jobs on app.py's io worker; callbacks only change
the document and set flags; widgets are created and rebuilt on the render loop (update()).
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

import dearpygui.dearpygui as dpg

import eventsini as ev
from protocol import ProtocolError

MIN_FW = 23100                  # first Orchestron with <KF> <KU> <KV> <KI>
POLL_S = 0.25                   # <KI> while the editor or the Inputs tab is open
LEARN_S = 4.0
FIRED_S = 1.5
MAX_MODIFIERS = 8

COL_HEAD = (150, 200, 255)
COL_DIM = (140, 140, 140)
COL_BAD = (235, 110, 110)
COL_WARN = (230, 180, 90)
COL_FIRED = (255, 225, 90)
COL_OK = (150, 220, 150)
COL_TEXT = (230, 230, 230)


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.doc = ev.EventsDoc(ev.NEW_FILE)
        self.loaded = False             # a document is showing (from the robot, a file or New)
        self.source = ""                # "robot" / a path / "new"
        self.dirty = False              # edited since it was loaded or saved
        self.need_rebuild = True        # render loop rebuilds the views
        self.busy = ""                  # a job is running: what it's doing
        self.note = ""                  # one-line result under the toolbar
        # Problems from the robot's last load, and the line -> Line map of the file it loaded
        # (both keyed by Line object, so they stay on the right row while lines are edited)
        self.problems: dict[int, str] = {}       # id(Line) -> text
        self.loose_problems: list[str] = []      # line 0: not one line
        self.synced: dict[int, ev.Line] = {}     # robot's line number -> Line
        self.fired: dict[int, float] = {}        # id(Line) -> time it fired
        self.notices: deque = deque(maxlen=64)   # line numbers from the reader thread
        # From the robot, for the dropdowns
        self.sequences: list[str] = []
        self.wavs: list[str] = []
        self.inputs: tuple[bool, int, list[int]] | None = None
        self.poll_pending = False
        self.next_poll = 0.0
        self.learn_until = 0.0
        self.learn_base: list[int] | None = None
        self.learn_target = ""          # "rule" or "mod"
        # Connection
        self.fw = 0
        self.supported = False


S = _State()

# Hooks into app.py (init())
_io_put: Callable[[Callable[[], None]], None] = lambda job: None
_get_bot: Callable[[], object] = lambda: None
_set_status: Callable[[str], None] = lambda msg: None
_param_keys: Callable[[], list[str]] = lambda: []
_on_saved: Callable[[], None] = lambda: None

# Editor state (main thread only)
_edit_line: int | None = None       # line being edited, None = a new rule
_edit_mod_line: int | None = None
_raw_line: int | None = None


def init(io_put, get_bot, set_status, param_keys, on_saved) -> None:
    global _io_put, _get_bot, _set_status, _param_keys, _on_saved
    _io_put, _get_bot, _set_status, _param_keys, _on_saved = io_put, get_bot, set_status, param_keys, on_saved


# --------------------------------------------------------------------------- #
# Connection (called by app.py)
# --------------------------------------------------------------------------- #
def on_connect(bot, fw: int, profile: str) -> None:
    """io worker, right after connecting."""
    with S.lock:
        S.fw = fw
        S.supported = profile == "Orchestron" and fw >= MIN_FW
        S.need_rebuild = True
    if not S.supported:
        return
    bot.set_notice_handler(S.notices.append)
    try:
        bot.rule_notices(True)
    except ProtocolError:
        pass


def on_disconnect() -> None:
    with S.lock:
        S.supported = False
        S.inputs = None
        S.poll_pending = False
        S.fired.clear()
        S.busy = ""
        S.need_rebuild = True


def _bot():
    return _get_bot() if S.supported else None


# --------------------------------------------------------------------------- #
# Jobs (io worker)
# --------------------------------------------------------------------------- #
def _sync_from(lines: list[ev.Line], rules: int, problems: list[tuple[int, str]]) -> str:
    """After a load or save: the robot's file is these lines. Caller holds S.lock."""
    S.synced = {i + 1: l for i, l in enumerate(lines)}
    S.problems, S.loose_problems = {}, []
    for line, text in problems:
        l = S.synced.get(line)
        if l is not None:
            S.problems[id(l)] = (S.problems.get(id(l), "") + "\n" + text).strip()
        else:
            S.loose_problems.append(text)
    n = len(problems)
    return f"{rules} rule(s) loaded" + (f", {n} problem(s) - see the red rows" if n else ", no problems")


def job_load() -> None:
    def run() -> None:
        bot = _bot()
        if bot is None:
            return
        _busy("Reading events.ini ...")
        try:
            data = bot.read_file("events.ini", lambda d, n: _busy(f"Reading events.ini {d}/{n} bytes"))
            rules, problems = bot.events_report()
            _busy("Reading sequences.ini ...")
            seq = bot.read_file("sequences.ini", lambda d, n: _busy(f"Reading sequences.ini {d}/{n} bytes"))
            _busy("Listing WAV files ...")
            wavs = bot.wav_files()
        except ProtocolError as e:
            _done(f"Load failed: {e}")
            return
        doc = ev.EventsDoc(data.decode("utf-8", "replace") if data is not None else ev.NEW_FILE)
        with S.lock:
            S.doc, S.loaded, S.source, S.dirty = doc, True, "robot", data is None
            S.sequences = re.findall(r"^\s*\[sequence\.([^\]\s]+)\]", (seq or b"").decode("utf-8", "replace"),
                                     re.MULTILINE | re.IGNORECASE)
            S.wavs = wavs
            note = _sync_from(doc.lines, rules, problems) if data is not None else \
                "The robot has no events.ini yet - this is a new one; Save to robot writes it"
            if data is None:
                S.synced, S.problems, S.loose_problems = {}, {}, []
            S.need_rebuild = True
        _done(note)
    _io_put(run)


def job_save() -> None:
    with S.lock:
        doc = S.doc
        text = doc.text()
        lines = list(doc.lines)          # what is uploaded, whatever is edited meanwhile
    data = text.encode("utf-8")

    def run() -> None:
        bot = _bot()
        if bot is None:
            return
        try:
            bot.write_file("events.ini", data, lambda d, n: _busy(f"Writing events.ini {d}/{n} bytes"))
            _busy("The robot is loading events.ini ...")
            rules, problems = bot.events_report(reload=True)
        except ProtocolError as e:
            _done(f"Save failed: {e}. The file on the robot wasn't changed.")
            return
        with S.lock:
            if S.doc is doc and doc.text() == text:
                S.dirty = False
            note = _sync_from(lines, rules, problems)
            S.need_rebuild = True
        _done("Saved and loaded: " + note)
        _on_saved()
    _io_put(run)


def job_test(actions: list[str]) -> None:
    def run() -> None:
        bot = _bot()
        if bot is None:
            return
        out = []
        for a in actions:
            try:
                ok, msg = bot.run_action(a)
                out.append(f"{a}: {msg}" if ok else f"{a}: FAILED {msg}")
            except ProtocolError as e:
                out.append(f"{a}: {e}")
        _set_status("Test - " + "; ".join(out) if out else "Nothing to test")
    _io_put(run)


def job_poll_inputs() -> None:
    def run() -> None:
        bot = _bot()
        try:
            got = bot.inputs() if bot is not None else None
        except ProtocolError:
            got = None
        with S.lock:
            S.inputs = got
            S.poll_pending = False
    _io_put(run)


def _busy(msg: str) -> None:
    with S.lock:
        S.busy = msg


def _done(msg: str) -> None:
    with S.lock:
        S.busy = ""
        S.note = msg
    _set_status(msg)


# --------------------------------------------------------------------------- #
# Document edits (callbacks)
# --------------------------------------------------------------------------- #
def _changed() -> None:
    with S.lock:
        S.dirty = True
        S.need_rebuild = True


def _new_doc() -> None:
    with S.lock:
        S.doc, S.loaded, S.source, S.dirty = ev.EventsDoc(ev.NEW_FILE), True, "new", True
        S.synced, S.problems, S.loose_problems = {}, {}, []
        S.need_rebuild = True
        S.note = "New events.ini - Save to robot or Save file when it's ready"


def _open_file(path: str) -> None:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        _set_status(f"Can't open {path}: {e}")
        return
    with S.lock:
        S.doc, S.loaded, S.source, S.dirty = ev.EventsDoc(text), True, path, False
        S.synced, S.problems, S.loose_problems = {}, {}, []
        S.need_rebuild = True
        S.note = f"Opened {path} - Save to robot to use it"


def _save_file(path: str) -> None:
    with S.lock:
        text = S.doc.text()
    try:
        Path(path).write_bytes(text.encode("utf-8"))
    except OSError as e:
        _set_status(f"Can't save {path}: {e}")
        return
    _set_status(f"Saved {path}")


def _delete(line_no: int) -> None:
    with S.lock:
        S.doc.delete_line(line_no)
    _changed()


# --------------------------------------------------------------------------- #
# Rule editor
# --------------------------------------------------------------------------- #
_SOURCES = ["Pad button", "Channel (switch / stick)", "RC link", "Mode change"]
_SOURCE_KEYS = ["pad", "channel", "link", "mode"]
_ACTION_ITEMS = ["(none)"] + [label for _w, label, _a in ev.ACTIONS]
_CHANNELS = [f"ch{c}" for c in range(1, ev.MAX_CHANNEL + 1)]


def _cond_set(prefix: str, cond: ev.Condition) -> None:
    dpg.set_value(f"{prefix}_ch", f"ch{cond.channel}")
    dpg.set_value(f"{prefix}_kind", ev.COND_LABELS[cond.kind])
    dpg.set_value(f"{prefix}_a", cond.a if cond.kind not in ev.ZONES else 1500)
    dpg.set_value(f"{prefix}_b", cond.b)
    _cond_show(prefix)


def _cond_get(prefix: str) -> ev.Condition:
    ch = int(dpg.get_value(f"{prefix}_ch")[2:])
    label = dpg.get_value(f"{prefix}_kind")
    kind = next(k for k, v in ev.COND_LABELS.items() if v == label)
    a, b = int(dpg.get_value(f"{prefix}_a")), int(dpg.get_value(f"{prefix}_b"))
    if kind == "range" and b < a:
        a, b = b, a
    if kind not in ("near", "range"):
        b = 0
    return ev.Condition(ch, kind, a, b)


def _cond_show(prefix: str) -> None:
    label = dpg.get_value(f"{prefix}_kind")
    kind = next(k for k, v in ev.COND_LABELS.items() if v == label)
    dpg.configure_item(f"{prefix}_a", show=kind not in ev.ZONES)
    dpg.configure_item(f"{prefix}_b", show=kind in ("near", "range"),
                       label="high end (us)" if kind == "range" else "+/- us (0 = deadband)")
    dpg.configure_item(f"{prefix}_a", label="low end (us)" if kind == "range" else "us")
    _preview()


def _cond_widgets(prefix: str) -> None:
    """Channel, condition and value(s), with Learn and the channel's live value."""
    with dpg.group(horizontal=True):
        dpg.add_combo(_CHANNELS, tag=f"{prefix}_ch", width=70, default_value="ch1",
                      callback=lambda: _preview())
        dpg.add_combo(list(ev.COND_LABELS.values()), tag=f"{prefix}_kind", width=210,
                      default_value=ev.COND_LABELS["high"], callback=lambda: _cond_show(prefix))
        dpg.add_button(label="Learn", tag=f"{prefix}_learn", callback=lambda: _learn_start(prefix))
        with dpg.tooltip(dpg.last_item()):
            dpg.add_text("Press, then within 4 s move the switch or stick to where it should\n"
                         "trigger and leave it there. Picks the channel that moved most.")
    with dpg.group(horizontal=True):
        dpg.add_input_int(tag=f"{prefix}_a", width=110, default_value=1500, min_value=ev.US_MIN,
                          max_value=ev.US_MAX, min_clamped=True, max_clamped=True, step=10,
                          callback=lambda: _preview())
        dpg.add_input_int(tag=f"{prefix}_b", width=110, default_value=0, min_value=0, max_value=1000,
                          min_clamped=True, max_clamped=True, step=10, callback=lambda: _preview())
    with dpg.group(horizontal=True):
        dpg.add_progress_bar(tag=f"{prefix}_live", width=320, default_value=0.5, overlay="-")
        dpg.add_text("", tag=f"{prefix}_met")


def _rule_from_editor() -> ev.Rule:
    src = _SOURCE_KEYS[_SOURCES.index(dpg.get_value("re_source"))]
    t = ev.Trigger(src)
    mods = [dpg.get_item_label(f"re_mod_{i}") for i in range(MAX_MODIFIERS)
            if dpg.is_item_shown(f"re_mod_{i}") and dpg.get_value(f"re_mod_{i}")]
    if src == "pad":
        t.button = int(dpg.get_value("re_button"))
        t.gesture = dpg.get_value("re_gesture")
        t.mods = mods
    elif src == "channel":
        t.cond = _cond_get("re_c")
        t.exit = bool(dpg.get_value("re_exit"))
    elif src == "link":
        t.link = dpg.get_value("re_link")
    else:
        t.mode = dpg.get_value("re_mode")
    actions = []
    for i in range(ev.MAX_ACTIONS):
        label = dpg.get_value(f"re_act_{i}")
        if label == "(none)":
            continue
        word = next(w for w, l, _a in ev.ACTIONS if l == label)
        kind = ev.ACTION_ARG[word]
        arg = ""
        if kind:
            arg = (dpg.get_value(f"re_argc_{i}") if dpg.is_item_shown(f"re_argc_{i}")
                   else dpg.get_value(f"re_argt_{i}")) or ""
            if kind == "wav":
                arg = re.match(r"\s*(\d*)", arg).group(1)
        actions.append(ev.join_action(word, arg))
    when_mods = [] if src == "pad" else mods
    when_modes = [m for m in ev.MODES if dpg.get_value(f"re_when_{m}")]
    if len(when_modes) == len(ev.MODES):
        when_modes = []
    comment = ""
    if _edit_line is not None:
        old = S.doc.rule_at(_edit_line)
        if old is not None and old.rule is not None:
            comment = old.rule.comment
    return ev.Rule(t, actions, when_mods, when_modes, comment)


def _preview(*_args) -> None:
    if not dpg.does_item_exist("re_preview") or not dpg.is_item_shown("rule_editor"):
        return
    src = _SOURCE_KEYS[_SOURCES.index(dpg.get_value("re_source"))]
    for key in _SOURCE_KEYS:
        dpg.configure_item(f"re_grp_{key}", show=key == src)
    dpg.set_value("re_mods_label", "Only while these modifiers are held:" if src == "pad"
                  else "Only while held (when=):")
    for i in range(ev.MAX_ACTIONS):
        label = dpg.get_value(f"re_act_{i}")
        word = next((w for w, l, _a in ev.ACTIONS if l == label), None)
        kind = ev.ACTION_ARG.get(word) if word else None
        choices = _arg_choices(kind)
        dpg.configure_item(f"re_argc_{i}", show=bool(kind) and bool(choices), items=choices)
        dpg.configure_item(f"re_argt_{i}", show=bool(kind) and not choices)
        if choices and dpg.get_value(f"re_argc_{i}") not in choices:
            dpg.set_value(f"re_argc_{i}", choices[0])
    try:
        rule = _rule_from_editor()
        text = rule.line()
        if not rule.actions:
            text += "      <- pick at least one action"
        dpg.set_value("re_preview", text)
        dpg.set_value("re_words", f"When {rule.trigger.describe()}"
                      + (f" ({rule.describe_when()})" if rule.describe_when() else "")
                      + ": " + (", ".join(ev.describe_action(a) for a in rule.actions) or "-")
                      + ("\nState rule: also applied at power-up and when the link returns."
                         if rule.is_state_rule() else ""))
    except (ValueError, StopIteration) as e:
        dpg.set_value("re_preview", f"? {e}")


def _arg_choices(kind: str | None) -> list[str]:
    with S.lock:
        if kind == "seq":
            names = list(S.sequences)
            names += [n for n in S.doc.sequence_names() if n not in names]
            return names
        if kind == "wav":
            return [f"{m.group(1)}  {w}" for w in S.wavs if (m := re.match(r"(\d+)", w))]
        if kind == "preset":
            return list(S.doc.presets())
    return ev.ARG_CHOICES.get(kind or "", [])


def _open_rule_editor(line_no: int | None) -> None:
    global _edit_line
    _edit_line = line_no
    with S.lock:
        names = S.doc.modifier_names()[:MAX_MODIFIERS]
        l = S.doc.rule_at(line_no) if line_no else None
    rule = l.rule if l is not None and l.rule is not None else ev.Rule(ev.Trigger("pad"), ["seq:wave"])
    t = rule.trigger
    dpg.set_value("re_source", _SOURCES[_SOURCE_KEYS.index(t.source)])
    dpg.set_value("re_button", str(t.button))
    dpg.set_value("re_gesture", t.gesture)
    _cond_set("re_c", t.cond)
    dpg.set_value("re_exit", t.exit)
    dpg.set_value("re_link", t.link)
    dpg.set_value("re_mode", t.mode)
    held = set(t.mods if t.source == "pad" else rule.when_mods)
    for i in range(MAX_MODIFIERS):
        shown = i < len(names)
        dpg.configure_item(f"re_mod_{i}", show=shown, label=names[i] if shown else "")
        dpg.set_value(f"re_mod_{i}", shown and names[i] in held)
    dpg.configure_item("re_nomods", show=not names)
    for m in ev.MODES:
        dpg.set_value(f"re_when_{m}", m in rule.when_modes)
    for i in range(ev.MAX_ACTIONS):
        if i < len(rule.actions):
            word, arg = ev.split_action(rule.actions[i])
            dpg.set_value(f"re_act_{i}", ev.ACTION_LABEL.get(word, "(none)"))
            choices = _arg_choices(ev.ACTION_ARG.get(word))
            match = next((c for c in choices if c == arg or c.split()[0] == arg), None) if arg else None
            dpg.set_value(f"re_argc_{i}", match or (choices[0] if choices else ""))
            dpg.set_value(f"re_argt_{i}", arg)
        else:
            dpg.set_value(f"re_act_{i}", "(none)")
            dpg.set_value(f"re_argt_{i}", "")
    dpg.configure_item("rule_editor", show=True, label="Edit rule" if line_no else "New rule")
    _preview()


def _rule_ok() -> None:
    rule = _rule_from_editor()
    if not rule.actions:
        _set_status("Pick at least one action")
        return
    with S.lock:
        S.doc.set_rule(_edit_line, rule)
    dpg.configure_item("rule_editor", show=False)
    _changed()


def _rule_test() -> None:
    job_test(_rule_from_editor().actions)


def _build_rule_editor() -> None:
    with dpg.window(tag="rule_editor", label="Rule", modal=True, show=False, width=640, height=470,
                    on_close=lambda: _learn_stop()):
        dpg.add_text("When", color=COL_HEAD)
        dpg.add_radio_button(_SOURCES, tag="re_source", horizontal=True, default_value=_SOURCES[0],
                             callback=_preview)
        with dpg.group(tag="re_grp_pad"):
            with dpg.group(horizontal=True):
                dpg.add_text("button")
                dpg.add_combo([str(b) for b in range(1, ev.MAX_BUTTON + 1)], tag="re_button", width=60,
                              default_value="1", callback=_preview)
                dpg.add_text("is")
                dpg.add_combo(ev.GESTURES, tag="re_gesture", width=90, default_value="click",
                              callback=_preview)
                dpg.add_text("", tag="re_pad_live", color=COL_DIM)
        with dpg.group(tag="re_grp_channel", show=False):
            _cond_widgets("re_c")
            dpg.add_checkbox(label="fire when it LEAVES this position instead (.exit)", tag="re_exit",
                             callback=_preview)
        with dpg.group(tag="re_grp_link", show=False, horizontal=True):
            dpg.add_text("the RC link is")
            dpg.add_combo(["lost", "up"], tag="re_link", width=80, default_value="lost", callback=_preview)
        with dpg.group(tag="re_grp_mode", show=False, horizontal=True):
            dpg.add_text("the motion mode changes to")
            dpg.add_combo(ev.MODES, tag="re_mode", width=100, default_value="idle", callback=_preview)

        dpg.add_spacer(height=4)
        dpg.add_text("Only while these modifiers are held:", tag="re_mods_label", color=COL_DIM)
        with dpg.group(horizontal=True):
            for i in range(MAX_MODIFIERS):
                dpg.add_checkbox(label="", tag=f"re_mod_{i}", show=False, callback=_preview)
            dpg.add_text("(none defined - see the Modifiers tab)", tag="re_nomods", color=COL_DIM)
        dpg.add_text("Only in these modes (none ticked = any mode):", color=COL_DIM)
        with dpg.group(horizontal=True):
            for m in ev.MODES:
                dpg.add_checkbox(label=m, tag=f"re_when_{m}", callback=_preview)

        dpg.add_separator()
        dpg.add_text("Do", color=COL_HEAD)
        for i in range(ev.MAX_ACTIONS):
            with dpg.group(horizontal=True):
                dpg.add_combo(_ACTION_ITEMS, tag=f"re_act_{i}", width=230, default_value="(none)",
                              callback=_preview)
                dpg.add_combo([], tag=f"re_argc_{i}", width=300, show=False, callback=_preview)
                dpg.add_input_text(tag=f"re_argt_{i}", width=300, show=False, hint="name",
                                   callback=_preview)

        dpg.add_separator()
        dpg.add_text("", tag="re_words", wrap=600)
        dpg.add_text("", tag="re_preview", color=COL_DIM)
        with dpg.group(horizontal=True):
            dpg.add_button(label="OK", width=100, callback=_rule_ok)
            dpg.add_button(label="Test now", width=100, callback=_rule_test, tag="re_test")
            with dpg.tooltip(dpg.last_item()):
                dpg.add_text("Runs the actions on the robot now (the rule isn't saved).")
            dpg.add_button(label="Cancel", width=100,
                           callback=lambda: (_learn_stop(), dpg.configure_item("rule_editor", show=False)))


# --------------------------------------------------------------------------- #
# Modifier editor, raw line editor
# --------------------------------------------------------------------------- #
def _open_mod_editor(line_no: int | None) -> None:
    global _edit_mod_line
    _edit_mod_line = line_no
    with S.lock:
        mods = {n: (name, c) for n, name, c, _e in S.doc.modifiers()}
    name, cond = mods.get(line_no, ("", None)) if line_no else ("", None)
    dpg.set_value("me_name", name)
    _cond_set("me_c", cond or ev.Condition(1, "high"))
    dpg.configure_item("mod_editor", show=True)


def _mod_ok() -> None:
    name = (dpg.get_value("me_name") or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,15}", name) or \
            re.match(r"(pad|button|ch|mode|link)", name, re.IGNORECASE):
        dpg.set_value("me_err", "A name of letters/digits (up to 16), not starting with pad, ch, mode, "
                      "link or button")
        return
    with S.lock:
        S.doc.set_modifier(_edit_mod_line, name, _cond_get("me_c"))
    _learn_stop()
    dpg.configure_item("mod_editor", show=False)
    _changed()


def _build_mod_editor() -> None:
    with dpg.window(tag="mod_editor", label="Modifier", modal=True, show=False, width=520, height=300,
                    on_close=lambda: _learn_stop()):
        dpg.add_text("A modifier is a switch or stick position that other rules can require\n"
                     "(\"shift + pad button 1\", or when=shift).", color=COL_DIM)
        with dpg.group(horizontal=True):
            dpg.add_text("name")
            dpg.add_input_text(tag="me_name", width=160)
        dpg.add_text("is held while", color=COL_HEAD)
        _cond_widgets("me_c")
        dpg.add_text("", tag="me_err", color=COL_BAD)
        with dpg.group(horizontal=True):
            dpg.add_button(label="OK", width=100, callback=_mod_ok)
            dpg.add_button(label="Cancel", width=100,
                           callback=lambda: (_learn_stop(), dpg.configure_item("mod_editor", show=False)))


def _open_raw_editor(line_no: int) -> None:
    global _raw_line
    _raw_line = line_no
    with S.lock:
        l = S.doc.lines[line_no - 1]
        err = l.error
    dpg.set_value("raw_text", l.raw)
    dpg.set_value("raw_err", err)
    dpg.configure_item("raw_editor", show=True)


def _raw_ok() -> None:
    with S.lock:
        S.doc.set_raw(_raw_line, dpg.get_value("raw_text"))
        err = S.doc.lines[_raw_line - 1].error
    if err:
        dpg.set_value("raw_err", err)
        _changed()
        return
    dpg.configure_item("raw_editor", show=False)
    _changed()


def _build_raw_editor() -> None:
    with dpg.window(tag="raw_editor", label="Edit line", modal=True, show=False, width=640, height=170):
        dpg.add_text("This line couldn't be read as a rule. Fix the text:", color=COL_DIM)
        dpg.add_input_text(tag="raw_text", width=-1)
        dpg.add_text("", tag="raw_err", color=COL_BAD, wrap=600)
        with dpg.group(horizontal=True):
            dpg.add_button(label="OK", width=100, callback=_raw_ok)
            dpg.add_button(label="Cancel", width=100, callback=lambda: dpg.configure_item("raw_editor", show=False))


# --------------------------------------------------------------------------- #
# Learn and live values
# --------------------------------------------------------------------------- #
def _learn_start(prefix: str) -> None:
    with S.lock:
        if S.inputs is None:
            _set_status("Learn needs the robot connected (and its transmitter on)")
            return
        S.learn_base = list(S.inputs[2])
        S.learn_until = time.time() + LEARN_S
        S.learn_target = prefix
    _set_status("Learn: move the switch or stick to where it should trigger, and leave it there ...")


def _learn_stop() -> None:
    with S.lock:
        S.learn_until = 0.0
        S.learn_base = None


def _learn_finish() -> None:
    with S.lock:
        base, prefix = S.learn_base, S.learn_target
        now = list(S.inputs[2]) if S.inputs else None
        S.learn_until, S.learn_base = 0.0, None
    if not base or not now:
        return
    ch = ev.learn_channel(base, now)
    if not ch:
        _set_status("Learn: nothing moved - try again and move the control further")
        return
    cond = ev.suggest_condition(ch, now[ch - 1])
    _cond_set(prefix, cond)
    _set_status(f"Learn: ch{ch} at {now[ch - 1]} us -> {cond.describe()}")


def _update_live(inputs) -> None:
    up, pad, us = inputs if inputs else (False, 0, [])
    with S.lock:
        deadband = S.doc.deadband()
    for prefix in ("re_c", "me_c"):
        if not dpg.does_item_exist(f"{prefix}_live"):
            continue
        ch = int(dpg.get_value(f"{prefix}_ch")[2:])
        if not us or ch > len(us):
            dpg.configure_item(f"{prefix}_live", overlay="no live value (robot not connected)")
            dpg.set_value(f"{prefix}_live", 0.0)
            dpg.set_value(f"{prefix}_met", "")
            continue
        v = us[ch - 1]
        dpg.set_value(f"{prefix}_live", min(1.0, max(0.0, (v - 900) / 1200.0)))
        dpg.configure_item(f"{prefix}_live", overlay=f"ch{ch} now {v} us" + ("" if up else "  (link down)"))
        met = _cond_get(prefix).holds(v, deadband)
        dpg.set_value(f"{prefix}_met", "condition met" if met else "")
        dpg.configure_item(f"{prefix}_met", color=COL_OK)
    if dpg.does_item_exist("re_pad_live"):
        dpg.set_value("re_pad_live", f"   (pad now: {'button ' + str(pad) if pad else 'none'})" if us else "")
    if dpg.does_item_exist("inputs_group") and dpg.is_item_visible("inputs_group"):
        for ch in range(1, ev.MAX_CHANNEL + 1):
            tag = f"in_bar_{ch}"
            if not us:
                dpg.configure_item(tag, overlay=f"ch{ch}  -")
                dpg.set_value(tag, 0.0)
                continue
            v = us[ch - 1]
            dpg.set_value(tag, min(1.0, max(0.0, (v - 900) / 1200.0)))
            dpg.configure_item(tag, overlay=f"ch{ch}  {v} us")
        dpg.set_value("in_status", ("RC link up" if up else "RC link DOWN") +
                      (f"   pad: button {pad}" if pad else "   pad: -") if us else "Not connected")


# --------------------------------------------------------------------------- #
# Views (render loop)
# --------------------------------------------------------------------------- #
def _rebuild() -> None:
    with S.lock:
        doc = S.doc
        problems = dict(S.problems)
        loose = list(S.loose_problems)
        supported, loaded = S.supported, S.loaded
    _rebuild_rules(doc, problems, loose, supported, loaded)
    _rebuild_mods(doc, problems)
    _rebuild_presets(doc, problems)
    _rebuild_settings(doc)
    dpg.set_value("ev_text", doc.text().replace("\r\n", "\n"))


def _rebuild_rules(doc, problems, loose, supported, loaded) -> None:
    dpg.delete_item("ev_rules", children_only=True)
    if not loaded:
        dpg.add_text("Load from robot, open a file, or start a new events.ini.", parent="ev_rules",
                     color=COL_WARN)
        return
    for text in loose:
        dpg.add_text(f"Robot: {text}", parent="ev_rules", color=COL_BAD, wrap=900)
    with dpg.table(parent="ev_rules", header_row=True, row_background=True, policy=dpg.mvTable_SizingStretchProp,
                   borders_innerH=False, borders_outerH=True):
        dpg.add_table_column(label="line", width_fixed=True, init_width_or_weight=40)
        dpg.add_table_column(label="When", init_width_or_weight=3)
        dpg.add_table_column(label="Do", init_width_or_weight=3)
        dpg.add_table_column(label="Only if", init_width_or_weight=2)
        dpg.add_table_column(label="", width_fixed=True, init_width_or_weight=100)
        dpg.add_table_column(label="", width_fixed=True, init_width_or_weight=170)
        section_comment = ""
        for n, l in doc.rules():
            # The comment line just above a rule is shown as its heading
            prev = doc.lines[n - 2].raw.strip() if n >= 2 else ""
            if prev.startswith((";", "#")) and prev != section_comment:
                section_comment = prev
                with dpg.table_row():
                    dpg.add_text("")
                    dpg.add_text(prev.lstrip(";# "), color=COL_HEAD)
            problem = problems.get(id(l), "") or l.error
            with dpg.table_row():
                dpg.add_text(str(n), color=COL_DIM)
                if l.rule is None:
                    dpg.add_text(l.raw.strip(), color=COL_BAD)
                    dpg.add_text("(can't read this line)", color=COL_BAD)
                    dpg.add_text("")
                else:
                    dpg.add_text(l.rule.trigger.describe(), color=COL_BAD if problem else COL_TEXT)
                    with dpg.tooltip(dpg.last_item()):
                        dpg.add_text(l.raw.strip())
                    dpg.add_text(", ".join(ev.describe_action(a) for a in l.rule.actions))
                    dpg.add_text(l.rule.describe_when(), color=COL_DIM)
                with dpg.group(horizontal=True):
                    dpg.add_text("", tag=f"ev_fired_{id(l)}", color=COL_FIRED)
                    if problem:
                        dpg.add_text("problem", color=COL_BAD)
                        with dpg.tooltip(dpg.last_item()):
                            dpg.add_text(problem, wrap=500)
                with dpg.group(horizontal=True):
                    if l.rule is None:
                        dpg.add_button(label="Fix", small=True, user_data=n,
                                       callback=lambda s, a, u: _open_raw_editor(u))
                    else:
                        dpg.add_button(label="Edit", small=True, user_data=n,
                                       callback=lambda s, a, u: _open_rule_editor(u))
                        dpg.add_button(label="Test", small=True, user_data=list(l.rule.actions),
                                       enabled=supported, callback=lambda s, a, u: job_test(u))
                    dpg.add_button(label="Delete", small=True, user_data=n,
                                   callback=lambda s, a, u: _delete(u))


def _rebuild_mods(doc, problems) -> None:
    dpg.delete_item("ev_mods", children_only=True)
    with dpg.table(parent="ev_mods", header_row=True, row_background=True, policy=dpg.mvTable_SizingStretchProp):
        dpg.add_table_column(label="line", width_fixed=True, init_width_or_weight=40)
        dpg.add_table_column(label="Name", init_width_or_weight=1)
        dpg.add_table_column(label="Held while", init_width_or_weight=3)
        dpg.add_table_column(label="", width_fixed=True, init_width_or_weight=120)
        for n, name, cond, err in doc.modifiers():
            problem = problems.get(id(doc.lines[n - 1]), "") or err
            with dpg.table_row():
                dpg.add_text(str(n), color=COL_DIM)
                dpg.add_text(name)
                dpg.add_text(cond.describe() if cond else doc.lines[n - 1].raw.strip(),
                             color=COL_BAD if problem else COL_TEXT)
                if problem:
                    with dpg.tooltip(dpg.last_item()):
                        dpg.add_text(problem, wrap=500)
                with dpg.group(horizontal=True):
                    if cond:
                        dpg.add_button(label="Edit", small=True, user_data=n,
                                       callback=lambda s, a, u: _open_mod_editor(u))
                    dpg.add_button(label="Delete", small=True, user_data=n, callback=lambda s, a, u: _delete(u))


def _rebuild_presets(doc, problems) -> None:
    dpg.delete_item("ev_presets", children_only=True)
    presets = doc.presets()
    if not presets:
        dpg.add_text("No presets yet.", parent="ev_presets", color=COL_DIM)
    bad = {l.section[7:] for l in doc.lines if l.section.startswith("preset.") and id(l) in problems}
    for name, entries in presets.items():
        with dpg.group(parent="ev_presets"):
            with dpg.group(horizontal=True):
                dpg.add_text(f"preset:{name}", color=COL_BAD if name in bad else COL_HEAD)
                dpg.add_button(label="Apply text", small=True, user_data=name,
                               callback=lambda s, a, u: _preset_apply(u))
                dpg.add_button(label="Delete preset", small=True, user_data=name,
                               callback=lambda s, a, u: _preset_delete(u))
                dpg.add_button(label="Test", small=True, user_data=[f"preset:{name}"],
                               callback=lambda s, a, u: job_test(u))
                with dpg.tooltip(dpg.last_item()):
                    dpg.add_text("Applies the preset as the robot has it (Save to robot first).")
            if name in bad:
                dpg.add_text("\n".join(problems[id(l)] for l in doc.lines
                                       if l.section == "preset." + name and id(l) in problems),
                             color=COL_BAD, wrap=800)
            dpg.add_input_text(tag=f"ev_preset_{name}", multiline=True, width=600, height=90,
                               default_value="\n".join(f"{k} = {v}" for k, v in entries))
            dpg.add_spacer(height=6)


def _preset_apply(name: str) -> None:
    text = dpg.get_value(f"ev_preset_{name}") or ""
    entries = []
    for line in text.splitlines():
        if "=" in line and not line.strip().startswith((";", "#")):
            k, v = line.split("=", 1)
            if k.strip():
                entries.append((k.strip(), v.strip()))
    with S.lock:
        S.doc.set_preset(name, entries)
    _changed()


def _preset_delete(name: str) -> None:
    with S.lock:
        S.doc.delete_preset(name)
    _changed()


def _preset_add() -> None:
    name = (dpg.get_value("ev_new_preset") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", name):
        _set_status("A preset name is 1-15 letters, digits or _")
        return
    key = dpg.get_value("ev_preset_key") or "fx.pitch.on"
    with S.lock:
        if name.lower() in S.doc.presets():
            _set_status(f"There's already a preset {name}")
            return
        S.doc.set_preset(name, [(key, "1")])
    _changed()


def _rebuild_settings(doc) -> None:
    dpg.set_value("ev_deadband", doc.deadband())
    pad = doc.pad_channel()
    dpg.set_value("ev_pad", f"ch{pad}" if pad else "none")
    keys = _param_keys()
    dpg.configure_item("ev_preset_key", items=keys)


def _settings_changed() -> None:
    with S.lock:
        S.doc.set_value("settings", "deadband", str(int(dpg.get_value("ev_deadband"))))
        S.doc.set_value("inputs", "pad", dpg.get_value("ev_pad"))
    _changed()


def _text_apply() -> None:
    text = (dpg.get_value("ev_text") or "").replace("\r\n", "\n")
    with S.lock:
        S.doc = ev.EventsDoc(text if text.endswith("\n") else text + "\n")
        S.loaded = True
    _changed()


# --------------------------------------------------------------------------- #
# Layout and the per-frame update (render loop)
# --------------------------------------------------------------------------- #
def build() -> None:
    """Inside app.py's `with dpg.tab(label="Events"):`."""
    with dpg.file_dialog(tag="ev_open_dlg", show=False, width=640, height=420, directory_selector=False,
                         callback=lambda s, a: _open_file(a["file_path_name"])):
        dpg.add_file_extension(".ini")
        dpg.add_file_extension(".*")
    with dpg.file_dialog(tag="ev_save_dlg", show=False, width=640, height=420, directory_selector=False,
                         default_filename="events", callback=lambda s, a: _save_file(a["file_path_name"])):
        dpg.add_file_extension(".ini")

    dpg.add_text("What the transmitter's switches, sticks and button pad do (the robot's events.ini). "
                 "Orchestron firmware 2.31.0+ for the robot buttons; files work without a robot.",
                 wrap=1000, color=COL_DIM)
    with dpg.group(horizontal=True):
        dpg.add_button(label="Load from robot", tag="ev_btn_load", callback=lambda: job_load())
        dpg.add_button(label="Save to robot", tag="ev_btn_save", callback=lambda: job_save())
        with dpg.tooltip(dpg.last_item()):
            dpg.add_text("Writes events.ini (the old one is kept as events.ini.bak),\n"
                         "then the robot loads it and reports any line it didn't accept.")
        dpg.add_spacer(width=20)
        dpg.add_button(label="Open file...", callback=lambda: dpg.show_item("ev_open_dlg"))
        dpg.add_button(label="Save file...", callback=lambda: dpg.show_item("ev_save_dlg"))
        dpg.add_button(label="New", callback=_new_doc)
        dpg.add_spacer(width=20)
        dpg.add_text("", tag="ev_dirty", color=COL_WARN)
    dpg.add_text("", tag="ev_note", wrap=1000)
    dpg.add_separator()
    with dpg.tab_bar():
        with dpg.tab(label="Rules"):
            with dpg.group(horizontal=True):
                dpg.add_button(label="Add rule", callback=lambda: _open_rule_editor(None))
                dpg.add_text("A row flashes when its rule fires on the robot.", color=COL_DIM)
            with dpg.child_window(border=False, height=-1):
                dpg.add_group(tag="ev_rules")
        with dpg.tab(label="Modifiers"):
            dpg.add_button(label="Add modifier", callback=lambda: _open_mod_editor(None))
            dpg.add_group(tag="ev_mods")
        with dpg.tab(label="Presets"):
            dpg.add_text("A preset sets any config settings by name (rule action preset:NAME). "
                         "Edit the lines, then Apply text.", color=COL_DIM, wrap=900)
            with dpg.group(horizontal=True):
                dpg.add_input_text(tag="ev_new_preset", width=140, hint="new preset name")
                dpg.add_combo([], tag="ev_preset_key", width=260)
                with dpg.tooltip(dpg.last_item()):
                    dpg.add_text("The first setting of the new preset (the connected robot's keys)")
                dpg.add_button(label="Add preset", callback=_preset_add)
            with dpg.child_window(border=False, height=-1):
                dpg.add_group(tag="ev_presets")
        with dpg.tab(label="Settings"):
            with dpg.group(horizontal=True):
                dpg.add_text("Value deadband (us)")
                dpg.add_input_int(tag="ev_deadband", width=120, default_value=50, min_value=1, max_value=1000,
                                  min_clamped=True, max_clamped=True, on_enter=True, callback=_settings_changed)
            with dpg.group(horizontal=True):
                dpg.add_text("Button pad channel  ")
                dpg.add_combo(["none"] + _CHANNELS, tag="ev_pad", width=120, default_value="none",
                              callback=_settings_changed)
            dpg.add_text("The deadband is how far either side of a value a rule such as \"ch13 at 1500 us\" "
                         "still counts. The pad channel carries the 14-button pad.", color=COL_DIM, wrap=800)
        with dpg.tab(label="Inputs", tag="ev_inputs_tab"):
            dpg.add_text("", tag="in_status")
            with dpg.group(tag="inputs_group"):
                for ch in range(1, ev.MAX_CHANNEL + 1):
                    dpg.add_progress_bar(tag=f"in_bar_{ch}", width=500, default_value=0.0, overlay=f"ch{ch}")
        with dpg.tab(label="Text"):
            dpg.add_text("The whole file. Edit it here if you prefer, then Use this text.", color=COL_DIM)
            dpg.add_button(label="Use this text", callback=_text_apply)
            dpg.add_input_text(tag="ev_text", multiline=True, width=-1, height=-1, tab_input=True)

    _build_rule_editor()
    _build_mod_editor()
    _build_raw_editor()


def update() -> None:
    """Every frame, from app.py's render loop."""
    now = time.time()
    with S.lock:
        rebuild, S.need_rebuild = S.need_rebuild, False
        busy, note, dirty, supported, source = S.busy, S.note, S.dirty, S.supported, S.source
        inputs = S.inputs
        learn_until = S.learn_until
        lines = []
        while S.notices:
            lines.append(S.notices.popleft())
        for n in lines:
            l = S.synced.get(n)
            if l is not None:
                S.fired[id(l)] = now
        fired = dict(S.fired)
        polling = supported and not S.poll_pending and now >= S.next_poll
    if rebuild:
        _rebuild()
    dpg.configure_item("ev_btn_load", enabled=supported and not busy)
    dpg.configure_item("ev_btn_save", enabled=supported and not busy)
    dpg.set_value("ev_note", busy or note)
    dpg.set_value("ev_dirty", ("unsaved changes" if dirty else "") +
                  (f"   ({Path(source).name})" if source and source not in ("robot", "new") else ""))
    for key, t in fired.items():
        tag = f"ev_fired_{key}"
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, "fired" if now - t < FIRED_S else "")
    with S.lock:
        for key in [k for k, t in S.fired.items() if now - t >= FIRED_S]:
            del S.fired[key]

    # Live values while an editor or the Inputs tab is open
    want = dpg.is_item_shown("rule_editor") or dpg.is_item_shown("mod_editor") or \
        dpg.is_item_visible("inputs_group")
    if want and polling:
        with S.lock:
            S.poll_pending = True
            S.next_poll = now + POLL_S
        job_poll_inputs()
    if want:
        _update_live(inputs)
    if learn_until and now >= learn_until:
        _learn_finish()
    elif learn_until:
        for prefix in ("re_c", "me_c"):
            if dpg.does_item_exist(f"{prefix}_learn"):
                dpg.configure_item(f"{prefix}_learn", label=f"Learning {learn_until - now:.0f}s")
    else:
        for prefix in ("re_c", "me_c"):
            if dpg.does_item_exist(f"{prefix}_learn"):
                dpg.configure_item(f"{prefix}_learn", label="Learn")
