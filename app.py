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
whatever telemetry fields it streams.

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

import pages as pages_mod                                                # noqa: E402
import profiles                                                          # noqa: E402
from client import RobotClient                                           # noqa: E402
from models import TelemetryFrame, scale_for_key                         # noqa: E402
from protocol import ProtocolError                                       # noqa: E402
from transport import (HM10_DEFAULT_NAME, BleTransport,                   # noqa: E402
                       SerialTransport, TransportError)
from version import __version__                                          # noqa: E402

DEBOUNCE_S = 0.12
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


def on_telemetry(fr: TelemetryFrame) -> None:
    with state.lock:
        state.telemetry = fr
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
        state.dirty.clear()        # drop queued edits; they can never land now
        state.desc_pending.clear()  # a queued fetch step sees the empty list and stops
    _reset_plot_buffers()
    set_status(status)


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
            state.desc_cache_file = _desc_cache_file(profiles.active.NAME, fw)
            state.desc = _load_desc_cache(state.desc_cache_file)
            state.desc_supported = True if state.desc else None
            state.desc_pending.clear()
            state.desc_fetching = False
            state.desc_new = set()
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
    job_stream(int(dpg.get_value("stream_hz")), mask or profiles.active.TELEMETRY_MASK_IMPLEMENTED)
    build_dashboard_fields(mask or profiles.active.TELEMETRY_MASK_IMPLEMENTED)


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
    for name, _bit, fields in profiles.active.TELEMETRY_GROUPS:
        dpg.add_checkbox(label=name, tag=f"grp_{name}", parent="telemetry_groups",
                         default_value=bool(fields), enabled=bool(fields))
    build_dashboard_fields(profiles.active.TELEMETRY_MASK_IMPLEMENTED)


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
                    dpg.add_group(tag="dash_fields")
                    dpg.add_spacer(height=6)
                    dpg.add_group(tag="dash_plots")


def main() -> int:
    global io_running
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
