"""Angle-bracket (<...>) frame reader - Python mirror of the firmware SerialPacket.

A byte stream (USB serial) is fed in; complete frames are returned with the
surrounding < > stripped. Overflow resets the reader so it resynchronises on
the next '<', exactly like src/SerialPacket.hpp on the firmware side.

Everything else on the stream is the robot's console text (Serial.print on the
same USB port), returned in order with the frames, for the Terminal tab. A frame
never holds a line break or a second '<', so a '<' in console text ("speed < 5")
comes back as text at the end of its line, not taken as a frame.
"""
from __future__ import annotations

import codecs

FRAME = "frame"
TEXT = "text"


class FrameReader:
    # Cap generously: command replies are short, but a <KT,...> telemetry frame
    # can be ~300+ bytes. (The firmware's inbound reader is 64 - it only parses
    # short commands; this reader parses the longer telemetry frames it sends back.)
    def __init__(self, max_len: int = 512) -> None:
        self._buf = bytearray()
        self._in_frame = False
        self._max = max_len
        # A UTF-8 character split across two reads still decodes
        self._text_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, data: bytes) -> list[tuple[str, str]]:
        """Feed received bytes; return what they completed, in order:
        (FRAME, payload) for each frame, (TEXT, text) for the console text between them."""
        out: list[tuple[str, str]] = []
        text = bytearray()

        def flush_text() -> None:
            s = self._text_decoder.decode(bytes(text))
            if s:
                out.append((TEXT, s))
            text.clear()

        for b in data:
            if self._in_frame:
                if b == 0x3E:  # '>'
                    flush_text()
                    out.append((FRAME, self._buf.decode("ascii", "replace")))
                    self._buf.clear()
                    self._in_frame = False
                elif b in (0x0A, 0x0D, 0x3C) or len(self._buf) >= self._max:
                    # Not a frame after all (a line break, another '<', too long):
                    # it was text. A '<' starts the next try.
                    text += b"<" + self._buf
                    self._buf.clear()
                    self._in_frame = b == 0x3C
                    if not self._in_frame:
                        text.append(b)
                else:
                    self._buf.append(b)
            elif b == 0x3C:  # '<'
                self._in_frame = True
                self._buf.clear()
            else:
                text.append(b)
        flush_text()
        return out
