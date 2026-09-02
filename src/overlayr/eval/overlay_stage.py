"""Draw translated blocks onto an image the way the overlay draws them.

CLAUDE.md section 9 item 24 asks for a measurable pass criterion for "the
original text disappears": render the result, read it again, and check the
*source* words are gone. That needs a renderer the harness can run headless, so
this draws with PIL instead of Tk.

Only the drawing differs. The sizing rules come from core/overlay_render, the
same module the Tk overlay uses, so what is measured here is what the user
sees rather than a second implementation that happens to agree.
"""
from __future__ import annotations

import os
from typing import List, Optional, Sequence

from ..core.overlay_render import (PADDING_X, PADDING_Y, STYLE_REPLACE,
                                   corner_radius, fit_font_size, wrap_text,
                                   wrap_width_for)

# Fonts that ship with Windows and cover Latin, Thai and CJK. The first one
# that loads wins; without a real font PIL falls back to a bitmap face whose
# size cannot be set, which would make every measurement meaningless.
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\tahoma.ttf",
    r"C:\Windows\Fonts\arial.ttf",
)

_FONT_CACHE: dict = {}


def _font(size: int):
    from PIL import ImageFont

    if size in _FONT_CACHE:
        return _FONT_CACHE[size]

    font = None
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size)
                break
            except OSError as exc:
                print(f"Overlay render: cannot load {path}: {exc}")
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


def _line_width(text: str, size: int) -> int:
    box = _font(size).getbbox(text)
    return int(box[2] - box[0])


def _measure(text: str, size: int, wrap: int):
    """Size of ``text`` wrapped to ``wrap`` pixels, as the overlay would wrap it."""
    lines = wrap_text(text, size, wrap, _line_width)
    font = _font(size)
    width = max((_line_width(line, size) for line in lines), default=0)
    line_height = font.getbbox("Ay")[3] - font.getbbox("Ay")[1]
    spacing = max(1, int(line_height * 1.2))
    return width, spacing * len(lines)


def render_blocks(image, blocks: Sequence[dict], style: str = STYLE_REPLACE,
                  key: str = "translated"):
    """Paint ``blocks`` onto a copy of ``image`` and return the copy.

    Only Replace is rendered: it is the mode whose whole purpose is to hide the
    source text, so it is the only one there is anything to measure.
    """
    from PIL import ImageDraw

    canvas = image.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas)

    for block in blocks:
        text = block.get(key)
        if not text:
            continue

        left, top, right, bottom = block["bbox"]
        box_w = max(1, right - left)
        box_h = max(1, bottom - top)

        patch = [left - PADDING_X, top - PADDING_Y,
                 right + PADDING_X, bottom + PADDING_Y]
        radius = corner_radius(patch[2] - patch[0], patch[3] - patch[1])
        fill = block.get("bg_color") or "#010101"
        if radius >= 2:
            draw.rounded_rectangle(patch, radius=radius, fill=fill)
        else:
            draw.rectangle(patch, fill=fill)

        size = fit_font_size(text, box_w, box_h, _measure,
                             line_height=block.get("line_height", 0))
        lines = wrap_text(text, size, wrap_width_for(box_w), _line_width)
        draw.multiline_text(
            (left + box_w / 2, top + box_h / 2),
            "\n".join(lines),
            font=_font(size),
            fill=block.get("text_color") or "#ffffff",
            anchor="mm",
            align="center",
        )

    return canvas


def save_render(canvas, directory: Optional[str], name: str) -> None:
    """Write a rendered frame out so the numbers can be eyeballed."""
    if not directory:
        return
    os.makedirs(directory, exist_ok=True)
    canvas.save(os.path.join(directory, f"{name.replace('/', '_')}.png"))
