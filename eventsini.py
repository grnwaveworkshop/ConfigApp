"""events.ini as an editable document (Orchestron 2.30+), no UI and no I/O.

The firmware's events.ini says what every transmitter control does: one rule per line,

    trigger = action[, action[, action]] [when=...]

in an [events] section, plus [settings], [inputs], [modifiers] and [preset.NAME]
sections (see Orchestron docs/examples/events.ini and src/ButtonBindings.hpp).

EventsDoc keeps the file as a list of lines so a load / edit / save round trip
changes only the lines that were edited: comments, blank lines and the order stay
as the user wrote them. Line numbers are 1-based file lines, the same numbers the
firmware reports for a bad line (<KU##>) and for a rule that fired (<KV,line>).

The editor only ever writes well-formed lines (from dropdowns), but a file edited
by hand can hold anything: a line this module can't read is kept as it is, with
`error` set, and the firmware has the final word when it loads the file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

GESTURES = ["click", "press", "double", "triple", "long"]   # click is the default
MODES = ["idle", "manual", "control", "auto"]
ZONES = ["low", "mid", "high"]
MAX_CHANNEL = 24
MAX_BUTTON = 14
MAX_ACTIONS = 3

# Condition kinds, in the order the editor offers them
COND_KINDS = ["low", "mid", "high", "near", "above", "below", "range"]
COND_LABELS = {
    "low": "switch low (bottom third)",
    "mid": "switch middle",
    "high": "switch high (top third)",
    "near": "at a value",
    "above": "above a value",
    "below": "below a value",
    "range": "between two values",
}

# (action word, label, what its argument is: None, "seq", "wav", "bank", "mode", "audio",
#  "preset", "rec")
ACTIONS: list[tuple[str, str, str | None]] = [
    ("seq", "Play sequence", "seq"),
    ("toggle", "Start / stop sequence", "seq"),
    ("stopseq", "Stop sequences", None),
    ("wavA", "Play WAV (player A)", "wav"),
    ("wavB", "Play WAV (player B)", "wav"),
    ("randomA", "Random WAV from a bank (A)", "bank"),
    ("randomB", "Random WAV from a bank (B)", "bank"),
    ("stopaudio", "Stop audio", None),
    ("stop", "Stop everything", None),
    ("mode", "Set the motion mode", "mode"),
    ("audio", "Random sounds", "audio"),
    ("preset", "Apply a preset", "preset"),
    ("home", "Home the servos", None),
    ("rec", "Recorder", "rec"),
]
ACTION_ARG = {word: arg for word, _label, arg in ACTIONS}
ACTION_LABEL = {word: label for word, label, _arg in ACTIONS}
ARG_CHOICES = {
    "mode": MODES,
    "audio": ["manual", "random"],
    "rec": ["toggle", "start", "stop"],
    "bank": [str(b) for b in range(1, 11)],
}

US_MIN, US_MAX = 500, 2500       # what the firmware accepts as microseconds


class EventsError(ValueError):
    """A trigger, condition or action this module can't read."""


def _int(text: str, lo: int, hi: int, what: str) -> int:
    try:
        v = int(text.strip())
    except ValueError:
        raise EventsError(f"{what}: '{text.strip()}' isn't a whole number") from None
    if not lo <= v <= hi:
        raise EventsError(f"{what} must be {lo}-{hi}, not {v}")
    return v


def strip_comment(value: str) -> tuple[str, str]:
    """'seq:wave   ; wave hello' -> ('seq:wave', '; wave hello'). As the firmware: ';' or '#'."""
    m = re.search(r"[;#]", value)
    if not m:
        return value.strip(), ""
    return value[:m.start()].strip(), value[m.start():].strip()


