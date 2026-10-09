"""Events tab: edit the robot's events.ini without touching the text (Orchestron 2.31.0+;
activities and the 2.32 actions with firmware 2.32.0+; named buttons and cycle() 2.36.0+;
set: raise by / lower by and the repeat gesture 2.37.0+).

What every transmitter control does is a rule in events.ini (see eventsini.py). This tab
reads the file from the robot's SD card, shows each rule in plain words, edits them with
dropdowns (with Learn: move a control and the editor picks the channel and position), and
writes the file back. The robot then reloads it and reports any line it didn't accept,
which shows on that row. While connected, a row lights up when its rule fires. The Buttons
tab lists the [buttons] with a live "down" light; its editor's Capture takes a transmitter
button's channel and value.

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
POLL_S = 0.25                   # <KI> while an editor, the Buttons or the Inputs tab is open
LEARN_S = 4.0
LEARN_MOVE = 150                # us a control must move for Learn
CAPTURE_MOVE = 40               # ... for Capture: buttons on one channel can be ~90 us apart
FIRED_S = 1.5
MAX_MODIFIERS = ev.MAX_MODIFIERS
_LEARN_LABELS = {"re_c": "Learn", "me_c": "Learn", "be_c": "Capture"}   # condition widgets

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
        # <KI>: link up, pad button, channels in us, named buttons held (None: before 2.36)
        self.inputs: tuple[bool, int, list[int], int | None] | None = None
        self.button_width = ev.button_width_us()   # config.ini button.deadband in us
        self.poll_pending = False
        self.next_poll = 0.0
        self.learn_until = 0.0
        self.learn_base: list[int] | None = None
        self.learn_target = ""          # the condition widgets' prefix (_LEARN_LABELS)
        self.learn_move = LEARN_MOVE
        self.capture_base: list[int] | None = None  # channels when the button editor opened
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
_raw_line: int | None = None
_cycle_next: dict[str, int] = {}    # Test on a cycle(...): the item it runs next, as the robot steps it
# Buttons tab rows: (id(Line), name, condition; None if the line has a problem), file order
_btn_rows: list[tuple[int, str, ev.Condition | None]] = []


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
    # A cycle runs its next item, each Test the next one
    todo = []
    for a in actions:
        try:
            items = ev.cycle_items(a)
        except ev.EventsError:
            items = None
        if items:
            i = _cycle_next.get(a, 0) % len(items)
            _cycle_next[a] = i + 1
            a = items[i]
        todo.append(a)

    def run() -> None:
        bot = _bot()
        if bot is None:
            return
        out = []
        for a in todo:
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
            got = bot.inputs_and_buttons() if bot is not None else None
        except ProtocolError:
            got = None
        width = None
        if bot is not None:
            # button.deadband in us, from the robot's settings (a live edit counts at once)
            width = ev.button_width_us({k: p.value for k, p in bot.params.items() if k in ev.BUTTON_WIDTH_KEYS})
        with S.lock:
            S.inputs = got
            S.button_width = width or S.button_width
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
_SOURCES = ["Pad button", "Named button", "Channel (switch / stick)", "RC link", "Mode change"]
_SOURCE_KEYS = ["pad", "named", "channel", "link", "mode"]
_BUTTONS = ("pad", "named")         # sources with gestures and modifier+ prefixes (banks)
_ACTION_ITEMS = ["(none)"] + [label for _w, label, _a in ev.ACTIONS]
_CHANNELS = [f"ch{c}" for c in range(1, ev.MAX_CHANNEL + 1)]
# set:'s operation (raise by / lower by: Orchestron 2.37.0+)
_SET_OPS = {"set to": "=", "raise by": "+=", "lower by": "-="}
_SET_OP_LABELS = {op: label for label, op in _SET_OPS.items()}


def _setting_ranges() -> dict[str, tuple[int, int]] | None:
    """The connected Orchestron's settings, {key: (min, max)} as set: takes them; None when not
    connected (then the robot checks set:'s setting and range when it loads the file)."""
    bot = _bot()
    params = getattr(bot, "params", None)
    return {k: (p.vmin, p.vmax) for k, p in params.items()} if params else None


# --------------------------------------------------------------------------- #
# Action rows: an action dropdown and its value widgets (rule and activity editors)
# --------------------------------------------------------------------------- #
def _row(prefix: str, i: int | None = None) -> Callable[[str], str]:
    """An action row's widget tags: _row("re", 0)("argt") -> "re_argt_0", _row("ae")("argt") -> "ae_argt"."""
    return lambda name: f"{prefix}_{name}" + ("" if i is None else f"_{i}")


def _arg_widgets(tag: Callable[[str], str], callback) -> None:
    """The value widgets after a row's action dropdown: a dropdown or a text field, and for set:
    the operation and the value (the text field then holds the setting)."""
    dpg.add_combo([], tag=tag("argc"), width=300, show=False, callback=callback)
    dpg.add_input_text(tag=tag("argt"), width=300, show=False, callback=callback)
    dpg.add_combo(list(_SET_OPS), tag=tag("setop"), width=90, default_value="set to", show=False,
                  callback=callback)
    with dpg.tooltip(dpg.last_item()):
        dpg.add_text("set to: KEY=VALUE (a state rule on a channel).\n"
                     "raise by / lower by: KEY+=N / KEY-=N, a step of 1 or more that stops at the\n"
                     "setting's max / min (Orchestron 2.37.0+). With a repeat gesture: hold to keep going.")
    dpg.add_input_text(tag=tag("setv"), width=70, show=False, callback=callback)


def _row_word(tag: Callable[[str], str]) -> str | None:
    label = dpg.get_value(tag("act"))
    return next((w for w, l, _a in ev.ACTIONS if l == label), None)


def _action_get(tag: Callable[[str], str]) -> str | None:
    """The action a row says ('seq:wave', 'set:audio.mix.wavB+=10'); None for (none)."""
    word = _row_word(tag)
    if word is None:
        return None
    kind = ev.ACTION_ARG[word]
    arg = ""
    if kind == "set":
        arg = ev.join_set(dpg.get_value(tag("argt")) or "", _SET_OPS.get(dpg.get_value(tag("setop")), "="),
                          dpg.get_value(tag("setv")) or "")
    elif kind:
        arg = (dpg.get_value(tag("argc")) if dpg.is_item_shown(tag("argc"))
               else dpg.get_value(tag("argt"))) or ""
        if kind == "wav":
            arg = re.match(r"\s*(\d*)", arg).group(1)
    return ev.join_action(word, arg)


def _action_show(tag: Callable[[str], str]) -> None:
    """Show the row's value widgets for its action."""
    word = _row_word(tag)
    kind = ev.ACTION_ARG.get(word) if word else None
    choices = _arg_choices(kind)
    setting = kind == "set"
    dpg.configure_item(tag("argc"), show=bool(kind) and bool(choices), items=choices)
    dpg.configure_item(tag("argt"), show=bool(kind) and not choices, hint=ev.ARG_HINTS.get(kind or "", ""),
                       width=200 if setting else 300)
    dpg.configure_item(tag("setop"), show=setting)
    dpg.configure_item(tag("setv"), show=setting, hint="value" if dpg.get_value(tag("setop")) == "set to" else "step")
    if choices and dpg.get_value(tag("argc")) not in choices:
        dpg.set_value(tag("argc"), choices[0])


def _action_put(tag: Callable[[str], str], action: str | None, unknown: str) -> None:
    """Fill a row from one action (None: (none)); unknown: the action dropdown's item for a word
    it doesn't offer."""
    word, arg = ev.split_action(action) if action else ("", "")
    kind = ev.ACTION_ARG.get(word)
    dpg.set_value(tag("act"), ev.ACTION_LABEL.get(word, unknown))
    choices = _arg_choices(kind)
    match = next((c for c in choices if c == arg or c.split()[0] == arg), None) if arg else None
    dpg.set_value(tag("argc"), match or (choices[0] if choices else ""))
    key, op, value = (ev.split_set(arg) or (arg, "=", "")) if kind == "set" else (arg, "=", "")
    dpg.set_value(tag("argt"), key)
    dpg.set_value(tag("setop"), _SET_OP_LABELS[op])
    dpg.set_value(tag("setv"), value)


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
                       label="high end (us)" if kind == "range" else
                       "+/- us (0 = button.deadband)" if prefix == "be_c" else "+/- us (0 = deadband)")
    dpg.configure_item(f"{prefix}_a", label="low end (us)" if kind == "range" else "us")
    _preview()


