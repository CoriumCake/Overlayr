"""Snipping-style source-region selector."""

from __future__ import annotations

import os
import tkinter as tk
from typing import Any, Callable, Mapping, Optional

from ..core.region_selection import (
    local_selection_to_screen,
    normalize_drag_selection,
)
from ..utils.display_utils import format_tk_geometry

Bounds = dict[str, int]


class ScreenRegionSelector(tk.Toplevel):
    """Topmost drag surface constrained to the selected capture source."""

    _TRANSPARENT_KEY = "#ff00ff"

    def __init__(
        self,
        parent: tk.Misc,
        source_bounds: Mapping[str, Any],
        *,
        on_select: Callable[[Bounds], None],
        on_cancel: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self._source_bounds = {
            "left": int(source_bounds["left"]),
            "top": int(source_bounds["top"]),
            "width": max(1, int(source_bounds["width"])),
            "height": max(1, int(source_bounds["height"])),
        }
        self._on_select = on_select
        self._on_cancel = on_cancel
        self._drag_start: Optional[tuple[int, int]] = None
        self._closed = False

        self.withdraw()
        self.title("Overlayr Area Selection")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(background=self._TRANSPARENT_KEY)
        if os.name == "nt":
            self.attributes("-transparentcolor", self._TRANSPARENT_KEY)
        else:
            self.attributes("-alpha", 0.35)

        self.canvas = tk.Canvas(
            self,
            background=self._TRANSPARENT_KEY,
            cursor="crosshair",
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self._draw_shade()
        self._instruction_id = self.canvas.create_text(
            24,
            24,
            anchor="nw",
            text="ลากเพื่อเลือกพื้นที่แปล  •  Esc ยกเลิก",
            fill="white",
            font=("Segoe UI", -18, "bold"),
            tags=("instruction",),
        )

        self.canvas.bind("<ButtonPress-1>", self._start_drag)
        self.canvas.bind("<B1-Motion>", self._update_drag)
        self.canvas.bind("<ButtonRelease-1>", self._finish_drag)
        self.bind("<Escape>", lambda _event: self.cancel())
        self.bind("<ButtonPress-3>", lambda _event: self.cancel())

        self.geometry(format_tk_geometry(self._source_bounds))
        self.update_idletasks()
        self._place_at_physical_bounds()
        self.deiconify()
        self.lift()
        self.focus_force()
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _place_at_physical_bounds(self) -> None:
        """Use Win32 positioning so monitors left/above primary stay exact."""
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            if not hwnd:
                hwnd = self.winfo_id()

            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            user32.SetWindowPos.restype = wintypes.BOOL
            hwnd_topmost = wintypes.HWND(-1)
            show_window = 0x0040
            user32.SetWindowPos(
                hwnd,
                hwnd_topmost,
                self._source_bounds["left"],
                self._source_bounds["top"],
                self._source_bounds["width"],
                self._source_bounds["height"],
                show_window,
            )

            # Keep the temporary selector out of Alt-Tab without preventing focus.
            gwl_exstyle = -20
            ws_ex_toolwindow = 0x00000080
            style = user32.GetWindowLongW(hwnd, gwl_exstyle)
            user32.SetWindowLongW(hwnd, gwl_exstyle, style | ws_ex_toolwindow)
        except Exception:
            # Tk geometry remains a usable fallback.
            pass

    def _event_point(self, event: tk.Event) -> tuple[int, int]:
        width = self._source_bounds["width"]
        height = self._source_bounds["height"]
        return (
            max(0, min(width, int(event.x))),
            max(0, min(height, int(event.y))),
        )

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_start = self._event_point(event)
        self._draw_selection(self._drag_start)

    def _update_drag(self, event: tk.Event) -> None:
        if self._drag_start is None:
            return
        self._draw_selection(self._event_point(event))

    def _finish_drag(self, event: tk.Event) -> None:
        if self._drag_start is None:
            return

        selection = normalize_drag_selection(
            start=self._drag_start,
            end=self._event_point(event),
            canvas_size=(
                self._source_bounds["width"],
                self._source_bounds["height"],
            ),
        )
        if selection is None:
            self._drag_start = None
            self._draw_shade()
            return

        absolute_selection = local_selection_to_screen(
            selection,
            self._source_bounds,
        )
        self._close()
        self._on_select(absolute_selection)

    def _draw_selection(self, current: tuple[int, int]) -> None:
        start_x, start_y = self._drag_start or current
        current_x, current_y = current
        left, right = sorted((start_x, current_x))
        top, bottom = sorted((start_y, current_y))
        self._draw_shade((left, top, right, bottom))
        self.canvas.create_rectangle(
            left,
            top,
            right,
            bottom,
            outline="#35d9ff",
            width=3,
            tags=("selection",),
        )
        self.canvas.create_text(
            min(right + 8, self._source_bounds["width"] - 8),
            min(bottom + 8, self._source_bounds["height"] - 8),
            anchor="se",
            text=f"{right - left} × {bottom - top}",
            fill="white",
            font=("Segoe UI", -14, "bold"),
            tags=("selection",),
        )
        self.canvas.tag_raise("instruction")

    def _draw_shade(
        self,
        selection: Optional[tuple[int, int, int, int]] = None,
    ) -> None:
        self.canvas.delete("shade")
        self.canvas.delete("selection")
        width = self._source_bounds["width"]
        height = self._source_bounds["height"]
        if selection is None:
            rectangles = [(0, 0, width, height)]
        else:
            left, top, right, bottom = selection
            rectangles = [
                (0, 0, width, top),
                (0, top, left, bottom),
                (right, top, width, bottom),
                (0, bottom, width, height),
            ]

        for x1, y1, x2, y2 in rectangles:
            if x2 <= x1 or y2 <= y1:
                continue
            self.canvas.create_rectangle(
                x1,
                y1,
                x2,
                y2,
                fill="black",
                outline="",
                stipple="gray50",
                tags=("shade",),
            )
        if hasattr(self, "_instruction_id"):
            self.canvas.tag_raise("instruction")

    def cancel(self) -> None:
        if self._closed:
            return
        self._close()
        self._on_cancel()

    def dismiss(self) -> None:
        """Close without invoking callbacks during application shutdown."""
        self._close()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
