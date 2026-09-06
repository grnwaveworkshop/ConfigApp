"""Transport abstraction: USB serial. Kept as its own class so a BLE transport
could be added later without touching the rest of the app.

A transport delivers received bytes via the callback set with set_on_bytes();
the callback runs on a background thread.
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable


class Transport(ABC):
    def __init__(self) -> None:
        self._on_bytes: Callable[[bytes], None] | None = None

    def set_on_bytes(self, cb: Callable[[bytes], None]) -> None:
        self._on_bytes = cb

    def _emit(self, data: bytes) -> None:
        if self._on_bytes and data:
            self._on_bytes(data)

    @abstractmethod
    def open(self) -> None: ...
    @abstractmethod
    def close(self) -> None: ...
    @abstractmethod
    def write(self, data: bytes) -> None: ...

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    @abstractmethod
    def describe(self) -> str: ...


# --------------------------------------------------------------------------- #
# USB serial (pyserial).
# --------------------------------------------------------------------------- #
class SerialTransport(Transport):
    def __init__(self, port: str, baud: int = 115200) -> None:
        super().__init__()
        self.port = port
        self.baud = baud
        self._ser = None
        self._thread: threading.Thread | None = None
        self._running = False

    def open(self) -> None:
        import serial  # pyserial

        self._ser = serial.Serial(self.port, self.baud, timeout=0.05)
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        ser = self._ser
        while self._running and ser is not None:
            try:
                n = ser.in_waiting
                data = ser.read(n if n else 1)
            except Exception:
                break
            if data:
                self._emit(data)

    def write(self, data: bytes) -> None:
        if self._ser is not None:
            self._ser.write(data)

    def close(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def describe(self) -> str:
        return f"USB {self.port} @ {self.baud}"

    @staticmethod
    def list_ports() -> list[tuple[str, str]]:
        from serial.tools import list_ports

        return [(p.device, p.description) for p in list_ports.comports()]
