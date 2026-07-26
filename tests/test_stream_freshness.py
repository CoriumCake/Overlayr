import threading

import numpy as np
from PIL import Image

from overlayr.core.frame_pipeline import (
    LatestFrameBuffer,
    build_region_fingerprints,
)
from overlayr.ui.main_window import (
    MainWindow,
    _requires_overlay_capture_exclusion,
)


def _window_without_tk():
    window = object.__new__(MainWindow)
    window._overlay_tracking_lock = threading.Lock()
    window._minimum_render_sequence = 0
    window._overlay_tracking_active = False
    window._overlay_change_hits = 0
    window._latest_frames = LatestFrameBuffer()
    window._stream_generation = 1
    return window


def _source_with_text_panel():
    pixels = np.full((200, 300, 3), 25, dtype=np.uint8)
    pixels[70:120, 80:240] = 215
    return Image.fromarray(pixels)


def test_application_capture_does_not_toggle_overlay_affinity():
    assert _requires_overlay_capture_exclusion("window", True) is False


def test_screen_capture_excludes_included_overlay_from_internal_frame():
    assert _requires_overlay_capture_exclusion("screen", True) is True


def test_screen_capture_without_overlay_inclusion_needs_no_toggle():
    assert _requires_overlay_capture_exclusion("screen", False) is False


def test_completed_ocr_is_dropped_when_source_region_disappeared():
    window = _window_without_tk()
    submitted = _source_with_text_panel()
    latest = Image.fromarray(np.full((200, 300, 3), 25, dtype=np.uint8))
    window._latest_frames.publish(submitted, {"left": 0, "top": 0})
    latest_packet = window._latest_frames.publish(latest, {"left": 0, "top": 0})
    results = [{"bbox": [78, 68, 242, 122], "translated": "ข้อความ"}]

    is_current = window._result_is_current(1, submitted, results)

    assert is_current is False
    assert window._minimum_render_sequence == latest_packet.sequence


def test_completed_ocr_survives_animation_outside_source_region():
    window = _window_without_tk()
    submitted = _source_with_text_panel()
    latest_pixels = np.asarray(submitted).copy()
    latest_pixels[0:40, 0:40] = 180
    latest = Image.fromarray(latest_pixels)
    window._latest_frames.publish(submitted, {"left": 0, "top": 0})
    window._latest_frames.publish(latest, {"left": 0, "top": 0})
    results = [{"bbox": [78, 68, 242, 122], "translated": "ข้อความ"}]

    is_current = window._result_is_current(1, submitted, results)

    assert is_current is True


def test_fast_clear_requires_two_consecutive_changed_captures():
    window = _window_without_tk()
    window.is_streaming = True
    callbacks = []
    window.after = lambda _delay, callback: callbacks.append(callback)
    submitted = _source_with_text_panel()
    results = [{"bbox": [78, 68, 242, 122], "translated": "ข้อความ"}]
    window._overlay_fingerprints = build_region_fingerprints(
        submitted,
        results,
    )
    window._overlay_tracking_active = True

    changed = Image.fromarray(
        np.full((200, 300, 3), 25, dtype=np.uint8)
    )
    first = window._latest_frames.publish(changed, {"left": 0, "top": 0})
    window._track_overlay_change(first)

    assert window._overlay_tracking_active is True
    assert callbacks == []

    second = window._latest_frames.publish(changed, {"left": 0, "top": 0})
    window._track_overlay_change(second)

    assert window._overlay_tracking_active is False
    assert window._minimum_render_sequence == second.sequence
    assert len(callbacks) == 1


def test_result_from_previous_stream_session_is_dropped():
    window = _window_without_tk()
    submitted = _source_with_text_panel()
    results = [{"bbox": [78, 68, 242, 122], "translated": "ข้อความ"}]

    is_current = window._result_is_current(
        1,
        submitted,
        results,
        stream_generation=0,
    )

    assert is_current is False
