import numpy as np
from PIL import Image

from overlayr.ui.overlay import ResultOverlay


def test_paragraph_anchor_uses_typical_source_line_start():
    item = {
        "glyph_height": 18,
        "source_boxes": [
            [95, 10, 300, 24],
            [101, 28, 320, 42],
            [100, 46, 290, 60],
            [100, 64, 310, 78],
        ]
    }

    anchor_x = ResultOverlay._resolve_text_anchor_x(
        item,
        bbox=[95, 10, 320, 78],
        offset_x=7,
    )

    assert anchor_x == 107


def test_paragraph_anchor_ignores_large_indentation_shift():
    item = {
        "glyph_height": 18,
        "source_boxes": [
            [95, 10, 140, 24],
            [180, 28, 320, 42],
            [185, 46, 290, 60],
        ]
    }

    anchor_x = ResultOverlay._resolve_text_anchor_x(
        item,
        bbox=[95, 10, 320, 60],
        offset_x=7,
    )

    assert anchor_x == 105


def test_single_line_anchor_keeps_ocr_box_position():
    item = {"source_boxes": [[95, 10, 160, 24]]}

    anchor_x = ResultOverlay._resolve_text_anchor_x(
        item,
        bbox=[95, 10, 160, 24],
        offset_x=7,
    )

    assert anchor_x == 102


def test_translated_text_centers_on_source_ink():
    item = {"ink_bbox": [100, 10, 200, 24]}

    text_left = ResultOverlay._resolve_centered_text_left(
        item,
        bbox=[95, 10, 210, 24],
        offset_x=7,
        text_width=60,
        layout_left=102,
    )

    assert text_left == 127


def test_centered_text_is_clamped_to_replacement_area():
    item = {"ink_bbox": [100, 10, 200, 24]}

    text_left = ResultOverlay._resolve_centered_text_left(
        item,
        bbox=[95, 10, 210, 24],
        offset_x=7,
        text_width=120,
        layout_left=102,
    )

    assert text_left == 102


def test_inpaint_source_region_removes_original_glyphs_and_keeps_background_shape():
    """The replacement patch must retain the captured background dimensions."""
    pixels = np.zeros((24, 32, 3), dtype=np.uint8)
    for y in range(pixels.shape[0]):
        for x in range(pixels.shape[1]):
            pixels[y, x] = (30 + x, 40 + y, 50)

    # Simulated bright source glyph inside a textured background.
    pixels[8:15, 9:23] = (245, 245, 245)
    source = Image.fromarray(pixels, mode="RGB")

    patch = ResultOverlay._inpaint_source_region(
        source,
        clear_box=(6, 5, 26, 18),
        ink_box=[(9, 8, 16, 15), (16, 8, 23, 15)],
    )

    assert patch is not None
    assert patch.size == (20, 13)
    # The center of the patch should no longer be the original white glyph.
    assert np.asarray(patch)[7, 10].mean() < 180


def test_inpaint_source_region_preserves_pixels_outside_glyph_mask():
    pixels = np.full((20, 30, 3), (32, 38, 44), dtype=np.uint8)
    pixels[10, :] = (70, 80, 90)
    pixels[7:14, 10:20] = (240, 240, 240)
    source = Image.fromarray(pixels, mode="RGB")
    glyph_mask = np.full((7, 10), 255, dtype=np.uint8)

    patch = ResultOverlay._inpaint_source_region(
        source,
        clear_box=(5, 4, 25, 17),
        ink_box=[{
            "bbox": (10, 7, 20, 14),
            "mask": glyph_mask,
        }],
    )

    assert patch is not None
    patch_pixels = np.asarray(patch)
    # This grid-line pixel is outside the glyph mask and must remain exact.
    assert tuple(patch_pixels[6, 2]) == tuple(pixels[10, 7])
