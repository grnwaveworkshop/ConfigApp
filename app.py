"""Robot config app - Dear PyGui desktop UI over the <K...> serial protocol.
Works against any robot in the family (RX-80B, Orchestron, T4-IMU) - the
connected robot's profile (telemetry layout + scale table) is auto-detected
after the param sweep, see profiles/__init__.py.

    py app.py

~200+ config params is too many for one scrolling list, so the Config tab is
paginated: a left nav of groups derived purely from the key namespaces (see
pages.py) - no per-project curation table, so a new key prefix in any
project's ConfigParams.def shows up automatically. Pages whose params
naturally split into sub-groups (e.g. "m.headR.*" vs "m.tRing.*") get
sub-tabs; everything else is one flat table. A search box cuts across all
pages when you know part of a key name.

The Dashboard tab is also generic: it shows whatever telemetry fields the
detected profile defines, rather than hardcoding field names for one robot.

Threading model: all robot I/O runs on a single background worker thread fed
by a job queue; the render loop polls shared state, rebuilds the page when
needed, and flushes debounced edits. Widget *creation* happens only on the
render-loop (main) thread.
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import dearpygui.dearpygui as dpg                                        # noqa: E402

import pages as pages_mod                                                # noqa: E402
import profiles                                                          # noqa: E402
from client import RobotClient                                           # noqa: E402
from models import TelemetryFrame, scale_for_key                         # noqa: E402
from protocol import ProtocolError                                       # noqa: E402
from transport import SerialTransport                                    # noqa: E402from version import __version__                                          # noqa: E402
DEBOUNCE_S = 0.12
DEFAULT_STREAM_HZ = 10


class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
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
def on_telemetry(fr: TelemetryFrame) -> None:
    with state.lock:
        state.telemetry = fr


def job_connect(transport) -> None:
    def run() -> None:
        global bot
        set_status(f"Connecting {transport.describe()} ...")
        b = RobotClient(transport)
        b.open()
        fw = b.ping()
        set_status(f"Loading {b.count()} params ...")
        b.set_telemetry_handler(on_telemetry)
        params = b.refresh_params()  # also auto-detects profiles.active
        bot = b
        with state.lock:
            state.connected = True
            state.fw = fw
            state.profile_name = profiles.active.NAME
            state.params = sorted(params.values(), key=lambda p: p.id)
            state.params_ready = True
            state.nav_built = False
            state.groups_ready = True
        set_status(f"Connected - {profiles.active.NAME}, fw {fw // 10000}.{(fw // 100) % 100}.{fw % 100}, "
                   f"{len(params)} params")
    io_q.put(run)


def job_disconnect() -> None:
    def run() -> None:
        global bot
        if bot is not None:
            try:
                bot.stream(0)
                bot.close()
            finally:
                bot = None
        with state.lock:
            state.connected = False
            state.params = []
            state.nav_built = False
            state.telemetry = None
            state.profile_name = ""
        set_status("Disconnected")
    io_q.put(run)


def job_set(key: str, val: int) -> None:
    def run() -> None:
        if bot is None:
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
        if bot is None:
            return
        ok = bot.save()
        set_status("Saved to SD (config.ini)" if ok else "SAVE FAILED")
        if ok:
            with state.lock:
                state.unsaved = False
    io_q.put(run)


def job_reload() -> None:
    def run() -> None:
        if bot is None:
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
        if bot is None:
            return
        bot.stream(hz, mask)
        set_status(f"Telemetry {hz} Hz" if hz else "Telemetry stopped")
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
    port = dpg.get_value("port_combo")
    if not port:
        set_status("Pick a COM port first")
        return
    job_connect(SerialTransport(port.split()[0], 115200))


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

LABEL_W, SLIDER_W, INPUT_W, SCALED_W = 220, 260, 110, 60


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
                    dpg.add_text(f"{info.key}\nid {info.id}   range {info.vmin}..{info.vmax}")
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
        for sub in sorted(per):
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


def build_telemetry_groups() -> None:
    """Rebuild the Dashboard's group checkboxes for whatever profile is active."""
    dpg.delete_item("telemetry_groups", children_only=True)
    for name, _bit, fields in profiles.active.TELEMETRY_GROUPS:
        dpg.add_checkbox(label=name, tag=f"grp_{name}", parent="telemetry_groups",
                         default_value=bool(fields), enabled=bool(fields))
    build_dashboard_fields(profiles.active.TELEMETRY_MASK_IMPLEMENTED)


def build_dashboard_fields(mask: int) -> None:
    """Rebuild the Dashboard's field table for whichever groups are in `mask` -
    generic across robots, since field names come entirely from the profile."""
    global _dash_fields
    dpg.delete_item("dash_fields", children_only=True)
    fields: list[str] = []
    for _name, bit, group_fields in profiles.active.TELEMETRY_GROUPS:
        if mask & bit:
            fields.extend(group_fields)
    _dash_fields = fields
    cols = 4  # keeps it compact for the wider telemetry groups
    with dpg.table(parent="dash_fields", header_row=False,
                   policy=dpg.mvTable_SizingFixedFit, row_background=True):
        for _ in range(cols):
            dpg.add_table_column(init_width_or_weight=130, width_fixed=True)
        for row_start in range(0, len(fields), cols):
            with dpg.table_row():
                for f in fields[row_start:row_start + cols]:
                    dpg.add_text(f"{f}: -", tag=f"d_{f}")


def update_dashboard(fr: TelemetryFrame) -> None:
    dpg.set_value("d_mode", f"Mode: {fr.mode_name}")
    for f in _dash_fields:
        if dpg.does_item_exist(f"d_{f}"):
            dpg.set_value(f"d_{f}", f"{f}: {fr.get(f):g}")


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def build_layout() -> None:
    with dpg.window(tag="root"):
        with dpg.group(horizontal=True):
            dpg.add_text("Port:")
            dpg.add_combo([], tag="port_combo", width=250)
            dpg.add_button(label="Refresh", callback=on_refresh_ports)
            dpg.add_button(label="Connect", tag="btn_connect", callback=on_connect)
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
                dpg.add_text("Mode: -", tag="d_mode")
                dpg.add_group(tag="dash_fields")


def main() -> int:
    global io_running
    dpg.create_context()
    build_layout()
    dpg.create_viewport(title=f"Robot Config v{__version__}", width=1100, height=760)
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
        dpg.set_value("status_text", status)
        dpg.configure_item("btn_connect", label="Disconnect" if connected else "Connect")
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
        if fr is not None:
            update_dashboard(fr)
            with state.lock:
                state.telemetry = None

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
