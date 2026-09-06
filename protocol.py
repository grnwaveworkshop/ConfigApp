"""<K...> protocol layer: request/reply correlation + telemetry routing.

Sits on a Transport, runs a FrameReader on the incoming byte stream, and splits
frames into telemetry (<KT,...>, dispatched to a handler) vs command replies
(returned to the caller of request()). One request is outstanding at a time,
which is plenty for a config app / dashboard.

Shared across the whole robot family - RX-80B, Orchestron, T4-IMU all speak
this same protocol under the same category letter (see profiles/ for what
differs: telemetry field layout and scale table per project).
"""
from __future__ import annotations

import queue
import threading
from collections.abc import Callable

from framing import FrameReader
from transport import Transport

# Category letter. Must match kCmdCategory in each project's CommandProtocol.hpp.
# 'K' for "keys" (config keys) - deliberately not a project name, since one
# client drives the whole family.
CATEGORY = "K"

# Frames whose payload starts with this are telemetry, not a command reply.
_TELEMETRY_TAG = CATEGORY + "T"


class ProtocolError(Exception):
    """A <KE##,code> error reply, or a malformed/missing reply."""


def split_frame(frame: str) -> tuple[str, list[str]]:
    """'KG44,880' -> ('KG44', ['880']); 'KP,20401' -> ('KP', ['20401'])."""
    parts = frame.split(",")
    return parts[0], parts[1:]


class Protocol:
    def __init__(self, transport: Transport) -> None:
        self.t = transport
        self._reader = FrameReader()
        self._lock = threading.Lock()
        self._resp: "queue.Queue[str]" = queue.Queue()
        self._on_telemetry: Callable[[str], None] | None = None
        transport.set_on_bytes(self._on_bytes)

    def set_telemetry_handler(self, cb: Callable[[str], None] | None) -> None:
        self._on_telemetry = cb

    def _on_bytes(self, data: bytes) -> None:
        for frame in self._reader.feed(data):
            if frame.startswith(_TELEMETRY_TAG):
                if self._on_telemetry:
                    self._on_telemetry(frame)
            else:
                self._resp.put(frame)

    def send(self, body: str) -> None:
        """Fire-and-forget a command (e.g. <KR20> stream-rate)."""
        self.t.write(f"<{body}>".encode("ascii"))

    def request(self, body: str, timeout: float = 1.5) -> str:
        """Send <body> and return the next non-telemetry reply frame."""
        with self._lock:
            while not self._resp.empty():  # drop stale replies
                self._resp.get_nowait()
            self.t.write(f"<{body}>".encode("ascii"))
            try:
                return self._resp.get(timeout=timeout)
            except queue.Empty as e:
                raise ProtocolError(f"no reply to <{body}>") from e
