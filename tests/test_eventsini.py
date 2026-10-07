"""eventsini.py: the events.ini model. Run: py -m unittest discover tests"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest  # noqa: E402

from eventsini import (NEW_FILE, Condition, EventsDoc, EventsError, Rule, Trigger,  # noqa: E402
                       describe_action, format_pick, join_action, learn_channel, parse_pick,
                       split_action, suggest_condition)

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
                "ch4 1700-1300", "+pad.1", "wobble"]:
        _raises(lambda: Trigger.parse(bad), bad)


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


def load_tests(loader, tests, pattern):
    """Every test_ function above (plain asserts; no third-party test runner needed)."""
    return unittest.TestSuite(unittest.FunctionTestCase(fn) for name, fn in sorted(globals().items())
                              if name.startswith("test_") and callable(fn))


if __name__ == "__main__":
    unittest.main()
