"""How a translated block is laid out, independent of what draws it.

Two things draw translations: the Tk overlay the user sees, and the harness
renderer that scores ``--stage overlay``. If they sized text differently the
measurement would not be measuring the overlay, so the sizing rules live here
once and both callers pass in their own way of measuring a string.

There is no Tk, no PIL and no Windows in this module, which is what lets the
rules be tested with a stub measurer.
"""
from __future__ import annotations

from typing import Callable, List, Tuple

# The two ways a translation can be presented (CLAUDE.md section 9 item 24).
# Replace paints over the original text so it disappears; Card floats a panel
# above it, which is what the overlay did before. Presentation is taste, so the
# user picks - but only between these two, never a pile of sub-settings.
STYLE_REPLACE = "Replace"
STYLE_CARD = "Card"

OVERLAY_STYLE_CHOICES = [STYLE_REPLACE, STYLE_CARD]
DEFAULT_OVERLAY_STYLE = STYLE_REPLACE

# Breathing room inside the original box, in pixels.
PADDING_X = 4
PADDING_Y = 2

# Replace mode paints a patch over text that was almost never sitting in a
# rectangle - a speech bubble is round, a dialogue panel has soft corners - and
# a hard-cornered fill announces itself as something pasted on top. Rounding
# the corners costs nothing and stops the patch reading as a separate object.
CORNER_RADIUS_RATIO = 0.4
MAX_CORNER_RADIUS = 20

MIN_FONT_SIZE = 8
MAX_FONT_SIZE = 80

# A translation is allowed to run this far past the bottom of the original box
# before the font shrinks again. Thai renders longer than English, and cutting
# it off would lose meaning, which section 1 ranks as the worse failure.
HEIGHT_TOLERANCE = 1.0

# Measure a string: (text, font_size, wrap_width) -> (width, height) in pixels.
Measure = Callable[[str, int, int], Tuple[int, int]]


def resolve_saved_style(saved) -> str:
    """Turn the stored ``overlay_style`` setting into a style name.

    An unrecognised value falls back to the default instead of raising: a stale
    settings file must never stop the overlay from drawing.
    """
    if saved in OVERLAY_STYLE_CHOICES:
        return saved
    if saved:
        print(f"Unknown overlay_style {saved!r} in settings, using "
              f"{DEFAULT_OVERLAY_STYLE}.")
    return DEFAULT_OVERLAY_STYLE


def base_font_size(text: str, box_w: int, box_h: int, line_height: float = 0.0) -> int:
    """First font size to try for a block.

    The line height OCR measured is the best estimate available - matching it
    keeps the translation the same visual weight as the words it covers. When
    OCR did not report one, the box itself is the only clue.
    """
    if line_height and line_height > 0:
        size = int(line_height * 0.9)
    else:
        by_height = box_h * 0.8
        by_width = box_w / max(1.0, len(text) * 0.6)
        size = int(min(by_height, by_width * 2))
    return max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, size))


def wrap_width_for(box_w: int) -> int:
    return max(1, box_w - 2 * PADDING_X)


def corner_radius(box_w: int, box_h: int) -> int:
    """Corner rounding for a fill of this size.

    Tied to the shorter side so a thin single line is not rounded away to a
    lozenge, and capped so a large block does not turn into an oval.
    """
    shorter = max(0, min(box_w, box_h))
    return int(min(MAX_CORNER_RADIUS, shorter * CORNER_RADIUS_RATIO))


def fit_font_size(text: str, box_w: int, box_h: int, measure: Measure,
                  line_height: float = 0.0,
                  min_size: int = MIN_FONT_SIZE) -> int:
    """Largest size at which the text still fits inside the original box.

    Both dimensions are checked. Wrapping alone does not guarantee a fit: a
    single long word cannot be broken, so it can still run past the right edge
    and paint over whatever is beside the block.
    """
    wrap = wrap_width_for(box_w)
    limit_w = max(1, box_w)
    limit_h = max(1, int(box_h * HEIGHT_TOLERANCE))

    size = base_font_size(text, box_w, box_h, line_height)
    while size > min_size:
        width, height = measure(text, size, wrap)
        if width <= limit_w and height <= limit_h:
            break
        size -= 1
    return max(min_size, size)


def wrap_text(text: str, font_size: int, wrap_width: int,
              measure_line: Callable[[str, int], int]) -> List[str]:
    """Break text into lines that fit ``wrap_width``.

    Used by callers that have to lay text out themselves; Tk wraps on its own.
    A word wider than the line is kept whole rather than split mid-word - the
    font shrinks around it instead, which is handled by ``fit_font_size``.
    """
    lines: List[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue

        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if measure_line(candidate, font_size) <= wrap_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines
