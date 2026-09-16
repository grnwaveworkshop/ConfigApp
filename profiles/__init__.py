"""Robot profiles: per-project telemetry layout + scale table + signature keys.

The wire protocol (<K...> framing, ConfigManager get/set/describe) is identical
across every robot in this family, and the config-editing side of the app is
100% generic (pages are derived from key prefixes at runtime). The only thing
that actually differs per robot is:
  - which config keys use a display scale (SCALE)
  - the telemetry field layout baked into each firmware's EmitTelemetry()
    (TELEMETRY_GROUPS / TELEMETRY_MASK_IMPLEMENTED)
  - operating mode names (MODE_NAMES) - the 0..3 IDLE/MANUAL/CONTROL/AUTO
    convention is only a default; T4-IMU genuinely diverges (0=SAFE 1=LOOP-ON
    2=ARMED 3=E-STOP 4=TILT), which is exactly why this is per-profile
  - an OPTIONAL richer Dashboard layout (DASHBOARD). Without it a profile gets
    the generic flat "key: value" grid, which is still the right answer for a
    Droid nobody has curated yet. With it you get titled panels and rolling
    plots:
        DASHBOARD = {
          "state_labels": {code: (label, (r, g, b)), ...},   # optional
          "panels": [(title, [(key, label, "{:+8.2f}"), ...]), ...],
          "plots":  [(title, y_label, [(key, series_label), ...]), ...],
        }
    Panels and plot series whose key is not in the active telemetry mask are
    dropped automatically, so the spec never has to track the group checkboxes

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
