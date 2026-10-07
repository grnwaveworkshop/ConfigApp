"""client.py's loop profiler commands against a pretend Orchestron 2.34 (<KQ>).
Run: py -m unittest discover tests"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client import RobotClient  # noqa: E402
from protocol import ProtocolError  # noqa: E402
from test_client_files import FakeOrchestron  # noqa: E402

# As src/Profiling.cpp + LoopProfiler::formatCsvRow()
SECTIONS = [("loop", 5000, 180, 200, 2400), ("servos", 5000, 40, 45, 300)]


class FakeProfiler(FakeOrchestron):
    def __init__(self) -> None:
        super().__init__()
        self.enabled = False
        self.resets = 0

    def write(self, data: bytes) -> None:
        body = data.decode()[1:-1]
        if not body.startswith("KQ"):
            return super().write(data)
        rest = body[2:]
        if rest[:1].isdigit():
            i = int(rest)
            if i >= len(SECTIONS):
                return self.reply(f"KE{i},1")
            name, calls, dmin, davg, dmax = SECTIONS[i]
            return self.reply(f"KQ{i},{name},{calls},{dmin},{davg},{dmax},990,1000,3100")
        if rest == "R":
            self.resets += 1
        elif rest.startswith("E"):
            self.enabled = rest == "E1"
        self.reply(f"KQ,{len(SECTIONS)},{int(self.enabled)},0,23.4,31.0,12,20,96")


class OldFirmware(FakeOrchestron):
    """Before 2.34: <KQ> is an unknown command."""


class ProfilerTests(unittest.TestCase):
    def test_status_and_switch(self):
        t = FakeProfiler()
        bot = RobotClient(t)
        st = bot.profiler()
        self.assertEqual((st.sections, st.enabled, st.auto_report), (2, False, False))
        self.assertAlmostEqual(st.audio_cpu, 23.4)
        self.assertEqual((st.blocks, st.blocks_max, st.blocks_total), (12, 20, 96))
        self.assertTrue(bot.profiler("E1").enabled)
        self.assertFalse(bot.profiler("E0").enabled)
        bot.profiler("R")
        self.assertEqual(t.resets, 1)

    def test_sections(self):
        bot = RobotClient(FakeProfiler())
        rows = bot.profiler_sections(bot.profiler())
        self.assertEqual([r.name for r in rows], ["loop", "servos"])
        self.assertEqual((rows[1].id, rows[1].calls, rows[1].dur_avg, rows[1].dur_max), (1, 5000, 45, 300))
        self.assertEqual((rows[0].period_avg, rows[0].period_max), (1000, 3100))
        with self.assertRaises(ProtocolError):
            bot.profiler_section(9)

    def test_old_firmware_has_none(self):
        self.assertIsNone(RobotClient(OldFirmware()).profiler())


if __name__ == "__main__":
    unittest.main()
