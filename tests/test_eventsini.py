"""eventsini.py: the events.ini model. Run: py -m unittest discover tests"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest  # noqa: E402

from eventsini import (NEW_FILE, Condition, EventsDoc, EventsError, Rule, Trigger,  # noqa: E402
                       button_width_us, buttons_down, cycle_items, describe_action, flat_actions,
                       format_cycle, format_pick, join_action, learn_channel, name_error, parse_pick,
                       split_action, split_actions, suggest_condition)

# The firmware's example (Orchestron docs/examples/events.ini), shortened
EXAMPLE = """; events.ini - example
; a comment

[settings]
deadband = 50                ; us either side

[inputs]
pad = ch17                   ; the pad

[modifiers]
shift = ch9 high             ; switch SA up
left  = ch4 <1200

[preset.deep]
fx.pitch.on = 1
fx.pitch.amount = 85

[events]
; Motion mode
ch13 low         = mode:idle
ch13 1500        = mode:auto
pad.1.double     = seq:nod
shift+pad.1      = toggle:look
pad.3            = home, when=mode.idle|manual
ch10 >1800.exit  = stopseq
ch18 high        = preset:deep, audio:random   ; voice
link.lost        = stopseq
ch9 sideways     = stop
"""


def _raises(fn, what=""):
    try:
        fn()
    except EventsError:
        return
    raise AssertionError(f"no EventsError: {what}")


def test_round_trip_keeps_every_line():
    doc = EventsDoc(EXAMPLE)
    assert doc.text() == EXAMPLE.replace("\n", "\r\n")
    assert EventsDoc(doc.text()).text() == doc.text()


def test_line_numbers_match_the_file():
    doc = EventsDoc(EXAMPLE)
    lines = EXAMPLE.split("\n")
    for n, l in doc.rules():
        assert lines[n - 1].startswith(l.key)
    assert doc.rule_at(20).rule.trigger.text() == "ch13 low"
    assert doc.rule_at(19) is None          # a comment
    assert doc.rule_at(5) is None           # not [events]


def test_reads_the_sections():
    doc = EventsDoc(EXAMPLE)
    assert doc.deadband() == 50
    assert doc.pad_channel() == 17
    assert doc.modifier_names() == ["shift", "left"]
    _n, _name, cond, err = doc.modifiers()[1]
    assert cond == Condition(4, "below", 1200) and not err
    assert doc.presets() == {"deep": [("fx.pitch.on", "1"), ("fx.pitch.amount", "85")]}
    assert doc.sequence_names() == ["nod", "look"]


def test_rules_parse():
    doc = EventsDoc(EXAMPLE)
    rules = {l.key: l for _n, l in doc.rules()}
    r = rules["pad.3"].rule
    assert r.trigger == Trigger("pad", button=3)
    assert r.actions == ["home"] and r.when_modes == ["idle", "manual"]
    r = rules["shift+pad.1"].rule
    assert r.trigger.mods == ["shift"] and r.trigger.gesture == "click"
    r = rules["ch10 >1800.exit"].rule
    assert r.trigger.exit and r.trigger.cond == Condition(10, "above", 1800)
    r = rules["ch18 high"].rule
    assert r.actions == ["preset:deep", "audio:random"] and r.comment == "; voice"
    assert r.is_state_rule()
    assert rules["ch13 1500"].rule.trigger.cond == Condition(13, "near", 1500, 0)
    assert rules["ch9 sideways"].rule is None and "sideways" in rules["ch9 sideways"].error


def test_trigger_text_round_trip():
    for text in ["pad.1", "pad.14.long", "left+shift+pad.3.double", "ch13 low", "ch13 mid",
                 "ch7 1200~80", "ch6 <1300", "ch11 1300-1700", "ch10 >1800.exit", "link.up",
                 "mode.control"]:
        assert Trigger.parse(text).text() == text, text


def test_old_forms_refused():
    # buttons.ini forms: Orchestron 2.33 refuses them, so the editor does too, saying what to write
    _raises(lambda: Trigger.parse("button2"))
    _raises(lambda: Trigger.parse("ch8.high"))
    _raises(lambda: Rule.parse("pad.1", "random:2"))
    _raises(lambda: Rule.parse("pad.1", "next:2"))
    assert Trigger.parse("pad.1.click").text() == "pad.1"


def test_bad_triggers():
    for bad in ["pad.15", "pad.1.quad", "ch25 high", "ch9", "ch9 3000", "mode.sleep",
                "ch4 1700-1300", "+pad.1", "link.down", "pad", "pad.x", "mode", "wob ble",
                "dome.quad", "shift+ch5 high"]:
        _raises(lambda: Trigger.parse(bad), bad)
    # "wobble" reads as a [buttons] name: whether there is one is the document's check
    assert Trigger.parse("wobble") == Trigger("named", name="wobble")


def test_rule_line_and_when():
    r = Rule(Trigger.parse("ch3 low"), ["mode:control"], ["gate"])
    assert r.value_text() == "mode:control, when=gate"
    r = Rule(Trigger.parse("pad.3"), ["home"], [], ["idle", "manual"])
    assert Rule.parse("pad.3", r.value_text()) == r
    r = Rule(Trigger.parse("pad.2"), ["seq:wave", "wavA:2001"], ["shift"], ["auto"], "; hi")
    back = Rule.parse(*[p.strip() for p in r.line().split("=", 1)])
    assert back == r
    assert Rule.parse("ch16 high", "home when=mode.idle|manual").when_modes == ["idle", "manual"]
    _raises(lambda: Rule.parse("pad.1", "seq:a, seq:b, seq:c, seq:d"))
    _raises(lambda: Rule.parse("pad.1", "home, when=mode.sleep"))


def test_actions():
    assert split_action("seq:wave") == ("seq", "wave")
    assert split_action("WAVA:12") == ("wavA", "12")
    assert split_action("randomA:2") == ("randomA", "2")
    assert split_action("home") == ("home", "")
    assert split_action("look") == ("seq", "look")      # buttons.ini form
    assert join_action("mode", "idle") == "mode:idle" and join_action("stop") == "stop"
    assert describe_action("mode:idle") == "Set the motion mode idle"
    assert describe_action("audio:random") == "Random sounds on"


def test_edit_rule_in_place_and_add():
    doc = EventsDoc(EXAMPLE)
    n = 20
    doc.set_rule(n, Rule(Trigger.parse("ch13 low"), ["mode:manual"]))
    assert doc.rule_at(n).rule.actions == ["mode:manual"]
    before = len(doc.lines)
    new = doc.set_rule(None, Rule(Trigger.parse("pad.5"), ["seq:wave"]))
    assert len(doc.lines) == before + 1
    assert doc.rule_at(new).rule.trigger.text() == "pad.5"
    assert doc.lines[new - 2].key == "ch9 sideways"      # appended after the last rule
    doc.delete_line(new)
    assert len(doc.lines) == before


def test_edit_other_sections():
    doc = EventsDoc(EXAMPLE)
    doc.set_value("settings", "deadband", "60")
    assert doc.deadband() == 60 and "; us either side" in doc.text()
    doc.set_value("inputs", "pad", "none")
    assert doc.pad_channel() == 0
    n = doc.set_modifier(None, "gate", Condition(5, "high"))
    assert doc.lines[n - 1].raw == "gate = ch5 high"
    assert doc.modifier_names() == ["shift", "left", "gate"]
    doc.set_modifier(n, "gate", Condition(5, "low"))
    assert doc.modifiers()[2][2] == Condition(5, "low")
    doc.set_preset("deep", [("fx.pitch.amount", "90")])
    doc.set_preset("bright", [("fx.ring.on", "0")])
    assert doc.presets() == {"deep": [("fx.pitch.amount", "90")], "bright": [("fx.ring.on", "0")]}
    doc.delete_preset("bright")
    assert "bright" not in doc.presets()
    assert EventsDoc(doc.text()).presets() == doc.presets()


def test_new_file_and_empty():
    doc = EventsDoc(NEW_FILE)
    assert [l.rule.trigger.text() for _n, l in doc.rules()] == ["link.lost"]
    empty = EventsDoc("")
    n = empty.set_rule(None, Rule(Trigger.parse("pad.1"), ["home"]))
    assert empty.rule_at(n) and "[events]" in empty.text()


def test_learn_helpers():
    before = [1500] * 24
    after = list(before)
    after[12] = 1000
    after[3] = 1560                               # stick noise
    assert learn_channel(before, after) == 13
    assert learn_channel(before, before) == 0
    assert suggest_condition(13, 990) == Condition(13, "low")
    assert suggest_condition(13, 2011) == Condition(13, "high")
    assert suggest_condition(13, 1503) == Condition(13, "mid")
    assert suggest_condition(10, 1787) == Condition(10, "near", 1790)
    assert Condition(10, "above", 1800).holds(1850) and not Condition(10, "above", 1800).holds(1700)
    assert Condition(7, "near", 1200, 0).holds(1240, deadband=50)


ACTIVITIES = """[events]
pad.1 = seq:wave
pad.2 = seq:look

