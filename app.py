"""Droid config app - Dear PyGui desktop UI over the <K...> serial protocol.
Works against any Droid in the family (RX-80B, Orchestron, T4-IMU) - the
connected Droid's profile (telemetry layout + scale table) is auto-detected
after the param sweep, see profiles/__init__.py.

    py app.py

~200+ config params is too many for one scrolling list, so the Config tab is
paginated: a left nav of groups derived purely from the key namespaces (see
pages.py) - no per-project curation table, so a new key prefix in any
Droid project's ConfigParams.def shows up automatically. Pages whose params
naturally split into sub-groups (e.g. "m.headR.*" vs "m.tRing.*") get
sub-tabs; everything else is one flat table. A search box cuts across all
pages when you know part of a key name.

The Dashboard tab is profile-driven: a profile with a DASHBOARD spec gets
titled panels plus rolling plots; one without gets a generic grid of
whatever telemetry fields it streams. Firmware with a loop profiler (<KQ>,
Orchestron 2.34+) also gets a "Loop profiler" table, polled while it is open.

Threading model: all droid I/O runs on a single background worker thread fed
by a job queue; the render loop polls shared state, rebuilds the page when
needed, and flushes debounced edits. Widget *creation* happens only on the
render-loop (main) thread.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import dearpygui.dearpygui as dpg                                        # noqa: E402

import events_page                                                      # noqa: E402
import pages as pages_mod                                                # noqa: E402
import profiles                                                          # noqa: E402
from client import RobotClient                                           # noqa: E402
from models import TelemetryFrame, scale_for_key                         # noqa: E402
from protocol import ProtocolError                                       # noqa: E402
from transport import (HM10_DEFAULT_NAME, BleTransport,                   # noqa: E402
                       SerialTransport, TransportError)
from version import __version__                                          # noqa: E402

DEBOUNCE_S = 0.12
PROF_POLL_S = 1.0        # loop profiler table refresh while open (BLE: PROF_POLL_BLE_S)
PROF_POLL_BLE_S = 3.0    # 16 replies a sweep: keep a 9600-baud BLE link free for the rest
PROF_SLOW_US = 1000      # a section whose longest run is this long is shown in amber
DEFAULT_STREAM_HZ = 10
PLOT_POINTS = 400        # rolling history per series (~20 s at 20 Hz)


class AppState:
    def __init__(self) -> None:
        # RLock, not Lock: helpers that take the lock are called from code that
        # already holds it (v0.3.0 deadlocked Disconnect exactly that way).
        self.lock = threading.RLock()
        self.status = "Disconnected"
        self.connected = False
        self.fw = 0
        self.profile_name = ""
        self.params: list = []                 # list[ParamInfo] in id order
        self.params_ready = False              # render loop should (re)build nav
        self.nav_built = False
        self.groups_ready = False              # render loop should (re)build telemetry checkboxes
        self.current_page = ""
        self.page_dirty = False                # render loop should rebuild the pane
        self.search = ""
        self.dirty: dict[str, tuple[int, float]] = {}   # key -> (value, last change)
        self.telemetry: TelemetryFrame | None = None
        self.confirm = ""                      # right-side ack: "\u2713 key=val" / "\u2717 rejected"
        self.unsaved = False                   # True once a set() is confirmed, until Save/Reload
        self.mtp_active: bool | None = None     # None = unknown (not connected/not queried yet)
        self.plot_t: deque = deque(maxlen=PLOT_POINTS)          # seconds since first sample
        self.plot_series: dict[str, deque] = {}                 # key -> rolling values
        self.plot_t0: float | None = None                       # firmware ms of first sample
        self.ble_devices: list[tuple[str, str]] = []   # last BLE scan result
        self.ble_ready = False                 # render loop should refill the BLE combo
        # Per-key one-line descriptions from <KD##> (firmware that supports it), fetched in the
        # background for the page on screen and cached on disk per robot + firmware version.
        self.desc: dict[str, str] = {}
        self.desc_supported: bool | None = None  # None = not tried yet on this connection
        self.desc_pending: list[str] = []      # keys still to fetch, in page order
        self.desc_fetching = False             # a fetch step is queued on the io worker
        self.desc_new: set[str] = set()        # fetched since the render loop last looked
        self.desc_cache_file: Path | None = None
        # Actions (<KA>, Orchestron 2.27.2+): the firmware's catalogue as (group, label, command)
        self.actions: list[tuple[str, str, str]] = []
        self.actions_ready = False             # render loop should rebuild the Actions tab
        # Recorder, from the rec telemetry group or an action reply: None = unknown,
        # 0 idle, 1 recording, 2 writing the CSV
        self.rec_state: int | None = None
        self.rec_text = ""
        # Dashboard Controls panel: action commands pinned as buttons (app 0.7.0)
        self.controls: list[str] = []
        self.controls_ready = False            # render loop should rebuild the Controls panel
        self.controls_file: Path | None = None
        self.last_telemetry = 0.0              # time.time() of the last <KT> frame
        # Loop profiler (<KQ>, Orchestron 2.34+): None = this firmware has none
        self.prof = None                       # ProfilerStatus
        self.prof_rows: list = []              # [ProfileSection], the last sweep
        self.prof_ready = False                # render loop should refresh the panel
        self.prof_poll_due = 0.0
        self.rec_poll_due = 0.0                # next rec:status poll (only while no telemetry)


state = AppState()
bot: RobotClient | None = None
io_q: "queue.Queue" = queue.Queue()
io_running = True


def set_status(msg: str) -> None:
    with state.lock:
        state.status = msg


def set_confirm(msg: str) -> None:
    with state.lock:
        state.confirm = msg


def io_worker() -> None:
    while io_running:
        try:
            job = io_q.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            job()
        except Exception as e:  # noqa: BLE001
            set_status(f"ERR: {e}")
        finally:
            io_q.task_done()


# --------------------------------------------------------------------------- #
# Jobs (worker thread)
# --------------------------------------------------------------------------- #
def job_ble_scan() -> None:
    """BLE discovery. Blocks ~8 s, so it runs on the io worker like every other
    transport call; the render loop picks the result up via state.ble_ready."""
    def run() -> None:
        set_status("Scanning BLE (~8 s) ...")
        try:
            found = BleTransport.scan()
        except ImportError:
            set_status("ERR: bleak not installed - pip install -r requirements.txt")
            return
        with state.lock:
            state.ble_devices = found
            state.ble_ready = True
        set_status(f"BLE scan: {len(found)} device(s) found"
                   if found else "BLE scan: nothing found (module powered? in range?)")
    io_q.put(run)


def _rec_from_telemetry(fr: TelemetryFrame) -> None:
    """Recorder state from the rec group (firmware 2.27.2+). Caller holds state.lock."""
    if "recState" not in fr.raw:
        return
    st = int(fr.get("recState"))
    state.rec_state = st
    if st == 1:
        s, lim = int(fr.get("recMs")) // 1000, int(fr.get("recLimitMs")) // 1000
        state.rec_text = f"REC {s // 60}:{s % 60:02d} / {lim // 60}:{lim % 60:02d}"
    elif st == 2:
        state.rec_text = f"writing {int(fr.get('recPct'))}%"
    else:
        state.rec_text = ""


def _rec_from_reply(msg: str) -> None:
    """Recorder state from a rec: action reply ("REC 0:00 / 10:00", "writing 3%", "idle")."""
    with state.lock:
        if msg.startswith("REC"):
            state.rec_state, state.rec_text = 1, msg
        elif msg.startswith("writing"):
            state.rec_state, state.rec_text = 2, msg
        elif msg.startswith("idle"):
            state.rec_state, state.rec_text = 0, ""


def implemented_mask() -> int:
    """The profile's telemetry groups, minus those this firmware is too old to emit
    (profile TELEMETRY_MIN_FW)."""
    mask = profiles.active.TELEMETRY_MASK_IMPLEMENTED
    for bit, min_fw in getattr(profiles.active, "TELEMETRY_MIN_FW", {}).items():
        if state.fw < min_fw:
            mask &= ~bit
    return mask


def on_telemetry(fr: TelemetryFrame) -> None:
    with state.lock:
        state.telemetry = fr
        state.last_telemetry = time.time()
        _rec_from_telemetry(fr)
        if not _plot_keys:
            return
        # Firmware `ms` is the honest time base (the UI thread may lag); fall back
        # to sample count if a profile ever omits it.
        ms = fr.raw.get("ms")
        if state.plot_t0 is None:
            state.plot_t0 = ms if ms is not None else 0.0
        t = ((ms - state.plot_t0) / 1000.0) if ms is not None else float(len(state.plot_t))
        state.plot_t.append(t)
        for k in _plot_keys:
            state.plot_series.setdefault(k, deque(maxlen=PLOT_POINTS)).append(fr.get(k))


def link_is_up() -> bool:
    """True only if we have a client whose transport is actually alive."""
    return bot is not None and bot.is_open


def _force_disconnect(status: str) -> None:
    """Tear the connection down and get the UI back to a usable state.

    Every step is independently guarded: this has to work when the device has
    already vanished, which is precisely when the old code gave up half way
    (a failing stream(0) aborted before close(), leaving the UI stuck on
    "Connected" with a live Disconnect button that did nothing).
    """
    global bot
    b, bot = bot, None
    if b is not None:
        try:
            b.set_telemetry_handler(None)
        except Exception:  # noqa: BLE001
            pass
        if b.is_open:
            try:
                b.stream(0)        # politely stop the firmware streaming
            except Exception:      # noqa: BLE001 - expected on a dead link
                pass
            if events_page.S.supported:
                try:
                    b.rule_notices(False)
                except Exception:  # noqa: BLE001
                    pass
        try:
            b.close()
        except Exception:  # noqa: BLE001
            pass
    with state.lock:
        state.connected = False
        state.params = []
        state.nav_built = False
        state.telemetry = None
        state.profile_name = ""
        state.mtp_active = None
        state.actions = []
        state.actions_ready = True
        state.controls_ready = True    # the panel hides while disconnected
        state.rec_state = None
        state.rec_text = ""
        state.prof = None
        state.prof_rows = []
        state.prof_ready = True        # the profiler panel hides
        state.dirty.clear()        # drop queued edits; they can never land now
        state.desc_pending.clear()  # a queued fetch step sees the empty list and stops
    events_page.on_disconnect()
    _reset_plot_buffers()
    set_status(status)


def _controls_file(profile: str) -> Path:
    """~/.droid_config/controls_<profile>.json - the Dashboard's pinned actions for this robot."""
    safe = "".join(c if c.isalnum() else "_" for c in profile) or "robot"
    return Path.home() / ".droid_config" / f"controls_{safe}.json"


