"""Work out what colour a text block's paper and ink are.

The overlay has to paint over the original words, not float a card above them
(CLAUDE.md section 9 item 24). To do that it needs two colours per block: the
colour to fill the box with so the source text disappears, and the colour to
write the translation in so it keeps the contrast direction the reader already
had - white on dark stays white on dark.

The engine used to take the mean of the whole crop, which mixes the letters
into the answer and drags it towards the ink. A speech bubble came out grey
instead of white. So the pixels are split into two clusters first and only the
paper cluster decides the fill.

Pure numpy: no Windows, no Tk, no cv2, so the rules can be tested directly.
"""
from __future__ import annotations

from typing import Tuple

import numpy as np

# Below this spread between the two clusters the crop has no real text/paper
# split - it is a flat patch, and inventing an ink colour from noise would give
# a random result. The fallback picks black or white by luminance instead.
MIN_CLUSTER_SEPARATION = 24.0

# Pure black is the overlay window's transparent colour, so a block that is
# genuinely black must be nudged one step off it or the fill turns into a hole
# that shows the original text straight through.
TRANSPARENT_BLACK = (0, 0, 0)
NEAREST_TO_BLACK = (1, 1, 1)

# Width of the ring sampled around the crop edge to decide which cluster is the
# paper. Text is drawn inside a box; its border belongs to the background.
BORDER_RING = 2

# Channel width used when looking for the dominant tone. Four levels is below
# what the eye separates on a flat fill, so tones inside one bucket are
# interchangeable, while real differences stay in buckets of their own.
COLOUR_BUCKET = 4


def _luminance(rgb: np.ndarray) -> np.ndarray:
    """Rec. 601 luma, matching how the eye weights the channels."""
    return (rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114)


def _otsu_threshold(gray: np.ndarray) -> float:
    """Threshold that best splits the histogram into two groups."""
    counts = np.bincount(gray.astype(np.uint8).ravel(), minlength=256).astype(np.float64)
    total = counts.sum()
    if total == 0:
        return 128.0

    levels = np.arange(256, dtype=np.float64)
    weight_low = np.cumsum(counts)
    weight_high = total - weight_low
    sum_low = np.cumsum(counts * levels)
    sum_total = sum_low[-1]

    valid = (weight_low > 0) & (weight_high > 0)
    if not valid.any():
        return float(gray.mean())

    mean_low = np.divide(sum_low, weight_low, out=np.zeros_like(sum_low), where=weight_low > 0)
    mean_high = np.divide(sum_total - sum_low, weight_high,
                          out=np.zeros_like(sum_low), where=weight_high > 0)
    variance = weight_low * weight_high * (mean_low - mean_high) ** 2
    variance[~valid] = -1.0
    return float(np.argmax(variance))


def _border_mask(shape: Tuple[int, int]) -> np.ndarray:
    """True on the outer ring of a crop of this shape."""
    height, width = shape
    mask = np.zeros((height, width), dtype=bool)
    ring = min(BORDER_RING, max(1, height // 2), max(1, width // 2))
    mask[:ring, :] = True
    mask[-ring:, :] = True
    mask[:, :ring] = True
    mask[:, -ring:] = True
    return mask


def _to_hex(rgb) -> str:
    values = tuple(int(max(0, min(255, round(float(c))))) for c in rgb)
    return "#%02x%02x%02x" % values


def _fill_hex(rgb) -> str:
    """Hex for a colour that will be painted as a solid fill.

    Only fills need the nudge off pure black: the ink is drawn *on* the fill,
    where black is a legitimate colour and not a transparent hole.
    """
    values = tuple(int(max(0, min(255, round(float(c))))) for c in rgb)
    if values == TRANSPARENT_BLACK:
        values = NEAREST_TO_BLACK
    return "#%02x%02x%02x" % values


def _flat_fallback(pixels: np.ndarray) -> Tuple[str, str]:
    """A crop with no usable split: fill with its own colour, write in the
    colour that stands out from it."""
    background = _dominant_colour(pixels.reshape(-1, 3))
    ink = (0, 0, 0) if _luminance(np.asarray(background, dtype=np.float64)) > 127 \
        else (255, 255, 255)
    return _fill_hex(background), _to_hex(ink)


def _dominant_colour(pixels: np.ndarray) -> np.ndarray:
    """The most common tone in a set of pixels, not their average.

    The fill this produces is painted next to pixels it did not cover, so any
    difference between the two shows up as a visible rectangle. A median is
    pulled off the paper tone by everything else caught in the box - the edge
    of a speech bubble, a band of screentone, the page tint outside it - and a
    fill that is a few levels off white against white reads as a pasted patch.

    Snapping to the dominant bin instead lands on the flat tone that actually
    covers most of the area, which is the one the fill has to match.
    """
    if pixels.size == 0:
        return np.array([1.0, 1.0, 1.0])

    # Quantise to buckets wide enough to absorb JPEG noise and antialiasing but
    # narrow enough to keep two nearby tones apart.
    keys = (pixels // COLOUR_BUCKET).astype(np.int64)
    flat = keys[:, 0] * 4096 + keys[:, 1] * 64 + keys[:, 2]
    values, counts = np.unique(flat, return_counts=True)
    winner = values[int(np.argmax(counts))]

    # Average within the winning bucket so the result is an exact tone rather
    # than the bucket's corner.
    return np.median(pixels[flat == winner], axis=0)


def estimate_block_colors(crop: np.ndarray) -> Tuple[str, str]:
    """Return ``(background_hex, ink_hex)`` for one text block's pixels.

    ``crop`` is the RGB region under the block, height x width x 3.
    """
    if crop is None or crop.size == 0:
        return "#010101", "#ffffff"

    pixels = np.asarray(crop)
    if pixels.ndim == 2:
        pixels = np.repeat(pixels[:, :, None], 3, axis=2)
    pixels = pixels[:, :, :3].astype(np.float64)

    # Otsu bins the histogram into whole levels, so the split has to be applied
    # to the same whole levels. Comparing the un-rounded luminance against a
    # level index leaves pixels just above it stranded outside both classes -
    # which emptied the dark side entirely and picked the letters as paper.
    gray = np.floor(_luminance(pixels))
    threshold = _otsu_threshold(gray)

    dark = gray <= threshold
    light = ~dark
    if not dark.any() or not light.any():
        return _flat_fallback(pixels)

    dark_pixels = pixels[dark]
    light_pixels = pixels[light]
    dark_level = float(np.median(_luminance(dark_pixels)))
    light_level = float(np.median(_luminance(light_pixels)))

    if light_level - dark_level < MIN_CLUSTER_SEPARATION:
        return _flat_fallback(pixels)

    # Whichever cluster owns the edge of the box is the paper the text sits on.
    # Falling back to the larger cluster keeps a block that is all border - a
    # single tall line, say - from picking the letters as its background.
    border = _border_mask(gray.shape)
    dark_on_border = int(np.count_nonzero(dark & border))
    light_on_border = int(np.count_nonzero(light & border))
    if dark_on_border == light_on_border:
        background_is_dark = dark.sum() >= light.sum()
    else:
        background_is_dark = dark_on_border > light_on_border

    if background_is_dark:
        background, ink = dark_pixels, light_pixels
    else:
        background, ink = light_pixels, dark_pixels

    return (_fill_hex(_dominant_colour(background)),
            _to_hex(np.median(ink, axis=0)))
