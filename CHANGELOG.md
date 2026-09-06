# Changelog

All notable changes to this project will be documented in this file.

## [0.1.0] - 2026-09-06

Initial release. One shared config/telemetry app for the whole
Orchestron/RX-80B/T4-IMU robot family, replacing three near-identical
per-project copies (`orchestron_link`, `rx80b_link`, `ballbot_gui`).

### Added
- `<K...>` protocol client (`framing.py`, `transport.py`, `protocol.py`, `client.py`) - generic across all three robots.
- Dear PyGui desktop app (`app.py`): paginated Config tab (pages derived purely from key-namespace prefixes, no per-project curation), search box, Dashboard tab with live telemetry streaming.
- Headless CLI REPL (`cli.py`) for scripting/smoke tests.
- Robot auto-detection (`profiles/`): after the `<KC>`/`<KN##>` param sweep, matches discovered key prefixes against each robot's signature prefixes (`hero`/`lift` for RX-80B, `stormtrooper` for Orchestron, `balance`/`velocity`/`turn`/`kin`/`phys`/`cal`/`imu`/`move` for T4-IMU) to pick the correct telemetry field layout and scale table - no firmware changes required to identify which robot is connected.
- Explicit send/confirm status feedback: a slider/input edit shows "... sending key = value" immediately, then "\u2713 confirmed key = value" once the board actually replies, or "\u2717 REJECTED" on error.
- Persistent "unsaved changes" banner - config edits are live/RAM-only until "Save to SD" is clicked; the banner clears on Save or Reload.
