# Changelog

All notable changes to this project will be documented in this file.

## [0.13.0] - 2026-10-09

### Added (Orchestron firmware 2.37.0)
- **Raise or lower a setting:** `set:KEY+=N` and `set:KEY-=N` step a setting by N, and the robot
  stops it at the setting's max or min. The rule and activity editors' "Change a setting" row is
  now the setting, **set to / raise by / lower by**, and the value or step. Rules show it in words
  ("Raise audio.mix.wavB by 5 (stops at its maximum)"). It works anywhere an action does: rules,
  `cycle(...)` items, an activity's `play =`. A step is not a state rule (`set:KEY=VALUE` still
  is): applied again at power-up it would move the setting again.
- **The repeat gesture:** `NAME.repeat` / `pad.N.repeat` fires at the press, then again every
  `button.repeatMs` while the button is held, from `button.longPressMs` after the press (a key's
  auto-repeat). It's in the rule editor's gesture list and reads "press, repeating while held".
  `button.repeatMs` shows on the button config page with the other `button.` keys.

### Changed
- `set:` is checked as the firmware checks it at load: KEY=VALUE, KEY+=N or KEY-=N; VALUE a whole
  number; N a whole number of 1 or more with no sign (`+=0`, `+=x`, `+=-5` are refused). While
  connected, the robot's settings table also checks the setting exists, VALUE is within its
  min..max and N is no more than max - min: a red row, and OK refuses it in the rule and activity
  editors. Offline, the robot does those checks when it loads the file.

### Tests
- `tests/test_eventsini.py`: the volume-button example (`volup.repeat = set:audio.mix.wavB+=5`)
  read, described and written back; relative steps with spaces, in cycles and activities, never
  state rules; the errors (`+=0`, `+=x`, `+=-5`, no `=`, a fraction) and, with a settings table,
  an unknown setting, a step larger than the range, a value out of range, per line in the
  document; `.repeat` on pad and named buttons, `pad.1.repeating` refused. The shipped
  `events.ini` files' `set:` lines are checked against Orchestron's `ConfigParams.def`.

## [0.12.0] - 2026-10-09

### Added (Orchestron firmware 2.36.0)
- **Buttons tab** in the Events tab: the `[buttons]` section, one row per button with its name,
  channel, value as written and a live **DOWN** light. Add, Edit and Delete. A button is a value
  on any channel (`dome = ch3 1900`), so one channel can carry several, like the pad.
- **Capture** in the button editor: hold the transmitter button and press Capture. It takes the
  channel that moved since the editor opened, at its value in us. If nothing has moved yet, it
  waits 4 s for you to hold the button (Learn, for buttons).
- **Named buttons in the rule editor:** "When" offers **Named button**, with the gestures and the
  modifiers as banks (`bank2+happy`). The new **release** gesture is there for pad buttons too.
- **cycle(A, B[, C]):** tick "take turns" under the actions and the 2 or 3 actions become a
  cycle: each time the rule fires it runs the next one. Rules show it in words ("Each time: the
  next of ..."). Test runs the next item each time, as the robot does.
- **The robot's buttons held** (`<KI>`'s new last field) light the Buttons tab and show on the
  Inputs tab. Older firmware doesn't send it: the app then decides from the channel values.
- **Checks as the firmware's:** button names (1-15 letters, digits or `_`, not read as a
  trigger), unique across `[buttons]` and `[modifiers]`, not on the pad's channel, at most 14 a
  channel, 32 in all, on 8 channels. A rule naming a button or modifier the file doesn't define
  is a red row before you save.
- **New actions** in the rule and activity editors: `stopA` / `stopB` stop one player.
  `toggleA:BANK` / `toggleB:BANK` stop that player if it's playing, else play the bank's next
  file (bank 0-10 from a list). `toggleaudio:random` / `toggleaudio:music` turn that sound mode
  on, or back to manual if it's on. `toggleaudio` is not a state rule; `audio:` still is.
- **Rules by sound mode:** the rule editor has a "sound mode" row (manual, random, music) under
  the modes. It writes `when=audio.manual|random`, after the modifiers and `mode.`, as the
  firmware does. Rules show it in words ("when sound mode is music"), as activities do.