def _load_controls(path: Path) -> list[str]:
    """The pinned actions, or the profile's DASHBOARD_CONTROLS if never customised."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("controls"), list):
            return [str(c) for c in data["controls"]]
    except (OSError, ValueError):
        pass
    return list(getattr(profiles.active, "DASHBOARD_CONTROLS", []))


def _save_controls() -> None:
    with state.lock:
        path, controls = state.controls_file, list(state.controls)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"controls": controls}, indent=1), encoding="utf-8")
    except OSError:
        set_status(f"Couldn't save the Dashboard controls to {path}")


def pin_control(command: str) -> None:
    command = command.strip()
    with state.lock:
        if not command or command in state.controls:
            return
        state.controls.append(command)
        state.controls_ready = True
        state.actions_ready = True             # Actions tab menus say Pin/Unpin
    _save_controls()
    set_status(f"Pinned {command} to the Dashboard")


def unpin_control(command: str) -> None:
    with state.lock:
        if command not in state.controls:
            return
        state.controls.remove(command)
        state.controls_ready = True
        state.actions_ready = True
    _save_controls()
    set_status(f"Unpinned {command} from the Dashboard")


def reset_controls() -> None:
    with state.lock:
        state.controls = list(getattr(profiles.active, "DASHBOARD_CONTROLS", []))
        state.controls_ready = True
        state.actions_ready = True
        path = state.controls_file
    if path is not None:
        try:
            path.unlink()                      # no file = the profile's defaults
        except OSError:
            pass
    set_status("Dashboard controls reset to the defaults")


def _desc_cache_file(profile: str, fw: int) -> Path:
    """~/.droid_config/desc_<profile>_<fw>.json - descriptions never change within a firmware
    version, so each is fetched once per robot + version, ever."""
    safe = "".join(c if c.isalnum() else "_" for c in profile) or "robot"
    return Path.home() / ".droid_config" / f"desc_{safe}_{fw}.json"


def _load_desc_cache(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_desc_cache() -> None:
    with state.lock:
        path, data = state.desc_cache_file, dict(state.desc)
    if path is None or not data:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    except OSError:
        pass        # a cache that can't be written just means fetching again next time


def job_fetch_descriptions(keys: list[str]) -> None:
    """Queue description fetches for these keys (the page on screen). One key per io job, and
    each step re-queues itself, so a parameter edit queued meanwhile runs between two fetches
    instead of waiting for the whole page. Connecting stays as fast as before: nothing is
    fetched until a page is shown, and nothing twice per firmware version."""
    with state.lock:
        if state.desc_supported is False:
            return
        known = set(state.desc) | set(state.desc_pending)
        state.desc_pending.extend(k for k in keys if k not in known)
        if state.desc_fetching or not state.desc_pending:
            return
        state.desc_fetching = True
    io_q.put(_fetch_description_step)


def _fetch_description_step() -> None:
    with state.lock:
        key = state.desc_pending.pop(0) if state.desc_pending else None
    b = bot
    info = b.params.get(key) if (b is not None and key) else None
    if info is None or not link_is_up():
        with state.lock:
            state.desc_fetching = False
            state.desc_pending.clear()
        return
    try:
        text = b.describe_text(info.id)
    except ProtocolError:
        text = None
    with state.lock:
        if text is None:
            if state.desc_supported is None:      # first answer was an error: firmware without <KD>
                state.desc_supported = False
                state.desc_pending.clear()
        else:
            state.desc_supported = True
            state.desc[key] = text
            state.desc_new.add(key)
        more = bool(state.desc_pending)
        state.desc_fetching = more
    if more:
        io_q.put(_fetch_description_step)
    else:
        _save_desc_cache()


def job_connect(transport) -> None:
    def run() -> None:
        global bot
        set_status(f"Connecting {transport.describe()} ...")
        b = RobotClient(transport)
        # Report a link that dies on its own (cable pulled, BLE dropped). The
        # callback fires on a background thread, so hand the teardown to the io
        # worker rather than doing it there - closing a transport from inside
        # its own reader thread would try to join itself.
        transport.set_on_lost(
            lambda reason: io_q.put(lambda: _force_disconnect(f"Connection lost - {reason}")))
        b.open()
        try:
            fw = b.ping()
            set_status(f"Loading {b.count()} params ...")
            b.set_telemetry_handler(on_telemetry)
            params = b.refresh_params()  # also auto-detects profiles.active
            mtp = b.mtp_status()
            try:
                acts = b.actions()       # [] on firmware without <KA>
            except ProtocolError:
                acts = []
            try:
                prof = b.profiler()      # None on firmware without <KQ>
            except ProtocolError:
                prof = None
        except Exception:
            # Never leave an opened-but-unusable transport behind: it holds the
            # COM port and blocks the next connect attempt.
            try:
                b.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        bot = b
        with state.lock:
            state.connected = True
            state.fw = fw
            state.profile_name = profiles.active.NAME
            state.params = sorted(params.values(), key=lambda p: p.id)
            state.params_ready = True
            state.nav_built = False
            state.groups_ready = True
            state.mtp_active = mtp
            state.actions = acts
            state.actions_ready = True
            state.rec_state = None
            state.rec_text = ""
            state.prof = prof
            state.prof_rows = []
            state.prof_ready = True
            state.controls_file = _controls_file(profiles.active.NAME)
            state.controls = _load_controls(state.controls_file)
            state.controls_ready = True
            state.desc_cache_file = _desc_cache_file(profiles.active.NAME, fw)
            state.desc = _load_desc_cache(state.desc_cache_file)
            state.desc_supported = True if state.desc else None
            state.desc_pending.clear()
            state.desc_fetching = False
            state.desc_new = set()
        events_page.on_connect(b, fw, profiles.active.NAME)
        set_status(f"Connected - {profiles.active.NAME}, fw {fw // 10000}.{(fw // 100) % 100}.{fw % 100}, "
                   f"{len(params)} params")
    io_q.put(run)


def job_disconnect() -> None:
    io_q.put(lambda: _force_disconnect("Disconnected"))


def job_set(key: str, val: int) -> None:
    def run() -> None:
        if not link_is_up():
            return
        try:
            got = bot.set(key, val)
            set_confirm(f"\u2713 confirmed {key} = {got}")
            with state.lock:
                state.unsaved = True
        except ProtocolError as e:
            set_confirm(f"\u2717 REJECTED {key}={val}: {e}")
    io_q.put(run)


def job_save() -> None:
    def run() -> None:
        if not link_is_up():
            return
        ok = bot.save()
        set_status("Saved to SD (config.ini)" if ok else "SAVE FAILED")
        if ok:
            with state.lock:
                state.unsaved = False
    io_q.put(run)


def job_reload() -> None:
    def run() -> None:
        if not link_is_up():
            return
        params = bot.reload()
        with state.lock:
            state.params = sorted(params.values(), key=lambda p: p.id)
            state.params_ready = True
            state.nav_built = False
            state.unsaved = False
        set_status("Reloaded from SD")
    io_q.put(run)


def job_stream(hz: int, mask: int | None = None) -> None:
    def run() -> None:
        if not link_is_up():
            return
        bot.stream(hz, mask)
        set_status(f"Telemetry {hz} Hz" if hz else "Telemetry stopped")
    io_q.put(run)


def job_mtp_toggle() -> None:
    def run() -> None:
        if not link_is_up():
            return
        try:
            entering = not bool(state.mtp_active)
            active = bot.mtp_enter() if entering else bot.mtp_exit()
            with state.lock:
                state.mtp_active = active
            set_status("MTP mode ON - SD card exposed via USB" if active else "MTP mode OFF")
        except ProtocolError as e:
            set_status(f"MTP toggle failed: {e}")
    io_q.put(run)


def job_action(command: str) -> None:
    """Run one action on the robot (<KA,command>) - same vocabulary as its events.ini."""
    def run() -> None:
        if not link_is_up():
            return
        try:
            ok, msg = bot.run_action(command)
        except ProtocolError as e:
            set_confirm(f"✗ {command}: {e}")
            return
        set_confirm(("✓ " if ok else "✗ ") + f"{command}: {msg}")
        if command.strip().lower().startswith("rec:"):
            _rec_from_reply(msg)
    io_q.put(run)


def job_rec_poll() -> None:
    """rec:status once a second while no telemetry is streaming, so the Record button
    follows a take started from the transmitter or ended by its time limit."""
    def run() -> None:
        if not link_is_up():
            return
        try:
            _ok, msg = bot.run_action("rec:status")
        except ProtocolError:
            return
        _rec_from_reply(msg)
    io_q.put(run)


def job_profiler(command: str = "") -> None:
    """<KQ> for the Dashboard's profiler panel, with every section while profiling is on.
    command: "R" reset, "E1" / "E0" on / off (see RobotClient.profiler)."""
    def run() -> None:
        if not link_is_up():
            return
        try:
            st = bot.profiler(command)
            rows = bot.profiler_sections(st) if st is not None and st.enabled else None
        except ProtocolError as e:
            set_status(f"Profiler: {e}")
            return
        with state.lock:
            state.prof = st
            if rows is not None or command == "R":
                state.prof_rows = rows or []
            state.prof_ready = True
    io_q.put(run)


def job_refresh_actions() -> None:
    def run() -> None:
        if not link_is_up():
            return
        try:
            acts = bot.actions()
        except ProtocolError as e:
            set_status(f"Action list failed: {e}")
            return
        with state.lock:
            state.actions = acts
            state.actions_ready = True
        set_status(f"{len(acts)} actions")
    io_q.put(run)


# --------------------------------------------------------------------------- #
# Callbacks (main thread)
# --------------------------------------------------------------------------- #
def fmt_scaled(key: str, raw: int) -> str:
    s = scale_for_key(key)
    return "" if s == 1 else f"{raw / s:g}"


def on_param_change(sender, app_data, user_data) -> None:
    """Slider/input moved - mirror to the sibling widget, debounce the write."""
    key = user_data
    val = int(app_data)
    for tag in (f"sld_{key}", f"inp_{key}"):
        if dpg.does_item_exist(tag) and tag != sender:
            dpg.set_value(tag, val)
    if dpg.does_item_exist(f"val_{key}"):
        dpg.set_value(f"val_{key}", fmt_scaled(key, val))
    set_confirm(f"\u2026 sending {key} = {val}")
    with state.lock:
        state.dirty[key] = (val, time.time())


def on_nav_select(sender, app_data, user_data) -> None:
    with state.lock:
        state.current_page = user_data
        state.page_dirty = True
    for name in _nav_tags:
        if dpg.does_item_exist(f"nav_{name}"):
            dpg.set_value(f"nav_{name}", name == user_data)


def on_search(sender, app_data) -> None:
    with state.lock:
        state.search = str(app_data).strip().lower()
        state.page_dirty = True


def on_connect() -> None:
    if state.connected:
        job_disconnect()
        return
    if dpg.get_value("chk_ble"):
        # Combo entries are "<address>  <name>"; with none picked, connect by name.
        sel = (dpg.get_value("ble_combo") or "").strip()
        addr = sel.split()[0] if sel else ""
        job_connect(BleTransport(address=addr))
        return
    port = dpg.get_value("port_combo")
    if not port:
        set_status("Pick a COM port first")
        return
    job_connect(SerialTransport(port.split()[0], 115200))


def on_toggle_ble(sender, app_data) -> None:
    """Swap the connect bar between the USB port picker and the BLE device picker."""
    ble = bool(app_data)
    for tag in ("lbl_port", "port_combo", "btn_refresh"):
        dpg.configure_item(tag, show=not ble)
    for tag in ("lbl_ble", "ble_combo", "btn_scan"):
        dpg.configure_item(tag, show=ble)


def on_refresh_ports() -> None:
    ports = [f"{d}  {desc}" for d, desc in SerialTransport.list_ports()]
    dpg.configure_item("port_combo", items=ports)
    if ports:
        dpg.set_value("port_combo", ports[0])


def on_apply_hz() -> None:
    mask = 0
    for name, bit, fields in profiles.active.TELEMETRY_GROUPS:
        if fields and dpg.does_item_exist(f"grp_{name}") and dpg.get_value(f"grp_{name}"):
            mask |= bit
    mask &= implemented_mask()
    job_stream(int(dpg.get_value("stream_hz")), mask or implemented_mask())
    build_dashboard_fields(mask or implemented_mask())


def on_run_action(sender=None, app_data=None) -> None:
    text = (dpg.get_value("action_text") or "").strip()
    if text:
        job_action(text)


def flush_dirty() -> None:
    if not link_is_up():
        with state.lock:
            state.dirty.clear()
        return
    now = time.time()
    due: list[tuple[str, int]] = []
    with state.lock:
        for key, (val, t) in list(state.dirty.items()):
            if now - t >= DEBOUNCE_S:
                due.append((key, val))
                del state.dirty[key]
    for key, val in due:
        job_set(key, val)


# --------------------------------------------------------------------------- #
# Page building (main thread)
# --------------------------------------------------------------------------- #
_nav_tags: list[str] = []
_dash_fields: list[str] = []
# Dashboard render state, rebuilt by build_dashboard_fields():
_dash_items: list[tuple[str, str, str]] = []   # (item tag, telemetry key, format)
_plot_keys: set[str] = set()                   # keys the worker thread must buffer
_plot_axes: list[int] = []                     # indices of the plots actually built

LABEL_W, SLIDER_W, INPUT_W, SCALED_W = 220, 260, 110, 60


def _tip_text(info) -> str:
    """Row tooltip: the firmware's one-line description (when it has sent one), then key / id /
    range as before."""
    d = state.desc.get(info.key)
    return (f"{d}\n\n" if d else "") + f"{info.key}\nid {info.id}   range {info.vmin}..{info.vmax}"


def _param_rows(params: list, parent: str, label_of=None) -> None:
    """The standard label / slider / input / scaled-value table."""
    with dpg.table(parent=parent, header_row=False, policy=dpg.mvTable_SizingFixedFit,
                   row_background=True, borders_innerH=False, borders_outerH=False,
                   borders_innerV=False, borders_outerV=False):
        dpg.add_table_column(init_width_or_weight=LABEL_W, width_fixed=True)
        dpg.add_table_column(init_width_or_weight=SLIDER_W, width_fixed=True)
        dpg.add_table_column(init_width_or_weight=INPUT_W, width_fixed=True)
        dpg.add_table_column(init_width_or_weight=SCALED_W, width_fixed=True)
        for info in params:
            with dpg.table_row():
                label = label_of(info) if label_of else info.key
                dpg.add_text(label)
                with dpg.tooltip(dpg.last_item()):
                    dpg.add_text(_tip_text(info), tag=f"tip_{info.key}", wrap=420)
                dpg.add_slider_int(tag=f"sld_{info.key}", default_value=info.value,
                                   min_value=info.vmin, max_value=info.vmax, width=-1,
                                   callback=on_param_change, user_data=info.key)
                dpg.add_input_int(tag=f"inp_{info.key}", default_value=info.value,
                                  min_value=info.vmin, max_value=info.vmax, width=-1,
                                  min_clamped=True, max_clamped=True, on_enter=True,
                                  callback=on_param_change, user_data=info.key)
                dpg.add_text(fmt_scaled(info.key, info.value), tag=f"val_{info.key}")


def _build_subgrouped_page(params: list, parent: str) -> None:
    """Pages whose params naturally split by second key component
    (e.g. m.headR.* / m.tRing.*) get one sub-tab per subgroup."""
    glob, per = pages_mod.subgroups(params)
    if glob:
        dpg.add_text("General", parent=parent, color=(150, 200, 255))
        _param_rows(glob, parent)
        dpg.add_separator(parent=parent)
    with dpg.tab_bar(parent=parent):
        for sub in per:            # firmware order (0.5.0; was alphabetical)
            with dpg.tab(label=sub):
                # Strip the "page.sub." prefix - the tab already says which one.
                _param_rows(per[sub], dpg.last_item(),
                            label_of=lambda i: i.key.split(".", 2)[2])


def build_nav() -> None:
    """Left nav list - one entry per page, with counts."""
    global _nav_tags
    dpg.delete_item("nav_group", children_only=True)
    grouped = pages_mod.group_params(state.params)
    names = pages_mod.ordered_pages(grouped)
    _nav_tags = names
    for name in names:
        dpg.add_selectable(label=f"{name}  ({len(grouped[name])})", tag=f"nav_{name}",
                           parent="nav_group", callback=on_nav_select, user_data=name,
                           default_value=(name == state.current_page))
    if names and state.current_page not in names:
        state.current_page = names[0]
        dpg.set_value(f"nav_{names[0]}", True)
    state.page_dirty = True


def build_page() -> None:
    """Rebuild the right-hand pane for the selected page (or the search results)."""
    dpg.delete_item("page_group", children_only=True)
    if not state.params:
        dpg.add_text("Not connected.", parent="page_group")
        return

    if state.search:
        hits = [p for p in state.params if state.search in p.key.lower()]
        dpg.add_text(f'Search "{state.search}" - {len(hits)} of {len(state.params)}',
                     parent="page_group", color=(150, 200, 255))
        dpg.add_separator(parent="page_group")
        if hits:
            _param_rows(hits[:200], "page_group")
            if len(hits) > 200:
                dpg.add_text(f"... {len(hits) - 200} more, narrow the search",
                             parent="page_group", color=(200, 160, 100))
            job_fetch_descriptions([p.key for p in hits[:200]])
        else:
            dpg.add_text("No matching keys.", parent="page_group")
        return

    grouped = pages_mod.group_params(state.params)
    params = grouped.get(state.current_page, [])

    dpg.add_text(f"{state.current_page}  -  {len(params)} parameters",
                 parent="page_group", color=(150, 200, 255))
    dpg.add_separator(parent="page_group")

    if pages_mod.has_subgroups(params):
        _build_subgrouped_page(params, "page_group")
    else:
        _param_rows(params, "page_group")
    job_fetch_descriptions([p.key for p in params])


def build_telemetry_groups() -> None:
    """Rebuild the Dashboard's group checkboxes for whatever profile is active."""
    dpg.delete_item("telemetry_groups", children_only=True)
    avail = implemented_mask()
    for name, bit, fields in profiles.active.TELEMETRY_GROUPS:
        on = bool(fields) and bool(avail & bit)
        dpg.add_checkbox(label=name, tag=f"grp_{name}", parent="telemetry_groups",
                         default_value=on, enabled=on)
    build_dashboard_fields(avail)


