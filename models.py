"""Data models: param descriptors + telemetry, shared across the robot family.

Wire values are raw integers (each firmware's x10/x100/x1000 storage
conventions where they apply). The display scale comes from the firmware itself
when its <KN> descriptor carries one (6th field, BallBot v0.6.83+); only older
firmware falls back to the per-profile SCALE table. Telemetry field layout is
read from `profiles.active` (see profiles/__init__.py), which client.py sets
after a param sweep auto-detects which robot is connected.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import profiles


# Scales reported by the firmware in the last param sweep, keyed by config key.
# Filled by client.refresh_params(); authoritative over profiles.active.SCALE.
FW_SCALE: dict[str, int] = {}


def scale_for_key(key: str) -> int:
    s = FW_SCALE.get(key)
    if s is not None:
        return s
    return profiles.active.SCALE.get(key, 1)   # legacy firmware without a scale field


@dataclass
class ParamInfo:
    id: int
    key: str
    vmin: int
    vmax: int
    value: int
    fw_scale: int | None = None     # from the firmware's <KN> descriptor, if it sends one

    @property
    def group(self) -> str:
        """Top-level key prefix - the only thing the UI needs to page params;
        see pages.py. Purely derived from the key string, so a new prefix in
        any project's ConfigParams.def shows up automatically with no app change."""
        return self.key.split(".")[0]

    @property
    def scale(self) -> int:
        return self.fw_scale if self.fw_scale else scale_for_key(self.key)

    def human(self, raw: int | None = None) -> float:
        v = self.value if raw is None else raw
        return v / self.scale


@dataclass
class TelemetryFrame:
    raw: dict[str, float] = field(default_factory=dict)

    @classmethod
    def parse(cls, args: list[str]) -> "TelemetryFrame":
        """Walk profiles.active.TELEMETRY_GROUPS in order, consuming
        len(fields) args per group whose bit is set in the mask (3rd field)."""
        def as_float(s: str) -> float:
            try:
                return float(s)
            except ValueError:
                return float("nan")

        d: dict[str, float] = {}
        if len(args) < 3:
            return cls(raw=d)
        d["ms"] = as_float(args[0])
        d["mode"] = as_float(args[1])
        mask = int(as_float(args[2]))
        d["mask"] = mask

        idx = 3
        for _name, bit, fields in profiles.active.TELEMETRY_GROUPS:
            if not (mask & bit):
                continue
            for f in fields:
                if idx < len(args):
                    d[f] = as_float(args[idx])
                idx += 1
        return cls(raw=d)

    def get(self, name: str, default: float = 0.0) -> float:
        return self.raw.get(name, default)

    @property
    def mode_name(self) -> str:
        return profiles.active.MODE_NAMES.get(int(self.raw.get("mode", -1)), "?")


@dataclass
class ProfilerStatus:
    """<KQ> (Orchestron 2.34+): the loop profiler and the audio interrupt's load."""
    sections: int
    enabled: bool
    auto_report: bool           # the robot prints a report on its console every 5 s
    audio_cpu: float = 0.0      # % of the CPU in the audio interrupt, now and the max
    audio_cpu_max: float = 0.0
    blocks: int = 0             # audio memory blocks in use, the max, and how many there are
    blocks_max: int = 0
    blocks_total: int = 0


@dataclass
class ProfileSection:
    """<KQ##>: one part of the robot's main loop. Times in microseconds; 0 = no data yet."""
    id: int
    name: str
    calls: int
    dur_min: int
    dur_avg: int
    dur_max: int
    period_min: int
    period_avg: int
    period_max: int
