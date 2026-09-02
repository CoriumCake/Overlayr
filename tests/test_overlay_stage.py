"""The headless renderer that makes `--stage overlay` measurable.

What matters here is that a block's original pixels are gone after drawing -
that is the whole pass criterion of CLAUDE.md 9.24.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PIL import Image  # noqa: E402

from overlayr.eval.overlay_stage import render_blocks  # noqa: E402


def _page(color=(255, 255, 255), size=(400, 200)):
    return Image.new("RGB", size, color)


def _block(**overrides):
    block = {
        "bbox": [20, 40, 300, 90],
        "original": "HELLO WORLD",
        "translated": "สวัสดีชาวโลก",
        "bg_color": "#ffffff",
        "text_color": "#000000",
        "line_height": 30,
    }
    block.update(overrides)
    return block


def test_the_source_pixels_under_a_block_are_painted_over():
    page = _page()
    # A red bar standing in for the source text inside the block.
    for x in range(30, 290):
        for y in range(50, 80):
            page.putpixel((x, y), (255, 0, 0))

    rendered = render_blocks(page, [_block()])
    remaining_red = sum(1 for x in range(30, 290) for y in range(50, 80)
                        if rendered.getpixel((x, y)) == (255, 0, 0))
    assert remaining_red == 0


def test_the_original_image_is_left_alone():
    page = _page()
    before = page.tobytes()
    render_blocks(page, [_block()])
    assert page.tobytes() == before


def test_pixels_outside_the_block_are_untouched():
    page = _page((10, 20, 30))
    rendered = render_blocks(page, [_block()])
    assert rendered.getpixel((380, 190)) == (10, 20, 30)


def test_the_translation_is_actually_drawn():
    """A renderer that painted a blank rectangle would score perfectly on
    coverage while showing the reader nothing."""
    rendered = render_blocks(_page(), [_block()])
    inside = [rendered.getpixel((x, y))
              for x in range(25, 295) for y in range(45, 85)]
    assert any(pixel != (255, 255, 255) for pixel in inside)


def test_a_block_with_no_translation_is_skipped():
    page = _page()
    rendered = render_blocks(page, [_block(translated="")])
    assert rendered.tobytes() == page.tobytes()


def test_the_fill_uses_the_block_colour():
    rendered = render_blocks(_page(), [_block(bg_color="#204060")])
    # Sampled on the left edge at mid-height, which is inside the rounding.
    assert rendered.getpixel((18, 65)) == (32, 64, 96)


def test_the_corners_of_the_fill_are_rounded():
    """A hard-cornered patch reads as something pasted over the page; speech
    bubbles and dialogue panels have no square corners to match."""
    rendered = render_blocks(_page(), [_block(bg_color="#204060")])
    assert rendered.getpixel((17, 39)) == (255, 255, 255)


def test_missing_colours_fall_back_instead_of_raising():
    block = _block()
    del block["bg_color"]
    del block["text_color"]
    rendered = render_blocks(_page(), [block])
    assert rendered.getpixel((18, 65)) != (255, 255, 255)


@pytest.mark.parametrize("bbox", [[0, 0, 5, 5], [0, 0, 400, 200]])
def test_extreme_box_sizes_render_without_error(bbox):
    render_blocks(_page(), [_block(bbox=bbox)])
