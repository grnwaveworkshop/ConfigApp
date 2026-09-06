"""Generic robot client over the <K...> protocol - works against RX-80B,
Orchestron, or T4-IMU without knowing which in advance.

    bot = RobotClient(SerialTransport("COM5"))
    bot.open(); bot.ping()
    bot.refresh_params()               # {key: ParamInfo} via <KC> + <KN##> sweep,
                                        # also auto-detects the robot profile
    bot.set("hero.elbowHome", 95)      # live - takes effect immediately, no reboot
    bot.save()                         # <KW> -> config.ini on SD

See profiles/__init__.py: refresh_params() sets `profiles.active` based on
which key prefixes are present, so models.py decodes telemetry/scale correctly
without the caller having to say which robot this is.
"""
from __future__ import annotations

from collections.abc import Callable

import profiles
from models import ParamInfo, TelemetryFrame
from protocol import CATEGORY, Protocol, ProtocolError, split_frame
from transport import Transport

_ERR = CATEGORY + "E"


class RobotClient:
    def __init__(self, transport: Transport) -> None:
        self.t = transport
        self.proto = Protocol(transport)
        self.params: dict[str, ParamInfo] = {}
        self.params_by_id: dict[int, ParamInfo] = {}

    # -- lifecycle ---------------------------------------------------------- #
    def open(self) -> None:
        self.t.open()

    def close(self) -> None:
        self.t.close()

    @property
    def is_open(self) -> bool:
        return self.t.is_open

    # -- primitives --------------------------------------------------------- #
    def ping(self) -> int:
        """Returns the firmware version as a compact int: 2.7.0 -> 20700."""
        tag, args = split_frame(self.proto.request(f"{CATEGORY}P"))
        if not tag.startswith(CATEGORY + "P") or not args:
            raise ProtocolError(f"bad ping reply: {tag},{args}")
        return int(args[0])

    def count(self) -> int:
        tag, args = split_frame(self.proto.request(f"{CATEGORY}C"))
        if not tag.startswith(CATEGORY + "C") or not args:
            raise ProtocolError(f"bad count reply: {tag},{args}")
        return int(args[0])

    def describe(self, key_id: int) -> ParamInfo:
        tag, args = split_frame(self.proto.request(f"{CATEGORY}N{key_id}"))
        if tag.startswith(_ERR):
            raise ProtocolError(f"describe {key_id} failed: {tag},{args}")
        # KN##,min,max,val,key
        vmin, vmax, val = int(args[0]), int(args[1]), int(args[2])
        key = args[3]
        return ParamInfo(id=key_id, key=key, vmin=vmin, vmax=vmax, value=val)

    def get(self, key_or_id: str | int) -> int:
        kid = self._to_id(key_or_id)
        tag, args = split_frame(self.proto.request(f"{CATEGORY}G{kid}"))
        if tag.startswith(_ERR):
            raise ProtocolError(f"get {key_or_id} failed: {tag},{args}")
        val = int(args[0])
        if kid in self.params_by_id:
            self.params_by_id[kid].value = val
        return val

    def set(self, key_or_id: str | int, value: int) -> int:
        kid = self._to_id(key_or_id)
        tag, args = split_frame(self.proto.request(f"{CATEGORY}S{kid},{int(value)}"))
        if tag.startswith(_ERR):
            code = args[0] if args else "?"
            raise ProtocolError(f"set {key_or_id}={value} rejected (code {code})")
        val = int(args[0])
        if kid in self.params_by_id:
            self.params_by_id[kid].value = val
        return val

    def save(self) -> bool:
        """<KW> - write config.ini to the SD card (the config of record)."""
        tag, args = split_frame(self.proto.request(f"{CATEGORY}W"))
        return tag.startswith(CATEGORY + "W") and bool(int(args[0]))

    def reload(self) -> dict[str, ParamInfo]:
        """<KL> - discard RAM edits, re-read config.ini from SD."""
        self.proto.request(f"{CATEGORY}L")
        return self.refresh_params()

    # -- param map ---------------------------------------------------------- #
    def refresh_params(self) -> dict[str, ParamInfo]:
        """Sweep <KN0..N-1> and auto-detect the robot profile from the key
        prefixes seen (see profiles.detect()). Can take a moment on a slow
        link with ~200+ keys - the app should show progress rather than
        appear hung."""
        n = self.count()
        params: dict[str, ParamInfo] = {}
        by_id: dict[int, ParamInfo] = {}
        for i in range(n):
            try:
                info = self.describe(i)
            except ProtocolError:
                continue
            params[info.key] = info
            by_id[i] = info
        self.params = params
        self.params_by_id = by_id
        profiles.active = profiles.detect({p.group for p in params.values()})
        return params

    # -- telemetry ---------------------------------------------------------- #
    def snapshot(self) -> TelemetryFrame:
        """<KT> - one frame, all implemented groups."""
        _tag, args = split_frame(self.proto.request(f"{CATEGORY}T"))
        return TelemetryFrame.parse(args)

    def stream(self, hz: int, mask: int | None = None) -> None:
        """Start/stop the telemetry stream. `mask` selects which field groups to
        include (profiles.active.TELEMETRY_GROUPS); omit to leave the
        firmware's current mask unchanged."""
        self.proto.send(f"{CATEGORY}R{hz}" if mask is None else f"{CATEGORY}R{hz},{mask}")

    def set_telemetry_handler(self, cb: Callable[[TelemetryFrame], None] | None) -> None:
        if cb is None:
            self.proto.set_telemetry_handler(None)
            return

        def _wrap(frame: str) -> None:
            _tag, args = split_frame(frame)
            cb(TelemetryFrame.parse(args))

        self.proto.set_telemetry_handler(_wrap)

    # -- helpers ------------------------------------------------------------ #
    def _to_id(self, key_or_id: str | int) -> int:
        if isinstance(key_or_id, int):
            return key_or_id
        info = self.params.get(key_or_id)
        if info is None:
            raise ProtocolError(f"unknown key '{key_or_id}' (refresh_params first?)")
        return info.id