def _cond_widgets(prefix: str) -> None:
    """Channel, condition and value(s), with Learn (Capture in the button editor) and the
    channel's live value."""
    capture = _LEARN_LABELS[prefix] == "Capture"
    with dpg.group(horizontal=True):
        dpg.add_combo(_CHANNELS, tag=f"{prefix}_ch", width=70, default_value="ch1",
                      callback=lambda: _preview())
        dpg.add_combo(list(ev.COND_LABELS.values()), tag=f"{prefix}_kind", width=210,
                      default_value=ev.COND_LABELS["high"], callback=lambda: _cond_show(prefix))
        dpg.add_button(label=_LEARN_LABELS[prefix], tag=f"{prefix}_learn",
                       callback=lambda: _capture(prefix) if capture else _learn_start(prefix))
        with dpg.tooltip(dpg.last_item()):
            dpg.add_text("Hold the transmitter button, then press Capture: takes the channel that\n"
                         "moved since this editor opened, and its value. (Or press Capture first,\n"
                         "then hold the button until it finishes.)" if capture else
                         "Press, then within 4 s move the switch or stick to where it should\n"
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
    if src in _BUTTONS:
        if src == "pad":
            t.button = int(dpg.get_value("re_button"))
        else:
            t.name = dpg.get_value("re_name") or ""
        t.gesture = dpg.get_value("re_gesture")
        t.mods = mods
    elif src == "channel":
        t.cond = _cond_get("re_c")
        t.exit = bool(dpg.get_value("re_exit"))
    elif src == "link":
        t.link = dpg.get_value("re_link")
    else:
        t.mode = dpg.get_value("re_mode")
    actions = [a for a in (_action_get(_row("re", i)) for i in range(ev.MAX_ACTIONS)) if a]
    if dpg.get_value("re_cycle") and actions:
        actions = [ev.format_cycle(actions)]
    when_mods = [] if src in _BUTTONS else mods
    when_modes = [m for m in ev.MODES if dpg.get_value(f"re_when_{m}")]
    if len(when_modes) == len(ev.MODES):
        when_modes = []
    when_audio = [a for a in ev.AUDIO_STATES if dpg.get_value(f"re_audio_{a}")]
    if len(when_audio) == len(ev.AUDIO_STATES):
        when_audio = []
    comment = ""
    if _edit_line is not None:
        old = S.doc.rule_at(_edit_line)
        if old is not None and old.rule is not None:
            comment = old.rule.comment
    return ev.Rule(t, actions, when_mods, when_modes, when_audio, comment)


def _preview(*_args) -> None:
    if not dpg.does_item_exist("re_preview") or not dpg.is_item_shown("rule_editor"):
        return
    src = _SOURCE_KEYS[_SOURCES.index(dpg.get_value("re_source"))]
    for key in _SOURCE_KEYS:
        dpg.configure_item(f"re_grp_{key}", show=key == src)
    dpg.configure_item("re_grp_button", show=src in _BUTTONS)
    dpg.configure_item("re_nonamed", show=src == "named" and not dpg.get_item_configuration("re_name")["items"])
    dpg.set_value("re_mods_label", "Only while these modifiers are held (a bank):" if src in _BUTTONS
                  else "Only while held (when=):")
    for i in range(ev.MAX_ACTIONS):
        _action_show(_row("re", i))
    try:
        rule = _rule_from_editor()
        text = rule.line()
        if not rule.actions:
            text += "      <- pick at least one action"
        dpg.set_value("re_preview", text)
        cycle = any(ev.is_cycle(a) for a in rule.actions)
        dpg.set_value("re_words", f"When {rule.trigger.describe()}"
                      + (f" ({rule.describe_when()})" if rule.describe_when() else "")
                      + ": " + (", ".join(ev.describe_action(a) for a in rule.actions) or "-")
                      + ("\nState rule: also applied at power-up and when the link returns."
                         if rule.is_state_rule() and rule.trigger.source == "channel" else "")
                      + ("\nIt starts at the first at power-up and on every reload." if cycle else ""))
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
        buttons = S.doc.button_names()
        l = S.doc.rule_at(line_no) if line_no else None
    rule = l.rule if l is not None and l.rule is not None else ev.Rule(ev.Trigger("pad"), ["seq:wave"])
    t = rule.trigger
    dpg.set_value("re_source", _SOURCES[_SOURCE_KEYS.index(t.source)])
    dpg.set_value("re_button", str(t.button))
    dpg.configure_item("re_name", items=buttons)
    dpg.set_value("re_name", t.name or (buttons[0] if buttons else ""))
    dpg.set_value("re_gesture", t.gesture)
    _cond_set("re_c", t.cond)
    dpg.set_value("re_exit", t.exit)
    dpg.set_value("re_link", t.link)
    dpg.set_value("re_mode", t.mode)
    held = set(t.mods if t.source in _BUTTONS else rule.when_mods)
    for i in range(MAX_MODIFIERS):
        shown = i < len(names)
        dpg.configure_item(f"re_mod_{i}", show=shown, label=names[i] if shown else "")
        dpg.set_value(f"re_mod_{i}", shown and names[i] in held)
    dpg.configure_item("re_nomods", show=not names)
    for m in ev.MODES:
        dpg.set_value(f"re_when_{m}", m in rule.when_modes)
    for a in ev.AUDIO_STATES:
        dpg.set_value(f"re_audio_{a}", a in rule.when_audio)
    items = ev.cycle_items(rule.actions[0]) if len(rule.actions) == 1 else None
    dpg.set_value("re_cycle", items is not None)
    actions = items or rule.actions
    for i in range(ev.MAX_ACTIONS):
        _action_put(_row("re", i), actions[i] if i < len(actions) else None, "(none)")
    dpg.configure_item("rule_editor", show=True, label="Edit rule" if line_no else "New rule")
    _preview()


def _rule_ok() -> None:
    rule = _rule_from_editor()
    if not rule.actions:
        _set_status("Pick at least one action")
        return
    try:
        ev.Rule.parse(rule.trigger.text(), rule.value_text())    # a cycle of one, no button name ...
        ranges = _setting_ranges()
        for a in ev.flat_actions(rule.actions):
            ev.check_action(a, ranges)                           # set:'s setting and range, when known
    except ev.EventsError as e:
        _set_status(f"Can't use this rule: {e}")
        return
    with S.lock:
        S.doc.set_rule(_edit_line, rule)
    dpg.configure_item("rule_editor", show=False)
    _changed()


def _rule_test() -> None:
    job_test(_rule_from_editor().actions)


def _build_rule_editor() -> None:
    with dpg.window(tag="rule_editor", label="Rule", modal=True, show=False, width=720, height=530,
                    on_close=lambda: _learn_stop()):
        dpg.add_text("When", color=COL_HEAD)
        dpg.add_radio_button(_SOURCES, tag="re_source", horizontal=True, default_value=_SOURCES[0],
                             callback=_preview)
        with dpg.group(tag="re_grp_button", horizontal=True):
            with dpg.group(tag="re_grp_pad", horizontal=True):
                dpg.add_text("button")
                dpg.add_combo([str(b) for b in range(1, ev.MAX_BUTTON + 1)], tag="re_button", width=60,
                              default_value="1", callback=_preview)
            with dpg.group(tag="re_grp_named", horizontal=True, show=False):
                dpg.add_text("button")
                dpg.add_combo([], tag="re_name", width=150, callback=_preview)
            dpg.add_text("is")
            dpg.add_combo(ev.GESTURES, tag="re_gesture", width=90, default_value="click",
                          callback=_preview)
            with dpg.tooltip(dpg.last_item()):
                dpg.add_text("press: at once; click: let go (waits for a double if there is one);\n"
                             "double / triple; long: held; release: let go after any press;\n"
                             "repeat: at once, then again every button.repeatMs while held, from\n"
                             "button.longPressMs after the press (like a key's auto-repeat; 2.37.0+)")
            dpg.add_text("", tag="re_pad_live", color=COL_DIM)
        dpg.add_text("(no buttons defined - see the Buttons tab)", tag="re_nonamed", show=False, color=COL_DIM)
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
        dpg.add_text("Only in these modes (none ticked in a row = any):", color=COL_DIM)
        with dpg.group(horizontal=True):
            dpg.add_text("mode:")
            for m in ev.MODES:
                dpg.add_checkbox(label=m, tag=f"re_when_{m}", callback=_preview)
        with dpg.group(horizontal=True):
            dpg.add_text("sound mode:")
            with dpg.tooltip(dpg.last_item()):
                dpg.add_text("when=audio.NAME (Orchestron 2.36.0+): manual is neither random sounds nor music")
            for a in ev.AUDIO_STATES:
                dpg.add_checkbox(label=a, tag=f"re_audio_{a}", callback=_preview)

        dpg.add_separator()
        dpg.add_text("Do", color=COL_HEAD)
        for i in range(ev.MAX_ACTIONS):
            with dpg.group(horizontal=True):
                dpg.add_combo(_ACTION_ITEMS, tag=f"re_act_{i}", width=230, default_value="(none)",
                              callback=_preview)
                _arg_widgets(_row("re", i), _preview)
        dpg.add_checkbox(label="take turns: each time, the next of these (cycle)", tag="re_cycle",
                         callback=_preview)
        with dpg.tooltip(dpg.last_item()):
            dpg.add_text("cycle(A, B[, C]): 2 or 3 actions; each time the rule fires it runs the next\n"
                         "one, wrapping (Orchestron 2.36.0+). Never a state rule.")

        dpg.add_separator()
        dpg.add_text("", tag="re_words", wrap=680)
        dpg.add_text("", tag="re_preview", color=COL_DIM)
        with dpg.group(horizontal=True):
            dpg.add_button(label="OK", width=100, callback=_rule_ok)
            dpg.add_button(label="Test now", width=100, callback=_rule_test, tag="re_test")
            with dpg.tooltip(dpg.last_item()):
                dpg.add_text("Runs the actions on the robot now (the rule isn't saved).\n"
                             "A cycle runs its next action, each Test the next one.")
            dpg.add_button(label="Cancel", width=100,
                           callback=lambda: (_learn_stop(), dpg.configure_item("rule_editor", show=False)))


# --------------------------------------------------------------------------- #
# Modifier and button editors, raw line editor
# --------------------------------------------------------------------------- #
# Editor prefix -> (section, window, a new line's condition)
_NAMED = {"me": ("modifiers", "mod_editor", ev.Condition(1, "high")),
          "be": ("buttons", "btn_editor", ev.Condition(1, "near", 1500))}
_edit_named: dict[str, int | None] = {"me": None, "be": None}   # line being edited, None = new


def _open_named_editor(p: str, line_no: int | None) -> None:
    section, window, new = _NAMED[p]
    _edit_named[p] = line_no
    with S.lock:
        rows = {n: (name, c) for n, name, c, _e in S.doc.named(section)}
        S.capture_base = list(S.inputs[2]) if S.inputs else None   # Capture: what moved since now
    name, cond = rows.get(line_no, ("", None)) if line_no else ("", None)
    dpg.set_value(f"{p}_name", name)
    _cond_set(f"{p}_c", cond or new)
    dpg.set_value(f"{p}_err", "")
    dpg.configure_item(window, show=True)


def _named_ok(p: str) -> None:
    section, window, _new = _NAMED[p]
    name = (dpg.get_value(f"{p}_name") or "").strip()
    with S.lock:
        err = S.doc.name_problem(name, section, _edit_named[p])
        if err:
            dpg.set_value(f"{p}_err", err)
            return
        n = S.doc.set_named(section, _edit_named[p], name, _cond_get(f"{p}_c"))
        _edit_named[p] = n
        # What the robot would refuse it for: the pad's channel, too many ... The line is kept,
        # as the raw editor keeps a bad line: fix it here, or Cancel and see it in red
        err = next((e for m, _nm, _c, e in S.doc.named(section) if m == n), "")
    _changed()
    if err:
        dpg.set_value(f"{p}_err", err)
        return
    _learn_stop()
    dpg.configure_item(window, show=False)


def _build_named_editor(p: str, title: str, intro: str, held: str) -> None:
    _section, window, _new = _NAMED[p]
    with dpg.window(tag=window, label=title, modal=True, show=False, width=560, height=320,
                    on_close=lambda: _learn_stop()):
        dpg.add_text(intro, color=COL_DIM)
        with dpg.group(horizontal=True):
            dpg.add_text("name")
            dpg.add_input_text(tag=f"{p}_name", width=160, hint="letters, digits, _")
        dpg.add_text(held, color=COL_HEAD)
        _cond_widgets(f"{p}_c")
        dpg.add_text("", tag=f"{p}_err", color=COL_BAD, wrap=520)
        with dpg.group(horizontal=True):
            dpg.add_button(label="OK", width=100, callback=lambda: _named_ok(p))
            dpg.add_button(label="Cancel", width=100,
                           callback=lambda: (_learn_stop(), dpg.configure_item(window, show=False)))


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
def _learn_start(prefix: str, min_move: int = LEARN_MOVE,
                 msg: str = "Learn: move the switch or stick to where it should trigger, and leave it there ...") -> None:
    with S.lock:
        if S.inputs is None:
            _set_status(f"{_LEARN_LABELS[prefix]} needs the robot connected (and its transmitter on)")
            return
        S.learn_base = list(S.inputs[2])
        S.learn_until = time.time() + LEARN_S
        S.learn_target = prefix
        S.learn_move = min_move
    _set_status(msg)


def _learn_stop() -> None:
    with S.lock:
        S.learn_until = 0.0
        S.learn_base = None


def _learn_finish() -> None:
    with S.lock:
        base, prefix, move = S.learn_base, S.learn_target, S.learn_move
        now = list(S.inputs[2]) if S.inputs else None
        S.learn_until, S.learn_base = 0.0, None
    if not base or not now:
        return
    ch = ev.learn_channel(base, now, move)
    if not ch:
        _set_status(f"{_LEARN_LABELS[prefix]}: nothing moved - try again and move the control further")
        return
    if prefix == "be_c":
        _capture_set(prefix, ch, now[ch - 1])
        return
    cond = ev.suggest_condition(ch, now[ch - 1])
    _cond_set(prefix, cond)
    _set_status(f"Learn: ch{ch} at {now[ch - 1]} us -> {cond.describe()}")


def _capture(prefix: str) -> None:
    """Capture (the button editor's Learn): the channel that moved since the editor opened, at
    its value now - the user holds the transmitter button and presses Capture. If nothing has
    moved yet, Learn: hold the button within 4 s."""
    with S.lock:
        base = S.capture_base
        now = list(S.inputs[2]) if S.inputs else None
    ch = ev.learn_channel(base, now, CAPTURE_MOVE) if base and now else 0
    if ch:
        _capture_set(prefix, ch, now[ch - 1])
        return
    _learn_start(prefix, CAPTURE_MOVE, "Capture: hold the transmitter button now, until this finishes ...")


def _capture_set(prefix: str, ch: int, us: int) -> None:
    _cond_set(prefix, ev.Condition(ch, "near", us))
    _set_status(f"Capture: ch{ch} at {us} us")


def _button_bits() -> dict[int, int]:
    """id(Line) -> its bit in <KI>'s buttons held: the [buttons] lines of the file the robot
    loaded, in file order, without those it refused. Caller holds S.lock."""
    loaded = [l for _n, l in sorted(S.synced.items())
              if l.section == "buttons" and l.key and id(l) not in S.problems]
    return {id(l): i for i, l in enumerate(loaded)}


def _buttons_down(inputs) -> set[int]:
    """id(Line) of the Buttons tab's rows that are down now: from the robot's <KI> for the lines
    it loaded, else (firmware before 2.36, a line edited since) decided from the channel values."""
    if not inputs or not inputs[0]:
        return set()                     # link down: the robot ignores buttons
    _up, _pad, us, held = inputs
    with S.lock:
        width = S.button_width
        bits = _button_bits() if held is not None else {}
    down = ev.buttons_down([(key, c) for key, _n, c in _btn_rows if c], us, width)
    for key, bit in bits.items():
        down.discard(key)
        if held >> bit & 1:
            down.add(key)
    return down


def _update_live(inputs) -> None:
    up, pad, us, _held = inputs if inputs else (False, 0, [], None)
    down = _buttons_down(inputs)
    with S.lock:
        deadband, width = S.doc.deadband(), S.button_width
    for prefix in _LEARN_LABELS:
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
        button = prefix == "be_c"
        met = _cond_get(prefix).holds(v, width if button else deadband)
        dpg.set_value(f"{prefix}_met", ("down" if button else "condition met") if met else "")
        dpg.configure_item(f"{prefix}_met", color=COL_OK)
    if dpg.does_item_exist("re_pad_live"):
        live = f"pad now: {'button ' + str(pad) if pad else 'none'}"
        if dpg.get_value("re_source") == _SOURCES[_SOURCE_KEYS.index("named")]:
            name = dpg.get_value("re_name") or ""
            live = f"{name} now: " + ("down" if any(k in down for k, n, _c in _btn_rows
                                                    if n.lower() == name.lower()) else "up")
        dpg.set_value("re_pad_live", f"   ({live})" if us else "")
    if dpg.does_item_exist("ev_btns") and dpg.is_item_visible("ev_btns"):
        dpg.set_value("ev_btn_width", f"A value counts within +/- {width} us (config.ini button.deadband)")
        for key, _name, _c in _btn_rows:
            tag = f"ev_btn_down_{key}"
            if dpg.does_item_exist(tag):
                dpg.set_value(tag, "DOWN" if key in down else ("up" if us else ""))
                dpg.configure_item(tag, color=COL_FIRED if key in down else COL_DIM)
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
        held = ", ".join(n for k, n, _c in _btn_rows if k in down)
        dpg.set_value("in_status", ("RC link up" if up else "RC link DOWN") +
                      (f"   pad: button {pad}" if pad else "   pad: -") +
                      (f"   buttons: {held}" if held else "") if us else "Not connected")



# --------------------------------------------------------------------------- #
# Activities ([activity.NAME], Orchestron 2.32+)
# --------------------------------------------------------------------------- #
_ACT_KIND_KEYS = list(ev.ACTIVITY_KINDS)
_ACT_KIND_LABELS = list(ev.ACTIVITY_KINDS.values())
_ACT_PICKS = 8
_edit_activity: str | None = None


def _act_kind() -> str:
    return _ACT_KIND_KEYS[_ACT_KIND_LABELS.index(dpg.get_value("ae_kind"))]


def _act_action_text() -> str:
    return _action_get(_row("ae")) or ""


def _act_entries() -> list[tuple[str, str]]:
    """The section's lines from the editor, keeping lines it doesn't edit (sN.*, unknown keys)."""
    kind = _act_kind()
    entries: list[tuple[str, str]] = []
    if kind == "action":
        entries.append(("play", _act_action_text()))
    elif kind == "playlist":
        entries.append(("play", f"playlist{dpg.get_value('ae_player')}:{dpg.get_value('ae_bank')}"))
        if dpg.get_value("ae_shuffle"):
            entries.append(("shuffle", "1"))
    elif kind == "pick":
        items = []
        for i in range(_ACT_PICKS):
            name = dpg.get_value(f"ae_pick_{i}")
            if name and name != "(none)":
                items.append((name, int(dpg.get_value(f"ae_w_{i}"))))
        entries.append(("play", ev.format_pick(items)))
    else:
        servos = [f"s{i}" for i in range(1, 9) if dpg.get_value(f"ae_s{i}")]
        entries.append(("servos", ", ".join(servos)))
        for p in ("rest", "swing", "duty", "slew"):
            entries.append((p, str(int(dpg.get_value(f"ae_{p}")))))
        for p in ("period", "dwell"):
            if (dpg.get_value(f"ae_{p}") or "").strip():
                entries.append((p, dpg.get_value(f"ae_{p}").strip()))
        entries.append(("minActive", str(int(dpg.get_value("ae_minActive")))))
        entries.append(("intensity", str(int(dpg.get_value("ae_intensity")))))
    every = (dpg.get_value("ae_every") or "").strip()
    if every:
        entries.append(("every", every))
    cooldown = (dpg.get_value("ae_cooldown") or "").strip()
    if kind == "pick" and cooldown:
        entries.append(("cooldown", cooldown))
    if kind in ("action", "pick") and dpg.get_value("ae_startnow"):
        entries.append(("start", "now"))
    if int(dpg.get_value("ae_seed")):
        entries.append(("seed", str(int(dpg.get_value("ae_seed")))))
    # keep what the editor doesn't show
    edited = {k for k, _v in entries} | {"play", "servos", "shuffle", "every", "cooldown", "start", "seed",
                                        "when", "rest", "swing", "duty", "slew", "period", "dwell",
                                        "minactive", "intensity"}
    if _edit_activity:
        with S.lock:
            old = S.doc.activities().get(_edit_activity.lower())
        for k, v in (old.entries if old else []):
            if k.lower() not in {e.lower() for e in edited}:
                entries.append((k, v))
    when = ev.format_when([dpg.get_item_label(f"ae_mod_{i}") for i in range(MAX_MODIFIERS)
                           if dpg.is_item_shown(f"ae_mod_{i}") and dpg.get_value(f"ae_mod_{i}")],
                          [m for m in ev.MODES if dpg.get_value(f"ae_mode_{m}")],
                          [a for a in ev.AUDIO_STATES if dpg.get_value(f"ae_audio_{a}")])
    if when:
        entries.append(("when", when))
    return entries


def _act_preview(*_args) -> None:
    if not dpg.does_item_exist("act_editor") or not dpg.is_item_shown("act_editor"):
        return
    kind = _act_kind()
    for k in _ACT_KIND_KEYS:
        dpg.configure_item(f"ae_grp_{k}", show=k == kind)
    dpg.configure_item("ae_grp_cooldown", show=kind == "pick")
    dpg.configure_item("ae_startnow", show=kind in ("action", "pick"))
    dpg.set_value("ae_every_label", "gap between tracks" if kind == "playlist" else "every")
    _action_show(_row("ae"))
    seqs = ["(none)"] + _arg_choices("seq")
    for i in range(_ACT_PICKS):
        dpg.configure_item(f"ae_pick_{i}", items=seqs)
    name = (dpg.get_value("ae_name") or "").strip() or "NAME"
    act = ev.Activity(name, _act_entries())
    dpg.set_value("ae_words", act.describe())
    dpg.set_value("ae_preview", f"[activity.{name}]\n" + "\n".join(f"{k:<10}= {v}" for k, v in act.entries))


def _open_activity_editor(name: str | None) -> None:
    global _edit_activity
    _edit_activity = name
    with S.lock:
        act = S.doc.activities().get(name.lower()) if name else None
        mods = S.doc.modifier_names()[:MAX_MODIFIERS]
    act = act or ev.Activity("", [("play", "randomA:2"), ("every", "20-120s")])
    kind = act.kind or "action"
    dpg.set_value("ae_name", act.name)
    dpg.set_value("ae_kind", ev.ACTIVITY_KINDS[kind])
    play = act.get("play")
    if kind == "action":
        _action_put(_row("ae"), play, ev.ACTIONS[0][1])
    m = re.match(r"playlist([AB]?):(\d+)", play, re.IGNORECASE)
    dpg.set_value("ae_player", (m.group(1) or "A").upper() if m else "A")
    dpg.set_value("ae_bank", m.group(2) if m else "1")
    dpg.set_value("ae_shuffle", act.get("shuffle") == "1")
    picks = []
    if kind == "pick":
        try:
            picks = ev.parse_pick(play)
        except ev.EventsError:
            picks = []
    for i in range(_ACT_PICKS):
        dpg.set_value(f"ae_pick_{i}", picks[i][0] if i < len(picks) else "(none)")
        dpg.set_value(f"ae_w_{i}", picks[i][1] if i < len(picks) else 1)
    servos = {s.strip().lower() for s in act.get("servos").split(",")}
    for i in range(1, 9):
        dpg.set_value(f"ae_s{i}", f"s{i}" in servos)
    for p, d in (("rest", 50), ("swing", 60), ("duty", 70), ("slew", 0), ("minActive", 1), ("intensity", 100)):
        try:
            dpg.set_value(f"ae_{p}", int(act.get(p, str(d))))
        except ValueError:
            dpg.set_value(f"ae_{p}", d)
    dpg.set_value("ae_period", act.get("period", "3s"))
    dpg.set_value("ae_dwell", act.get("dwell", "800ms"))
    dpg.set_value("ae_every", act.get("every"))
    dpg.set_value("ae_cooldown", act.get("cooldown"))
    dpg.set_value("ae_startnow", act.get("start").lower() == "now")
    try:
        dpg.set_value("ae_seed", int(act.get("seed", "0")))
    except ValueError:
        dpg.set_value("ae_seed", 0)
    held, modes, audio = act.when_parts()
    for i in range(MAX_MODIFIERS):
        shown = i < len(mods)
        dpg.configure_item(f"ae_mod_{i}", show=shown, label=mods[i] if shown else "")
        dpg.set_value(f"ae_mod_{i}", shown and mods[i] in held)
    for mo in ev.MODES:
        dpg.set_value(f"ae_mode_{mo}", mo in modes)
    for a in ev.AUDIO_STATES:
        dpg.set_value(f"ae_audio_{a}", a in audio)
    dpg.set_value("ae_err", "")
    dpg.configure_item("act_editor", show=True, label="Edit activity" if name else "New activity")
    _act_preview()


def _activity_ok() -> None:
    name = (dpg.get_value("ae_name") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", name):
        dpg.set_value("ae_err", "A name of 1-15 letters, digits or _")
        return
    entries = _act_entries()
    kind = _act_kind()
    if kind == "pick" and ev.format_pick([]) == dict(entries).get("play"):
        dpg.set_value("ae_err", "Pick at least one sequence")
        return
    if kind == "alive" and not dict(entries).get("servos"):
        dpg.set_value("ae_err", "Tick at least one servo")
        return
    if kind in ("action", "pick") and not dict(entries).get("every"):
        dpg.set_value("ae_err", "Say how often (every = 30s, 20-120s, 2m ...)")
        return
    if kind == "action":
        try:
            ev.check_action(dict(entries)["play"], _setting_ranges())   # set:'s value, step and range
        except ev.EventsError as e:
            dpg.set_value("ae_err", str(e))
            return
    with S.lock:
        if _edit_activity and _edit_activity.lower() != name.lower():
            S.doc.delete_activity(_edit_activity)
        S.doc.set_activity(name, entries)
    dpg.configure_item("act_editor", show=False)
    _changed()


def _activity_test() -> None:
    kind = _act_kind()
    if kind == "action":
        job_test([_act_action_text()])
    elif kind == "pick":
        first = next((dpg.get_value(f"ae_pick_{i}") for i in range(_ACT_PICKS)
                      if dpg.get_value(f"ae_pick_{i}") not in ("", "(none)")), None)
        if first:
            job_test([f"seq:{first}"])
    elif kind == "playlist":
        job_test([f"next{dpg.get_value('ae_player')}:{dpg.get_value('ae_bank')}"])
    else:
        _set_status("Idle motion runs in AUTO: save to the robot and switch to AUTO to see it")


def _delete_activity(name: str) -> None:
    with S.lock:
        S.doc.delete_activity(name)
    _changed()


def _build_activity_editor() -> None:
    with dpg.window(tag="act_editor", label="Activity", modal=True, show=False, width=680, height=640):
        with dpg.group(horizontal=True):
            dpg.add_text("name")
            dpg.add_input_text(tag="ae_name", width=160, callback=_act_preview)
        dpg.add_radio_button(_ACT_KIND_LABELS, tag="ae_kind", default_value=_ACT_KIND_LABELS[0],
                             callback=_act_preview)
        with dpg.group(tag="ae_grp_action", horizontal=True):
            dpg.add_combo(_ACTION_ITEMS[1:], tag="ae_act", width=230, default_value=ev.ACTIONS[0][1],
                          callback=_act_preview)
            _arg_widgets(_row("ae"), _act_preview)
        with dpg.group(tag="ae_grp_playlist", show=False, horizontal=True):
            dpg.add_text("player")
            dpg.add_combo(["A", "B"], tag="ae_player", width=50, default_value="A", callback=_act_preview)
            dpg.add_text("bank")
            dpg.add_combo([str(b) for b in range(0, 11)], tag="ae_bank", width=60, default_value="1",
                          callback=_act_preview)
            dpg.add_checkbox(label="shuffle (each file once per round)", tag="ae_shuffle", callback=_act_preview)
        with dpg.group(tag="ae_grp_pick", show=False):
            dpg.add_text("Sequences and their weights (higher = picked more often):", color=COL_DIM)
            for i in range(0, _ACT_PICKS, 2):
                with dpg.group(horizontal=True):
                    for j in (i, i + 1):
                        dpg.add_combo(["(none)"], tag=f"ae_pick_{j}", width=170, default_value="(none)",
                                      callback=_act_preview)
                        dpg.add_input_int(tag=f"ae_w_{j}", width=90, default_value=1, min_value=1, max_value=100,
                                          min_clamped=True, max_clamped=True, callback=_act_preview)
        with dpg.group(tag="ae_grp_alive", show=False):
            dpg.add_text("Servos (they move only in AUTO, within servo.sN.autoMin..autoMax):", color=COL_DIM)
            with dpg.group(horizontal=True):
                for i in range(1, 9):
                    dpg.add_checkbox(label=f"s{i}", tag=f"ae_s{i}", callback=_act_preview)
            for p, label, hi in (("rest", "rest (% of the AUTO range)", 100), ("swing", "swing (% it covers)", 100),
                                 ("duty", "duty (% of the time moving)", 100), ("intensity", "intensity (%)", 200)):
                dpg.add_slider_int(label=label, tag=f"ae_{p}", width=300, min_value=0, max_value=hi,
                                   callback=_act_preview)
            with dpg.group(horizontal=True):
                dpg.add_text("one move")
                dpg.add_input_text(tag="ae_period", width=80, hint="3s", callback=_act_preview)
                dpg.add_text("pause")
                dpg.add_input_text(tag="ae_dwell", width=80, hint="800ms", callback=_act_preview)
                dpg.add_text("slew deg/s (0 = servo's)")
                dpg.add_input_int(tag="ae_slew", width=90, min_value=0, max_value=1000, min_clamped=True,
                                  max_clamped=True, callback=_act_preview)
            with dpg.group(horizontal=True):
                dpg.add_text("at least")
                dpg.add_input_int(tag="ae_minActive", width=90, min_value=0, max_value=8, min_clamped=True,
                                  max_clamped=True, callback=_act_preview)
                dpg.add_text("servos moving at once")
            dpg.add_text("Per-servo lines (s2.swing = 30 ...) are kept; edit them in the Text tab.",
                         color=COL_DIM)
        with dpg.group(horizontal=True):
            dpg.add_text("every", tag="ae_every_label")
            dpg.add_input_text(tag="ae_every", width=120, hint="20-120s", callback=_act_preview)
            with dpg.group(tag="ae_grp_cooldown", horizontal=True):
                dpg.add_text("no repeat within")
                dpg.add_input_text(tag="ae_cooldown", width=90, hint="60s", callback=_act_preview)
            dpg.add_checkbox(label="start at once", tag="ae_startnow", callback=_act_preview)
            dpg.add_text("seed")
            dpg.add_input_int(tag="ae_seed", width=90, min_value=0, min_clamped=True, callback=_act_preview)
        dpg.add_text("Runs while (none ticked in a row = any):", color=COL_HEAD)
        with dpg.group(horizontal=True):
            dpg.add_text("mode:")
            for mo in ev.MODES:
                dpg.add_checkbox(label=mo, tag=f"ae_mode_{mo}", callback=_act_preview)
        with dpg.group(horizontal=True):
            dpg.add_text("sound mode:")
            for a in ev.AUDIO_STATES:
                dpg.add_checkbox(label=a, tag=f"ae_audio_{a}", callback=_act_preview)
        with dpg.group(horizontal=True):
            dpg.add_text("held:")
            for i in range(MAX_MODIFIERS):
                dpg.add_checkbox(label="", tag=f"ae_mod_{i}", show=False, callback=_act_preview)
        dpg.add_separator()
        dpg.add_text("", tag="ae_words", wrap=640)
        dpg.add_text("", tag="ae_preview", color=COL_DIM)
        dpg.add_text("", tag="ae_err", color=COL_BAD)
        with dpg.group(horizontal=True):
            dpg.add_button(label="OK", width=100, callback=_activity_ok)
            dpg.add_button(label="Test now", width=100, callback=_activity_test)
            with dpg.tooltip(dpg.last_item()):
                dpg.add_text("Runs the action (or the first sequence, or the bank's next file) now.")
            dpg.add_button(label="Cancel", width=100, callback=lambda: dpg.configure_item("act_editor", show=False))


def _rebuild_activities(doc, problems) -> None:
    dpg.delete_item("ev_acts", children_only=True)
    acts = doc.activities()
    if not acts:
        dpg.add_text("No activities yet.", parent="ev_acts", color=COL_DIM)
        return
    bad: dict[str, str] = {}
    for l in doc.lines:
        if l.section.startswith("activity.") and id(l) in problems:
            bad[l.section[9:]] = (bad.get(l.section[9:], "") + "\n" + problems[id(l)]).strip()
    with dpg.table(parent="ev_acts", header_row=True, row_background=True, policy=dpg.mvTable_SizingStretchProp):
        dpg.add_table_column(label="Name", init_width_or_weight=1)
        dpg.add_table_column(label="What it does", init_width_or_weight=5)
        dpg.add_table_column(label="", width_fixed=True, init_width_or_weight=170)
        for name, act in acts.items():
            with dpg.table_row():
                dpg.add_text(name, color=COL_BAD if name in bad else COL_TEXT)
                dpg.add_text(act.describe(), wrap=700)
                if name in bad:
                    with dpg.tooltip(dpg.last_item()):
                        dpg.add_text(bad[name], wrap=500)
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Edit", small=True, user_data=name,
                                   callback=lambda s, a, u: _open_activity_editor(u))
                    dpg.add_button(label="Delete", small=True, user_data=name,
                                   callback=lambda s, a, u: _delete_activity(u))


def _move(line_no: int, step: int) -> None:
    with S.lock:
        S.doc.move_entry(line_no, step)
    _changed()

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
    _rebuild_buttons(doc, problems)
    _rebuild_mods(doc, problems)
    _rebuild_presets(doc, problems)
    _rebuild_activities(doc, problems)
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
        dpg.add_table_column(label="", width_fixed=True, init_width_or_weight=230)
        section_comment = ""
        # buttons / modifiers the file doesn't define; set:'s setting and range (connected)
        unknown = doc.rule_problems(_setting_ranges())
        for n, l in doc.rules():
            # The comment line just above a rule is shown as its heading
            prev = doc.lines[n - 2].raw.strip() if n >= 2 else ""
            if prev.startswith((";", "#")) and prev != section_comment:
                section_comment = prev
                with dpg.table_row():
                    dpg.add_text("")
                    dpg.add_text(prev.lstrip(";# "), color=COL_HEAD)
            problem = problems.get(id(l), "") or l.error or unknown.get(n, "")
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
                    dpg.add_button(label="^", small=True, user_data=n, callback=lambda s, a, u: _move(u, -1))
                    with dpg.tooltip(dpg.last_item()):
                        dpg.add_text("Move up (when two rules fire together, the upper one runs first)")
                    dpg.add_button(label="v", small=True, user_data=n, callback=lambda s, a, u: _move(u, +1))


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
                                       callback=lambda s, a, u: _open_named_editor("me", u))
                    dpg.add_button(label="Delete", small=True, user_data=n, callback=lambda s, a, u: _delete(u))


