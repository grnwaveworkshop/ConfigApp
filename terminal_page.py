"""Terminal tab (py app.py --terminal): what the robot prints on its console.

On USB the robot's console (Serial.print) shares the port with the <K...> replies, and while
the app holds the port no serial monitor can open it. The protocol layer hands the console
part to LOG (console.py); this tab shows it: the boot messages after Reboot, [REMOTE] lines,
warnings and errors, the profiler's reports. The log runs across reconnects, with a line
where the link closed and opened again.

Read-only on purpose: keys typed at the serial menu would take the port's input away from
the app. Over BLE only what the robot sends to that UART shows (Orchestron's console is USB).

Threading as app.py: the reader thread only feeds LOG; widgets are created in update(), on
the render loop.
"""
from __future__ import annotations

import time
from collections import deque
from pathlib import Path

import dearpygui.dearpygui as dpg

from console import ConsoleLog

LOG = ConsoleLog()
SAVE_DIR = Path.home() / ".droid_config" / "terminal"
FOLLOW_SLACK = 30               # px from the bottom that still counts as "at the bottom"

COL_DIM = (140, 140, 140)       # the app's own lines
COL_BAD = (235, 110, 110)
COL_WARN = (230, 180, 90)


class _View:
    def __init__(self) -> None:
        self.items: deque[int | str] = deque()   # one text item per line, oldest first
        self.seen = 0                            # LOG lines already shown
        self.partial = ""
        self.clear = False                       # Clear pressed: update() does it


_V = _View()


def _colour(text: str, from_app: bool) -> tuple[int, int, int] | None:
    if from_app:
        return COL_DIM
    if "ERROR" in text or "FAIL" in text:
        return COL_BAD
    if "WARN" in text:
        return COL_WARN
    return None


def _clear() -> None:
    """On the render loop: a button callback must not delete items update() is adding."""
    _V.clear = False
    LOG.clear()
    for item in _V.items:
        dpg.delete_item(item)
    _V.items.clear()
    _V.partial = ""
    dpg.set_value("term_partial", "")
    dpg.set_value("term_note", "")


def _copy() -> None:
    dpg.set_clipboard_text(LOG.text())
    dpg.set_value("term_note", "Copied to the clipboard")


def _save() -> None:
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    path = SAVE_DIR / time.strftime("terminal_%Y%m%d_%H%M%S.txt")
    path.write_text(LOG.text() + "\n", encoding="utf-8")
    dpg.set_value("term_note", f"Saved {path}")


def build() -> None:
    """The tab's contents; call inside `with dpg.tab(...)`."""
    dpg.add_text("What the robot prints on its console: boot messages (Reboot, then watch), "
                 "warnings, errors, [REMOTE] lines. USB shows everything; over BLE only what "
                 "the robot sends to that port. Scroll up to stop following.",
                 wrap=900, color=COL_DIM)
    with dpg.group(horizontal=True):
        dpg.add_button(label="Clear", callback=lambda: setattr(_V, "clear", True))
        dpg.add_button(label="Copy all", callback=_copy)
        dpg.add_button(label="Save", callback=_save)
        with dpg.tooltip(dpg.last_item()):
            dpg.add_text(f"Write it to a text file in {SAVE_DIR}")
        dpg.add_text("", tag="term_note", color=COL_DIM)
    with dpg.child_window(tag="term_lines", height=-1, horizontal_scrollbar=True):
        dpg.add_text("", tag="term_partial")     # the line being printed; lines go above it
    with dpg.theme() as dense:                   # lines close together, as in a terminal
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 1, category=dpg.mvThemeCat_Core)
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 4, 0, category=dpg.mvThemeCat_Core)
    dpg.bind_item_theme("term_lines", dense)


def update() -> None:
    """Add the lines that came in since the last frame (render loop)."""
    if _V.clear:
        _clear()
    lines, partial, count = LOG.new_lines(_V.seen)
    if count == _V.seen and partial == _V.partial:
        return
    at_bottom = dpg.get_y_scroll("term_lines") >= dpg.get_y_scroll_max("term_lines") - FOLLOW_SLACK
    for text, from_app in lines:
        colour = _colour(text, from_app)
        kw = {"color": colour} if colour else {}
        _V.items.append(dpg.add_text(text, parent="term_lines", before="term_partial", **kw))
    while len(_V.items) > LOG.max_lines:
        dpg.delete_item(_V.items.popleft())
    dpg.set_value("term_partial", partial)
    _V.seen = count
    _V.partial = partial
    if at_bottom:
        dpg.set_y_scroll("term_lines", -1.0, when=dpg.mvSetScrollFlags_Delayed)
