# Architecture

## Overview

A single Python config/telemetry tool that talks to any robot in the
Orchestron/RX-80B/T4-IMU family over the shared `<K...>` serial protocol. One
app instead of three near-identical copies - the protocol, config paging, and
GUI are 100% generic; only the telemetry field layout and scale table differ
per robot, and those live in `profiles/`.

## Wire protocol

Angle-bracket framed commands over USB serial (115200 baud) or BLE (HM-10 class
module, transparent UART bridge), category letter
`'K'` ("keys" - config keys), matching each firmware's `CommandProtocol.hpp`:

```
<KP>          -> <KP,fwIntVersion>          ping / liveness
<KC>          -> <KC,count>                 number of config keys
<KN##>        -> <KN##,min,max,val,key[,scale]>  descriptor of key id ##; scale = display divisor (optional)
<KD##>        -> <KD##,text>                     one-line description of key id ## (BallBot v0.6.94+;
                                                 older firmware answers <KE##,1> and is not asked again)
<KG##>        -> <KG##,value>               get value of key id ##
<KS##,value>  -> <KS##,value> | <KE##,code> set value (live, RAM only)
<KW>          -> <KW,1|0>                   write config to SD (config.ini)
<KL>          -> <KL,1>                     reload config from SD
<KT>          -> <KT,ms,mode,mask,...>      one telemetry snapshot
<KR##>        -> <KR##,mask> then <KT,...>  stream at ## Hz (0=stop)
```

Orchestron 2.31.0+ adds, for the Events tab:

```
<KI>                  -> <KI,linkUp,pad,us1..us24>   inputs now (Learn, live bars)
<KFL> / <KFL##>       -> <KFL,count> / <KFL##,name>  WAV files
<KFR,name,offset>     -> <KFR,offset,total,hex>      read 96 bytes (total -1 = no file)
<KFO,name,size>       -> <KFO,size>                  start an upload (events.ini, sequences.ini)
<KFW,hex>             -> <KFW,received>              28 bytes a chunk (inbound frames <= 63 chars)
<KFC,crc32>           -> <KFC,size>                  CRC matches: written, old file kept as .bak
<KU> / <KUR>          -> <KU,rules,problems>         last load's report (<KUR> reloads first)
<KU##>                -> <KU##,line,text>            problem ##
<KV1> / <KV0>         -> <KV1> / <KV0>, then <KV,line> each time a rule fires (routed like telemetry)
<KE0,code>            3 file not allowed, 4 SD busy, 5 upload refused, 6 SD write failed
```

Orchestron 2.34.0+ adds the loop profiler, for the Dashboard's "Loop profiler" table (polled once a
second while it is open, every 3 s over BLE):

```
<KQ>                  -> <KQ,sections,on,auto,cpu,cpuMax,blocks,blocksMax,blocksTotal>
<KQR> / <KQE1> / <KQE0> -> as <KQ>, after a reset / switching it on / off
<KQ##>                -> <KQ##,name,calls,durMin,durAvg,durMax,periodMin,periodAvg,periodMax>  (us)
```

Firmware without it answers `<KQ>` with an error, and the panel stays hidden. The profiler is
the shared `LoopProfiler` library, so RX-80B and T4-IMU get the same panel when they take it.

Line numbers are 1-based file lines. The Events tab keys problems and fired notices to the
line objects of the file the robot loaded, so they stay on the right row while you edit.

Key ids are positional indices into the firmware's `ConfigParams.def` -
append-only, never reordered. The app re-learns the full key map every
connection via `<KC>` + a `<KN##>` sweep; nothing is hardcoded client-side.

## Module layout

| File | Responsibility |
|---|---|
| `framing.py` | Byte-level `<...>` frame reader, mirrors the firmware's `SerialPacket.hpp` |
| `transport.py` | `SerialTransport` (pyserial) and `BleTransport` (bleak, HM-10) behind a `Transport` ABC |
| `protocol.py` | Request/reply correlation + telemetry routing on top of a transport |
| `client.py` | `RobotClient` - high-level get/set/save/reload/stream API |
| `models.py` | `ParamInfo`, `TelemetryFrame` - reads scale/telemetry layout from `profiles.active` |
| `pages.py` | Turns the flat param list into nav pages, purely from key-prefix structure |
| `profiles/` | Per-robot telemetry layout + scale table + signature keys + optional Dashboard spec (see below) |
| `app.py` | Dear PyGui desktop UI |
| `cli.py` | Headless REPL for scripting/smoke tests |
| `eventsini.py` | Orchestron's `events.ini` as an editable, round-trip-safe document (no UI, no I/O): rules, modifiers, presets, activities |
| `events_page.py` | The Events tab: rule / modifier / activity editors, Learn, live inputs, fired markers, move up/down, robot load/save |
| `tests/` | `py -m unittest discover tests`: the events.ini model, file commands against a simulated robot |

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
- `SCALE` - display divisor per key (raw wire value / divisor = human value). **Fallback only**: a
  firmware that sends `scale` in `<KN>` (BallBot v0.6.83+) is authoritative, so the table is only
  used for firmware that doesn't yet. Goal: every firmware sends it and these tables go away.
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