def build_actions() -> None:
    """Rebuild the Actions tab from the firmware's catalogue: one row of buttons per group."""
    dpg.delete_item("actions_group", children_only=True)
    if not state.actions:
        dpg.add_text("Not connected, or this firmware has no action list (Orchestron 2.27.2+).",
                     parent="actions_group", color=(200, 160, 100))
        return
    groups: dict[str, list[tuple[str, str]]] = {}
    for group, label, command in state.actions:     # firmware order
        groups.setdefault(group, []).append((label, command))
    per_row = 4
    pinned = set(state.controls)
    for group, items in groups.items():
        dpg.add_text(group, parent="actions_group", color=(150, 200, 255))
        for i in range(0, len(items), per_row):
            row = dpg.add_group(horizontal=True, parent="actions_group")
            for label, command in items[i:i + per_row]:
                btn = dpg.add_button(label=label, parent=row, width=190, user_data=command,
                                     callback=lambda s, a, u: job_action(u))
                with dpg.tooltip(btn):
                    dpg.add_text(f"{command}\n\nRight-click to pin it to the Dashboard"
                                 + (" (pinned)" if command in pinned else ""))
                _pin_menu(btn, command, command in pinned)
        dpg.add_spacer(height=6, parent="actions_group")


def _pin_menu(item: int, command: str, pinned: bool) -> None:
    """Right-click menu on a button: pin it to the Dashboard, or unpin it."""
    with dpg.popup(item, mousebutton=dpg.mvMouseButton_Right):
        if pinned:
            dpg.add_menu_item(label="Unpin from Dashboard", user_data=command,
                              callback=lambda s, a, u: unpin_control(u))
        else:
            dpg.add_menu_item(label="Pin to Dashboard", user_data=command,
                              callback=lambda s, a, u: pin_control(u))


