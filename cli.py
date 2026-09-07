"""Headless REPL for the <K...> protocol - smoke test and scripting. Works
against any robot in the family (RX-80B, Orchestron, T4-IMU); the connected
robot's profile is auto-detected after the param sweep.

    py app.py --list
    py app.py --port COM5

Commands:
    ping                      firmware version
    count                     number of config keys
    dump [filter]             list keys (optionally substring-filtered) with values
    get <key|id>              read one value
    set <key|id> <value>      write one value (live, RAM only)
    save                      <KW> write config.ini to SD
    reload                    <KL> discard RAM edits, re-read from SD
    kt                        one telemetry snapshot
    stream <hz> [mask]        start/stop telemetry (0 stops)
    sweep                     time a full <KN##> descriptor sweep
    raw <KG44>                send a literal frame body
    mtp [on|off]              query, or enter/exit, MTP/mass-storage mode
    q                         quit
"""
from __future__ import annotations

import argparse
import time

import profiles
from client import RobotClient
from protocol import CATEGORY, ProtocolError, split_frame
from transport import SerialTransport
from version import __version__


def on_telemetry(fr) -> None:
    fields = ", ".join(f"{k}={v:g}" for k, v in fr.raw.items() if k not in ("ms", "mode", "mask"))
    print(f"  [KT] mode={fr.mode_name}  {fields}")


def main() -> int:
    ap = argparse.ArgumentParser(description=f"Robot config/telemetry REPL (v{__version__})")
    ap.add_argument("--list", action="store_true", help="list serial ports and exit")
    ap.add_argument("--port", help="serial port, e.g. COM5")
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args()

    if args.list:
        for dev, desc in SerialTransport.list_ports():
            print(f"  {dev:10s} {desc}")
        return 0

    if not args.port:
        ap.error("need --port or --list")
        return 2

    transport = SerialTransport(args.port, args.baud)
    bot = RobotClient(transport)
    print(f"Connecting: {transport.describe()} ...")
    bot.open()

    try:
        ver = bot.ping()
        print(f"Connected. Firmware {ver // 10000}.{(ver // 100) % 100}.{ver % 100} "
              f"(category '{CATEGORY}')")
    except ProtocolError as e:
        print(f"No response: {e}")
        bot.close()
        return 1

    print("Loading parameter map ...", end=" ", flush=True)
    t0 = time.time()
    bot.refresh_params()
    print(f"{len(bot.params)} keys in {time.time() - t0:.1f}s - profile: {profiles.active.NAME}")
    print("Type 'q' to quit, or a command (see --help).")

    bot.set_telemetry_handler(on_telemetry)

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()

        try:
            if cmd in ("q", "quit", "exit"):
                break
            elif cmd == "ping":
                print(bot.ping())
            elif cmd == "count":
                print(bot.count())
            elif cmd == "dump":
                filt = parts[1].lower() if len(parts) > 1 else ""
                for info in sorted(bot.params.values(), key=lambda p: p.id):
                    if filt and filt not in info.key.lower():
                        continue
                    human = f" ({info.human():g})" if info.scale != 1 else ""
                    print(f"  {info.id:4d}  {info.key:34s} = {info.value}{human}"
                          f"   [{info.vmin}..{info.vmax}]")
            elif cmd == "get":
                print(bot.get(parts[1]))
            elif cmd == "set":
                print(bot.set(parts[1], int(parts[2])))
            elif cmd == "save":
                print("saved" if bot.save() else "SAVE FAILED")
            elif cmd == "reload":
                bot.reload()
                print(f"reloaded, {len(bot.params)} keys")
            elif cmd == "kt":
                fr = bot.snapshot()
                for k, v in fr.raw.items():
                    print(f"  {k:12s} {v:g}")
            elif cmd == "stream":
                hz = int(parts[1])
                mask = int(parts[2], 0) if len(parts) > 2 else profiles.active.TELEMETRY_MASK_IMPLEMENTED
                bot.stream(hz, mask)
                print(f"stream {hz} Hz mask 0x{mask:02X}" if hz else "stream stopped")
            elif cmd == "sweep":
                t0 = time.time()
                bot.refresh_params()
                dt = time.time() - t0
                print(f"{len(bot.params)} keys in {dt:.2f}s "
                      f"({dt / max(len(bot.params), 1) * 1000:.1f} ms/key)")
            elif cmd == "raw":
                print(split_frame(bot.proto.request(parts[1].strip("<>"))))
            elif cmd == "mtp":
                if len(parts) > 1 and parts[1].lower() in ("on", "1"):
                    print("MTP ON" if bot.mtp_enter() else "MTP enter failed")
                elif len(parts) > 1 and parts[1].lower() in ("off", "0"):
                    print("MTP OFF" if not bot.mtp_exit() else "MTP exit failed")
                else:
                    print("MTP ON" if bot.mtp_status() else "MTP OFF")
            else:
                print("?")
        except (ProtocolError, IndexError, ValueError) as e:
            print(f"! {e}")

    bot.stream(0)
    bot.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
