"""events.ini as an editable document (Orchestron 2.30+; activities 2.32+; named buttons,
cycle(), stopA/B, toggleA/B, toggleaudio and rules' when=audio.NAME 2.36+; set:KEY+=N /
set:KEY-=N and the .repeat gesture 2.37+), no UI and no I/O.

The firmware's events.ini says what every transmitter control does: one rule per line,

    trigger = action[, action[, action]] [when=...]
    trigger = cycle(action, action[, action]) [when=...]

in an [events] section, plus [settings], [inputs], [buttons], [modifiers] and [preset.NAME]
sections (see Orchestron docs/examples/events.ini, src/EventRules.hpp and
docs/BUTTON_TRIGGERS.md section 5).

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

# click is the default; release 2.36.0+; repeat 2.37.0+ (a press, then again every
# button.repeatMs while held, from button.longPressMs after the press)
GESTURES = ["click", "press", "double", "triple", "long", "release", "repeat"]
MODES = ["idle", "manual", "control", "auto"]
ZONES = ["low", "mid", "high"]
MAX_CHANNEL = 24
MAX_BUTTON = 14              # pad buttons; also the most [buttons] on one channel
MAX_BUTTONS = 32             # [buttons] lines in all
MAX_BUTTON_CHANNELS = 8      # channels that carry [buttons]
MAX_MODIFIERS = 8
MAX_ACTIONS = 3
NAME_LEN = 15                # [modifiers] and [buttons] names

# config.ini settings that turn button.deadband (SBUS units) into microseconds, with the
# firmware's defaults
BUTTON_WIDTH_KEYS = {"button.deadband": 20, "rc.pwm.minUs": 1000, "rc.pwm.maxUs": 2000,
                     "rc.sbus.min": 172, "rc.sbus.max": 1811}

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

# (action word, label, what its argument is: None, "seq", "wav", "bank", "random" (a bank or
#  FIRST-LAST), "mode", "audio", "audio_on" (random or music), "preset", "set" (KEY=VALUE, or
#  KEY+=N / KEY-=N: see split_set()), "rec")
# stopA / stopB, toggleA / toggleB and toggleaudio: Orchestron 2.36.0+
ACTIONS: list[tuple[str, str, str | None]] = [
    ("seq", "Play sequence", "seq"),
    ("toggle", "Start / stop sequence", "seq"),
    ("stopseq", "Stop sequences", None),
    ("wavA", "Play WAV (player A)", "wav"),
    ("wavB", "Play WAV (player B)", "wav"),
    ("randomA", "Random WAV (A)", "random"),
    ("randomB", "Random WAV (B)", "random"),
    ("nextA", "Next WAV in a bank (A)", "bank"),
    ("nextB", "Next WAV in a bank (B)", "bank"),
    ("toggleA", "Play / stop a bank (A)", "bank"),
    ("toggleB", "Play / stop a bank (B)", "bank"),
    ("stopA", "Stop player A", None),
    ("stopB", "Stop player B", None),
    ("stopaudio", "Stop audio", None),
    ("stop", "Stop everything", None),
    ("mode", "Set the motion mode", "mode"),
    ("audio", "Sound mode", "audio"),
    ("toggleaudio", "Sound mode on / off", "audio_on"),
    ("preset", "Apply a preset", "preset"),
    ("set", "Change a setting", "set"),
    ("home", "Home the servos", None),
    ("rec", "Recorder", "rec"),
]
ARG_HINTS = {
    "random": "a bank (2) or numbers (2001-2013)",
    "set": "setting, e.g. audio.mix.wavB",     # then set to / raise by / lower by, and the value
    "seq": "sequence name",
    "wav": "WAV number",
}
ACTION_ARG = {word: arg for word, _label, arg in ACTIONS}
ACTION_LABEL = {word: label for word, label, _arg in ACTIONS}
AUDIO_STATES = ["manual", "random", "music"]
MAX_BANK = 10
ARG_CHOICES = {
    "mode": MODES,
    "audio": AUDIO_STATES,
    "audio_on": ["random", "music"],    # toggleaudio: manual is where it toggles back to
    "rec": ["toggle", "start", "stop"],
    "bank": [str(b) for b in range(0, MAX_BANK + 1)],
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


def _one_of(names: list[str]) -> str:
    """['a', 'b', 'c'] -> 'a, b or c' (the firmware's wording)."""
    return ", ".join(names[:-1]) + " or " + names[-1] if len(names) > 1 else "".join(names)


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
        """'ch13 low', 'ch10 >1800', 'ch7 1200~80'."""
        if re.fullmatch(r"\s*ch\d+\.\S+\s*", text, re.IGNORECASE):
            fixed = re.sub(r"\.", " ", text.strip(), count=1)
            raise EventsError(f"write '{fixed}', not '{text.strip()}' (the buttons.ini form; "
                              "Orchestron 2.33 refuses it)")
        m = re.fullmatch(r"\s*ch(\d+)\s+(\S+)\s*", text, re.IGNORECASE)
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
# Names, and [buttons] (Orchestron 2.36.0+)
# --------------------------------------------------------------------------- #
_TRIGGER_WORD = re.compile(r"(ch|button|pad|mode|link)(?:$|[.\d])", re.IGNORECASE)


def name_error(name: str, what: str, plain: bool = False) -> str:
    """Why a [modifiers] / [buttons] name is refused, "" if it isn't: 1-15 characters and not
    read as a trigger (ch5, button3, pad, mode, link; "chin" or "modest" are fine), as the
    firmware's LooksLikeTrigger. plain: also only letters, digits and _ (the firmware asks it
    of buttons; the editor writes no other names)."""
    if not 1 <= len(name) <= NAME_LEN:
        return f"{what} name must be 1-{NAME_LEN} characters: {name}"
    if _TRIGGER_WORD.match(name):
        return f"{what} name reads as a trigger (chN, buttonN, pad, mode, link): {name}"
    if plain and not re.fullmatch(r"[A-Za-z0-9_]+", name):
        return f"{what} names are letters, digits and _: {name}"
    return ""


def button_width_us(settings: dict[str, int] | None = None) -> int:
    """config.ini's button.deadband (SBUS units) in microseconds: how far either side of a
    [buttons] value without its own ~W the channel may be, as the firmware converts it.
    settings: config values by key; missing ones take the firmware's defaults (20 -> 12 us)."""
    s = {**BUTTON_WIDTH_KEYS, **(settings or {})}
    span = s["rc.sbus.max"] - s["rc.sbus.min"]
    if span <= 0:
        return 1
    return max(1, int(s["button.deadband"] * (s["rc.pwm.maxUs"] - s["rc.pwm.minUs"]) / span + 0.5))


def buttons_down(buttons: list[tuple[object, Condition]], us: list[int], width: int) -> set:
    """The [buttons] down for these channel values (us[0] = ch1), decided here for firmware
    that doesn't report them: on each channel the first button whose condition holds, as the
    firmware (one at a time per channel, like the pad). buttons: (key, condition) in file
    order, the key whatever the caller wants back; width: button.deadband in us."""
    down, taken = set(), set()
    for key, cond in buttons:
        ch = cond.channel
        if ch in taken or ch > len(us) or not cond.holds(us[ch - 1], width):
            continue
        down.add(key)
        taken.add(ch)
    return down


# --------------------------------------------------------------------------- #
# Triggers and rules
# --------------------------------------------------------------------------- #
@dataclass
class Trigger:
    source: str = "pad"           # pad | named | channel | link | mode
    button: int = 1               # pad
    gesture: str = "click"        # pad, named
    mods: list[str] = field(default_factory=list)   # pad, named: modifier prefixes ("shift+pad.1")
    cond: Condition = field(default_factory=Condition)   # channel
    exit: bool = False            # channel: fire on leaving
    link: str = "lost"            # link: lost | up
    mode: str = "idle"            # mode
    name: str = ""                # named: a [buttons] name ("dome.long")

    def text(self) -> str:
        if self.source in ("pad", "named"):
            g = "" if self.gesture == "click" else f".{self.gesture}"
            button = f"pad.{self.button}" if self.source == "pad" else self.name
            return "".join(m + "+" for m in self.mods) + button + g
        if self.source == "channel":
            return self.cond.text() + (".exit" if self.exit else "")
        if self.source == "link":
            return f"link.{self.link}"
        return f"mode.{self.mode}"

    def describe(self) -> str:
        if self.source in ("pad", "named"):
            held = "".join(f"{m} + " for m in self.mods)
            button = f"pad button {self.button}" if self.source == "pad" else f"{self.name} button"
            gesture = "press, repeating while held" if self.gesture == "repeat" else self.gesture
            return f"{held}{button} {gesture}"
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
        # [mod+[mod+]]pad.N[.gesture] or [mod+[mod+]]NAME[.gesture] (a [buttons] button: whether
        # the file has one by that name is EventsDoc.rule_problems()'s job)
        parts = [p.strip() for p in k.split("+")]
        mods, last = parts[:-1], parts[-1]
        old = re.fullmatch(r"button(\d+)(\.\w+)?", last, re.IGNORECASE)
        if old:
            raise EventsError(f"write 'pad.{old.group(1)}{old.group(2) or ''}', not '{last}' "
                              "(the buttons.ini form; Orchestron 2.33 refuses it)")
        pad = re.fullmatch(r"pad\.(\d+)(?:\.(\w+))?", last, re.IGNORECASE)
        m = pad or re.fullmatch(r"([A-Za-z0-9_]+)(?:\.(\w+))?", last)
        if not m or (not pad and _TRIGGER_WORD.match(m.group(1))):
            raise EventsError(f"unknown trigger: {k}")
        gesture = (m.group(2) or "click").lower()
        if gesture not in GESTURES:
            raise EventsError(f"gesture must be one of {', '.join(GESTURES)}: {k}")
        if any(not x for x in mods):
            raise EventsError(f"empty modifier name: {k}")
        if not pad:
            return Trigger("named", name=m.group(1), gesture=gesture, mods=mods)
        button = _int(m.group(1), 1, MAX_BUTTON, "pad button")
        return Trigger("pad", button=button, gesture=gesture, mods=mods)


def split_action(text: str) -> tuple[str, str]:
    """'seq:wave' -> ('seq', 'wave'); 'home' -> ('home', ''); a bare name is seq:NAME."""
    t = text.strip()
    if ":" in t:
        word, arg = t.split(":", 1)
        word = next((w for w in ACTION_ARG if w.lower() == word.strip().lower()), word.strip())
        return word, arg.strip()
    word = next((w for w in ACTION_ARG if w.lower() == t.lower()), None)
    if word is not None and ACTION_ARG[word] is None:
        return word, ""
    return "seq", t


def join_action(word: str, arg: str = "") -> str:
    return f"{word}:{arg.strip()}" if ACTION_ARG.get(word) else word


# set:'s value: KEY=VALUE sets the setting (a state rule); KEY+=N / KEY-=N (Orchestron 2.37.0+)
# step it by N, held within its min..max by the firmware (not a state rule)
_SET = re.compile(r"\s*(.*?)\s*([+-]?=)\s*(.*?)\s*")


def split_set(arg: str) -> tuple[str, str, str] | None:
    """set:'s value, as written: 'audio.mix.wavB += 10' -> ('audio.mix.wavB', '+=', '10'),
    'fx.pitch.amount=120' -> ('fx.pitch.amount', '=', '120'); None without '='. As the firmware,
    the key is what comes before the first '=' (less a + or - just before it)."""
    m = _SET.fullmatch(arg)
    return (m.group(1), m.group(2), m.group(3)) if m else None


def join_set(key: str, op: str, value: str) -> str:
    """split_set() back: ('audio.mix.wavB', '+=', '10') -> 'audio.mix.wavB+=10'."""
    return f"{key.strip()}{op}{value.strip()}"


def is_relative_set(action: str) -> bool:
    """set:KEY+=N or set:KEY-=N: steps a setting from what it is (so never a state rule)."""
    word, arg = split_action(action)
    parts = split_set(arg) if word == "set" else None
    return parts is not None and parts[1] != "="


def check_set(arg: str, settings: dict[str, tuple[int, int]] | None = None) -> None:
    """Raise EventsError for a set: value the firmware refuses: not KEY=VALUE, KEY+=N or KEY-=N;
    a VALUE that isn't a whole number; a step N that isn't a whole number of 1 or more, written
    without a sign (+=-5 is refused). settings ({key: (min, max)}: the robot's table, when it's
    known) also checks that the setting exists, VALUE is within min..max and N isn't more than
    max - min, as the firmware does at load."""
    parts = split_set(arg)
    if not parts or not parts[0]:
        raise EventsError(f"set needs KEY=VALUE, KEY+=N or KEY-=N: {arg.strip()}")
    key, op, value = parts
    if op == "=":
        if not re.fullmatch(r"[+-]?[0-9]+", value):
            raise EventsError(f"{key} needs a whole number, not '{value}'")
    elif not re.fullmatch(r"[0-9]+", value) or int(value) < 1:
        raise EventsError(f"set:{key}{op}N steps by a whole number of 1 or more, not '{value}'")
    if settings is None:
        return
    if key not in settings:
        raise EventsError(f"no setting called {key}")
    lo, hi = settings[key]
    v = int(value)
    if op == "=" and not lo <= v <= hi:
        raise EventsError(f"{key} must be {lo}-{hi}")
    if op != "=" and v > hi - lo:
        raise EventsError(f"set:{key}{op}N steps by 1-{hi - lo} ({key} is {lo}-{hi}), not {v}")


def check_action(text: str, settings: dict[str, tuple[int, int]] | None = None) -> None:
    """Raise EventsError for one action the firmware refuses by its word or a value it can check
    without the SD card: an unknown word, random: / next: (the old forms), a value on a word that
    takes none, no value after ':', a bank outside 0-10, a mode or sound mode it doesn't know,
    toggleaudio:manual, a set: that isn't KEY=VALUE / KEY+=N / KEY-=N (check_set()). Sequence,
    preset and WAV names are the robot's to check; settings (the robot's {key: (min, max)}, when
    it's known) checks set:'s setting and range too."""
    t = text.strip()
    if ":" not in t:
        return                      # a word that takes no value, or a bare NAME (seq:NAME)
    word, arg = split_action(t)
    low = word.lower()
    if low in ("random", "next"):
        raise EventsError(f"write '{low}A:' (or '{low}B:'), not '{low}:' (Orchestron 2.33 refuses it)")
    if word not in ACTION_ARG:
        raise EventsError(f"unknown action: {word}")
    kind = ACTION_ARG[word]
    if kind is None:
        raise EventsError(f"{word} takes no value: {t}")
    if not arg:
        raise EventsError(f"{word} needs a value after ':'")
    if kind == "bank":
        _int(arg, 0, MAX_BANK, f"{word} bank")
    elif kind in ("mode", "audio", "audio_on") and arg.lower() not in ARG_CHOICES[kind]:
        raise EventsError(f"{word} is {_one_of(ARG_CHOICES[kind])}: {arg}")
    elif kind == "set":
        check_set(arg, settings)


def _split_top(text: str) -> list[str]:
    """Split on the commas outside brackets, each part trimmed (empty ones too)."""
    parts, depth, start = [], 0, 0
    for i, c in enumerate(text):
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif c == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return parts


def split_actions(text: str) -> list[str]:
    """'seq:a, home' -> ['seq:a', 'home']: a rule's actions, as the firmware splits them on
    commas, except that cycle(...) keeps the commas inside its brackets."""
    return [a for a in _split_top(text) if a]


def is_cycle(text: str) -> bool:
    return text.strip().lower().startswith("cycle(")


def cycle_items(text: str) -> list[str] | None:
    """'cycle(seq:a, seq:b)' -> ['seq:a', 'seq:b'], None if the action isn't a cycle. Each time
    its rule fires it runs the next item, wrapping (Orchestron 2.36.0+). Raises EventsError for
    a bad one: 2 or 3 items (a line's action limit), each one action, not another cycle."""
    t = text.strip()
    if not is_cycle(t):
        return None
    depth, close = 0, -1
    for i, c in enumerate(t[5:], 5):
        depth += {"(": 1, ")": -1}.get(c, 0)
        if depth == 0:
            close = i
            break
    if close < 0:
        raise EventsError(f"cycle( needs a closing ): {t}")
    if t[close + 1:].strip():
        raise EventsError(f"cycle(...) must be the line's only action: {t}")
    items = _split_top(t[6:close])
    if any(not i for i in items):
        raise EventsError(f"empty action in {t}")
    if any(is_cycle(i) for i in items):
        raise EventsError(f"a cycle can't hold another cycle: {t}")
    if not 2 <= len(items) <= MAX_ACTIONS:
        raise EventsError(f"a cycle holds 2 or 3 actions: {t}")
    return items


def format_cycle(items: list[str]) -> str:
    return "cycle(" + ", ".join(i.strip() for i in items) + ")"


def flat_actions(actions: list[str]) -> list[str]:
    """A rule's single actions: a cycle's items in its place."""
    out = []
    for a in actions:
        try:
            out += cycle_items(a) or [a]
        except EventsError:
            out.append(a)
    return out


def describe_action(text: str) -> str:
    items = cycle_items(text)
    if items is not None:
        return "Each time: the next of " + ", ".join(describe_action(i) for i in items)
    word, arg = split_action(text)
    if word == "audio":
        return {"random": "Random sounds on", "music": "Music on"}.get(arg.lower(), "Random sounds / music off")
    if word == "toggleaudio" and arg.lower() in ARG_CHOICES["audio_on"]:
        return {"random": "Turn random sounds on, or off if they're on",
                "music": "Turn music on, or off if it's on"}[arg.lower()]
    if word in ("toggleA", "toggleB"):
        return f"Player {word[-1]}: play bank {arg}'s next file, or stop it if it's playing"
    parts = split_set(arg) if word == "set" else None
    if parts:
        key, op, value = parts
        return {"=": f"Set {key} to {value}",
                "+=": f"Raise {key} by {value} (stops at its maximum)",
                "-=": f"Lower {key} by {value} (stops at its minimum)"}[op]
    if word in ("randomA", "randomB"):
        what = f"numbered {arg}" if "-" in arg else f"from bank {arg}"
        return f"Random WAV {what} ({word[-1]})"
    if word in ("nextA", "nextB"):
        return f"Next WAV in bank {arg} ({word[-1]})"
    label = ACTION_LABEL.get(word, word)
    return f"{label} {arg}".strip()


# --------------------------------------------------------------------------- #
# when= (rules and activities)
# --------------------------------------------------------------------------- #
def parse_when(text: str, strict: bool = True) -> tuple[list[str], list[str], list[str]]:
    """'shift+mode.idle|manual+audio.music' -> (['shift'], ['idle', 'manual'], ['music']): the
    modifiers, motion modes and sound modes (audio states) that must all hold. A name may repeat
    its prefix (mode.idle|mode.manual), as the firmware reads it. strict: raise EventsError for a
    mode or sound mode the firmware doesn't know. Whether the modifiers exist is the document's
    check (EventsDoc.rule_problems())."""
    mods: list[str] = []
    groups = {"mode.": (MODES, []), "audio.": (AUDIO_STATES, [])}
    for item in (w.strip() for w in text.split("+") if w.strip()):
        prefix = next((p for p in groups if item.lower().startswith(p)), None)
        if prefix is None:
            mods.append(item)
            continue
        names, out = groups[prefix]
        for name in item[len(prefix):].split("|"):
            name = name.strip().lower()
            name = name[len(prefix):] if name.startswith(prefix) else name
            if strict and name not in names:
                raise EventsError(f"when=: {prefix[:-1]} must be {_one_of(names)}: {name}")
            out.append(name)
    return mods, groups["mode."][1], groups["audio."][1]


def format_when(mods: list[str], modes: list[str], audio: list[str]) -> str:
    """parse_when()'s parts back to text, in the firmware's order: modifiers, mode., audio."""
    parts = list(mods)
    if modes:
        parts.append("mode." + "|".join(modes))
    if audio:
        parts.append("audio." + "|".join(audio))
    return "+".join(parts)


def when_words(mods: list[str], modes: list[str], audio: list[str]) -> list[str]:
    """parse_when()'s parts in words: ['shift held', 'in idle / manual', 'when sound mode is music']."""
    words = [f"{m} held" for m in mods]
    if modes:
        words.append("in " + " / ".join(modes))
    if audio:
        words.append("when sound mode is " + " / ".join(audio))
    return words


@dataclass
class Rule:
    trigger: Trigger = field(default_factory=Trigger)
    actions: list[str] = field(default_factory=list)
    when_mods: list[str] = field(default_factory=list)
    when_modes: list[str] = field(default_factory=list)
    when_audio: list[str] = field(default_factory=list)   # sound modes (Orchestron 2.36.0+)
    comment: str = ""          # "; ..." kept at the end of the line

    def value_text(self) -> str:
        v = ", ".join(a.strip() for a in self.actions if a.strip())
        when = format_when(self.when_mods, self.when_modes, self.when_audio)
        if when:
            v += (", " if v else "") + "when=" + when
        return v

    def line(self) -> str:
        text = f"{self.trigger.text():<16} = {self.value_text()}"
        return f"{text:<40} {self.comment}" if self.comment else text

    def describe_when(self) -> str:
        return ", ".join(when_words(self.when_mods, self.when_modes, self.when_audio))

    def is_state_rule(self) -> bool:
        """mode:, audio:, preset: and set:KEY=VALUE rules also apply at power-up and link-up
        (toggleaudio: and set:KEY+=N / -=N don't: they change the sound mode or the setting from
        what it is); a cycle line never does (re-applying it would step it)."""
        if any(is_cycle(a) for a in self.actions):
            return False
        return any(split_action(a)[0] in ("mode", "audio", "preset", "set") and not is_relative_set(a)
                   for a in self.actions)

    @staticmethod
    def parse(key: str, value: str) -> "Rule":
        body, comment = strip_comment(value)
        when = ""
        m = re.search(r"(?:^|[\s,])when=", body, re.IGNORECASE)
        if m:
            when = body[m.end():].strip()
            body = body[:m.start()]
        actions = split_actions(body)
        if len(actions) > MAX_ACTIONS:
            raise EventsError(f"at most {MAX_ACTIONS} actions per line")
        if len(actions) > 1 and any(is_cycle(a) for a in actions):
            raise EventsError("cycle(...) must be the line's only action")
        items = cycle_items(actions[0]) if len(actions) == 1 else None   # raises for a bad cycle
        for a in items or actions:
            check_action(a)
        mods, modes, audio = parse_when(when)
        return Rule(Trigger.parse(key), actions, mods, modes, audio, comment)


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

    def _check_named(self) -> tuple[dict[int, tuple[Condition | None, str]], str]:
        """Every [modifiers] and [buttons] line checked as the firmware loads them, in file
        order: {line: (condition or None, problem)}, and the [inputs] pad line's problem. A line
        the firmware refuses doesn't count for the names, limits and pad check of later lines."""
        out: dict[int, tuple[Condition | None, str]] = {}
        names: set[str] = set()
        pad, pad_problem, mods = 0, "", 0
        button_channels: list[int] = []          # per button loaded so far
        for n, l in enumerate(self.lines, 1):
            spec = strip_comment(l.value)[0]
            if l.section == "inputs" and l.key.lower() == "pad":
                m = re.fullmatch(r"ch(\d+)", spec.lower())
                ch = int(m.group(1)) if m else 0
                if ch in button_channels:
                    pad_problem = f"ch{ch} carries [buttons]; it can't also be the pad ([inputs] pad)"
                else:
                    pad = ch
                continue
            if not l.key or l.section not in ("modifiers", "buttons"):
                continue
            what = l.section[:-1]
            cond, err = None, name_error(l.key, what, plain=what == "button")
            if not err and l.key.lower() in names:
                err = f"name already used by a modifier or button: {l.key}"
            try:
                cond = Condition.parse(spec)
            except EventsError as e:
                err = err or str(e)
            if not err and what == "modifier" and mods >= MAX_MODIFIERS:
                err = f"too many modifiers (max {MAX_MODIFIERS})"
            if not err and what == "button":
                ch = cond.channel
                if len(button_channels) >= MAX_BUTTONS:
                    err = f"too many buttons (max {MAX_BUTTONS})"
                elif ch == pad:
                    err = f"ch{ch} is the pad ([inputs] pad); it can't also carry [buttons]"
                elif button_channels.count(ch) >= MAX_BUTTON:
                    err = f"a channel carries at most {MAX_BUTTON} buttons: ch{ch}"
                elif ch not in button_channels and len(set(button_channels)) >= MAX_BUTTON_CHANNELS:
                    err = f"buttons on at most {MAX_BUTTON_CHANNELS} channels: ch{ch}"
            if not err:
                names.add(l.key.lower())
                if what == "modifier":
                    mods += 1
                else:
                    button_channels.append(cond.channel)
            out[n] = (cond, err)
        return out, pad_problem

    def named(self, section: str) -> list[tuple[int, str, Condition | None, str]]:
        """(line, name, condition or None, problem) per [modifiers] or [buttons] line. The
        condition is there whenever it reads, even on a line with a problem (a duplicate name,
        too many, the pad's channel)."""
        checked = self._check_named()[0]
        return [(n, l.key, *checked[n]) for n, l in self.entries(section)]

    def modifiers(self) -> list[tuple[int, str, Condition | None, str]]:
        return self.named("modifiers")

    def modifier_names(self) -> list[str]:
        return [name for _n, name, _c, _e in self.modifiers()]

    def buttons(self) -> list[tuple[int, str, Condition | None, str]]:
        """[buttons] (Orchestron 2.36.0+): NAME = chN VALUE, down while the channel is within
        config.ini's button.deadband of VALUE us (or VALUE~W, or any channel condition)."""
        return self.named("buttons")

    def button_names(self) -> list[str]:
        return [name for _n, name, _c, _e in self.buttons()]

    def pad_problem(self) -> str:
        """Why the firmware refuses [inputs] pad: its channel already carries [buttons]."""
        return self._check_named()[1]

    def name_problem(self, name: str, section: str, line_no: int | None = None) -> str:
        """Why the editor can't give the [modifiers] / [buttons] line at line_no (None: a new
        one) this name: name_error(), or another modifier or button has it."""
        err = name_error(name, section[:-1], plain=True)
        for n, l in self.entries("modifiers") + self.entries("buttons"):
            if not err and n != line_no and l.key.lower() == name.lower():
                err = f"{l.key} is already a {l.section[:-1]} (line {n})"
        return err

    def rule_problems(self, settings: dict[str, tuple[int, int]] | None = None) -> dict[int, str]:
        """{line: problem} for the [events] rules that name a button or modifier the file
        doesn't define (or whose line the firmware refuses), as the firmware reports them.
        settings (the robot's {key: (min, max)}, when it's known): also a set: whose setting,
        value or step the firmware refuses (check_set())."""
        checked = self._check_named()[0]
        known = {s: {l.key.lower() for n, l in self.entries(s) if not checked[n][1]}
                 for s in ("modifiers", "buttons")}
        out = {}
        for n, l in self.rules():
            if l.rule is None:
                continue
            t = l.rule.trigger
            missing = [m for m in t.mods + l.rule.when_mods if m.lower() not in known["modifiers"]]
            if t.source == "named" and t.name.lower() not in known["buttons"]:
                out[n] = f"no [buttons] line named {t.name}"
            elif missing:
                out[n] = f"unknown modifier (define it in [modifiers]): {missing[0]}"
            elif settings is not None:
                for a in flat_actions(l.rule.actions):
                    try:
                        check_action(a, settings)
                    except EventsError as e:
                        out[n] = str(e)
                        break
        return out

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
        """Sequences the rules and activities name (seq: / toggle: / cycle(...) / pick(...))."""
        names: list[str] = []
        for _n, l in self.rules():
            for a in flat_actions(l.rule.actions if l.rule else []):
                word, arg = split_action(a)
                if word in ("seq", "toggle") and arg and arg not in names:
                    names.append(arg)
        for act in self.activities().values():
            for name in act.sequence_names():
                if name not in names:
                    names.append(name)
        return names

    # -- activities ----------------------------------------------------------- #
    def activities(self) -> dict[str, "Activity"]:
        """{name: Activity} from the [activity.NAME] sections, in file order."""
        out: dict[str, Activity] = {}
        for i, l in enumerate(self.lines):
            if l.section.startswith("activity."):
                a = out.setdefault(l.section[9:], Activity(l.section[9:], line_no=i + 1))
                if l.key:
                    a.entries.append((l.key, strip_comment(l.value)[0]))
        return out

    def set_activity(self, name: str, entries: list[tuple[str, str]]) -> None:
        """Replace an [activity.NAME] section's settings (comments in it are kept)."""
        self._replace_section("activity." + name, f"[activity.{name}]", entries, 10)

    def delete_activity(self, name: str) -> None:
        s = "activity." + name.lower()
        self.lines = [l for l in self.lines if l.section != s]

    # -- moving rules ----------------------------------------------------------- #
    def move_entry(self, line_no: int, step: int) -> int:
        """Swap a key = value line with the previous (step -1) or next (+1) one in its section,
        leaving comments and blank lines where they are. Returns its new line number."""
        if not 1 <= line_no <= len(self.lines) or not self.lines[line_no - 1].key:
            return line_no
        section = self.lines[line_no - 1].section
        i = line_no - 1 + step
        while 0 <= i < len(self.lines) and self.lines[i].section == section and not self.lines[i].key:
            i += step
        if not (0 <= i < len(self.lines)) or self.lines[i].section != section or not self.lines[i].key:
            return line_no
        a, b = line_no - 1, i
        self.lines[a], self.lines[b] = self.lines[b], self.lines[a]
        return b + 1

    def rule_at(self, line_no: int) -> Line | None:
        if 1 <= line_no <= len(self.lines):
            l = self.lines[line_no - 1]
            return l if l.section == "events" and l.key else None
        return None

    # -- editing ------------------------------------------------------------ #
    def _section_end(self, section: str, create_header: str, before: tuple[str, ...] = ()) -> int:
        """Index to insert a new line at the end of a section. A missing section is created
        just above the first of the `before` sections, or else at the end of the file.
        Trailing blank lines stay after the insert."""
        s = section.lower()
        idx = [i for i, l in enumerate(self.lines) if l.section == s]
        if not idx:
            at = next((i for i, l in enumerate(self.lines) if l.section in before), None)
            if at is not None:
                self.lines[at:at] = [Line(create_header, s), Line("", s)]
                return at + 1
            if self.lines and self.lines[-1].raw.strip():
                self.lines.append(Line(""))
            self.lines.append(Line(create_header, s))
            return len(self.lines)
        end = idx[-1] + 1
        while end - 1 > idx[0] and not self.lines[end - 1].raw.strip():
            end -= 1
        return end

    def _insert(self, section: str, header: str, raw: str, before: tuple[str, ...] = ()) -> int:
        at = self._section_end(section, header, before)
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

    def set_named(self, section: str, line_no: int | None, name: str, cond: Condition) -> int:
        """Replace the [modifiers] / [buttons] line at line_no (keeping its comment), or add one
        (a new [buttons] section goes above [modifiers] or [events]); returns its line."""
        raw = f"{name} = {cond.text()}"
        if line_no and 1 <= line_no <= len(self.lines) and self.lines[line_no - 1].section == section:
            _v, comment = strip_comment(self.lines[line_no - 1].value)
            self.lines[line_no - 1] = _parse_line(raw + (f"   {comment}" if comment else ""), section)
            return line_no
        return self._insert(section, f"[{section}]", raw, ("modifiers", "events") if section == "buttons" else ())

    def set_modifier(self, line_no: int | None, name: str, cond: Condition) -> int:
        return self.set_named("modifiers", line_no, name, cond)

    def set_button(self, line_no: int | None, name: str, cond: Condition) -> int:
        """A [buttons] line, NAME = chN VALUE: a "near" condition whose width 0 means
        config.ini's button.deadband (or any other condition)."""
        return self.set_named("buttons", line_no, name, cond)

    def _replace_section(self, section: str, header: str, entries: list[tuple[str, str]], width: int) -> None:
        """Replace a section's key = value lines. Comment lines stay, and a key that is
        written again keeps its end-of-line comment."""
        s = section.lower()
        comments = {l.key.lower(): strip_comment(l.value)[1] for l in self.lines if l.section == s and l.key}
        self.lines = [l for l in self.lines if not (l.section == s and l.key)]
        for key, value in entries:
            raw = f"{key:<{width}}= {value}" if width else f"{key} = {value}"
            comment = comments.get(key.lower(), "")
            self._insert(s, header, f"{raw:<30} {comment}" if comment else raw)

    def set_preset(self, name: str, entries: list[tuple[str, str]]) -> None:
        """Replace a [preset.NAME] section's settings (comments in it are kept)."""
        self._replace_section("preset." + name, f"[preset.{name}]", entries, 0)

    def delete_preset(self, name: str) -> None:
        s = "preset." + name.lower()
        self.lines = [l for l in self.lines if l.section != s]


# --------------------------------------------------------------------------- #
# Activities ([activity.NAME], Orchestron 2.32+)
# --------------------------------------------------------------------------- #
ACTIVITY_KINDS = {
    "action": "Do an action every so often",
    "playlist": "Play a bank, one file after another",
    "pick": "Pick a sequence now and then",
    "alive": "Idle motion (AUTO)",
}
ALIVE_PARAMS = ["rest", "swing", "period", "dwell", "duty", "slew"]


def parse_pick(text: str) -> list[tuple[str, int]]:
    """'pick(nod 3, look 2, shrug)' (or 'seq:pick(...)') -> [('nod', 3), ('look', 2), ('shrug', 1)]."""
    t = text.strip()
    if t.lower().startswith("seq:"):
        t = t[4:].strip()
    m = re.fullmatch(r"pick\((.*)\)", t, re.IGNORECASE)
    if not m:
        raise EventsError(f"expected pick(name weight, ...): {text}")
    out = []
    for item in (x.strip() for x in m.group(1).split(",") if x.strip()):
        parts = item.split()
        weight = _int(parts[1], 1, 100, "pick weight") if len(parts) > 1 else 1
        out.append((parts[0], weight))
    return out


def format_pick(items: list[tuple[str, int]]) -> str:
    return "pick(" + ", ".join(f"{n} {w}" for n, w in items) + ")"


@dataclass
class Activity:
    name: str
    entries: list[tuple[str, str]] = field(default_factory=list)   # (key, value), file order
    line_no: int = 0

    def get(self, key: str, default: str = "") -> str:
        for k, v in self.entries:
            if k.lower() == key.lower():
                return v
        return default

    @property
    def kind(self) -> str:
        if self.get("servos"):
            return "alive"
        play = self.get("play").strip().lower()
        if not play:
            return ""
        if play.startswith(("pick(", "seq:pick(")):
            return "pick"
        if play.startswith("playlist"):
            return "playlist"
        return "action"

    def sequence_names(self) -> list[str]:
        if self.kind == "pick":
            try:
                return [n for n, _w in parse_pick(self.get("play"))]
            except EventsError:
                return []
        if self.kind == "action":
            word, arg = split_action(self.get("play"))
            return [arg] if word in ("seq", "toggle") and arg else []
        return []

    def when_parts(self) -> tuple[list[str], list[str], list[str]]:
        """(modifiers, modes, audio states) from when=, as written (the robot checks the names)."""
        return parse_when(self.get("when"), strict=False)

    def describe(self) -> str:
        kind = self.kind
        play = self.get("play")
        if kind == "action":
            what = describe_action(play)
        elif kind == "playlist":
            m = re.match(r"playlist([AB]?):(\d+)", play, re.IGNORECASE)
            player = (m.group(1) or "A").upper() if m else "?"
            what = f"Play bank {m.group(2) if m else '?'} ({player})" + (", shuffled" if self.get("shuffle") == "1" else ", in order")
        elif kind == "pick":
            try:
                what = "Pick one of " + ", ".join(f"{n} ({w})" for n, w in parse_pick(play))
            except EventsError as e:
                what = str(e)
        elif kind == "alive":
            what = "Idle motion for " + self.get("servos")
        else:
            what = "(nothing to do: needs play = or servos =)"
        every = self.get("every")
        if every:
            what += (", then a gap of " if kind == "playlist" else ", every ") + every
        cooldown = self.get("cooldown")
        if cooldown:
            what += f", no repeat within {cooldown}"
        mods, modes, audio = self.when_parts()
        cond = when_words(mods, modes, audio)
        if kind == "alive" and "auto" not in modes:
            cond.append("in AUTO")
        return what + (" - " + ", ".join(cond) if cond else "")


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
