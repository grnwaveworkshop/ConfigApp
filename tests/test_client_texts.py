"""client.py's text keys (<KX>, BallBot 0.7.9) against a pretend BallBot: the policy file dropdown.
Run: py -m unittest discover tests"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client import RobotClient  # noqa: E402
from protocol import ProtocolError  # noqa: E402
from transport import Transport  # noqa: E402


class FakeBallBot(Transport):
    """Answers <KX...> like src/CommandProtocol.cpp HandleTextKey + PolicyStore, synchronously."""

    DESC = "Policy to run: builtin (compiled in) or a .policy file in /policies on the SD card; loads when disarmed"

    def __init__(self, has_texts: bool = True) -> None:
        super().__init__()
        self.has_texts = has_texts
        self.value = "builtin"
        self.files = ["velp10", "velp11"]
        self.armed = False
        self.scans = 0

    def open(self) -> None: ...
    def close(self) -> None: ...

    @property
    def is_open(self) -> bool:
        return True

    def describe(self) -> str:
        return "fake"

    def reply(self, body: str) -> None:
        self._emit(f"<{body}>\r\n".encode())

    def status(self) -> str:
        if self.value == "builtin":
            return "built-in velp11"
        if self.value not in self.files:
            return f"{self.value}: not on the SD card; built-in velp11 runs"
        if self.armed:
            return f"{self.value} selected: loads when disarmed (velp11 runs)"
        return f"{self.value} from SD (crc 2d95813c; 65 inputs; self-test ok)"

    def write(self, data: bytes) -> None:
        body = data.decode()[1:-1]
        if not self.has_texts and body.startswith("KX"):
            return self.reply("KE0,1")
        choices = ["builtin"] + self.files
        if body == "KX":
            self.reply("KX,1")
        elif body == "KX0":
            self.scans += 1
            self.reply(f"KX0,ctrl.policy.file,{self.value},{len(choices)},{self.status()},{self.DESC}")
        elif body.startswith("KXO0,"):
            i = int(body.split(",")[1])
            self.reply(f"KXO0,{i},{choices[i]}" if 0 <= i < len(choices) else "KE0,2")
        elif body.startswith("KXS0,"):
            value = body.split(",", 1)[1]
            if not value or len(value) > 31 or any(not (c.isalnum() or c in "-_.") for c in value):
                return self.reply("KE0,2")
            self.value = value
            applied = value == "builtin" or (value in self.files and not self.armed)
            self.reply(f"KXS0,{1 if applied else 0},{value},{self.status()}")
        elif body.startswith("KX"):
            self.reply(f"KE{body[2:]},1")


class TextKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.robot = FakeBallBot()
        self.bot = RobotClient(self.robot)

    def test_lists_the_key_with_its_choices(self) -> None:
        texts = self.bot.texts()
        self.assertEqual(len(texts), 1)
        t = texts[0]
        self.assertEqual((t.id, t.key, t.value, t.group), (0, "ctrl.policy.file", "builtin", "ctrl"))
        self.assertEqual(t.choices, ["builtin", "velp10", "velp11"])
        self.assertEqual(t.status, "built-in velp11")
        self.assertIn("loads when disarmed", t.description)    # commas in it would survive too

    def test_rescan_sees_new_files(self) -> None:
        self.bot.texts()
        self.robot.files.append("velp12")
        self.assertIn("velp12", self.bot.text(0).choices)
        self.assertEqual(self.robot.scans, 2)

    def test_set_loads_or_waits(self) -> None:
        applied, value, status = self.bot.set_text(0, "velp10")
        self.assertTrue(applied)
        self.assertEqual(value, "velp10")
        self.assertIn("from SD", status)
        self.robot.armed = True
        applied, _value, status = self.bot.set_text(0, "velp11")
        self.assertFalse(applied)
        self.assertIn("loads when disarmed", status)

    def test_missing_file_is_stored_not_applied(self) -> None:
        applied, _value, status = self.bot.set_text(0, "velp99")
        self.assertFalse(applied)
        self.assertIn("not on the SD card", status)

    def test_bad_values_refused(self) -> None:
        with self.assertRaises(ProtocolError):
            self.bot.set_text(0, "a,b")              # never sent: it would split the frame
        with self.assertRaises(ProtocolError):
            self.bot.set_text(0, "bad name")         # the robot refuses it
        self.assertEqual(self.robot.value, "builtin")

    def test_firmware_without_text_keys(self) -> None:
        self.assertEqual(RobotClient(FakeBallBot(has_texts=False)).texts(), [])


if __name__ == "__main__":
    unittest.main()
