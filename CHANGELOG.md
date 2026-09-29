# Changelog

All notable changes to this project will be documented in this file.

## [0.4.0] - 2026-09-28

### Changed
- **Display scales come from the firmware, not from the app.** `<KN>` descriptors may now carry a
  6th field, the key's display divisor (`<KN##,min,max,val,key,scale>`; BallBot T4-IMU v0.6.83+).
  When present it is authoritative: `ParamInfo.fw_scale`, and `models.FW_SCALE` for the value
  column, both filled by `refresh_params()`. A new scaled key in firmware therefore shows
  correctly with no app change. Firmware that does not send it (Orchestron, RX-80B, BallBot
  before v0.6.83) falls back to the profile `SCALE` table exactly as before.
- T4-IMU profile `SCALE` is now a frozen fallback for pre-v0.6.83 firmware and should not be
  extended (it replaces the per-key additions made in 0.3.2).

## [0.3.2] - 2026-09-28

### Added
- **T4-IMU profile: display scales for the BallBot v0.6.82 LQR keys** - `est.alpha` (x1000),
  the eight `lqr.kx*` / `lqr.ky*` gains (x10) and `lqr.outScale` (x1000). The remaining new keys
  (`control.shadow`, `control.swapMs`, `est.rate100`, `lqr.phiErrMax`, `sysid.mode`,
  `sysid.ditherAmp`) are unscaled integers and need no entry.

## [0.3.1] - 2026-09-15

### Fixed
- **Unplugging the USB cable left the app stuck "Connected", and Disconnect hung it.** Four
  separate faults, all on that one path:
  - **Deadlock (regression, v0.3.0).** `state.lock` is a plain `threading.Lock`, and
    `job_disconnect()` called `_reset_plot_buffers()` - which takes the lock - from inside a
    `with state.lock:` block. The first press of Disconnect blocked the io worker forever, so
    every later job queued behind it and the app appeared frozen. The nesting is gone and the
    lock is now an `RLock`, since a coarse state lock guarded by many `with` blocks makes this
    easy to reintroduce.
  - **A dead link was invisible.** `SerialTransport._read_loop` swallowed the error and let its
    thread die silently, and pyserial's `is_open` stays `True` after the device is gone, so
    nothing could tell "idle" from "unplugged". Transports now mark themselves failed and report
    it through a new `set_on_lost()` callback; `is_open` honours that flag. The app tears the
    connection down by itself and shows `Connection lost - ...`.
  - **Teardown gave up half way.** `job_disconnect()` ran `stream(0)` before `close()`; on a dead
    port that write raises, so `close()` never ran and the state block that clears
    `connected` was skipped - leaving a live Disconnect button that did nothing. Teardown is now
    one shared `_force_disconnect()` with every step independently guarded, so it always
    finishes and always releases the port.
  - **Backlog after the failure.** Queued requests each waited out the full 1.5 s timeout.
    `Protocol.request()` now fails fast on a link known to be down, and all jobs plus
    `flush_dirty()` check the link first (pending edits are dropped, since they can never land).
- **BLE link loss is detected too**, via bleak's `disconnected_callback`, and failed BLE writes
  now surface instead of vanishing into a fire-and-forget future.
- **A failed connect no longer leaks the port.** If `ping`/`refresh_params` failed after the
  transport opened, the transport stayed open and held the COM port against the next attempt.

### Verified
- Development testing used a fake "yankable" transport (nested lock does not deadlock; unplug is
  self-detected and torn down; Disconnect works on a dead link and a healthy one; jobs go inert
  afterwards), since this machine could not reproduce a real Windows unplug.
  **Confirmed on hardware 2026-09-15**: pulling USB mid-session is now detected and recovers.

---

## [0.3.0] - 2026-09-15

### Added
- **Profile-driven Dashboard** - restores the richer layout the per-project tools had, without
  giving up the universal design. A profile may now define `DASHBOARD` with `panels`
  (titled groups of labelled, individually formatted fields), `plots` (rolling line plots) and
  `state_labels`. Profiles without it keep the generic flat grid, verified unchanged for
  Orchestron (32 fields) and RX-80B (17 fields).
- **T4-IMU dashboard**: 8 panels (Attitude, Rates, Velocity command vs actual, Control output,
  Motors, Encoders, Loop health, SBUS) and 3 rolling plots (Attitude; Velocity command vs actual;
  Control output - balance vs velocity). The last two are the pair that matter when tuning stops
  and drift. Plots buffer 400 points per series and use the firmware `ms` field as the time base
  rather than UI wall-clock, so a laggy render loop cannot distort them.
- Panels and plot series whose telemetry group is unchecked are dropped automatically, so the
  spec never has to agree with the group mask.

### Fixed
- **T4-IMU state was mislabelled.** Its telemetry `state` is `BtStateCode()` in the firmware
  (0=SAFE 1=LOOP-ON 2=ARMED 3=E-STOP 4=TILT), not the family's 0..3
  IDLE/MANUAL/CONTROL/AUTO - so the dashboard showed an **emergency stop as "AUTO"**, and a tilt
  trip as "?". Correct labels and colours restored (E-STOP red, TILT orange), carried in the
  profile where the divergence belongs.

---

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
