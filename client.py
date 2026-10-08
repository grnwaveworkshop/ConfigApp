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

import zlib
from collections.abc import Callable

import models
import profiles
from models import ParamInfo, ProfilerStatus, ProfileSection, TelemetryFrame, TextInfo
from protocol import CATEGORY, Protocol, ProtocolError, split_frame
from transport import Transport

_ERR = CATEGORY + "E"

# <KE0,code> codes for the file / events commands (Orchestron CommandProtocol.hpp)
FILE_ERRORS = {
    1: "this firmware has no file commands",
    3: "the robot doesn't allow that file",
    4: "the SD card is busy (no card, MTP mode, or a recording is running)",
    5: "the upload was refused (too big, or it arrived damaged)",
    6: "writing the SD card failed (the old file was kept)",
}
WRITE_CHUNK = 28        # bytes per <KFW,hex>: the firmware reads at most 63 characters a frame


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

    # -- text keys (BallBot 0.7.9+) ------------------------------------------- #
    def texts(self) -> list[TextInfo]:
        """<KX> + text(id) for each: every text key with its choices. [] on firmware without them
        (it answers <KX> with an error frame)."""
        tag, args = split_frame(self.proto.request(f"{CATEGORY}X"))
        if tag != CATEGORY + "X" or not args:
            return []
        return [self.text(i) for i in range(int(args[0]))]

    def text(self, text_id: int) -> TextInfo:
        """<KX##> (the robot rescans the key's choices, e.g. the policy files on its SD card), then
        <KXO##,i> for each choice."""
        tag, args = split_frame(self.proto.request(f"{CATEGORY}X{text_id}"))
        if tag != f"{CATEGORY}X{text_id}" or len(args) < 4:
            raise ProtocolError(f"bad <KX{text_id}> reply: {tag},{args}")
        # KX##,key,value,choices,status,description - the description may itself contain commas
        info = TextInfo(text_id, args[0], args[1], status=args[3], description=",".join(args[4:]).strip())
        for i in range(int(args[2])):
            t, a = split_frame(self.proto.request(f"{CATEGORY}XO{text_id},{i}"))
            if t == f"{CATEGORY}XO{text_id}" and len(a) >= 2:
                info.choices.append(a[1])
        return info

    def set_text(self, text_id: int, value: str) -> tuple[bool, str, str]:
        """<KXS##,value> - set a text key (live, RAM only; save() writes it). Returns (applied,
        value, status): applied is False when the robot stored it but could not apply it yet, e.g.
        a policy that loads at the disarm or a file it refused; the status says which."""
        value = value.strip()
        if any(c in value for c in "<>,"):
            raise ProtocolError("a text value can't contain < > or ,")
        tag, args = split_frame(self.proto.request(f"{CATEGORY}XS{text_id},{value}", timeout=3.0))
        if tag.startswith(_ERR):
            raise ProtocolError(f"{value!r} refused (letters, digits, - _ . only, at most 31)")
        if tag != f"{CATEGORY}XS{text_id}" or len(args) < 2:
            raise ProtocolError(f"bad <KXS{text_id}> reply: {tag},{args}")
        return args[0] == "1", args[1], ",".join(args[2:]).strip()

    # -- events.ini editor (Orchestron 2.31.0+) ------------------------------- #
    def _file_request(self, body: str, timeout: float = 1.5) -> list[str]:
        tag, args = split_frame(self.proto.request(body, timeout=timeout))
        if tag.startswith(_ERR):
            code = int(args[0]) if args and args[0].isdigit() else 0
            raise ProtocolError(FILE_ERRORS.get(code, f"error {code}"))
        return [tag] + args

    def inputs(self) -> tuple[bool, int, list[int]]:
        """<KI> - (link up, pad button held 0-14, channels 1-24 in microseconds)."""
        reply = self._file_request(f"{CATEGORY}I")
        if reply[0] != CATEGORY + "I" or len(reply) < 3:
            raise ProtocolError(f"bad <KI> reply: {reply}")
        return reply[1] == "1", int(reply[2]), [int(v) for v in reply[3:]]

    def wav_files(self) -> list[str]:
        """<KFL> + <KFL##> - the WAV files on the SD card (for wavA: / wavB: actions)."""
        reply = self._file_request(f"{CATEGORY}FL")
        out = []
        for i in range(int(reply[1])):
            r = self._file_request(f"{CATEGORY}FL{i}")
            out.append(",".join(r[1:]))
        return out

    def read_file(self, name: str, progress: Callable[[int, int], None] | None = None) -> bytes | None:
        """<KFR> in 96-byte chunks. None if the file doesn't exist on the card."""
        data = bytearray()
        while True:
            r = self._file_request(f"{CATEGORY}FR,{name},{len(data)}")
            total = int(r[2])
            if total < 0:
                return None
            chunk = bytes.fromhex(r[3]) if len(r) > 3 else b""
            data += chunk
            if progress:
                progress(len(data), total)
            if len(data) >= total or not chunk:
                return bytes(data[:total])

    def write_file(self, name: str, data: bytes, progress: Callable[[int, int], None] | None = None) -> None:
        """<KFO> / <KFW>... / <KFC>: upload a file. The robot keeps it in RAM until all of it has
        arrived with the right CRC, then writes it (keeping the old one as NAME.bak). Raises
        ProtocolError if anything goes wrong - the file on the card is then unchanged."""
        self._file_request(f"{CATEGORY}FO,{name},{len(data)}")
        try:
            for i in range(0, len(data), WRITE_CHUNK):
                self._file_request(f"{CATEGORY}FW,{data[i:i + WRITE_CHUNK].hex()}")
                if progress:
                    progress(min(i + WRITE_CHUNK, len(data)), len(data))
            self._file_request(f"{CATEGORY}FC,{zlib.crc32(data):08x}", timeout=5.0)
        except ProtocolError:
            try:
                self.proto.request(f"{CATEGORY}FX")
            except ProtocolError:
                pass
            raise

    def events_report(self, reload: bool = False) -> tuple[int, list[tuple[int, str]]]:
        """<KU> (<KUR> reloads events.ini first) + <KU##>: (rules loaded, [(line, problem)]).
        Line 0 means the problem isn't one line (no SD card, a sequence that didn't load)."""
        r = self._file_request(f"{CATEGORY}U{'R' if reload else ''}", timeout=5.0 if reload else 1.5)
        rules, count = int(r[1]), int(r[2])
        problems = []
        for i in range(min(count, 16)):
            p = self._file_request(f"{CATEGORY}U{i}")
            problems.append((int(p[1]), ",".join(p[2:]).strip()))
        if count > 16:
            problems.append((0, f"... and {count - 16} more (see the robot's console)"))
        return rules, problems

    def rule_notices(self, on: bool) -> None:
        """<KV1> / <KV0> - the robot sends <KV,line> each time an events.ini rule fires."""
        self._file_request(f"{CATEGORY}V{1 if on else 0}")

    def set_notice_handler(self, cb: Callable[[int], None] | None) -> None:
        """cb(line) for each rule-fired notice; runs on the transport's reader thread."""
        if cb is None:
            self.proto.set_notice_handler(None)
            return

        def _wrap(frame: str) -> None:
            _tag, args = split_frame(frame)
            if args and args[0].isdigit():
                cb(int(args[0]))

        self.proto.set_notice_handler(_wrap)

    # -- loop profiler (Orchestron 2.34+) ------------------------------------ #
    def profiler(self, command: str = "") -> ProfilerStatus | None:
        """<KQ>: the profiler's state. command "R" resets it first, "E1" / "E0" switches it
        on / off first. None when the firmware has no profiler command."""
        tag, args = split_frame(self.proto.request(f"{CATEGORY}Q{command}"))
        if tag != CATEGORY + "Q" or len(args) < 3:
            return None
        nums = [float(a) for a in args[3:8]] + [0.0] * (5 - len(args[3:8]))
        return ProfilerStatus(int(args[0]), args[1] == "1", args[2] == "1", nums[0], nums[1],
                              int(nums[2]), int(nums[3]), int(nums[4]))

    def profiler_section(self, section: int) -> ProfileSection:
        """<KQ##>: one section's statistics."""
        tag, args = split_frame(self.proto.request(f"{CATEGORY}Q{section}"))
        if tag != f"{CATEGORY}Q{section}" or len(args) < 8:
            raise ProtocolError(f"bad <KQ{section}> reply: {tag},{args}")
        return ProfileSection(section, args[0], *(int(a) for a in args[1:8]))

    def profiler_sections(self, status: ProfilerStatus) -> list[ProfileSection]:
        """Every section, in the robot's order."""
        return [self.profiler_section(i) for i in range(status.sections)]

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
