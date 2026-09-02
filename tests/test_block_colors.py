"""The colours Replace mode paints a block with.

These decide whether the source text disappears or shows through, so the rules
are pinned here rather than judged by eye (CLAUDE.md 9.24).
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from overlayr.core.block_colors import estimate_block_colors  # noqa: E402


def _hex_to_rgb(value):
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def _bubble(background, ink, size=(40, 120)):
    """A crop shaped like a speech bubble: paper with a band of ink inside."""
    crop = np.zeros((size[0], size[1], 3), dtype=np.uint8)
    crop[:, :] = background
    crop[15:25, 10:110] = ink
    return crop


def test_white_bubble_keeps_white_paper_and_dark_ink():
    bg, ink = estimate_block_colors(_bubble((255, 255, 255), (10, 10, 10)))
    assert _hex_to_rgb(bg) == (255, 255, 255)
    assert max(_hex_to_rgb(ink)) < 40


def test_light_text_on_dark_panel_keeps_its_contrast_direction():
    """A game panel is white on dark; the translation has to stay that way."""
    bg, ink = estimate_block_colors(_bubble((20, 24, 30), (240, 240, 240)))
    assert max(_hex_to_rgb(bg)) < 60
    assert min(_hex_to_rgb(ink)) > 200


def test_mean_of_the_whole_crop_is_not_used():
    """The old code averaged every pixel, which dragged white paper to grey."""
    crop = _bubble((255, 255, 255), (0, 0, 0))
    bg, _ = estimate_block_colors(crop)
    mean = crop.reshape(-1, 3).mean(axis=0)
    assert _hex_to_rgb(bg)[0] > mean[0] + 10


def test_pure_black_is_nudged_off_the_transparent_colour():
    """Black is the overlay window's transparent colour: a black fill would be
    a hole showing the original text straight through."""
    crop = np.zeros((20, 60, 3), dtype=np.uint8)
    crop[8:12, 5:55] = (255, 255, 255)
    bg, _ = estimate_block_colors(crop)
    assert bg != "#000000"
    assert _hex_to_rgb(bg) == (1, 1, 1)


def test_flat_patch_falls_back_instead_of_inventing_ink():
    crop = np.full((20, 60, 3), 200, dtype=np.uint8)
    bg, ink = estimate_block_colors(crop)
    assert _hex_to_rgb(bg) == (200, 200, 200)
    assert _hex_to_rgb(ink) == (0, 0, 0)


def test_empty_crop_returns_a_usable_pair():
    bg, ink = estimate_block_colors(np.zeros((0, 0, 3), dtype=np.uint8))
    assert bg != "#000000"
    assert ink


def test_grayscale_crop_is_accepted():
    crop = np.full((20, 60), 240, dtype=np.uint8)
    crop[8:12, 5:55] = 10
    bg, ink = estimate_block_colors(crop)
    assert _hex_to_rgb(bg)[0] > 200
    assert _hex_to_rgb(ink)[0] < 40


@pytest.mark.parametrize("background,ink", [
    ((255, 250, 240), (30, 20, 20)),
    ((12, 18, 40), (220, 230, 255)),
])
def test_paper_is_taken_from_the_border_not_the_larger_cluster(background, ink):
    """A block that is mostly letters still sits on the paper at its edge."""
    crop = np.zeros((20, 40, 3), dtype=np.uint8)
    crop[:, :] = background
    crop[3:17, 3:37] = ink
    estimated_bg, estimated_ink = estimate_block_colors(crop)
    assert _hex_to_rgb(estimated_bg) == background
    assert _hex_to_rgb(estimated_ink) == ink


def test_the_fill_snaps_to_the_dominant_tone_not_the_average_of_the_paper():
    """The box round a speech bubble catches the page outside it too.

    Averaging the whole paper cluster lands between bubble white and page
    tint, and a fill a few levels off the bubble shows up as a rectangle. The
    dominant tone is the one the fill has to match.
    """
    crop = np.full((40, 120, 3), 255, dtype=np.uint8)   # bubble paper
    crop[:, :14] = (214, 222, 236)                      # page tint at the edge
    crop[:, -14:] = (214, 222, 236)
    crop[15:25, 20:100] = 15                            # the lettering
    background, _ = estimate_block_colors(crop)
    assert _hex_to_rgb(background) == (255, 255, 255)


def test_a_dominant_page_tint_is_matched_exactly():
    """The same rule the other way round: when the box is mostly page, the
    fill has to be the page tone rather than a blend towards the bubble."""
    crop = np.full((40, 120, 3), 214, dtype=np.uint8)
    crop[:, :, 2] = 236
    crop[:, 50:70] = 255
    crop[15:25, 20:100] = 15
    background, _ = estimate_block_colors(crop)
    assert _hex_to_rgb(background) == (214, 214, 236)