# --------------------------------------------------------------------------- #
# Channel conditions: used by channel triggers and by [modifiers]
# --------------------------------------------------------------------------- #
@dataclass
class Condition:
    channel: int = 1
    kind: str = "high"      # one of COND_KINDS
    a: int = 1500           # near / above / below: the value; range: the low end
    b: int = 0              # near: +/- width (0 = the file's deadband); range: the high end

    def cond_text(self) -> str:
        if self.kind in ZONES:
            return self.kind
        if self.kind == "above":
            return f">{self.a}"
        if self.kind == "below":
            return f"<{self.a}"
        if self.kind == "range":
            return f"{self.a}-{self.b}"
        return f"{self.a}~{self.b}" if self.b else f"{self.a}"

    def text(self) -> str:
        return f"ch{self.channel} {self.cond_text()}"

    def describe(self) -> str:
        ch = f"ch{self.channel}"
        if self.kind in ZONES:
            return f"{ch} {self.kind}"
        if self.kind == "above":
            return f"{ch} above {self.a} us"
        if self.kind == "below":
            return f"{ch} below {self.a} us"
        if self.kind == "range":
            return f"{ch} between {self.a} and {self.b} us"
        return f"{ch} at {self.a} us" + (f" (+/- {self.b})" if self.b else "")

    def holds(self, us: int, deadband: int = 50) -> bool:
        """Whether a channel value (us) meets this condition (zones: thirds of 1000-2000),
        for the editor's live preview. The firmware's own test adds hysteresis."""
        if self.kind == "low":
            return us < 1250
        if self.kind == "high":
            return us > 1750
        if self.kind == "mid":
            return 1250 <= us <= 1750
        if self.kind == "above":
            return us > self.a
        if self.kind == "below":
            return us < self.a
        if self.kind == "range":
            return self.a <= us <= self.b
        w = self.b or deadband
        return self.a - w <= us <= self.a + w

    @staticmethod
    def parse_cond(channel: int, cond: str) -> "Condition":
        t = cond.strip().lower()
        if t in ZONES:
            return Condition(channel, t)
        if t[:1] in "<>" and t:
            v = _int(t[1:], US_MIN, US_MAX, "microseconds after < or >")
            return Condition(channel, "below" if t[0] == "<" else "above", v)
        if "-" in t[1:]:
            lo, hi = t.split("-", 1)
            a = _int(lo, US_MIN, US_MAX, "range low end")
            b = _int(hi, US_MIN, US_MAX, "range high end")
            if b < a:
                raise EventsError(f"range must be LOW-HIGH: {cond}")
            return Condition(channel, "range", a, b)
        if "~" in t:
            v, w = t.split("~", 1)
            return Condition(channel, "near", _int(v, US_MIN, US_MAX, "value"), _int(w, 1, 1000, "width after ~"))
        if not t:
            raise EventsError("missing condition (low, mid, high, N, >N, <N, N-M)")
        return Condition(channel, "near", _int(t, US_MIN, US_MAX, "value"), 0)

    @staticmethod
    def parse(text: str) -> "Condition":
        """'ch13 low', 'ch10 >1800', 'ch7 1200~80' (also the old 'ch13.low')."""
        m = re.fullmatch(r"\s*ch(\d+)[\s.]+(\S+)\s*", text, re.IGNORECASE)
        if not m:
            raise EventsError(f"expected chN and a condition: '{text.strip()}'")
        ch = _int(m.group(1), 1, MAX_CHANNEL, "channel")
        return Condition.parse_cond(ch, m.group(2))


def suggest_condition(channel: int, us: int) -> Condition:
    """What the editor's Learn proposes for a control left at `us`: a switch end or
    the middle as a zone (so the rule survives a trim change), anything else a value."""
    if us < 1150:
        return Condition(channel, "low")
    if us > 1850:
        return Condition(channel, "high")
    if 1450 <= us <= 1550:
        return Condition(channel, "mid")
    return Condition(channel, "near", int(round(us / 10.0)) * 10)


def learn_channel(before: list[int], after: list[int], min_move: int = 150) -> int:
    """The channel (1-based) that moved most between two <KI> snapshots, 0 if none moved
    at least min_move us."""
    best, best_move = 0, min_move - 1
    for i, (a, b) in enumerate(zip(before, after)):
        if abs(b - a) > best_move:
            best, best_move = i + 1, abs(b - a)
    return best


