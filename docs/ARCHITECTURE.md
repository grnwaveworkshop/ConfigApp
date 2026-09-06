# Architecture

## Overview

A single Python config/telemetry tool that talks to any robot in the
Orchestron/RX-80B/T4-IMU family over the shared `<K...>` serial protocol. One
app instead of three near-identical copies - the protocol, config paging, and
GUI are 100% generic; only the telemetry field layout and scale table differ
per robot, and those live in `profiles/`.

## Wire protocol

Angle-bracket framed commands over USB serial (115200 baud), category letter
`'K'` ("keys" - config keys), matching each firmware's `CommandProtocol.hpp`:

```
<KP>          -> <KP,fwIntVersion>          ping / liveness
<KC>          -> <KC,count>                 number of config keys
<KN##>        -> <KN##,min,max,val,key>     descriptor of key id ##
<KG##>        -> <KG##,value>               get value of key id ##
<KS##,value>  -> <KS##,value> | <KE##,code> set value (live, RAM only)
<KW>          -> <KW,1|0>                   write config to SD (config.ini)
<KL>          -> <KL,1>                     reload config from SD
<KT>          -> <KT,ms,mode,mask,...>      one telemetry snapshot
<KR##>        -> <KR##,mask> then <KT,...>  stream at ## Hz (0=stop)
```

Key ids are positional indices into the firmware's `ConfigParams.def` -
append-only, never reordered. The app re-learns the full key map every
connection via `<KC>` + a `<KN##>` sweep; nothing is hardcoded client-side.

## Module layout

| File | Responsibility |
|---|---|
| `framing.py` | Byte-level `<...>` frame reader, mirrors the firmware's `SerialPacket.hpp` |
| `transport.py` | `SerialTransport` (pyserial) behind a `Transport` ABC |
| `protocol.py` | Request/reply correlation + telemetry routing on top of a transport |
| `client.py` | `RobotClient` - high-level get/set/save/reload/stream API |
| `models.py` | `ParamInfo`, `TelemetryFrame` - reads scale/telemetry layout from `profiles.active` |
| `pages.py` | Turns the flat param list into nav pages, purely from key-prefix structure |
| `profiles/` | Per-robot telemetry layout + scale table + signature keys (see below) |
| `app.py` | Dear PyGui desktop UI |
| `cli.py` | Headless REPL for scripting/smoke tests |

## Config paging is fully generic

The firmware's parameter table has no notion of sections - just a flat list
of `(id, min, max, value, key)`. Keys are consistently dotted/namespaced, so:

- the page name is the *first* dotted component (`sbus`, `hero`, `m`, `lift`, `balance`, ...)
- a page gets sub-tabs automatically if its params have more than one distinct
  *second* component (e.g. `m.headR.*` vs `m.tRing.*`)

No per-project curation table. A new key prefix appended to any project's
`ConfigParams.def` shows up correctly with zero changes to this app.

## Robot auto-detection (`profiles/`)

Telemetry is the one thing that isn't generic - each firmware's
`EmitTelemetry()` bakes in a fixed field layout per bitmask group, and that
layout differs per robot. `profiles/<robot>.py` defines, per robot:

- `NAME` - display name
- `SIGNATURE_PREFIXES` - key prefixes unique to that robot (e.g. `hero`/`lift`
  for RX-80B, `stormtrooper` for Orchestron, `balance`/`velocity`/... for T4-IMU)
- `SCALE` - display divisor per key (raw wire value / divisor = human value)
- `TELEMETRY_GROUPS` - `(name, bitmask, [field names])` matching the
  firmware's `kTele*` enum and `EmitTelemetry()` field order
- `TELEMETRY_MASK_IMPLEMENTED` - which groups the firmware actually emits
- `MODE_NAMES` - operating mode code -> name (all three currently share the
  same `OPMODE_IDLE/MANUAL/CONTROL/AUTO` = 0/1/2/3 convention)

After the `<KC>`/`<KN##>` param sweep, `client.py`'s `refresh_params()` calls
`profiles.detect()`, which matches the sweep's key prefixes against each
profile's `SIGNATURE_PREFIXES` and sets `profiles.active` accordingly - no
firmware change needed to identify which robot is connected. Unknown/no match
falls back to `profiles/base.py` (config editing still works; the Dashboard
tab just has no telemetry fields to show).

## Threading model

All robot I/O (`RobotClient` calls) runs on a single background worker thread
fed by a job queue (`io_q`). The Dear PyGui render loop polls shared state
(`AppState`, guarded by a lock) and only creates/updates widgets from the main
thread - Dear PyGui widget calls aren't thread-safe. Slider/input edits are
debounced (120 ms) before being sent, so dragging a slider doesn't flood the
link with a `<KS...>` command per frame.
