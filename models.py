"""Data models: param descriptors + telemetry, shared across the robot family.

Wire values are raw integers (each firmware's x10/x100/x1000 storage
conventions where they apply). Scale and telemetry field layout differ per
robot, so both are read from `profiles.active` (see profiles/__init__.py),
which client.py sets after a param sweep auto-detects which robot is connected.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import profiles


def scale_for_key(key: str) -> int:
    return profiles.active.SCALE.get(key, 1)


@dataclass
class ParamInfo:
    id: int
    key: str
    vmin: int
    vmax: int
    value: int

    @property
    def group(self) -> str:
        """Top-level key prefix - the only thing the UI needs to page params;
        see pages.py. Purely derived from the key string, so a new prefix in
        any project's ConfigParams.def shows up automatically with no app change."""
        return self.key.split(".")[0]

    @property
    def scale(self) -> int:
        return scale_for_key(self.key)

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
