"""Angle-bracket (<...>) frame reader - Python mirror of the firmware SerialPacket.

A byte stream (USB serial) is fed in; complete frames are returned with the
surrounding < > stripped. Overflow resets the reader so it resynchronises on
the next '<', exactly like src/SerialPacket.hpp on the firmware side.
"""
from __future__ import annotations


class FrameReader:
    # Cap generously: command replies are short, but a <KT,...> telemetry frame
    # can be ~300+ bytes. (The firmware's inbound reader is 64 - it only parses
    # short commands; this reader parses the longer telemetry frames it sends back.)
    def __init__(self, max_len: int = 512) -> None:
        self._buf = bytearray()
        self._in_frame = False
        self._max = max_len

    def feed(self, data: bytes) -> list[str]:
        """Feed received bytes; return a list of completed frame payloads (str)."""
        out: list[str] = []
        for b in data:
            if self._in_frame:
                if b == 0x3E:  # '>'
                    out.append(self._buf.decode("ascii", "replace"))
                    self._buf.clear()
                    self._in_frame = False
                elif len(self._buf) >= self._max:
                    # Overflow - drop the frame, resync on the next '<'.
                    self._buf.clear()
                    self._in_frame = False
                else:
                    self._buf.append(b)
            elif b == 0x3C:  # '<'
                self._in_frame = True
                self._buf.clear()
        return out