def _control_label(command: str) -> str:
    """A pinned command's button label: the firmware's label for it, else the command."""
    for _group, label, cmd in state.actions:
        if cmd == command:
            return label
    return command


def build_controls() -> None:
    """The Dashboard's Controls panel: one button per pinned action. Hidden while
    disconnected or when the firmware has no actions (<KA>)."""
    dpg.delete_item("controls_group", children_only=True)
    if not (state.connected and state.actions):
        return
    with dpg.group(horizontal=True, parent="controls_group"):
        dpg.add_text("Controls", color=(150, 200, 255))
        dpg.add_text("  right-click a button to unpin it; pin more from the Actions tab",
                     color=(130, 130, 130))
        dpg.add_button(label="Reset", small=True, callback=lambda: reset_controls())
    if not state.controls:
        dpg.add_text("Nothing pinned.", parent="controls_group", color=(200, 160, 100))
    per_row = 6
    for i in range(0, len(state.controls), per_row):
        row = dpg.add_group(horizontal=True, parent="controls_group")
        for command in state.controls[i:i + per_row]:
            # rec:toggle keeps a fixed tag so the render loop can show Record / Stop recording
            tag = "ctl_rec" if command == "rec:toggle" else 0
            btn = dpg.add_button(label=_control_label(command), parent=row, width=150, tag=tag,
                                 user_data=command, callback=lambda s, a, u: job_action(u))
            with dpg.tooltip(btn):
                dpg.add_text(command)
            _pin_menu(btn, command, True)
    dpg.add_separator(parent="controls_group")


