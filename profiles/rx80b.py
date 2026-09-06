"""RX-80B (DJ-R3X) profile.

See d:\\OneDrive\\Projects\\DJ-R3X\\Software\\RX-80B\\src\\CommandProtocol.cpp
for the firmware side of this telemetry layout.
"""
from __future__ import annotations

NAME = "RX-80B"

# "hero" (HeroArm elbow/wrist) and "lift" (lifter) don't appear in the
# Orchestron or BallBot ConfigParams.def - unique to this robot.
SIGNATURE_PREFIXES = {"hero", "lift"}

SCALE: dict[str, int] = {
    "m.endstopAngle": 10,
    "m.endstopCurrent": 100,
    "lift.motion.maxVel": 10,
    "lift.motion.gravComp": 1000,
    "lift.motion.fricComp": 1000,
    "lift.vel.kp": 1000,
    "lift.pos.kp": 1000,
}

TELEMETRY_GROUPS: list[tuple[str, int, list[str]]] = [
    ("servo",  0x01, ["elbow", "wrist"]),
    ("sbus",   0x04, [f"ch{i}" for i in range(12)]),
    ("status", 0x08, ["sbusOk", "sdOk"]),
    ("diag",   0x80, ["freeRam"]),
]
TELEMETRY_MASK_IMPLEMENTED = 0x01 | 0x04 | 0x08 | 0x80

MODE_NAMES: dict[int, str] = {0: "IDLE", 1: "MANUAL", 2: "CONTROL", 3: "AUTO"}
