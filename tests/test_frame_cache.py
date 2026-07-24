import numpy as np

from overlayr.core.engine import _can_reuse_frame_cache


def test_frame_cache_reuses_effectively_identical_frame():
    previous = np.zeros((200, 300), dtype=np.uint8)
    current = previous.copy()
    current[0:2, 0:5] = 20

    can_reuse, changed_pixels, _ = _can_reuse_frame_cache(previous, current)

    assert can_reuse is True
    assert changed_pixels == 10


def test_frame_cache_rejects_small_changed_text_region():
    """A tiny text update must invalidate the cache despite >99% similarity."""
    previous = np.zeros((1000, 1000), dtype=np.uint8)
    current = previous.copy()
    current[400:405, 400:440] = 255

    can_reuse, changed_pixels, total_pixels = _can_reuse_frame_cache(
        previous,
        current,
    )

    assert changed_pixels / total_pixels < 0.01
    assert can_reuse is False


def test_frame_cache_rejects_different_dimensions():
    previous = np.zeros((100, 100), dtype=np.uint8)
    current = np.zeros((120, 100), dtype=np.uint8)

    can_reuse, _, _ = _can_reuse_frame_cache(previous, current)

    assert can_reuse is False
