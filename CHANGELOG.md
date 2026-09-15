# Changelog

All notable changes to this project will be documented in this file.

## [0.2.0] - 2026-09-15

### Added
- **BLE transport restored** (`transport.py` `BleTransport`) - connect wirelessly to HM-10 class
  modules ("DSD TECH"), currently BallBot/T4-IMU's `bt.*` link. Runs a private asyncio loop on
  its own thread behind the same synchronous `Transport` interface, so the protocol/client/GUI
  layers are unchanged. Ported from the retired BallBot-only client (BallBot repo, commit
  `daf6f83`: `T4-IMU/tools/ballbot_gui/transport.py`), where it was hardware-verified over BLE
  including while the robot was actively balancing. Writes are chunked to the HM-10's 20-byte MTU.
- **GUI**: `BLE` checkbox in the connect bar swaps the COM-port picker for a BLE device picker,
  with a `Scan` button. Scanning runs on the io worker (it blocks ~8 s) and preselects the HM-10
  if it advertises its default name.
- **CLI**: `--ble`, `--address <MAC>`, `--name <advertised name>` (default `DSD TECH`), and
  `--scan-ble` to list nearby devices and exit. Without `--address`, connects by name.
- `bleak>=0.22` added to `requirements.txt` (API verified against the installed 3.0.2:
  `find_device_by_address` / `find_device_by_filter` / `write_gatt_char(response=False)` /
  `start_notify` all still present with compatible signatures). A missing bleak is reported as a
  readable message rather than a traceback.

### Fixed
- **T4-IMU profile scale table** (`profiles/t4imu.py`): added three BallBot keys whose firmware
  storage is scaled but were showing raw integers in the scaled-value column:
  `velocity.brake` (x100, 150 = 1.50x — firmware v0.6.77), `velocity.leanMax` (x10, 40 = 4.0 deg
  — firmware v0.6.80), and `turn.slipRatio` (x100, 35 = 0.35 — pre-existing gap). Display-only:
  sliders/inputs still edit and send the raw integer. Found by auditing every firmware
  `ConfigParams.def` entry whose description declares an xN scale against the profile.

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