[activity.chatter]
play  = randomA:2001-2013
every = 20-120s
when  = audio.random

[activity.fidget]
play     = pick(nod 3, look 2, shrug)   ; weighted
every    = 10-40s
cooldown = 60s
when     = mode.auto|manual+shift

[activity.alive]
servos = s1, s2
s2.swing = 30
when = mode.auto
"""


def test_new_actions():
    assert split_action("set:fx.pitch.amount=120") == ("set", "fx.pitch.amount=120")
    assert describe_action("set:fx.pitch.amount=120") == "Set fx.pitch.amount to 120"
    assert describe_action("randomA:2001-2013") == "Random WAV numbered 2001-2013 (A)"
    assert describe_action("randomB:3") == "Random WAV from bank 3 (B)"
    assert describe_action("nextA:3") == "Next WAV in bank 3 (A)"
    assert split_action("nextA:3") == ("nextA", "3")
    assert describe_action("audio:music") == "Music on"
    assert Rule.parse("ch4 high", "set:audio.mix.master=50").is_state_rule()


def test_activities():
    doc = EventsDoc(ACTIVITIES)
    acts = doc.activities()
    assert list(acts) == ["chatter", "fidget", "alive"]
    assert [a.kind for a in acts.values()] == ["action", "pick", "alive"]
    assert parse_pick(acts["fidget"].get("play")) == [("nod", 3), ("look", 2), ("shrug", 1)]
    assert acts["fidget"].when_parts() == (["shift"], ["auto", "manual"], [])
    assert acts["chatter"].when_parts() == ([], [], ["random"])
    assert acts["chatter"].describe() == ("Random WAV numbered 2001-2013 (A), every 20-120s - "
                                          "when sound mode is random")
    assert acts["alive"].describe() == "Idle motion for s1, s2 - in auto"
    assert doc.sequence_names() == ["wave", "look", "nod", "shrug"]
    _raises(lambda: parse_pick("pick(nod 300)"))
    assert format_pick([("nod", 3), ("look", 1)]) == "pick(nod 3, look 1)"

    # edit, add, delete: the other lines are untouched
    doc.set_activity("chatter", [("play", "playlistB:3"), ("shuffle", "1"), ("when", "audio.music")])
    assert doc.activities()["chatter"].kind == "playlist"
    assert "Play bank 3 (B), shuffled" in doc.activities()["chatter"].describe()
    doc.set_activity("music", [("play", "nextA:3"), ("every", "5s")])
    assert list(doc.activities()) == ["chatter", "fidget", "alive", "music"]
    doc.delete_activity("fidget")
    assert list(doc.activities()) == ["chatter", "alive", "music"]
    again = EventsDoc(doc.text())
    assert {n: a.entries for n, a in again.activities().items()} == {n: a.entries for n, a in doc.activities().items()}
    assert [l.key for _n, l in again.rules()] == ["pad.1", "pad.2"]


def test_edits_keep_end_of_line_comments():
    doc = EventsDoc("[activity.alive]\nservos = s1, s5\ns5.duty = 40   ; S5 moves less often\n"
                    "[preset.deep]\nfx.pitch.amount = 85  ; lower\n")
    doc.set_activity("alive", [("servos", "s1, s5"), ("s5.duty", "30")])
    assert "s5.duty   = 30" in doc.text() and "; S5 moves less often" in doc.text()
    doc.set_preset("deep", [("fx.pitch.amount", "80")])
    assert "fx.pitch.amount = 80" in doc.text() and "; lower" in doc.text()
    assert doc.activities()["alive"].get("s5.duty") == "30"
    assert doc.presets() == {"deep": [("fx.pitch.amount", "80")]}


def test_shipped_files():
    """Every events.ini Orchestron ships (docs/examples, the SD card folders) reads cleanly
    and round-trips byte for byte."""
    orch = Path(__file__).resolve().parents[3] / "Teensy4VocalizerV3" / "Software" / "Orchestron"
    files = [orch / "docs" / "examples" / "events.ini"] + sorted(orch.glob("SDCard*/events.ini"))
    files = [f for f in files if f.exists()]
    if not files:
        return  # not next to the Orchestron repo
    for f in files:
        text = f.read_bytes().decode("utf-8")
        doc = EventsDoc(text)
        assert doc.text() == text.replace("\r\n", "\n").replace("\n", "\r\n"), f
        bad = [l.raw for _n, l in doc.rules() if l.rule is None]
        assert not bad, (f, bad)
        named = [(name, err) for _n, name, _c, err in doc.buttons() + doc.modifiers() if err]
        assert not named and not doc.pad_problem() and not doc.rule_problems(), (f, named, doc.rule_problems())
        for name, act in doc.activities().items():
            assert act.kind, (f, name)


def test_move_rules():
    doc = EventsDoc(EXAMPLE)
    keys = lambda: [l.key for _n, l in doc.rules()]
    before = keys()
    n = next(n for n, l in doc.rules() if l.key == "ch13 1500")
    n2 = doc.move_entry(n, -1)
    assert keys()[0:2] == ["ch13 1500", "ch13 low"] and doc.lines[n2 - 1].key == "ch13 1500"
    assert doc.lines[n2 - 2].raw.startswith("; Motion mode")      # the comment stayed put
    assert doc.move_entry(n2, -1) == n2                           # already first in [events]
    doc.move_entry(n2, +1)
    assert keys() == before
    last = doc.rules()[-1][0]
    assert doc.move_entry(last, +1) == last                       # already last


# Named buttons and cycle() (Orchestron 2.36.0, docs/BUTTON_TRIGGERS.md section 5)
BUTTONS = """; Sparky
[inputs]
pad = ch17

