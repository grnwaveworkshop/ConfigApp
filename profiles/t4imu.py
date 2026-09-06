"""T4-IMU (BallBot) profile.

See d:\\OneDrive\\Projects\\BallBot\\Software\\BallBot\\T4-IMU\\src\\CommandProtocol.cpp
for the firmware side of this telemetry layout.
"""
from __future__ import annotations

NAME = "T4-IMU"

# Balance-bot-specific concepts that don't appear in RX-80B/Orchestron's
# ConfigParams.def.
SIGNATURE_PREFIXES = {"balance", "velocity", "turn", "kin", "phys", "cal", "imu", "move"}

SCALE: dict[str, int] = {
    "velocity.filterAlpha": 1000,
    "velocity.tiltCouple": 1000,
    "cal.offsetX": 100,
    "cal.offsetY": 100,
    "kin.L": 100,
    "safety.batteryMin": 10,
    "phys.rBall": 1000,
    "phys.rWheel": 1000,
    "phys.gearRatio": 1000,
    "phys.fkScale": 1000,
}

TELEMETRY_GROUPS: list[tuple[str, int, list[str]]] = [
    ("attitude", 0x01, ["roll", "pitch", "yaw"]),
    ("gyro",     0x02, ["gyroX", "gyroY", "gyroZ"]),
    ("bal",      0x04, ["balX", "balY", "vCorrX", "vCorrY"]),
    ("cmd",      0x08, ["cmdVx", "cmdVy", "cmdVz", "fVx", "fVy"]),
    ("pwm",      0x10, ["pwmA", "pwmB", "pwmC", "pwmD"]),
    ("enc",      0x20, ["encD", "encB", "encC"]),
    ("diag",     0x40, ["imuAgeMs", "dL", "dB", "dV"]),
    ("sbus",     0x80, ["sbus1", "sbus2", "sbus3", "sbus4", "sbus5", "sbus6"]),
]
TELEMETRY_MASK_IMPLEMENTED = 0xFF

MODE_NAMES: dict[int, str] = {0: "IDLE", 1: "MANUAL", 2: "CONTROL", 3: "AUTO"}
