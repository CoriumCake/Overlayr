"""Geometry helpers for drag-to-translate region selection."""

from __future__ import annotations

from typing import Any, Mapping, Optional

Bounds = dict[str, int]
Point = tuple[float, float]
ROI = tuple[float, float, float, float]


def normalize_drag_selection(
    *,
    start: Point,
    end: Point,
    canvas_size: tuple[int, int],
    minimum_size: int = 8,
) -> Optional[Bounds]:
    """Return a clamped local rectangle regardless of drag direction."""
    canvas_width = max(0, int(canvas_size[0]))
    canvas_height = max(0, int(canvas_size[1]))
    if canvas_width == 0 or canvas_height == 0:
        return None

    start_x = max(0, min(canvas_width, int(round(start[0]))))
    start_y = max(0, min(canvas_height, int(round(start[1]))))
    end_x = max(0, min(canvas_width, int(round(end[0]))))
    end_y = max(0, min(canvas_height, int(round(end[1]))))

    left, right = sorted((start_x, end_x))
    top, bottom = sorted((start_y, end_y))
    width = right - left
    height = bottom - top
    threshold = max(1, int(minimum_size))
    if width < threshold or height < threshold:
        return None

    return {
        "left": left,
        "top": top,
        "width": width,
        "height": height,
    }


def local_selection_to_screen(
    selection: Mapping[str, Any],
    source_bounds: Mapping[str, Any],
) -> Bounds:
    """Offset a source-local drag rectangle into virtual-screen coordinates."""
    return {
        "left": int(source_bounds["left"]) + int(selection["left"]),
        "top": int(source_bounds["top"]) + int(selection["top"]),
        "width": int(selection["width"]),
        "height": int(selection["height"]),
    }


def selection_to_roi(
    selection: Mapping[str, Any],
    source_bounds: Mapping[str, Any],
    *,
    minimum_size: int = 8,
) -> Optional[ROI]:
    """Convert an absolute selection into a clamped source-relative ROI."""
    source_left = int(source_bounds["left"])
    source_top = int(source_bounds["top"])
    source_width = int(source_bounds["width"])
    source_height = int(source_bounds["height"])
    if source_width <= 0 or source_height <= 0:
        return None

    selection_left = int(selection["left"])
    selection_top = int(selection["top"])
    selection_right = selection_left + int(selection["width"])
    selection_bottom = selection_top + int(selection["height"])

    left = max(source_left, selection_left)
    top = max(source_top, selection_top)
    right = min(source_left + source_width, selection_right)
    bottom = min(source_top + source_height, selection_bottom)
    width = right - left
    height = bottom - top
    threshold = max(1, int(minimum_size))
    if width < threshold or height < threshold:
        return None

    return (
        (left - source_left) / source_width,
        (top - source_top) / source_height,
        width / source_width,
        height / source_height,
    )