# --------------------------------------------------------------------------- #
# Triggers and rules
# --------------------------------------------------------------------------- #
@dataclass
class Trigger:
    source: str = "pad"           # pad | channel | link | mode
    button: int = 1               # pad
    gesture: str = "click"        # pad
    mods: list[str] = field(default_factory=list)   # pad: modifier prefixes ("shift+pad.1")
    cond: Condition = field(default_factory=Condition)   # channel
    exit: bool = False            # channel: fire on leaving
    link: str = "lost"            # link: lost | up
    mode: str = "idle"            # mode

    def text(self) -> str:
        if self.source == "pad":
            g = "" if self.gesture == "click" else f".{self.gesture}"
            return "".join(m + "+" for m in self.mods) + f"pad.{self.button}{g}"
        if self.source == "channel":
            return self.cond.text() + (".exit" if self.exit else "")
        if self.source == "link":
            return f"link.{self.link}"
        return f"mode.{self.mode}"

    def describe(self) -> str:
        if self.source == "pad":
            held = "".join(f"{m} + " for m in self.mods)
            return f"{held}pad button {self.button} {self.gesture}"
        if self.source == "channel":
            return (("leaves " if self.exit else "") + self.cond.describe())
        if self.source == "link":
            return "RC link lost" if self.link == "lost" else "RC link back"
        return f"mode changes to {self.mode}"

    @staticmethod
    def parse(key: str) -> "Trigger":
        k = re.sub(r"\s+", " ", key.strip())
        low = k.lower()
        if low in ("link.lost", "link.up"):
            return Trigger("link", link=low[5:])
        if low.startswith("mode."):
            if low[5:] not in MODES:
                raise EventsError(f"mode must be one of {', '.join(MODES)}: {k}")
            return Trigger("mode", mode=low[5:])
        if re.match(r"ch\d", low):
            exit_ = low.endswith(".exit")
            body = k[:-5] if exit_ else k
            return Trigger("channel", cond=Condition.parse(body), exit=exit_)
        # [mod+[mod+]]pad.N[.gesture], or the old buttonN[.gesture]
        parts = [p.strip() for p in k.split("+")]
        mods, last = parts[:-1], parts[-1]
        m = re.fullmatch(r"(?:pad\.|button)(\d+)(?:\.(\w+))?", last, re.IGNORECASE)
        if not m:
            raise EventsError(f"unknown trigger: {k}")
        button = _int(m.group(1), 1, MAX_BUTTON, "pad button")
        gesture = (m.group(2) or "click").lower()
        if gesture not in GESTURES:
            raise EventsError(f"gesture must be one of {', '.join(GESTURES)}: {k}")
        if any(not x for x in mods):
            raise EventsError(f"empty modifier name: {k}")
        return Trigger("pad", button=button, gesture=gesture, mods=mods)


def split_action(text: str) -> tuple[str, str]:
    """'seq:wave' -> ('seq', 'wave'); 'home' -> ('home', ''); a bare name is the old seq:NAME."""
    t = text.strip()
    if ":" in t:
        word, arg = t.split(":", 1)
        word = next((w for w in ACTION_ARG if w.lower() == word.strip().lower()), word.strip())
        if word.lower() == "random":
            word = "randomA"
        return word, arg.strip()
    word = next((w for w in ACTION_ARG if w.lower() == t.lower()), None)
    if word is not None and ACTION_ARG[word] is None:
        return word, ""
    return "seq", t


def join_action(word: str, arg: str = "") -> str:
    return f"{word}:{arg.strip()}" if ACTION_ARG.get(word) else word


def describe_action(text: str) -> str:
    word, arg = split_action(text)
    if word == "audio":
        return "Random sounds " + ("on" if arg.lower() == "random" else "off")
    label = ACTION_LABEL.get(word, word)
    return f"{label} {arg}".strip()


