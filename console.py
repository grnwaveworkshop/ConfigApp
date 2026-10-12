"""The robot's console output as lines, for the Terminal tab (py app.py --terminal).

The link's reader thread feeds the text in chunks as it arrives (RobotClient's console
handler); the render loop takes the lines that are new since it last looked. The app adds
its own lines (connected, link closed) so a reboot reads as one story. Thread-safe; keeps
the last max_lines.
"""
from __future__ import annotations

import threading
from collections import deque

MAX_PARTIAL = 2000      # a "line" this long with no line break is shown as a line anyway


class ConsoleLog:
    def __init__(self, max_lines: int = 3000) -> None:
        self.max_lines = max_lines
        self._lock = threading.Lock()
        self._lines: deque[tuple[str, bool]] = deque(maxlen=max_lines)   # (text, from the app)
        self._partial = ""      # the line still being printed (no line break yet)
        self._count = 0         # lines completed since the start; never goes back

    def _add(self, text: str, from_app: bool = False) -> None:
        self._lines.append((text, from_app))
        self._count += 1

    def feed(self, text: str) -> None:
        """Text from the robot. Lines end at a line feed; carriage returns are dropped."""
        with self._lock:
            parts = (self._partial + text.replace("\r", "")).split("\n")
            self._partial = parts.pop()
            for line in parts:
                self._add(line)
            if len(self._partial) > MAX_PARTIAL:
                self._add(self._partial)
                self._partial = ""

    def note(self, text: str) -> None:
        """A line of the app's own, after the robot's unfinished line if there is one."""
        with self._lock:
            if self._partial:
                self._add(self._partial)
                self._partial = ""
            self._add(text, from_app=True)

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
            self._partial = ""

    def new_lines(self, seen: int) -> tuple[list[tuple[str, bool]], str, int]:
        """The lines completed after the first `seen`, the unfinished line, and the count to
        pass next time. After more than max_lines, only the last max_lines are there."""
        with self._lock:
            new = min(self._count - seen, len(self._lines))
            lines = list(self._lines)[len(self._lines) - new:] if new > 0 else []
            return lines, self._partial, self._count

    def text(self) -> str:
        """Everything kept, for Copy and Save."""
        with self._lock:
            out = "\n".join(t for t, _ in self._lines)
            if self._partial:
                out += ("\n" if out else "") + self._partial
            return out
