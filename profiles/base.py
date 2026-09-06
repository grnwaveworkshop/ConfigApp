"""Fallback profile for an unrecognized robot.

Config editing always works (ConfigManager keys are read generically), but
without a known telemetry layout the Dashboard tab has nothing to decode, so
TELEMETRY_GROUPS is empty and streaming is left off.
"""
from __future__ import annotations

NAME = "Unknown"
SIGNATURE_PREFIXES: set[str] = set()

SCALE: dict[str, int] = {}

TELEMETRY_GROUPS: list[tuple[str, int, list[str]]] = []
TELEMETRY_MASK_IMPLEMENTED = 0

# OPMODE_IDLE/MANUAL/CONTROL/AUTO - the one convention shared by every robot
# in this family so far.
MODE_NAMES: dict[int, str] = {0: "IDLE", 1: "MANUAL", 2: "CONTROL", 3: "AUTO"}