def build_profiler() -> None:
    """The Dashboard's Loop profiler panel: the switch, the audio load and one row per
    section of the robot's main loop. Hidden on firmware without <KQ>."""
    st = state.prof
    dpg.configure_item("prof_header", show=st is not None)
    if st is None:
        return
    dpg.set_value("prof_on", st.enabled)
    dpg.set_value("prof_audio", f"Audio interrupt {st.audio_cpu:.1f}% of the CPU (max {st.audio_cpu_max:.1f}%)"
                                f"   audio blocks {st.blocks} (max {st.blocks_max} of {st.blocks_total})")
    dpg.delete_item("prof_table", children_only=True)
    if not st.enabled:
        dpg.add_text("Profiling is off. Switch it on to time each part of the robot's loop "
                     "(it costs the robot a few microseconds a pass).",
                     parent="prof_table", color=(200, 160, 100), wrap=900)
        if not state.prof_rows:
            return
    # Share of the loop: a section's total time over the whole loop's, when one is called "loop"
    loop = next((r for r in state.prof_rows if r.name == "loop"), None)
    loop_total = loop.dur_avg * loop.calls if loop else 0
    cols = ["Section", "Calls", "Time avg us", "Time max us", "% of loop", "Every (avg) ms", "Longest gap ms"]
    with dpg.table(parent="prof_table", header_row=True, policy=dpg.mvTable_SizingFixedFit,
                   row_background=True, borders_innerH=False):
        for c in cols:
            dpg.add_table_column(label=c)
        for r in state.prof_rows:
            color = (235, 180, 90) if r.dur_max >= PROF_SLOW_US else (220, 220, 220)
            share = (f"{100.0 * r.dur_avg * r.calls / loop_total:.1f}"
                     if loop_total and r is not loop and r.calls else "")
            with dpg.table_row():
                for text in (r.name, str(r.calls), str(r.dur_avg), str(r.dur_max), share,
                             f"{r.period_avg / 1000:.2f}" if r.calls > 1 else "",
                             f"{r.period_max / 1000:.2f}" if r.calls > 1 else ""):
                    dpg.add_text(text, color=color)