### Changed
- The modifier editor refuses a name another modifier or button has, and names over 15
  characters (the firmware's limit).
- An action the firmware would refuse is a red row before you save: an unknown word, a value on
  a word that takes none (`stopA:1`), a bank outside 0-10 (`nextA:11`), an unknown mode or sound
  mode, `toggleaudio:manual`.
- The rule editor says "State rule" only for a channel rule. The firmware re-applies only those
  at power-up and link-up, not a button's `audio:manual`.

### Tests
- `tests/test_eventsini.py`: `[buttons]` read and written, bad and duplicate names, the pad
  channel, the limits, named triggers with gestures and banks, `.release`, `cycle()` and its
  errors, cycles never state rules, a whole file round trip, which buttons are down. The new
  actions (words, descriptions, errors, `toggleaudio:manual` refused, not state rules), rules'
  `when=audio.` with modifiers and modes, and Sparky's music and sounds buttons.
- `tests/test_client_files.py`: `<KI>` with and without the buttons held.

## [0.11.1] - 2026-10-08

### Changed (BallBot firmware 0.7.10)
- **BallBot's default Dashboard buttons include `policy:toggle`**: classic or the learned policy
  drives, the same as a short press on the transmitter's Ch4. Its Actions tab also lists
  `policy:on` / `policy:off` (from the firmware's catalogue). A Dashboard you already customised
  keeps your buttons; right-click an action to pin it.

## [0.11.0] - 2026-10-08

### Added (BallBot firmware 0.7.9)
- **Text keys, with dropdowns.** Settings whose value is a name (`<KX>`): each shows on its page
  and tab as a dropdown of the robot's choices (a text field when it offers none), with what the
  robot did beside it. **Rescan** asks the robot again. BallBot's `ctrl.policy.file` lists `builtin`
  and the learned policies in its SD card's `/policies` folder; a pick loads the file while the
  robot is disarmed (armed: it loads at the disarm) and says so. **Save to SD** keeps it.
  Firmware without `<KX>` shows none. CLI: `texts`, `settext <key> <value>`.
- **Reboot button** in the top bar, for firmware whose action list has `reboot` (BallBot): asks
  first, restarts the board (the robot refuses while armed), then reconnects by itself, over USB
  (once the port is back) or BLE.

### Tests
- `tests/test_client_texts.py`: the text-key commands against a pretend BallBot (choices, rescan,
  load or wait, a missing file, refused values, firmware without them).

## [0.10.0] - 2026-10-07

### Added (Orchestron 2.34.0)
- **Loop profiler on the Dashboard.** A "Loop profiler" section (closed until you open it) shows
  each part of the robot's main loop: calls, average and longest time, its share of the loop,
  how often it runs and its longest gap. Sections whose longest run is 1 ms or more are amber.
  Above the table: the audio interrupt's CPU share and audio memory, now and the max.
  **Profiling on** switches the robot's profiler on or off (it starts off); **Reset** clears the
  statistics. Polled once a second while open (every 3 s over BLE). Hidden on firmware without
  `<KQ>`.

## [0.9.2] - 2026-10-07

### Changed (Orchestron 2.33.0)
- **The events.ini editor takes only events.ini's own forms**, as the firmware now does:
  `buttonN`, `chN.high`, `random:` and `next:` are errors that say what to write instead
  (`pad.N`, `chN high`, `randomA:`, `nextA:`).
- **Modifier names:** only names that read as a trigger are refused (`ch5`, `button3`, `pad`,
  `mode`, `link`); "chin" or "modest" are fine, as on the robot.
- Nothing else needed: the settings pages come from the firmware, so `fx.ring.waveform` 0-3 and
  the retired `rec.switchCh` / `rec.switchMode` follow by themselves, and "audio:random plays
  nothing" arrives in the Events tab's problem list.

## [0.9.1] - 2026-10-06

### Added (BallBot firmware 0.7.2+)
- **T4-IMU Dashboard buttons:** the profile's default Controls are arm / disarm, E-stop, clear
  E-stop, start / stop logging and capture balance offset, from the firmware's `<KA>` catalogue.

## [0.9.0] - 2026-10-06

### Added (Orchestron 2.32.0)
- **Activities tab** in the Events tab: the robot's `[activity.NAME]` sections in words, with
  Add, Edit, Delete and Test now. The editor covers each kind:
  - **an action every so often:** any action, every 20-120 s ...
  - **a playlist:** player, bank, shuffle, the gap between tracks
  - **a pick of sequences:** up to 8, with weights and a no-repeat time
  - **idle motion in AUTO:** servos, rest, swing, duty, intensity, period, pause, slew, how many
    move at once. `sN.*` lines for one servo are kept.
  - **when it runs:** modes, sound mode (manual / random / music) and held modifiers
  - **start at once**, and a seed
  - a preview of the section it writes
- **Move rules up / down** (^ / v on each row). When two rules fire together, the upper one
  runs first.
- **The 2.32 actions in the rule editor:**
  - Change a setting (`set:KEY=VALUE`)
  - Next WAV in a bank
  - Random WAV from a bank *or* a number range (`randomA:2001-2013`)
  - Sound mode music

  Text fields say what they expect. Rules list them in words ("Set audio.mix.master to 50",
  "Random WAV numbered 2001-2013 (A)").
- **`eventsini.py`:** `EventsDoc.activities()` / `set_activity()` / `delete_activity()`,
  `move_entry()`, `Activity` (kind, describe, when parts), `parse_pick()` / `format_pick()`.
  Sequences named by activities count as used.
- **Tests:**
  - activities, moving rules, the new actions
  - comments kept on edit
  - every `events.ini` the Orchestron repository ships (`docs/examples`, `SDCard*`) reads cleanly
    and round-trips byte for byte

### Fixed
- **Editing a preset (and now an activity) dropped the comments at the ends of its lines.** A
  setting that is written again keeps its comment.

---

## [0.8.0] - 2026-10-05

### Added
- **Events tab** (Orchestron 2.31.0+): edit the robot's `events.ini`, what the transmitter's
  switches, sticks and button pad do, without touching the text or the SD card.
  - **Load from robot / Save to robot.** The file goes over `<KF...>` in CRC-checked chunks;
    the robot keeps the old one as `events.ini.bak`, loads the new one at once and reports
    every line it didn't accept. That row turns red, with the reason on hover.
  - **Rules:** every rule in words (*pad button 3 click: Home the servos, in idle / manual*),
    grouped under the file's own comments, with Edit, Test and Delete.
  - **Rule editor:**
    - the trigger (pad button + gesture, channel + position, RC link, mode change)
    - up to three actions, with dropdowns of the card's sequences (`sequences.ini`), WAV files,
      modes and presets
    - modifiers, and the modes it's limited to
    - a preview of the line it writes, and **Test now**
  - **Learn:** move a switch or stick and the editor picks the channel and position (a switch
    end or the middle as low / mid / high, anything else as a value). A live bar shows the
    channel and says when the condition is met.
  - **Fired:** while connected, a row shows *fired* each time its rule runs on the robot
    (`<KV>`).
  - **Modifiers**, **Presets** (as `key = value` lines) and **Settings** (deadband, pad channel)
    tabs; **Inputs** shows all 24 channels live; **Text** shows the whole file.
  - **Open file... / Save file... / New** work offline, for a card in a card reader.
  - A load / edit / save changes only the lines you edited: comments, blank lines and the order
    stay as you wrote them.
- **`eventsini.py`:** the events.ini model, pure and round-trip safe.
- **`events_page.py`:** the tab, kept out of `app.py`.
- **`client.py`:** `inputs()`, `wav_files()`, `read_file()`, `write_file()`, `events_report()`,
  `rule_notices()`, `set_notice_handler()`. `protocol.py` routes `<KV,line>` notices like
  telemetry, so they are never taken for a reply.
- **Tests:** `tests/` (run `py -m unittest discover tests`), standard library only:
  - the events.ini model
  - the file commands against a simulated robot, including the firmware's 63-character frame
    limit and a damaged upload

### Changed
- The Actions tab says `events.ini` (Orchestron 2.30.0 replaced `buttons.ini`).

---

## [0.7.0] - 2026-10-05

### Added
- **Controls panel on the Dashboard:** action buttons above the telemetry.
  - **Starting set:** the profile's `DASHBOARD_CONTROLS`. Orchestron starts with Record, Stop
    everything, Home and the four modes.
  - **Pin:** right-click any button in the Actions tab, or type an action and press **Pin**.
  - **Unpin:** right-click a Dashboard button. **Reset** goes back to the defaults.
  - **Saved** per robot in `~/.droid_config/controls_<robot>.json`.
  - The panel hides while disconnected, and for firmware without `<KA>`.
- **The Record buttons turn red** while a take is running, in the top bar and on the Dashboard.

---

## [0.6.0] - 2026-10-05

### Added
- **Actions tab** (firmware with `<KA>`, Orchestron 2.27.2+):
  - one button per entry in the robot's action list, grouped (Recorder, Robot, Audio, Sequences)
  - a box that runs any action in the robot's `buttons.ini` vocabulary (`seq:wave`, `wavA:2001`,
    `mode:control` ...)
  - the result shows in the confirm line. `client.actions()` / `client.run_action()`; CLI
    `actions` and `action <command>`.
- **Record button** in the top bar when the robot lists `rec:toggle`. It reads **Stop
  recording** during a take, with the take time (`REC 0:42 / 10:00`) or `writing NN%` beside it.
  - The state comes from the new Orchestron `rec` telemetry group while the dashboard streams.
    Otherwise the app polls `rec:status` once a second, so a take started from the transmitter,
    or ended by its time limit, still shows.
- **Orchestron `rec` telemetry group:** `recState`, `recFrames`, `recMs`, `recLimitMs`, `recPct`.

### Changed
- **Telemetry groups can require a firmware version** (profile `TELEMETRY_MIN_FW`). Orchestron
  firmware before 2.27.2 echoed a requested-but-unimplemented group bit without its fields, which
  shifted the rest of the frame. So the app no longer asks those boards for `rec`, and its
  checkbox is greyed out.

---

## [0.5.2] - 2026-10-04

### Changed
- **Orchestron channel telemetry is labelled `ch1`..`ch12`**, as the transmitter numbers them
  (firmware 2.26.0 numbers every channel setting that way; RC PWM IO1 = ch1). It was `ch0`..`ch11`.

---

## [0.5.1] - 2026-10-04

### Changed
- **Orchestron 2.25.0 detection.** The firmware regrouped its keys into ConfigApp pages (`audio`,
  `fx`, `servo`, `react`, `neo`, `rc`, `button`, `rec`, `pin`, `serial`), so the profile also
  recognises the new `fx` and `react` prefixes. `stormtrooper` still detects older firmware.
- Orchestron 2.25.0 sends scale in `<KN>` and descriptions over `<KD>`, so its pages get the scaled
  column and tooltips with no profile table. The fallback `SCALE` table is frozen at the old key
  names; the stale `noise.overSubtraction` entry is gone.

---

## [0.5.0] - 2026-10-01

### Added
- **Parameter descriptions in the row tooltip.** BallBot firmware v0.6.94 answers `<KD##>` with a
  key's one-line description (`client.describe_text()`):
  - **When fetched:** in the background for the page on screen, one key per io job, so an edit
    queued meanwhile isn't held up. Nothing is fetched at connect, so connecting over BLE is as
    fast as before.
  - **Cache:** `~/.droid_config/desc_<profile>_<fw>.json`, fetched once per robot and firmware
    version.
  - **Older firmware:** it answers with an error frame; the app stops after that first request
    and shows key, id and range as before.

### Changed
- **Pages and sub-tabs follow the firmware's key order**, not alphabetical. BallBot v0.6.94 lists
  `ctrl`, `est` and `cal` first. This also changes the page order on RX-80B and Orchestron, to
  their `ConfigParams.def` order.
- The id-0 `none` placeholder is hidden.
- T4-IMU detection also recognises the v0.6.94 key prefixes (`ctrl`, `est`, `drive`, `diag`).
  The `SCALE` fallback table keeps the old key names, since it only serves pre-v0.6.83 firmware.

---

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
