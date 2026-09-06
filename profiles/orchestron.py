"""Orchestron profile.

See d:\\OneDrive\\Projects\\Electronics\\Teensy4VocalizerV3\\Software\\Orchestron\\src\\CommandProtocol.cpp
for the firmware side of this telemetry layout.
"""
from __future__ import annotations

NAME = "Orchestron"

# "stormtrooper" (voice FX) doesn't appear in RX-80B or T4-IMU's ConfigParams.def.
SIGNATURE_PREFIXES = {"stormtrooper"}

SCALE: dict[str, int] = {
    "voice.fixedGain": 100,
    "voice.inputGain": 100,
    "stormtrooper.bassGain": 100,
    "stormtrooper.trebleGain": 100,
    "stormtrooper.resonance": 100,
    "noise.overSubtraction": 100,
}

TELEMETRY_GROUPS: list[tuple[str, int, list[str]]] = [
    ("servo",    0x01, [f"s{i}" for i in range(1, 9)]),  # matches board labels S1-S8
    ("servoTgt", 0x02, []),                              # RESERVED - not yet emitted
    ("sbus",     0x04, [f"ch{i}" for i in range(12)]),
    ("status",   0x08, ["sbusOk", "sdOk", "masterVol"]),
    ("pin",      0x10, [f"io{i}" for i in range(1, 9)]),  # RC PWM pulse width (us) per IO
    ("seq",      0x20, []),                              # RESERVED
    ("rec",      0x40, []),                              # RESERVED
    ("diag",     0x80, ["freeRam"]),
]
TELEMETRY_MASK_IMPLEMENTED = 0x01 | 0x04 | 0x08 | 0x10 | 0x80

MODE_NAMES: dict[int, str] = {0: "IDLE", 1: "MANUAL", 2: "CONTROL", 3: "AUTO"}
