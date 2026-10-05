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

import models
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
        # KN##,min,max,val,key[,scale] - scale is optional (older firmware omits it)
        vmin, vmax, val = int(args[0]), int(args[1]), int(args[2])
        key = args[3]
        fw_scale = None
        if len(args) > 4:
            try:
                fw_scale = max(1, int(args[4]))
            except ValueError:
                pass
        return ParamInfo(id=key_id, key=key, vmin=vmin, vmax=vmax, value=val,
                         fw_scale=fw_scale)

    def describe_text(self, key_id: int) -> str | None:
        """<KD##> - the key's one-line description, for a tooltip (BallBot T4-IMU v0.6.94+).
        None when the firmware doesn't support it: it answers an unknown sub-command with an
        error frame, so the caller can stop asking after the first try."""
        tag, args = split_frame(self.proto.request(f"{CATEGORY}D{key_id}"))
        if not tag.startswith(CATEGORY + "D"):
            return None
        text = ",".join(args).strip()   # the description may itself contain commas
        return text or None

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

    # -- MTP mode ------------------------------------------------------------ #
    def mtp_status(self) -> bool:
        """<KM> - query whether MTP/mass-storage mode is currently active."""
        tag, args = split_frame(self.proto.request(f"{CATEGORY}M"))
        if tag.startswith(_ERR):
            raise ProtocolError(f"mtp_status failed: {tag},{args}")
        return bool(int(args[0]))

    def mtp_enter(self) -> bool:
        """<KM1> - enter MTP mode (SD card exposed to the host via USB)."""
        tag, args = split_frame(self.proto.request(f"{CATEGORY}M1"))
        if tag.startswith(_ERR):
            raise ProtocolError(f"mtp_enter failed: {tag},{args}")
        return bool(int(args[0]))

    def mtp_exit(self) -> bool:
        """<KM0> - exit MTP mode, resume normal operation."""
        tag, args = split_frame(self.proto.request(f"{CATEGORY}M0"))
        if tag.startswith(_ERR):
            raise ProtocolError(f"mtp_exit failed: {tag},{args}")
        return bool(int(args[0]))

    # -- actions (Orchestron 2.27.2+) ------------------------------------------ #
    def actions(self) -> list[tuple[str, str, str]]:
        """<KA> + <KA##> sweep - the firmware's action catalogue as (group, label, command).
        Empty when the firmware has no <KA> (it answers with an error frame)."""
        tag, args = split_frame(self.proto.request(f"{CATEGORY}A"))
        if tag != CATEGORY + "A" or not args:
            return []
        out: list[tuple[str, str, str]] = []
        for i in range(int(args[0])):
            tag, args = split_frame(self.proto.request(f"{CATEGORY}A{i}"))
            if tag.startswith(_ERR) or len(args) < 3:
                continue
            out.append((args[0], args[1], args[2]))
        return out

    def run_action(self, command: str) -> tuple[bool, str]:
        """<KA,command> - run one action, in the robot's buttons.ini vocabulary
        ("rec:toggle", "seq:wave", "mode:control", "stop", "wavA:12" ...).
        Returns (ok, message), e.g. (True, "REC 0:00 / 10:00")."""
        command = command.strip()
        if not command or any(c in command for c in "<>"):
            raise ProtocolError("an action can't be empty or contain < >")
        tag, args = split_frame(self.proto.request(f"{CATEGORY}A,{command}", timeout=3.0))
        if tag != CATEGORY + "A" or not args:
            raise ProtocolError(f"action not supported by this firmware: {tag},{args}")
        return args[0] == "1", ",".join(args[1:]).strip()

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
        models.FW_SCALE.clear()
        models.FW_SCALE.update({k: p.fw_scale for k, p in params.items() if p.fw_scale})
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
