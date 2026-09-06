# 🤖 Robot Config

One config + telemetry app for an entire robot family — **RX-80B**, **Orchestron**, and **T4-IMU** — instead of three near-identical copies of the same tool.

Connect over USB, and the app automatically figures out which robot it's talking to, pulls its full parameter list, and gives you a live GUI (or a scriptable CLI) to tune it — no reboot required for most changes.

## Why this exists

Each robot's firmware already exposes the same `<K...>` bracket-framed protocol for reading/writing `ConfigManager` parameters and streaming telemetry. Three separate Python tools had grown up around three separate firmwares, each hand-tuned to one robot's parameter layout. This project asks: *what's actually robot-specific here?*

Turns out — almost nothing:

- **Config editing is 100% generic.** Parameter keys are dotted/namespaced (`sbus.min`, `hero.elbowHome`, `m.headR.address`...), so pages and sub-tabs are derived at runtime purely from the key strings. A brand-new key prefix in any firmware's `ConfigParams.def` just shows up — no app changes.
- **Only telemetry layout differs.** Each firmware bakes a fixed set of live-data fields into its `<KT,...>` frames. That's the one thing this app can't infer from the wire alone.

So there's one app, and a small `profiles/` folder that tells it how to decode telemetry for whichever robot answers the ping.

## ✨ Features

- 🔍 **Auto-detection** — connects, sweeps the parameter table, matches key-prefix "fingerprints" against known robots, and picks the right profile. No manual robot selection.
- 🗂️ **Zero-config pagination** — ~200+ parameters organized into navigable groups and sub-tabs, derived entirely from key structure.
- 📊 **Live dashboard** — streamable telemetry at up to 100 Hz, rendered generically from whatever fields the active profile defines.
- ✅ **Real send/confirm feedback** — you see *sending...* the instant you move a slider, and a clear ✓/✗ once the board actually replies. No more wondering if a change "took."
- ⚠️ **Unsaved-changes banner** — config edits are live/RAM-only until you hit **Save to SD**; a persistent warning reminds you it isn't durable yet.
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
```

In the GUI: pick a port, hit **Connect**. Once connected, the left nav shows every parameter group; the **Dashboard** tab streams live telemetry.

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
| **T4-IMU** | `balance.*`, `velocity.*`, `turn.*`, `kin.*`, `phys.*`, `cal.*`, `imu.*`, `move.*` |

An unrecognized robot still works for config editing (`profiles/base.py`) — it just won't have a decoded telemetry dashboard until it gets its own profile.

## 📖 Docs

- [Architecture](docs/ARCHITECTURE.md) — protocol reference, module layout, how auto-detection and paging work
- [Changelog](CHANGELOG.md)

## 🛠️ Requirements

- Python 3.10+
- `pyserial` (always)
- `dearpygui` (GUI only — the CLI works without it)
