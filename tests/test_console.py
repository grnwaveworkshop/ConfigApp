"""The robot's console text next to the <K...> frames (the Terminal tab, app.py --terminal):
framing.py hands it back in order, protocol.py keeps it (and HCR echoes) out of the replies,
console.py turns it into lines.
Run: py -m unittest discover tests"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client import RobotClient  # noqa: E402
from console import MAX_PARTIAL, ConsoleLog  # noqa: E402
from framing import FRAME, TEXT, FrameReader  # noqa: E402
from protocol import is_protocol_frame  # noqa: E402
from transport import Transport  # noqa: E402


class FrameReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.r = FrameReader()

    def test_text_and_frames_come_back_in_order(self) -> None:
        got = self.r.feed(b"[Setup] SD ok\r\n<KP,23500>\r\nloaded 3 rules\r\n")
        self.assertEqual(got, [(TEXT, "[Setup] SD ok\r\n"), (FRAME, "KP,23500"),
                               (TEXT, "\r\nloaded 3 rules\r\n")])

    def test_a_frame_split_across_reads(self) -> None:
        self.assertEqual(self.r.feed(b"ab<KG4"), [(TEXT, "ab")])
        self.assertEqual(self.r.feed(b"4,880>"), [(FRAME, "KG44,880")])

    def test_a_less_than_sign_in_text_is_text_at_the_line_break(self) -> None:
        got = self.r.feed(b"speed < 5 ok\r\n<KP,1>")
        self.assertEqual(got, [(TEXT, "speed < 5 ok\r\n"), (FRAME, "KP,1")])

    def test_a_second_less_than_sign_starts_the_frame_again(self) -> None:
        got = self.r.feed(b"x <- y<KC,261>")
        self.assertEqual(got, [(TEXT, "x <- y"), (FRAME, "KC,261")])

    def test_an_overlong_frame_is_text(self) -> None:
        r = FrameReader(max_len=8)
        got = r.feed(b"<0123456789\n")
        self.assertEqual(got, [(TEXT, "<01234567" + "89\n")])

    def test_utf8_split_across_reads(self) -> None:
        mu = "µs".encode()          # "µs", 2 bytes then 1
        self.assertEqual(self.r.feed(b"10 " + mu[:1]), [(TEXT, "10 ")])
        self.assertEqual(self.r.feed(mu[1:] + b"\n"), [(TEXT, "µs\n")])


class ProtocolFrameTests(unittest.TestCase):
    def test_which_frames_are_the_protocols(self) -> None:
        for frame in ("KP,20401", "KT,1,2,3", "KE5,1", "KA,1,ok", "KV,12", "KFL,3"):
            self.assertTrue(is_protocol_frame(frame), frame)
        for frame in ("QPA1", "QVP35", "K...", "K", "", "kp,1"):
            self.assertFalse(is_protocol_frame(frame), frame)


class FakeOrchestron(Transport):
    """Answers <KP> after printing on its console and echoing an HCR reply, all in one read,
    as Orchestron does when WAV A starts while ConfigApp pings."""

    def open(self) -> None: ...
    def close(self) -> None: ...

    @property
    def is_open(self) -> bool:
        return True

    def describe(self) -> str:
        return "fake"

    def write(self, data: bytes) -> None:
        if data == b"<KP>":
            self._emit(b"[WAV] A: 2001.wav\r\n<QPA1>\r\n<KT,10,2,0>\r\n<KP,23500>\r\n")


class ConsoleRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = RobotClient(FakeOrchestron())
        self.console: list[str] = []
        self.telemetry: list = []
        self.bot.set_console_handler(self.console.append)
        self.bot.set_telemetry_handler(self.telemetry.append)

    def test_an_hcr_echo_is_not_taken_as_the_reply(self) -> None:
        self.assertEqual(self.bot.ping(), 23500)

    def test_the_console_gets_the_text_and_the_echo_in_order(self) -> None:
        self.bot.ping()
        # The <K...> frames go, and so do their line breaks: no blank line per reply
        self.assertEqual("".join(self.console), "[WAV] A: 2001.wav\r\n<QPA1>\r\n")
        self.assertEqual(len(self.telemetry), 1)

    def test_without_a_console_handler_it_still_answers(self) -> None:
        self.bot.set_console_handler(None)
        self.assertEqual(self.bot.ping(), 23500)


class FrameLineBreakTests(unittest.TestCase):
    """A <K...> frame's own line break goes with it, even split across reads."""

    def setUp(self) -> None:
        self.bot = RobotClient(FakeOrchestron())
        self.console: list[str] = []
        self.bot.set_console_handler(self.console.append)
        self.bot.set_telemetry_handler(lambda fr: None)

    def feed(self, *reads: bytes) -> str:
        for data in reads:
            self.bot.proto._on_bytes(data)
        return "".join(self.console)

    def test_cr_and_lf_in_separate_reads(self) -> None:
        self.assertEqual(self.feed(b"a\r\n<KT,1,0,0>\r", b"\nb\r\n"), "a\r\nb\r\n")

    def test_a_frame_in_the_middle_of_a_console_line(self) -> None:
        self.assertEqual(self.feed(b"[Setup] <KT,1,0,0>\r\ndone\r\n"), "[Setup] done\r\n")

    def test_a_blank_line_the_robot_printed_stays(self) -> None:
        self.assertEqual(self.feed(b"<KT,1,0,0>\r\n\r\nx\r\n"), "\r\nx\r\n")

    def test_frames_back_to_back(self) -> None:
        self.assertEqual(self.feed(b"<KT,1,0,0><KT,2,0,0>\r\nok\r\n"), "ok\r\n")

    def test_an_hcr_echo_keeps_its_line_break(self) -> None:
        self.assertEqual(self.feed(b"<QPA0>\r\n"), "<QPA0>\r\n")


