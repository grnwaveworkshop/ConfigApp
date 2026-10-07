# 🤖 Droid Config

One config + telemetry app for an entire robot family — **RX-80B**, **Orchestron**, and **T4-IMU** — instead of three near-identical copies of the same tool.

Connect over USB **or Bluetooth LE**, and the app automatically figures out which robot it's talking to, pulls its full parameter list, and gives you a live GUI (or a scriptable CLI) to tune it — no reboot required for most changes.

## Why this exists

Each robot's firmware already exposes the same `<K...>` bracket-framed protocol for reading/writing `ConfigManager` parameters and streaming telemetry. Three separate Python tools had grown up around three separate firmwares, each hand-tuned to one robot's parameter layout. This project asks: *what's actually robot-specific here?*

Turns out — almost nothing:

- **Config editing is 100% generic.** Parameter keys are dotted/namespaced (`sbus.min`, `hero.elbowHome`, `m.headR.address`...), so pages and sub-tabs are derived at runtime purely from the key strings. A brand-new key prefix in any firmware's `ConfigParams.def` just shows up — no app changes.
- **Only telemetry layout differs.** Each firmware bakes a fixed set of live-data fields into its `<KT,...>` frames. That's the one thing this app can't infer from the wire alone.

So there's one app, and a small `profiles/` folder that tells it how to decode telemetry for whichever robot answers the ping.

## ✨ Features

- 🔍 **Auto-detection** — connects, sweeps the parameter table, matches key-prefix "fingerprints" against known robots, and picks the right profile. No manual robot selection.
- 🗂️ **Zero-config pagination** — ~200+ parameters organized into navigable groups and sub-tabs, derived entirely from key structure, in the order the firmware lists them.
- 💬 **Parameter descriptions** — hover a parameter for its one-line description, sent by the firmware (`<KD##>`, BallBot v0.6.94+), fetched in the background for the page you're on and cached per firmware version.
- 📊 **Live dashboard** — streamable telemetry at up to 100 Hz. Profiles can declare titled panels and rolling plots (T4-IMU does); anything without a spec still renders generically from whatever fields it streams. Firmware with a loop profiler (`<KQ>`, Orchestron 2.34.0+) adds a **Loop profiler** table: how long each part of the robot's main loop takes and how often it runs, plus the audio interrupt's CPU share.
- ▶️ **Actions** — robots that list actions (`<KA>`, Orchestron 2.27.2+) get an Actions tab of buttons (recorder, modes, home, stop, sequences) and a box to run any action in the robot's `events.ini` vocabulary. Orchestron also gets a **Record** button in the top bar that shows the take time. Pin any action to the **Dashboard** (right-click it) to keep it next to the live telemetry.
- 🎛️ **Events** — Orchestron 2.31.0+: edit what the transmitter's switches, sticks and button pad do (`events.ini`) with dropdowns, **Learn** (move the control, it picks the channel), **Test**, the robot's own check of every line, and a live *fired* marker per rule. Works offline on a file too. 0.9.0 adds an **Activities** tab (random sounds, music playlists, sequences picked now and then, AUTO idle motion) and moving rules up and down.
- ✅ **Real send/confirm feedback** — you see *sending...* the instant you move a slider, and a clear ✓/✗ once the board actually replies. No more wondering if a change "took."
- ⚠️ **Unsaved-changes banner** — config edits are live/RAM-only until you hit **Save to SD**; a persistent warning reminds you it isn't durable yet.
- 📶 **USB or BLE** — wired serial, or wireless to an HM-10 class module (BallBot's `bt.*` link). Same protocol, same app; pick BLE in the connect bar or pass `--ble` to the CLI.
- 🖥️ **GUI and CLI** — a full Dear PyGui desktop app for interactive tuning, plus a headless REPL for scripting and quick smoke tests.
- 🧩 **Extensible** — adding support for a fourth robot is a new ~30-line file in `profiles/`, not a new app.

## 🚀 Quick start

```bash
pip install -r requirements.txt

# GUI
py app.py

# or headless CLI
py cli.py --list          # see available COM ports
py cli.py --port COM5

# wireless (HM-10 / "DSD TECH" BLE module)
py cli.py --scan-ble      # list nearby BLE devices
py cli.py --ble           # connect by name
py cli.py --ble --address 00:35:FF:20:90:DD
```

In the GUI: pick a port and hit **Connect** — or tick **BLE**, hit **Scan**, pick the module, then **Connect**. Once connected, the left nav shows every parameter group; the **Dashboard** tab streams live telemetry, the **Actions** tab runs things on the robot, and the **Events** tab edits what the transmitter's controls do (Orchestron).

In the CLI:
```
> dump hero              # list all keys under the "hero" prefix
> set hero.elbowHome 95  # live - servo moves immediately
> save                   # write config.ini to SD
```

## 🔌 Supported robots

| Robot | Signature keys |
|---|---|
| **RX-80B** | `hero.*`, `lift.*` |
| **Orchestron** | `stormtrooper.*` |
| **T4-IMU** | `ctrl.*`, `est.*`, `cal.*`, `drive.*`, `diag.*` (v0.6.94+); `balance.*`, `velocity.*`, `turn.*`, `kin.*`, `phys.*`, `imu.*`, `move.*` (older) |

An unrecognized robot still works for config editing (`profiles/base.py`) — it just won't have a decoded telemetry dashboard until it gets its own profile.

## 📖 Docs

- [Architecture](docs/ARCHITECTURE.md) — protocol reference, module layout, how auto-detection and paging work
- [Changelog](CHANGELOG.md)

## 🛠️ Requirements

- Python 3.10+
- `pyserial` (always)
- `dearpygui` (GUI only — the CLI works without it)