[buttons]
; NAME = chN VALUE
dome   = ch3 1900
music  = ch4 1900   ; the music button
happy  = ch6 1106            ; several buttons on one channel
sad    = ch6 1194
horn   = ch7 high
siren  = ch8 1500~30

[modifiers]
bank2  = ch9 high

[events]
dome.press    = cycle(seq:dome_right, seq:dome_left)   ; each press: the next one
dome.release  = seq:dome_stop
music         = nextB:3
music.double  = nextB:3
music.long    = audio:music
happy         = seq:wave
bank2+happy   = seq:dance
pad.3.release = stopseq
ch12 high     = cycle(mode:auto, mode:idle), when=mode.idle|auto
"""


def test_buttons_section():
    doc = EventsDoc(BUTTONS)
    assert doc.text() == BUTTONS.replace("\n", "\r\n")
    assert doc.button_names() == ["dome", "music", "happy", "sad", "horn", "siren"]
    rows = {name: (cond, err) for _n, name, cond, err in doc.buttons()}
    assert rows["dome"] == (Condition(3, "near", 1900, 0), "")
    assert rows["horn"] == (Condition(7, "high"), "")
    assert rows["siren"] == (Condition(8, "near", 1500, 30), "")
    assert not doc.pad_problem() and not doc.rule_problems()
    assert all(l.rule is not None for _n, l in doc.rules())
    assert doc.modifiers()[0][1:] == ("bank2", Condition(9, "high"), "")
    assert doc.sequence_names() == ["dome_right", "dome_left", "dome_stop", "wave", "dance"]


def test_write_buttons():
    doc = EventsDoc(BUTTONS)
    n = doc.set_button(None, "lid", Condition(5, "near", 1980))
    assert doc.lines[n - 1].raw == "lid = ch5 1980" and doc.lines[n - 2].key == "siren"
    music = next(m for m, name, _c, _e in doc.buttons() if name == "music")
    doc.set_button(music, "music", Condition(4, "near", 1100, 40))
    assert doc.lines[music - 1].raw == "music = ch4 1100~40   ; the music button"
    again = EventsDoc(doc.text())
    assert again.buttons() == doc.buttons() and again.text() == doc.text()
    # A file without [buttons] gets the section above [modifiers] (or [events])
    doc = EventsDoc(EXAMPLE)
    n = doc.set_button(None, "dome", Condition(3, "near", 1900))
    assert doc.lines[n - 2].raw == "[buttons]" and doc.lines[n].raw == "" and doc.lines[n + 1].raw == "[modifiers]"
    assert doc.set_button(None, "music", Condition(4, "near", 1900)) == n + 1
    assert EventsDoc(doc.text()).button_names() == ["dome", "music"]
    doc = EventsDoc("[events]\npad.1 = home\n")
    n = doc.set_button(None, "dome", Condition(3, "near", 1900))
    assert [l.raw for l in doc.lines][:3] == ["[buttons]", "dome = ch3 1900", ""]


def test_button_names():
    for bad in ["ch5", "CH12", "pad", "pad3", "button3", "mode", "link", "a" * 16, ""]:
        assert name_error(bad, "button", plain=True), bad
    for good in ["chin", "modest", "padding", "linked", "dome_2", "a" * 15, "3way"]:
        assert not name_error(good, "button", plain=True), good
    assert name_error("dome-1", "button", plain=True) and not name_error("dome-1", "modifier")
    doc = EventsDoc("[buttons]\nch5 = ch5 1900\npad = ch3 1900\ndome-1 = ch4 1900\nok = ch2 1900\n"
                    "[modifiers]\nmy-mod = ch9 high\n")
    errs = {name: err for _n, name, _c, err in doc.buttons()}
    assert errs["ch5"] and errs["pad"] and errs["dome-1"] and not errs["ok"]
    assert not doc.modifiers()[0][3]                       # the firmware takes it for a modifier
    assert doc.name_problem("my-mod", "modifiers")         # ... the editor doesn't write it
    assert doc.name_problem("OK", "modifiers") == "ok is already a button (line 5)"
    assert doc.name_problem("ok", "buttons", 5) == ""      # its own line


def test_button_vs_modifier_names():
    # Unique across [buttons] and [modifiers], case-insensitively: the later line is refused
    doc = EventsDoc("[modifiers]\nbank2 = ch9 high\n[buttons]\nBank2 = ch3 1900\n")
    assert doc.modifiers()[0][3] == ""
    assert "already used" in doc.buttons()[0][3]
    doc = EventsDoc("[buttons]\ndome = ch3 1900\n[modifiers]\nDOME = ch9 high\n")
    assert doc.buttons()[0][3] == "" and "already used" in doc.modifiers()[0][3]
    doc = EventsDoc("[buttons]\ndome = ch3 1900\ndome = ch4 1900\n")
    assert [e != "" for _n, _nm, _c, e in doc.buttons()] == [False, True]


def test_buttons_not_on_the_pad_channel():
    doc = EventsDoc("[inputs]\npad = ch6\n[buttons]\nhappy = ch6 1106\nsad = ch5 1194\n")
    assert "pad" in doc.buttons()[0][3] and doc.buttons()[1][3] == "" and not doc.pad_problem()
    doc = EventsDoc("[buttons]\nhappy = ch6 1106\n[inputs]\npad = ch6\n")
    assert doc.buttons()[0][3] == "" and "ch6 carries [buttons]" in doc.pad_problem()
    doc = EventsDoc("[inputs]\npad = none\n[buttons]\nhappy = ch6 1106\n")
    assert doc.buttons()[0][3] == "" and not doc.pad_problem()


def test_button_limits():
    # 14 per channel
    doc = EventsDoc("[buttons]\n" + "".join(f"b{i} = ch6 {1000 + 50 * i}\n" for i in range(15)))
    assert [bool(e) for _n, _nm, _c, e in doc.buttons()] == [False] * 14 + [True]
    assert "14" in doc.buttons()[14][3]
    # 8 channels
    doc = EventsDoc("[buttons]\n" + "".join(f"b{c} = ch{c} 1900\n" for c in range(1, 10)) + "again = ch1 1100\n")
    assert [bool(e) for _n, _nm, _c, e in doc.buttons()] == [False] * 8 + [True, False]
    # 32 in all (4 channels of 8, then one more)
    doc = EventsDoc("[buttons]\n" + "".join(f"b{c}_{i} = ch{c} {1000 + 100 * i}\n"
                                            for c in range(1, 6) for i in range(8)))
    assert [bool(e) for _n, _nm, _c, e in doc.buttons()] == [False] * 32 + [True] * 8
    assert "32" in doc.buttons()[32][3]
    # a refused line doesn't count: the next good one is still taken
    doc = EventsDoc("[modifiers]\n" + "".join(f"m{i} = ch9 high\n" for i in range(9)))
    assert [bool(e) for _n, _nm, _c, e in doc.modifiers()] == [False] * 8 + [True]


def test_named_triggers():
    for text in ["dome", "dome.press", "dome.double", "dome.triple", "dome.long", "dome.release",
                 "bank2+happy", "bank2+bank3+happy.long", "pad.3.release", "shift+pad.1.release"]:
        assert Trigger.parse(text).text() == text, text
    assert Trigger.parse("dome.click").text() == "dome"
    t = Trigger.parse("bank2+happy.release")
    assert (t.source, t.name, t.gesture, t.mods) == ("named", "happy", "release", ["bank2"])
    assert t.describe() == "bank2 + happy button release"
    assert Trigger.parse("pad.3.release").describe() == "pad button 3 release"
    # The document checks the names: an undefined button or modifier is the firmware's error too
    doc = EventsDoc(BUTTONS + "wobble = stop\nbank9+dome = stop\nch5 high = home, when=gate\n"
                    "Dome.long = stop\n")
    problems = {doc.lines[n - 1].key: p for n, p in doc.rule_problems().items()}
    assert problems == {"wobble": "no [buttons] line named wobble",
                        "bank9+dome": "unknown modifier (define it in [modifiers]): bank9",
                        "ch5 high": "unknown modifier (define it in [modifiers]): gate"}
    # A button the firmware refuses (the pad's channel) can't be used either
    doc = EventsDoc("[inputs]\npad = ch6\n[buttons]\nhappy = ch6 1106\n[events]\nhappy = seq:wave\n")
    assert list(doc.rule_problems().values()) == ["no [buttons] line named happy"]


def test_cycle():
    r = Rule.parse("dome.press", "cycle(seq:dome_right, seq:dome_left)   ; each press")
    assert r.actions == ["cycle(seq:dome_right, seq:dome_left)"] and r.comment == "; each press"
    assert cycle_items(r.actions[0]) == ["seq:dome_right", "seq:dome_left"]
    assert describe_action(r.actions[0]) == ("Each time: the next of Play sequence dome_right, "
                                             "Play sequence dome_left")
    assert Rule.parse(*[p.strip() for p in r.line().split("=", 1)]) == r
    r = Rule.parse("ch12 high", "cycle(mode:auto, mode:idle, home), when=bank2+mode.idle")
    assert len(cycle_items(r.actions[0])) == 3 and r.when_mods == ["bank2"] and r.when_modes == ["idle"]
    assert not r.is_state_rule()                          # mode: in a cycle: never a state rule
    assert Rule.parse("ch12 high", "mode:auto").is_state_rule()
    assert Rule.parse("pad.1", "CYCLE(home, stop)").actions == ["CYCLE(home, stop)"]
    assert split_actions("seq:a, cycle(b, c), home") == ["seq:a", "cycle(b, c)", "home"]
    assert flat_actions(["cycle(seq:a, wavA:2)", "home"]) == ["seq:a", "wavA:2", "home"]
    assert format_cycle(["seq:a", " seq:b "]) == "cycle(seq:a, seq:b)"
    assert cycle_items("seq:a") is None
    assert cycle_items("cycle(cycle_left, seq:cycle_right)") == ["cycle_left", "seq:cycle_right"]  # names
    for bad in ["cycle(seq:a)", "cycle(a, b, c, d)", "cycle(cycle(a, b), c)", "cycle(a, cycle(b, c))",
                "seq:x, cycle(a, b)", "cycle(a, b), seq:x", "cycle(a, b) home", "cycle(a, b",
                "cycle(a, , b)", "cycle()", "cycle(random:2, seq:b)"]:
        _raises(lambda: Rule.parse("pad.1", bad), bad)


def test_whole_file_round_trip():
    doc = EventsDoc(BUTTONS + ACTIVITIES.replace("[events]\npad.1 = seq:wave\npad.2 = seq:look\n", ""))
    text = doc.text()
    for n, l in doc.rules():
        doc.set_rule(n, l.rule)                           # rewrite every rule from its model
    again = EventsDoc(doc.text())
    assert again.buttons() == EventsDoc(text).buttons()
    assert [l.rule for _n, l in again.rules()] == [l.rule for _n, l in EventsDoc(text).rules()]
    assert EventsDoc(text).text() == text


def test_buttons_down_and_width():
    assert button_width_us() == 12                       # 20 SBUS units, 1000 us over 172..1811
    assert button_width_us({"button.deadband": 40}) == 24
    assert button_width_us({"rc.sbus.min": 1811}) == 1   # nonsense range: still 1 us
    doc = EventsDoc(BUTTONS)
    rows = [(name, c) for _n, name, c, _e in doc.buttons()]
    us = [1000] * 24
    assert buttons_down(rows, us, 12) == set()
    us[2], us[5], us[6] = 1910, 1100, 1900               # dome, happy, horn
    assert buttons_down(rows, us, 12) == {"dome", "happy", "horn"}
    us[2] = 1913                                         # just outside +/- 12
    assert "dome" not in buttons_down(rows, us, 12)
    # One button per channel: the first line that matches wins
    wide = [("a", Condition(6, "near", 1150, 100)), ("b", Condition(6, "near", 1194, 0))]
    us[5] = 1194
    assert buttons_down(wide, us, 12) == {"a"}


def load_tests(loader, tests, pattern):
    """Every test_ function above (plain asserts; no third-party test runner needed)."""
    return unittest.TestSuite(unittest.FunctionTestCase(fn) for name, fn in sorted(globals().items())
                              if name.startswith("test_") and callable(fn))


if __name__ == "__main__":
    unittest.main()