@dataclass
class Rule:
    trigger: Trigger = field(default_factory=Trigger)
    actions: list[str] = field(default_factory=list)
    when_mods: list[str] = field(default_factory=list)
    when_modes: list[str] = field(default_factory=list)
    comment: str = ""          # "; ..." kept at the end of the line

    def value_text(self) -> str:
        v = ", ".join(a.strip() for a in self.actions if a.strip())
        when = list(self.when_mods)
        if self.when_modes:
            when.append("mode." + "|".join(self.when_modes))
        if when:
            v += (", " if v else "") + "when=" + "+".join(when)
        return v

    def line(self) -> str:
        text = f"{self.trigger.text():<16} = {self.value_text()}"
        return f"{text:<40} {self.comment}" if self.comment else text

    def describe_when(self) -> str:
        parts = [f"{m} held" for m in self.when_mods]
        if self.when_modes:
            parts.append("in " + " / ".join(self.when_modes))
        return ", ".join(parts)

    def is_state_rule(self) -> bool:
        """mode:, audio: and preset: rules also apply at power-up and link-up."""
        return any(split_action(a)[0] in ("mode", "audio", "preset") for a in self.actions)

    @staticmethod
    def parse(key: str, value: str) -> "Rule":
        body, comment = strip_comment(value)
        when = ""
        m = re.search(r"(?:^|[\s,])when=", body, re.IGNORECASE)
        if m:
            when = body[m.end():].strip()
            body = body[:m.start()]
        actions = [a.strip() for a in body.split(",") if a.strip()]
        if len(actions) > MAX_ACTIONS:
            raise EventsError(f"at most {MAX_ACTIONS} actions per line")
        mods: list[str] = []
        modes: list[str] = []
        for item in (w.strip() for w in when.split("+") if w.strip()):
            if item.lower().startswith("mode."):
                for name in item[5:].split("|"):
                    name = name.strip().lower()
                    name = name[5:] if name.startswith("mode.") else name
                    if name not in MODES:
                        raise EventsError(f"when=: mode must be one of {', '.join(MODES)}: {name}")
                    modes.append(name)
            else:
                mods.append(item)
        return Rule(Trigger.parse(key), actions, mods, modes, comment)


# --------------------------------------------------------------------------- #
# The document
# --------------------------------------------------------------------------- #
@dataclass
class Line:
    raw: str
    section: str = ""          # lower case, "" before the first header
    key: str = ""              # "" for blank / comment / header lines
    value: str = ""
    rule: Rule | None = None   # [events] lines this module could read
    error: str = ""            # [events] lines it couldn't


_ENTRY = re.compile(r"^\s*([^=;#\[][^=]*?)\s*=\s*(.*?)\s*$")
_HEADER = re.compile(r"^\s*\[([^\]]+)\]")


def _parse_line(raw: str, section: str) -> Line:
    line = Line(raw, section)
    m = _ENTRY.match(raw)
    if not m or raw.lstrip()[:1] in (";", "#"):
        return line
    line.key, line.value = m.group(1).strip(), m.group(2)
    if section == "events":
        try:
            line.rule = Rule.parse(line.key, line.value)
        except EventsError as e:
            line.error = str(e)
    return line