def _reset_plot_buffers() -> None:
    """Drop rolling history (called on connect/disconnect and on a layout rebuild)."""
    with state.lock:
        state.plot_t.clear()
        state.plot_series.clear()
        state.plot_t0 = None


def build_dashboard_fields(mask: int) -> None:
    """Rebuild the Dashboard for whichever telemetry groups are in `mask`.

    A profile may define DASHBOARD (see profiles/__init__.py) to get titled
    panels plus rolling plots; without it the generic flat "key: value" grid is
    used, so a Droid with no dashboard spec still shows everything it streams.
    Panels and plot series whose field is not in `mask` are dropped, so the spec
    never has to stay in sync with the group checkboxes.
    """
    global _dash_fields, _dash_items, _plot_keys, _plot_axes
    dpg.delete_item("dash_fields", children_only=True)
    dpg.delete_item("dash_plots", children_only=True)

    enabled: list[str] = []
    for _name, bit, group_fields in profiles.active.TELEMETRY_GROUPS:
        if mask & bit:
            enabled.extend(group_fields)
    enabled_set = set(enabled)
    _dash_fields = enabled
    _dash_items = []
    _plot_keys = set()
    _plot_axes = []

    spec = getattr(profiles.active, "DASHBOARD", None)
    panels = [(title, [f for f in fields if f[0] in enabled_set])
              for title, fields in (spec or {}).get("panels", [])] if spec else []
    panels = [(t, f) for t, f in panels if f]

    if not panels:
        # Generic fallback: flat grid of every streamed field.
        cols = 4
        with dpg.table(parent="dash_fields", header_row=False,
                       policy=dpg.mvTable_SizingFixedFit, row_background=True):
            for _ in range(cols):
                dpg.add_table_column(init_width_or_weight=130, width_fixed=True)
            for row_start in range(0, len(enabled), cols):
                with dpg.table_row():
                    for f in enabled[row_start:row_start + cols]:
                        dpg.add_text(f"{f}: -", tag=f"d_{f}")
                        _dash_items.append((f"d_{f}", f, f + ": {:g}"))
        return

    # Two panels per row keeps it readable without a very tall window.
    with dpg.table(parent="dash_fields", header_row=False,
                   policy=dpg.mvTable_SizingStretchProp, borders_innerV=True):
        dpg.add_table_column()
        dpg.add_table_column()
        for i in range(0, len(panels), 2):
            with dpg.table_row():
                for title, fields in panels[i:i + 2]:
                    with dpg.group():
                        dpg.add_text(title, color=(150, 200, 255))
                        with dpg.table(header_row=False, policy=dpg.mvTable_SizingFixedFit,
                                       row_background=True):
                            dpg.add_table_column(init_width_or_weight=125, width_fixed=True)
                            dpg.add_table_column(init_width_or_weight=95, width_fixed=True)
                            for key, label, fmt in fields:
                                with dpg.table_row():
                                    dpg.add_text(label)
                                    dpg.add_text("-", tag=f"d_{key}")
                                    _dash_items.append((f"d_{key}", key, fmt))
                if len(panels[i:i + 2]) == 1:
                    dpg.add_text("")   # keep the row shape

    for idx, (title, ylabel, series) in enumerate((spec or {}).get("plots", [])):
        series = [(k, lbl) for k, lbl in series if k in enabled_set]
        if not series:
            continue
        with dpg.plot(parent="dash_plots", label=title, height=210, width=-1):
            dpg.add_plot_legend()
            dpg.add_plot_axis(dpg.mvXAxis, label="t (s)", tag=f"plx_{idx}")
            with dpg.plot_axis(dpg.mvYAxis, label=ylabel, tag=f"ply_{idx}"):
                for key, lbl in series:
                    dpg.add_line_series([], [], label=lbl, tag=f"ser_{idx}_{key}")
                    _plot_keys.add(key)
        _plot_axes.append(idx)
    _reset_plot_buffers()


