"""Transport abstraction: USB serial and BLE behind one interface.

Both carry the same <K...> command protocol, so the rest of the app is
transport-agnostic. BLE targets HM-10 class modules (e.g. "DSD TECH") wired to
a robot UART as a transparent bridge - currently BallBot/T4-IMU.

A transport delivers received bytes via the callback set with set_on_bytes();
the callback runs on a background thread.
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable


class TransportError(Exception):
    """The link died underneath us (cable unplugged, BLE dropped, port revoked)."""


class Transport(ABC):
    def __init__(self) -> None:
        self._on_bytes: Callable[[bytes], None] | None = None
        self._on_lost: Callable[[str], None] | None = None
        self._failed = False          # set once the link is known-dead

    def set_on_bytes(self, cb: Callable[[bytes], None]) -> None:
        self._on_bytes = cb

    def set_on_lost(self, cb: Callable[[str], None] | None) -> None:
        """Called once, from a background thread, when the link dies on its own.

        Without this the app cannot tell "idle" from "unplugged": pyserial's
        is_open stays True after the device vanishes, and a dead reader thread
        is silent.
        """
        self._on_lost = cb

    def _emit(self, data: bytes) -> None:
        if self._on_bytes and data:
            self._on_bytes(data)

    def _fail(self, reason: str) -> None:
        """Mark the link dead and notify once (idempotent)."""
        if self._failed:
            return
        self._failed = True
        if self._on_lost:
            try:
                self._on_lost(reason)
            except Exception:  # noqa: BLE001 - never let a callback mask the failure
                pass

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

        self._failed = False
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
            except Exception as e:  # noqa: BLE001 - any serial error here means the port is gone
                # Only a failure if we did not ask it to stop; close() sets
                # _running False first, and the resulting read error is expected.
                if self._running:
                    self._fail(f"serial read failed: {e}")
                break
            if data:
                self._emit(data)

    def write(self, data: bytes) -> None:
        if self._failed:
            raise TransportError("serial link is down")
        if self._ser is None:
            raise TransportError("serial port not open")
        try:
            self._ser.write(data)
        except Exception as e:  # noqa: BLE001 - unplug surfaces here as PermissionError/OSError
            self._fail(f"serial write failed: {e}")
            raise TransportError(f"serial write failed: {e}") from e

    def close(self) -> None:
        """Idempotent and never raises - teardown must work on a dead port too."""
        self._running = False
        if self._thread is not None:
            # Never join ourselves: close() can be reached from the reader thread
            # via a failure path, and joining the current thread raises.
            if self._thread is not threading.current_thread():
                self._thread.join(timeout=1.0)
            self._thread = None
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001 - closing a vanished port can throw
                pass
            finally:
                self._ser = None

    @property
    def is_open(self) -> bool:
        # pyserial's is_open only means "opened and not closed" - it stays True
        # after the device is unplugged - so the failure flag is what matters.
        return (not self._failed) and self._ser is not None and self._ser.is_open

    def describe(self) -> str:
        return f"USB {self.port} @ {self.baud}"

    @staticmethod
    def list_ports() -> list[tuple[str, str]]:
        from serial.tools import list_ports

        return [(p.device, p.description) for p in list_ports.comports()]


# --------------------------------------------------------------------------- #
# BLE (HM-10 / "DSD TECH"). Runs a private asyncio loop on its own thread and
# bridges it to the sync Transport API, so callers never see asyncio.
#
# Restored in v0.2.0 from the retired BallBot-only client (git history of the
# BallBot repo, commit daf6f83: T4-IMU/tools/ballbot_gui/transport.py), where it
# was hardware-verified over BLE incl. while the robot was actively balancing.
# --------------------------------------------------------------------------- #
HM10_CHAR = "0000ffe1-0000-1000-8000-00805f9b34fb"
HM10_DEFAULT_NAME = "DSD TECH"


class BleTransport(Transport):
    @staticmethod
    def scan(timeout: float = 8.0) -> list[tuple[str, str]]:
        """Synchronous BLE scan -> [(address, name), ...]. Safe from a worker thread."""
        import asyncio

        from bleak import BleakScanner

        async def _scan() -> list[tuple[str, str]]:
            devices = await BleakScanner.discover(timeout=timeout)
            return [(d.address, d.name or "") for d in devices]

        return asyncio.run(_scan())

    def __init__(self, name: str = HM10_DEFAULT_NAME, address: str = "") -> None:
        super().__init__()
        self.name = name
        self.address = address
        self._loop = None
        self._thread: threading.Thread | None = None
        self._client = None
        self._char = None
        self._ready = threading.Event()
        self._err: Exception | None = None

    def open(self) -> None:
        import asyncio

        self._failed = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=20.0):
            raise TimeoutError("BLE connect timed out")
        if self._err:
            raise self._err

    def _run_loop(self) -> None:
        import asyncio

        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect())
            self._loop.run_forever()
        except BaseException as e:  # noqa: BLE001 - asyncio.CancelledError is a BaseException
            # Must always set _ready here, or open()'s wait() blocks the full 20 s
            # timeout instead of surfacing the real error immediately.
            self._err = e
            self._ready.set()

    async def _connect(self) -> None:
        from bleak import BleakClient, BleakScanner

        if self.address:
            dev = await BleakScanner.find_device_by_address(self.address, timeout=15.0)
        else:
            dev = await BleakScanner.find_device_by_filter(
                lambda d, ad: (d.name or "").strip().lower() == self.name.lower(),
                timeout=15.0,
            )
        if dev is None:
            raise RuntimeError("BLE device not found (powered? in range?)")

        def on_disconnect(_c) -> None:
            self._fail("BLE link dropped")

        self._client = BleakClient(dev, disconnected_callback=on_disconnect)
        await self._client.connect()
        self._char = HM10_CHAR

        def on_notify(_h, data: bytearray) -> None:
            self._emit(bytes(data))

        await self._client.start_notify(self._char, on_notify)
        self._ready.set()

    def write(self, data: bytes) -> None:
        import asyncio

        if self._failed:
            raise TransportError("BLE link is down")
        if self._loop is None or self._client is None:
            raise TransportError("BLE not connected")

        async def _w() -> None:
            for i in range(0, len(data), 20):  # HM-10 MTU
                await self._client.write_gatt_char(self._char, data[i:i + 20], response=False)

        fut = asyncio.run_coroutine_threadsafe(_w(), self._loop)

        def _check(f) -> None:
            exc = f.exception()
            if exc is not None:
                self._fail(f"BLE write failed: {exc}")

        fut.add_done_callback(_check)

    def close(self) -> None:
        import asyncio

        if self._loop is None:
            return

        async def _close() -> None:
            try:
                if self._client is not None:
                    await self._client.disconnect()
            except Exception:
                pass

        try:
            fut = asyncio.run_coroutine_threadsafe(_close(), self._loop)
            fut.result(timeout=5.0)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._client = None

    @property
    def is_open(self) -> bool:
        return ((not self._failed) and self._client is not None
                and getattr(self._client, "is_connected", False))

    def describe(self) -> str:
        return f"BLE {self.address or self.name}"
