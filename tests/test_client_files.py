"""client.py's events.ini commands against a pretend Orchestron (2.31.0 <KF> <KU> <KV> <KI>).
Run: py -m unittest discover tests"""
from __future__ import annotations

import sys
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client import RobotClient  # noqa: E402
from protocol import ProtocolError  # noqa: E402
from transport import Transport  # noqa: E402

FRAME_MAX = 63          # the firmware's SerialPacket::kMaxData


class FakeOrchestron(Transport):
    """Answers like src/FileCommands.cpp + CommandProtocol.cpp, synchronously."""

    def __init__(self) -> None:
        super().__init__()
        self.files: dict[str, bytes] = {"events.ini": b"[events]\r\npad.1 = seq:wave\r\n" * 10}
        self.upload: tuple[str, int, bytearray] | None = None
        self.problems = [(3, "ch9 sideways: expected low, mid, high")]
        self.buttons_held: int | None = None     # 2.36.0+: <KI>'s last field (None: older firmware)
        self.notices = False
        self.corrupt_next_chunk = False
        self.longest = 0

    def open(self) -> None: ...
    def close(self) -> None: ...

    @property
    def is_open(self) -> bool:
        return True

    def describe(self) -> str:
        return "fake"

    def reply(self, body: str) -> None:
        self._emit(f"<{body}>\r\n".encode())

    def write(self, data: bytes) -> None:
        body = data.decode()[1:-1]
        self.longest = max(self.longest, len(body))
        if len(body) > FRAME_MAX:
            return                                   # dropped, as SerialPacket does
        op, args = body[:3], body[4:] if len(body) > 3 and body[3] == "," else ""
        if body == "KI":
            held = "" if self.buttons_held is None else f",{self.buttons_held}"
            self.reply("KI,1,0," + ",".join(["1500"] * 12 + ["1000"] + ["1500"] * 11) + held)
        elif body == "KFL":
            self.reply("KFL,2")
        elif body.startswith("KFL"):
            self.reply(f"KFL{body[3:]},{['2001-happy-0.wav', '2002-happy-1.wav'][int(body[3:])]}")
        elif op == "KFR":
            name, offset = args.split(",")
            if name not in ("events.ini", "sequences.ini", "config.ini"):
                return self.reply("KE0,3")
            if name not in self.files:
                return self.reply("KFR,0,-1,")
            data, off = self.files[name], int(offset)
            self.reply(f"KFR,{off},{len(data)},{data[off:off + 96].hex()}")
        elif op == "KFO":
            name, size = args.split(",")
            if name not in ("events.ini", "sequences.ini"):
                return self.reply("KE0,3")
            self.upload = (name, int(size), bytearray())
            self.reply(f"KFO,{size}")
        elif op == "KFW":
            if not self.upload:
                return self.reply("KE0,5")
            chunk = bytes.fromhex(args)
            if self.corrupt_next_chunk:
                chunk, self.corrupt_next_chunk = b"X" + chunk[1:], False
            self.upload[2].extend(chunk)
            self.reply(f"KFW,{len(self.upload[2])}")
        elif op == "KFC":
            name, size, buf = self.upload
            self.upload = None
            if len(buf) != size or zlib.crc32(bytes(buf)) != int(args, 16):
                return self.reply("KE0,5")
            self.files[name] = bytes(buf)
            self.reply(f"KFC,{size}")
        elif body == "KFX":
            self.upload = None
            self.reply("KFX")
        elif body in ("KU", "KUR"):
            self.reply(f"KU,7,{len(self.problems)}")
        elif body.startswith("KU"):
            line, text = self.problems[int(body[2:])]
            self.reply(f"KU{body[2:]},{line},{text}")
        elif body in ("KV1", "KV0"):
            self.notices = body == "KV1"
            self.reply(body)
        else:
            self.reply("KE0,1")


def make() -> tuple[RobotClient, FakeOrchestron]:
    t = FakeOrchestron()
    return RobotClient(t), t


class ClientFileTests(unittest.TestCase):
    def test_read(self):
        bot, t = make()
        seen = []
        self.assertEqual(bot.read_file("events.ini", lambda d, n: seen.append((d, n))), t.files["events.ini"])
        self.assertEqual(seen[-1], (280, 280))
        self.assertIsNone(bot.read_file("sequences.ini"))
        with self.assertRaises(ProtocolError):
            bot.read_file("secret.txt")

    def test_write_fits_the_frame_limit(self):
        bot, t = make()
        text = ("; comment with , < > and ; in it\r\nch13 low = mode:idle\r\n" * 40).encode()
        bot.write_file("events.ini", text)
        self.assertEqual(t.files["events.ini"], text)
        self.assertLessEqual(t.longest, FRAME_MAX)
        bot.write_file("events.ini", b"")              # an empty file is allowed
        self.assertEqual(t.files["events.ini"], b"")

    def test_damaged_upload_is_refused_and_kept(self):
        bot, t = make()
        old = t.files["events.ini"]
        t.corrupt_next_chunk = True
        with self.assertRaises(ProtocolError) as e:
            bot.write_file("events.ini", b"[events]\r\npad.2 = home\r\n")
        self.assertIn("damaged", str(e.exception))
        self.assertEqual(t.files["events.ini"], old)
        with self.assertRaises(ProtocolError):
            bot.write_file("config.ini", b"x")

    def test_wav_list(self):
        bot, _t = make()
        self.assertEqual(bot.wav_files(), ["2001-happy-0.wav", "2002-happy-1.wav"])

    def test_report_inputs_notices(self):
        bot, t = make()
        self.assertEqual(bot.events_report(reload=True), (7, [(3, "ch9 sideways: expected low, mid, high")]))
        up, pad, us = bot.inputs()
        self.assertTrue(up)
        self.assertEqual((pad, len(us), us[12]), (0, 24, 1000))
        fired = []
        bot.set_notice_handler(fired.append)
        bot.rule_notices(True)
        self.assertTrue(t.notices)
        t.reply("KV,20")                              # unsolicited: not taken as a reply
        self.assertEqual(fired, [20])
        self.assertEqual(bot.events_report()[0], 7)

    def test_inputs_buttons_held(self):
        bot, t = make()
        self.assertEqual(bot.inputs_and_buttons()[3], None)          # before 2.36: no last field
        t.buttons_held = 0b100101                                    # 2.36.0: [buttons] 0, 2 and 5 held
        up, pad, us, held = bot.inputs_and_buttons()
        self.assertEqual((up, pad, len(us), us[12], held), (True, 0, 24, 1000, 37))
        up, pad, us = bot.inputs()                                   # as before: 24 channels
        self.assertEqual((len(us), us[23]), (24, 1500))


if __name__ == "__main__":
    unittest.main()