def update_dashboard(fr: TelemetryFrame) -> None:
    spec = getattr(profiles.active, "DASHBOARD", None)
    labels = (spec or {}).get("state_labels") if spec else None
    if labels:
        label, color = labels.get(int(fr.raw.get("mode", -1)), ("?", (200, 200, 200)))
        dpg.set_value("d_mode", f"State: {label}")
        dpg.configure_item("d_mode", color=color)
    else:
        dpg.set_value("d_mode", f"Mode: {fr.mode_name}")
    for tag, key, fmt in _dash_items:
        if dpg.does_item_exist(tag):
            try:
                dpg.set_value(tag, fmt.format(fr.get(key)))
            except (ValueError, TypeError):
                dpg.set_value(tag, f"{fr.get(key):g}")


def update_plots() -> None:
    """Push the rolling buffers into the line series (main thread only)."""
    if not _plot_keys:
        return
    with state.lock:
        xs = list(state.plot_t)
        data = {k: list(v) for k, v in state.plot_series.items()}
    if len(xs) < 2:
        return
    for idx in _plot_axes:
        for key in _plot_keys:
            tag = f"ser_{idx}_{key}"
            ys = data.get(key)
            if ys and dpg.does_item_exist(tag):
                n = min(len(xs), len(ys))
                dpg.set_value(tag, [xs[-n:], ys[-n:]])
        if dpg.does_item_exist(f"plx_{idx}"):
            dpg.set_axis_limits(f"plx_{idx}", xs[0], xs[-1])
            dpg.fit_axis_data(f"ply_{idx}")


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def build_layout() -> None:
    # Red Record buttons while a take runs (bound/unbound in the render loop)
    with dpg.theme(tag="theme_recording"):
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, (150, 35, 35))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (185, 50, 50))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (210, 60, 60))

    with dpg.window(tag="root"):
        with dpg.group(horizontal=True):
            dpg.add_checkbox(label="BLE", tag="chk_ble", callback=on_toggle_ble)
            dpg.add_text("Port:", tag="lbl_port")
            dpg.add_combo([], tag="port_combo", width=250)
            dpg.add_button(label="Refresh", tag="btn_refresh", callback=on_refresh_ports)
            dpg.add_text("Device:", tag="lbl_ble", show=False)
            dpg.add_combo([], tag="ble_combo", width=250, show=False)
            dpg.add_button(label="Scan", tag="btn_scan", show=False,
                           callback=lambda: job_ble_scan())
            dpg.add_button(label="Connect", tag="btn_connect", callback=on_connect)
            dpg.add_button(label="Enter MTP Mode", tag="btn_mtp", callback=lambda: job_mtp_toggle(),
                          enabled=False)
            # Shown when the firmware lists rec:toggle (Orchestron 2.27.2+)
            dpg.add_button(label="Record", tag="btn_rec", width=120, show=False,
                           callback=lambda: job_action("rec:toggle"))
            dpg.add_text("", tag="rec_text", color=(235, 90, 90))
            dpg.add_spacer(width=40)
            dpg.add_text("", tag="confirm_text", color=(150, 220, 150))
        dpg.add_text("Disconnected", tag="status_text", color=(200, 200, 120))
        dpg.add_separator()

        with dpg.tab_bar():
            with dpg.tab(label="Config"):
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Save to SD", callback=lambda: job_save())
                    dpg.add_button(label="Reload from SD", callback=lambda: job_reload())
                    dpg.add_text("   Search:")
                    dpg.add_input_text(tag="search_box", width=260, hint="part of a key name",
                                       callback=on_search)
                    dpg.add_spacer(width=20)
                    dpg.add_text("", tag="unsaved_text", color=(230, 180, 90))
                dpg.add_separator()
                with dpg.group(horizontal=True):
                    with dpg.child_window(width=230, tag="nav_panel"):
                        dpg.add_text("GROUPS", color=(160, 160, 160))
                        dpg.add_separator()
                        dpg.add_group(tag="nav_group")
                    with dpg.child_window(tag="page_panel"):
                        dpg.add_group(tag="page_group")

            with dpg.tab(label="Dashboard"):
                dpg.add_group(tag="controls_group")    # pinned action buttons (build_controls)
                with dpg.group(horizontal=True):
                    dpg.add_text("Rate Hz:")
                    dpg.add_input_int(tag="stream_hz", default_value=DEFAULT_STREAM_HZ,
                                      width=90, min_value=0, max_value=100,
                                      min_clamped=True, max_clamped=True)
                    dpg.add_button(label="Apply", callback=on_apply_hz)
                    dpg.add_button(label="Stop", callback=lambda: job_stream(0))
                dpg.add_text("Groups:", color=(160, 160, 160))
                dpg.add_group(horizontal=True, tag="telemetry_groups")
                dpg.add_separator()
                dpg.add_text("State: -", tag="d_mode")
                with dpg.child_window(border=False, height=-1):
                    # Firmware with <KQ> (Orchestron 2.34+); polled only while open
                    with dpg.collapsing_header(label="Loop profiler", tag="prof_header",
                                               default_open=False, show=False):
                        with dpg.group(horizontal=True):
                            dpg.add_checkbox(label="Profiling on", tag="prof_on",
                                             callback=lambda s, a: job_profiler("E1" if a else "E0"))
                            dpg.add_button(label="Reset", callback=lambda: job_profiler("R"))
                            with dpg.tooltip(dpg.last_item()):
                                dpg.add_text("Clear every section's statistics and the audio maxima")
                            dpg.add_text("", tag="prof_audio", color=(160, 160, 160))
                        dpg.add_group(tag="prof_table")
                        dpg.add_spacer(height=6)
                    dpg.add_group(tag="dash_fields")
                    dpg.add_spacer(height=6)
                    dpg.add_group(tag="dash_plots")

            with dpg.tab(label="Actions"):
                dpg.add_text("Run things on the robot. Any action its events.ini understands works "
                             "here too, e.g. seq:wave, wavA:2001, randomA:2, mode:control, rec:toggle.",
                             wrap=900, color=(160, 160, 160))
                with dpg.group(horizontal=True):
                    dpg.add_input_text(tag="action_text", width=300, hint="an action, e.g. seq:wave",
                                       on_enter=True, callback=on_run_action)
                    dpg.add_button(label="Run", callback=on_run_action)
                    dpg.add_button(label="Pin", callback=lambda: pin_control(
                        (dpg.get_value("action_text") or "").strip()))
                    with dpg.tooltip(dpg.last_item()):
                        dpg.add_text("Pin the typed action to the Dashboard as a button")
                    dpg.add_button(label="Refresh list", callback=lambda: job_refresh_actions())
                dpg.add_separator()
                with dpg.child_window(border=False, height=-1):
                    dpg.add_group(tag="actions_group")

            with dpg.tab(label="Events"):
                events_page.build()