class ConsoleLogTests(unittest.TestCase):
    def test_lines_from_chunks(self) -> None:
        log = ConsoleLog()
        log.feed("Orchestron Con")
        log.feed("trol System\r\n  Version: 2.35.0\r\n[Setup] Init")
        lines, partial, count = log.new_lines(0)
        self.assertEqual(lines, [("Orchestron Control System", False), ("  Version: 2.35.0", False)])
        self.assertEqual(partial, "[Setup] Init")
        self.assertEqual(count, 2)
        log.feed("ializing SD card...\r\n")
        lines, partial, count = log.new_lines(count)
        self.assertEqual(lines, [("[Setup] Initializing SD card...", False)])
        self.assertEqual((partial, count), ("", 3))

    def test_a_note_ends_the_unfinished_line(self) -> None:
        log = ConsoleLog()
        log.feed("Enter 1 to reboot anyway:")
        log.note("--- link closed ---")
        lines, partial, _ = log.new_lines(0)
        self.assertEqual(lines, [("Enter 1 to reboot anyway:", False), ("--- link closed ---", True)])
        self.assertEqual(partial, "")

    def test_keeps_the_last_max_lines(self) -> None:
        log = ConsoleLog(max_lines=3)
        log.feed("".join(f"line {i}\n" for i in range(5)))
        lines, _, count = log.new_lines(0)
        self.assertEqual([t for t, _ in lines], ["line 2", "line 3", "line 4"])
        self.assertEqual(count, 5)
        log.feed("line 5\n")
        lines, _, count = log.new_lines(count)
        self.assertEqual(([t for t, _ in lines], count), (["line 5"], 6))

    def test_clear_keeps_counting(self) -> None:
        log = ConsoleLog()
        log.feed("a\nb\npart")
        _, _, seen = log.new_lines(0)
        log.clear()
        self.assertEqual(log.new_lines(seen), ([], "", 2))
        log.feed("c\n")
        self.assertEqual(log.new_lines(seen), ([("c", False)], "", 3))

    def test_a_line_with_no_end_is_cut(self) -> None:
        log = ConsoleLog()
        log.feed("x" * (MAX_PARTIAL + 1))
        lines, partial, _ = log.new_lines(0)
        self.assertEqual((len(lines), partial), (1, ""))

    def test_text_for_copy_and_save(self) -> None:
        log = ConsoleLog()
        log.feed("one\r\ntwo\r\nthr")
        self.assertEqual(log.text(), "one\ntwo\nthr")


if __name__ == "__main__":
    unittest.main()
