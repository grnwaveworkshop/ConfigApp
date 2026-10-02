"""T4-IMU (BallBot) profile.

See d:\\OneDrive\\Projects\\BallBot\\Software\\BallBot\\T4-IMU\\src\\CommandProtocol.cpp
for the firmware side of this telemetry layout.
"""
from __future__ import annotations

NAME = "T4-IMU"

# Balance-bot-specific concepts that don't appear in RX-80B/Orchestron's
# ConfigParams.def. Firmware v0.6.94 regrouped its keys (ctrl.*, est.*, cal.*, drive.*, diag.*);
# the old prefixes stay for older firmware.
SIGNATURE_PREFIXES = {"balance", "velocity", "turn", "kin", "phys", "cal", "imu", "move",
                      "ctrl", "est", "drive", "diag"}

# Fallback ONLY for firmware older than v0.6.83. Since v0.6.83 each key's scale is a column
# in the firmware's ConfigParams.def and arrives in the <KN> descriptor, which wins over this
# table - so new or changed keys need no entry here. Frozen at the v0.6.82 set; do not extend.
SCALE: dict[str, int] = {
    "velocity.filterAlpha": 1000,
    "velocity.tiltCouple": 1000,
    "velocity.brake": 100,
    "velocity.leanMax": 10,
    "turn.slipRatio": 100,
    "cal.offsetX": 100,
    "cal.offsetY": 100,
    "kin.L": 100,
    "safety.batteryMin": 10,
    "phys.rBall": 1000,
    "phys.rWheel": 1000,
    "phys.gearRatio": 1000,
    "phys.fkScale": 1000,
    "est.alpha": 1000,
    "lqr.kxPhi": 10, "lqr.kxTheta": 10, "lqr.kxPhiDot": 10, "lqr.kxThetaDot": 10,
    "lqr.kyPhi": 10, "lqr.kyTheta": 10, "lqr.kyPhiDot": 10, "lqr.kyThetaDot": 10,
    "lqr.outScale": 1000,
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

# T4-IMU does NOT use the family's 0..3 IDLE/MANUAL/CONTROL/AUTO convention. Its
# telemetry "state" field is BtStateCode() in the firmware's CommandProtocol.cpp:
# 0=SAFE 1=LOOP-ON 2=ARMED 3=E-STOP 4=TILT. Labelling code 3 "AUTO" would show an
# emergency stop as a normal running mode, so MODE_NAMES is overridden here.
MODE_NAMES: dict[int, str] = {0: "SAFE", 1: "LOOP-ON", 2: "ARMED", 3: "E-STOP", 4: "TILT"}

# ---------------------------------------------------------------------------
# Optional richer Dashboard (see profiles/__init__.py). Without this a profile
# gets the generic flat "key: value" grid; with it, fields are grouped into
# titled panels and plotted. Panels/series whose telemetry group is unchecked
# are dropped automatically, so this never has to agree with the group mask.
# ---------------------------------------------------------------------------
DASHBOARD: dict = {
    # state code -> (label, RGB). Colour carries the warning, not just the word.
    "state_labels": {
        0: ("SAFE",    (140, 200, 140)),
        1: ("LOOP-ON", (210, 210, 130)),
        2: ("ARMED",   (120, 230, 120)),
        3: ("E-STOP",  (235,  90,  90)),
        4: ("TILT!",   (235, 160,  80)),
    },
    # (title, [(telemetry key, label, format)])
    "panels": [
        ("Attitude (deg)", [
            ("roll", "Roll", "{:+8.2f}"), ("pitch", "Pitch", "{:+8.2f}"),
            ("yaw", "Yaw", "{:8.1f}")]),
        ("Rates (deg/s)", [
            ("gyroX", "Roll rate", "{:+8.1f}"), ("gyroY", "Pitch rate", "{:+8.1f}"),
            ("gyroZ", "Yaw rate", "{:+8.1f}")]),
        ("Velocity - command vs actual (~mm/s)", [
            ("cmdVx", "cmd Vx", "{:+8.0f}"), ("fVx", "actual Vx", "{:+8.1f}"),
            ("cmdVy", "cmd Vy", "{:+8.0f}"), ("fVy", "actual Vy", "{:+8.1f}"),
            ("cmdVz", "cmd Vz", "{:+8.0f}")]),
        ("Control output (kinematic units)", [
            ("balX", "balance X", "{:+9.0f}"), ("vCorrX", "velocity X", "{:+9.0f}"),
            ("balY", "balance Y", "{:+9.0f}"), ("vCorrY", "velocity Y", "{:+9.0f}")]),
        ("Motors (PWM)", [
            ("pwmD", "D rear", "{:+7.0f}"), ("pwmB", "B front-left", "{:+7.0f}"),
            ("pwmC", "C front-right", "{:+7.0f}"), ("pwmA", "A (n/c)", "{:+7.0f}")]),
        ("Encoders (counts/cycle)", [
            ("encD", "D rear", "{:+9.0f}"), ("encB", "B front-left", "{:+9.0f}"),
            ("encC", "C front-right", "{:+9.0f}")]),
        ("Loop health", [
            ("imuAgeMs", "IMU age (ms)", "{:6.0f}"), ("dL", "loop Hz", "{:6.0f}"),
            ("dB", "balance Hz", "{:6.0f}"), ("dV", "velocity Hz", "{:6.0f}")]),
        ("SBUS channels", [
            ("sbus1", "Ch1 X", "{:6.0f}"), ("sbus2", "Ch2 Y", "{:6.0f}"),
            ("sbus3", "Ch3", "{:6.0f}"), ("sbus4", "Ch4 arm", "{:6.0f}"),
            ("sbus5", "Ch5 rotL", "{:6.0f}"), ("sbus6", "Ch6 rotR", "{:6.0f}")]),
    ],
    # (title, y-axis label, [(key, series label)])
    "plots": [
        ("Attitude", "deg", [("roll", "roll"), ("pitch", "pitch")]),
        ("Velocity - command vs actual", "~mm/s", [
            ("cmdVx", "cmd Vx"), ("fVx", "actual Vx"),
            ("cmdVy", "cmd Vy"), ("fVy", "actual Vy")]),
        ("Control output - balance vs velocity", "kinematic", [
            ("balX", "balX"), ("vCorrX", "vCorrX"),
            ("balY", "balY"), ("vCorrY", "vCorrY")]),
    ],
}