def main() -> int:
    global io_running
    events_page.init(io_put=io_q.put, get_bot=lambda: bot if link_is_up() else None,
                     set_status=set_status, param_keys=lambda: [p.key for p in state.params],
                     on_saved=job_refresh_actions)   # rules name the sequences the Actions tab lists
    dpg.create_context()
    build_layout()
    dpg.create_viewport(title=f"Droid Config v{__version__}", width=1100, height=760)
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("root", True)
    on_refresh_ports()

    worker = threading.Thread(target=io_worker, daemon=True)
    worker.start()

    while dpg.is_dearpygui_running():
        with state.lock:
            status = state.status
            connected = state.connected
            need_nav = state.params_ready and not state.nav_built
            need_page = state.page_dirty
            need_groups = state.groups_ready
            fr = state.telemetry
            confirm = state.confirm
            unsaved = state.unsaved
            mtp_active = state.mtp_active
            need_actions = state.actions_ready
            state.actions_ready = False
            need_controls = state.controls_ready
            state.controls_ready = False
            need_prof = state.prof_ready
            state.prof_ready = False
            has_prof = state.prof is not None
            can_record = any(cmd == "rec:toggle" for _g, _l, cmd in state.actions)
            rec_state = state.rec_state
            rec_text = state.rec_text
            ble_devs = state.ble_devices if state.ble_ready else None
            state.ble_ready = False
        if ble_devs is not None:
            items = [f"{a}  {n}" for a, n in ble_devs]
            dpg.configure_item("ble_combo", items=items)
            if items:  # preselect the HM-10 if it advertised its default name
                dpg.set_value("ble_combo", next(
                    (i for i in items if HM10_DEFAULT_NAME.lower() in i.lower()), items[0]))
        dpg.set_value("status_text", status)
        dpg.configure_item("btn_connect", label="Disconnect" if connected else "Connect")
        dpg.configure_item("btn_mtp", enabled=connected,
                           label="Exit MTP Mode" if mtp_active else "Enter MTP Mode")
        dpg.configure_item("btn_rec", show=connected and can_record,
                           enabled=rec_state != 2,      # writing the CSV: wait
                           label="Stop recording" if rec_state == 1 else "Record")
        dpg.set_value("rec_text", rec_text if connected else "")
        if need_actions:
            build_actions()
        if need_controls:
            build_controls()
        if need_prof:
            build_profiler()
        rec_theme = "theme_recording" if (connected and rec_state == 1) else 0
        dpg.bind_item_theme("btn_rec", rec_theme)
        if dpg.does_item_exist("ctl_rec"):
            dpg.configure_item("ctl_rec", enabled=rec_state != 2,
                               label="Stop recording" if rec_state == 1 else "Record")
            dpg.bind_item_theme("ctl_rec", rec_theme)
        now = time.time()
        if connected and can_record and now - state.last_telemetry > 2.0 \
                and now >= state.rec_poll_due and io_q.empty():
            state.rec_poll_due = now + 1.0
            job_rec_poll()
        if connected and has_prof and dpg.get_value("prof_header") \
                and now >= state.prof_poll_due and io_q.empty():
            ble = isinstance(getattr(bot, "t", None), BleTransport)
            state.prof_poll_due = now + (PROF_POLL_BLE_S if ble else PROF_POLL_S)
            job_profiler()
        dpg.set_value("confirm_text", confirm)
        dpg.configure_item("confirm_text",
                           color=(220, 120, 120) if confirm.startswith("\u2717") else (150, 220, 150))
        dpg.set_value("unsaved_text",
                      "\u26a0 unsaved changes - not written to SD until 'Save to SD'" if unsaved else "")

        if need_groups:
            build_telemetry_groups()
            with state.lock:
                state.groups_ready = False
        if need_nav:
            build_nav()
            with state.lock:
                state.nav_built = True
                state.params_ready = False
        if need_page:
            build_page()
            with state.lock:
                state.page_dirty = False
        with state.lock:                       # descriptions fetched in the background
            new_desc, state.desc_new = state.desc_new, set()
        if new_desc:
            by_key = {p.key: p for p in state.params}
            for key in new_desc:
                if key in by_key and dpg.does_item_exist(f"tip_{key}"):
                    dpg.set_value(f"tip_{key}", _tip_text(by_key[key]))
        if fr is not None:
            update_dashboard(fr)
            with state.lock:
                state.telemetry = None
        update_plots()

        events_page.update()
        flush_dirty()
        dpg.render_dearpygui_frame()

    io_running = False
    if bot is not None:
        try:
            bot.stream(0)
            bot.close()
        except Exception:  # noqa: BLE001
            pass
    dpg.destroy_context()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
