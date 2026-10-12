"""<K...> protocol layer: request/reply correlation + telemetry routing.

Sits on a Transport, runs a FrameReader on the incoming byte stream, and splits
frames into telemetry (<KT,...>, dispatched to a handler) vs command replies
(returned to the caller of request()). One request is outstanding at a time,
which is plenty for a config app / dashboard.

Anything that isn't a <K...> frame is the robot's console: the text it prints,
and the HCR frames it echoes there (<QPA1> when WAV A starts). That goes to the
console handler (the Terminal tab), never to request(): a <QPA1> arriving while
a request waits would otherwise be taken as its reply.

Shared across the whole robot family - RX-80B, Orchestron, T4-IMU all speak
this same protocol under the same category letter (see profiles/ for what
differs: telemetry field layout and scale table per project).
"""
from __future__ import annotations

import queue
import threading
from collections.abc import Callable

from framing import FRAME, FrameReader
from transport import Transport, TransportError

# Category letter. Must match kCmdCategory in each project's CommandProtocol.hpp.
# 'K' for "keys" (config keys) - deliberately not a project name, since one
# client drives the whole family.
CATEGORY = "K"

# Frames whose payload starts with this are telemetry, not a command reply.
_TELEMETRY_TAG = CATEGORY + "T"
# Rule-fired notices (Orchestron 2.31.0, after <KV1>): <KV,line>. Also not a reply - the
# reply to <KV1> itself is <KV1>, without the comma.
_NOTICE_TAG = CATEGORY + "V,"
# What the firmware sends after each frame (printf("<...>\r\n"))
_FRAME_EOL = "\r\n"


class ProtocolError(Exception):
    """A <KE##,code> error reply, or a malformed/missing reply."""


def is_protocol_frame(frame: str) -> bool:
    """A <K...> reply, telemetry or notice: the category letter, then a capital (KP, KT, KE).
    "<K...>" in a help text and HCR frames (QPA1) are not."""
    return len(frame) >= 2 and frame[0] == CATEGORY and frame[1].isupper()


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
        self._on_notice: Callable[[str], None] | None = None
        self._on_console: Callable[[str], None] | None = None
        # The line break after a <K...> frame isn't console text: what's left of it to drop
        # (it can arrive in the next read)
        self._eol_left = ""
        transport.set_on_bytes(self._on_bytes)

    def set_telemetry_handler(self, cb: Callable[[str], None] | None) -> None:
        self._on_telemetry = cb

    def set_notice_handler(self, cb: Callable[[str], None] | None) -> None:
        self._on_notice = cb

    def set_console_handler(self, cb: Callable[[str], None] | None) -> None:
        """cb(text) for the robot's console output, in chunks as it arrives (not whole
        lines); runs on the transport's reader thread."""
        self._on_console = cb

    def _on_bytes(self, data: bytes) -> None:
        for kind, payload in self._reader.feed(data):
            if kind != FRAME:
                text = self._drop_frame_eol(payload)
                if text and self._on_console:
                    self._on_console(text)
                continue
            frame = payload
            if not is_protocol_frame(frame):      # an HCR echo, or <...> in a help text
                self._eol_left = ""
                if self._on_console:
                    self._on_console(f"<{frame}>")
                continue
            self._eol_left = _FRAME_EOL
            if frame.startswith(_TELEMETRY_TAG):
                if self._on_telemetry:
                    self._on_telemetry(frame)
            elif frame.startswith(_NOTICE_TAG):
                if self._on_notice:
                    self._on_notice(frame)
            else:
                self._resp.put(frame)

    def _drop_frame_eol(self, text: str) -> str:
        """The text after a <K...> frame, without the frame's own line break."""
        while self._eol_left and text and text[0] in self._eol_left:
            self._eol_left = self._eol_left[self._eol_left.index(text[0]) + 1:]
            text = text[1:]
        if text:
            self._eol_left = ""
        return text

    def send(self, body: str) -> None:
        """Fire-and-forget a command (e.g. <KR20> stream-rate)."""
        try:
            self.t.write(f"<{body}>".encode("ascii"))
        except TransportError as e:
            raise ProtocolError(str(e)) from e

    def request(self, body: str, timeout: float = 1.5) -> str:
        """Send <body> and return the next non-telemetry reply frame."""
        # Fail fast on a known-dead link: without this every queued request
        # still waits out the full timeout, which is what makes the UI feel
        # hung after the cable is pulled.
        if not self.t.is_open:
            raise ProtocolError("link is down")
        with self._lock:
            while not self._resp.empty():  # drop stale replies
                self._resp.get_nowait()
            try:
                self.t.write(f"<{body}>".encode("ascii"))
            except TransportError as e:
                raise ProtocolError(str(e)) from e
            try:
                return self._resp.get(timeout=timeout)
            except queue.Empty as e:
                raise ProtocolError(f"no reply to <{body}>") from e