def _rebuild_buttons(doc, problems) -> None:
    global _btn_rows
    dpg.delete_item("ev_btns", children_only=True)
    rows = doc.buttons()
    _btn_rows = [(id(doc.lines[n - 1]), name, None if err else cond) for n, name, cond, err in rows]
    pad = doc.pad_problem()
    if pad:
        dpg.add_text(f"[inputs] pad: {pad}", parent="ev_btns", color=COL_BAD, wrap=900)
    if not rows:
        dpg.add_text("No buttons yet.", parent="ev_btns", color=COL_DIM)
        return
    with dpg.table(parent="ev_btns", header_row=True, row_background=True, policy=dpg.mvTable_SizingStretchProp):
        dpg.add_table_column(label="line", width_fixed=True, init_width_or_weight=40)
        dpg.add_table_column(label="Name", init_width_or_weight=2)
        dpg.add_table_column(label="Channel", init_width_or_weight=1)
        dpg.add_table_column(label="Value", init_width_or_weight=2)
        dpg.add_table_column(label="Now", width_fixed=True, init_width_or_weight=60)
        dpg.add_table_column(label="", width_fixed=True, init_width_or_weight=120)
        for n, name, cond, err in rows:
            l = doc.lines[n - 1]
            problem = problems.get(id(l), "") or err
            channel, _sp, value = ev.strip_comment(l.value)[0].partition(" ")
            with dpg.table_row():
                dpg.add_text(str(n), color=COL_DIM)
                dpg.add_text(name, color=COL_BAD if problem else COL_TEXT)
                if problem:
                    with dpg.tooltip(dpg.last_item()):
                        dpg.add_text(problem, wrap=500)
                dpg.add_text(channel)
                dpg.add_text(value.strip())        # as written
                if cond:
                    with dpg.tooltip(dpg.last_item()):
                        dpg.add_text("down while " + cond.describe() +
                                     (" (+/- button.deadband)" if cond.kind == "near" and not cond.b else ""))
                dpg.add_text("", tag=f"ev_btn_down_{id(l)}", color=COL_DIM)
                with dpg.group(horizontal=True):
                    if cond:
                        dpg.add_button(label="Edit", small=True, user_data=n,
                                       callback=lambda s, a, u: _open_named_editor("be", u))
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
        with dpg.tab(label="Buttons"):
            dpg.add_text("Named buttons (Orchestron firmware 2.36.0+): a button is a value on any channel, so "
                         "one channel can carry several, like the pad (one down at a time). Rules use them as "
                         "NAME (a click), NAME.press / .double / .triple / .long / .release / .repeat "
                         "(2.37.0+: again and again while held); hold a modifier "
                         "for another bank (bank2+NAME). To add one: Add button, hold the transmitter button, "
                         "press Capture.", color=COL_DIM, wrap=900)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Add button", callback=lambda: _open_named_editor("be", None))
                dpg.add_text("", tag="ev_btn_width", color=COL_DIM)
            with dpg.child_window(border=False, height=-1):
                dpg.add_group(tag="ev_btns")
        with dpg.tab(label="Modifiers"):
            dpg.add_button(label="Add modifier", callback=lambda: _open_named_editor("me", None))
            dpg.add_group(tag="ev_mods")
        with dpg.tab(label="Activities"):
            dpg.add_text("Things the droid does by itself while their conditions hold: random sounds, "
                         "music, a sequence now and then, idle motion in AUTO (firmware 2.32.0+).",
                         color=COL_DIM, wrap=900)
            dpg.add_button(label="Add activity", callback=lambda: _open_activity_editor(None))
            with dpg.child_window(border=False, height=-1):
                dpg.add_group(tag="ev_acts")
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
                         "still counts (named buttons use config.ini's button.deadband instead). The pad "
                         "channel carries the 14-button pad; it can't also carry named buttons.",
                         color=COL_DIM, wrap=800)
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
    _build_named_editor("me", "Modifier", "A modifier is a switch or stick position that other rules can require\n"
                        "(\"shift + pad button 1\", or when=shift). On a switch it's a bank.", "is held while")
    _build_named_editor("be", "Button", "A button is down while its channel is at its value (+/- button.deadband).\n"
                        "Hold the transmitter button and press Capture to take both.", "is down while")
    _build_raw_editor()
    _build_activity_editor()


def update() -> None:
    """Every frame, from app.py's render loop."""
    now = time.time()
    with S.lock:
        rebuild, S.need_rebuild = S.need_rebuild, False
        busy, note, dirty, supported, source = S.busy, S.note, S.dirty, S.supported, S.source
        inputs = S.inputs
        learn_until, learn_target = S.learn_until, S.learn_target
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

    # Live values while an editor, the Buttons or the Inputs tab is open
    want = dpg.is_item_shown("rule_editor") or dpg.is_item_shown("mod_editor") or \
        dpg.is_item_shown("btn_editor") or dpg.is_item_visible("ev_btns") or dpg.is_item_visible("inputs_group")
    if want and polling:
        with S.lock:
            S.poll_pending = True
            S.next_poll = now + POLL_S
        job_poll_inputs()
    if want:
        _update_live(inputs)
    if learn_until and now >= learn_until:
        _learn_finish()
    for prefix, label in _LEARN_LABELS.items():
        if dpg.does_item_exist(f"{prefix}_learn"):
            busy = learn_until > now and prefix == learn_target
            dpg.configure_item(f"{prefix}_learn", label=f"{label.rstrip('e')}ing {learn_until - now:.0f}s"
                               if busy else label)
