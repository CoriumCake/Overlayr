"""Helpers for DPI-stable multi-monitor overlay placement."""

import os
from typing import Any, Dict, Optional


def format_tk_geometry(bounds: Dict[str, Any]) -> str:
    """Format physical-pixel bounds using valid signed Tk coordinates."""
    width = max(1, int(round(bounds["width"])))
    height = max(1, int(round(bounds["height"])))
    left = int(round(bounds["left"]))
    top = int(round(bounds["top"]))
    return f"{width}x{height}{left:+d}{top:+d}"


def resolve_monitor_bounds(target: Dict[str, Any], monitors) -> Optional[Dict[str, int]]:
    """Resolve a selected display by stored physical bounds, then by index."""
    stored = target.get("bounds")
    if stored:
        return {
            "left": int(stored["left"]),
            "top": int(stored["top"]),
            "width": int(stored["width"]),
            "height": int(stored["height"]),
        }

    monitor_index = int(target.get("id", -1)) + 1
    if 0 < monitor_index < len(monitors):
        monitor = monitors[monitor_index]
        return {
            "left": int(monitor["left"]),
            "top": int(monitor["top"]),
            "width": int(monitor["width"]),
            "height": int(monitor["height"]),
        }
    return None


def tk_pixel_font(family: str, pixel_size: int, weight: str = "normal"):
    """Return a Tk font tuple whose negative size is interpreted as pixels."""
    size = max(1, int(round(pixel_size)))
    return family, -size, weight


def should_show_window_overlay(
    *,
    target_exists: bool,
    target_visible: bool,
    target_minimized: bool,
    foreground_is_target: bool,
    target_monitor: Optional[int],
    foreground_monitor: Optional[int],
) -> bool:
    """Keep an overlay visible when focus moves to a different monitor.

    A foreground window on the target monitor can obscure the translated
    window, so the overlay is hidden in that case. Unknown monitor information
    fails open to avoid interrupting translation.
    """
    if not target_exists or not target_visible or target_minimized:
        return False
    if foreground_is_target:
        return True
    if target_monitor is None or foreground_monitor is None:
        return True
    return target_monitor != foreground_monitor


def get_monitor_for_window(hwnd: Optional[int]) -> Optional[int]:
    """Return the native monitor handle nearest a window."""
    if os.name != "nt" or not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = wintypes.HMONITOR
        monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        return int(monitor) if monitor else None
    except Exception:
        return None