class EventsDoc:
    def __init__(self, text: str = "") -> None:
        self.lines: list[Line] = []
        section = ""
        for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            h = _HEADER.match(raw)
            if h:
                section = h.group(1).strip().lower()
                self.lines.append(Line(raw, section))
                continue
            self.lines.append(_parse_line(raw, section))
        # A file ending in a newline splits into a last empty string: not a line
        if len(self.lines) > 1 and self.lines[-1].raw == "" and text.endswith(("\n", "\r")):
            self.lines.pop()

    def text(self) -> str:
        """The file to upload: CRLF, ending in a newline (as the firmware writes its INI files)."""
        return "\r\n".join(l.raw for l in self.lines) + "\r\n"

    # -- reading ------------------------------------------------------------ #
    def entries(self, section: str) -> list[tuple[int, Line]]:
        """(line number, line) for every key = value line in a section."""
        s = section.lower()
        return [(i + 1, l) for i, l in enumerate(self.lines) if l.section == s and l.key]

    def rules(self) -> list[tuple[int, Line]]:
        """[events] lines, readable or not (Line.rule / Line.error)."""
        return self.entries("events")

    def get(self, section: str, key: str) -> str | None:
        for _n, l in self.entries(section):
            if l.key.lower() == key.lower():
                return strip_comment(l.value)[0]
        return None

    def deadband(self) -> int:
        try:
            return max(1, int(self.get("settings", "deadband") or 50))
        except ValueError:
            return 50

    def pad_channel(self) -> int:
        """[inputs] pad: 0 = none or not set."""
        v = (self.get("inputs", "pad") or "").lower()
        m = re.fullmatch(r"ch(\d+)", v)
        return int(m.group(1)) if m else 0

    def modifiers(self) -> list[tuple[int, str, Condition | None, str]]:
        """(line, name, condition or None, error) per [modifiers] line."""
        out = []
        for n, l in self.entries("modifiers"):
            try:
                out.append((n, l.key, Condition.parse(strip_comment(l.value)[0]), ""))
            except EventsError as e:
                out.append((n, l.key, None, str(e)))
        return out

    def modifier_names(self) -> list[str]:
        return [name for _n, name, _c, _e in self.modifiers()]

    def presets(self) -> dict[str, list[tuple[str, str]]]:
        """{name: [(setting, value), ...]} from the [preset.NAME] sections, in file order."""
        out: dict[str, list[tuple[str, str]]] = {}
        for l in self.lines:
            if l.section.startswith("preset."):
                entries = out.setdefault(l.section[7:], [])
                if l.key:
                    entries.append((l.key, strip_comment(l.value)[0]))
        return out

    def sequence_names(self) -> list[str]:
        """Sequences the rules name (seq: / toggle:)."""
        names: list[str] = []
        for _n, l in self.rules():
            for a in (l.rule.actions if l.rule else []):
                word, arg = split_action(a)
                if word in ("seq", "toggle") and arg and arg not in names:
                    names.append(arg)
        return names

    def rule_at(self, line_no: int) -> Line | None:
        if 1 <= line_no <= len(self.lines):
            l = self.lines[line_no - 1]
            return l if l.section == "events" and l.key else None
        return None

    # -- editing ------------------------------------------------------------ #
    def _section_end(self, section: str, create_header: str) -> int:
        """Index to insert a new line at the end of a section (created at the end of the
        file if it's missing). Trailing blank lines stay after the insert."""
        s = section.lower()
        idx = [i for i, l in enumerate(self.lines) if l.section == s]
        if not idx:
            if self.lines and self.lines[-1].raw.strip():
                self.lines.append(Line(""))
            self.lines.append(Line(create_header, s))
            return len(self.lines)
        end = idx[-1] + 1
        while end - 1 > idx[0] and not self.lines[end - 1].raw.strip():
            end -= 1
        return end

    def _insert(self, section: str, header: str, raw: str) -> int:
        at = self._section_end(section, header)
        self.lines.insert(at, _parse_line(raw, section.lower()))
        return at + 1

    def set_rule(self, line_no: int | None, rule: Rule) -> int:
        """Replace the rule on that line, or add it at the end of [events]; returns its line."""
        if line_no and self.rule_at(line_no):
            self.lines[line_no - 1] = _parse_line(rule.line(), "events")
            return line_no
        return self._insert("events", "[events]", rule.line())

    def set_raw(self, line_no: int, raw: str) -> None:
        """Replace one line with text the user typed (it's re-read in its section)."""
        old = self.lines[line_no - 1]
        self.lines[line_no - 1] = _parse_line(raw, old.section)

    def delete_line(self, line_no: int) -> None:
        if 1 <= line_no <= len(self.lines):
            del self.lines[line_no - 1]

    def set_value(self, section: str, key: str, value: str) -> int:
        """Set key = value in a section (first matching line), adding it if missing."""
        header = f"[{section}]"
        for n, l in self.entries(section):
            if l.key.lower() == key.lower():
                _v, comment = strip_comment(l.value)
                raw = f"{l.key} = {value}" + (f"   {comment}" if comment else "")
                self.lines[n - 1] = _parse_line(raw, section.lower())
                return n
        return self._insert(section, header, f"{key} = {value}")

    def set_modifier(self, line_no: int | None, name: str, cond: Condition) -> int:
        raw = f"{name} = {cond.text()}"
        if line_no and 1 <= line_no <= len(self.lines) and self.lines[line_no - 1].section == "modifiers":
            _v, comment = strip_comment(self.lines[line_no - 1].value)
            self.lines[line_no - 1] = _parse_line(raw + (f"   {comment}" if comment else ""), "modifiers")
            return line_no
        return self._insert("modifiers", "[modifiers]", raw)

    def set_preset(self, name: str, entries: list[tuple[str, str]]) -> None:
        """Replace a [preset.NAME] section's settings (comments in it are kept)."""
        s = "preset." + name.lower()
        self.lines = [l for l in self.lines if not (l.section == s and l.key)]
        for key, value in entries:
            self._insert(s, f"[preset.{name}]", f"{key} = {value}")

    def delete_preset(self, name: str) -> None:
        s = "preset." + name.lower()
        self.lines = [l for l in self.lines if l.section != s]


NEW_FILE = """; events.ini - what the transmitter's switches, sticks and button pad do
; Written by Droid Config. Rule:  trigger = action[, action[, action]] [when=...]

[settings]
deadband = 50

[inputs]
pad = none

[modifiers]

[events]
link.lost        = stopseq
"""
