"""Robot profiles: per-project telemetry layout + scale table + signature keys.

The wire protocol (<K...> framing, ConfigManager get/set/describe) is identical
across every robot in this family, and the config-editing side of the app is
100% generic (pages are derived from key prefixes at runtime). The only thing
that actually differs per robot is:
  - which config keys use a display scale (SCALE)
  - the telemetry field layout baked into each firmware's EmitTelemetry()
    (TELEMETRY_GROUPS / TELEMETRY_MASK_IMPLEMENTED)
  - operating mode names (MODE_NAMES) - same 0..3 IDLE/MANUAL/CONTROL/AUTO
    convention so far, but kept per-profile in case a robot diverges

`detect()` picks a profile after a param sweep by matching each profile's
SIGNATURE_PREFIXES against the key prefixes actually present - no firmware
change needed to identify which robot is connected.
"""
from __future__ import annotations

from . import base, orchestron, rx80b, t4imu

ALL = [rx80b, orchestron, t4imu]

# Current profile. Reassigned by client.py after a param sweep; models.py reads
# this at parse time so ParamInfo/TelemetryFrame don't need a profile threaded
# through every call.
active = base


def detect(prefixes: set[str]):
    """Pick the profile whose signature prefixes intersect `prefixes` (the
    top-level key prefixes seen in a param sweep). Falls back to `base`
    (config-only, no telemetry decoding) if nothing matches."""
    for prof in ALL:
        if prof.SIGNATURE_PREFIXES & prefixes:
            return prof
    return base
