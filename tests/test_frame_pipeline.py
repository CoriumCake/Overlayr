import numpy as np
from PIL import Image

from overlayr.core.frame_pipeline import (
    LatestFrameBuffer,
    build_region_fingerprints,
    compare_region_fingerprints,
    evaluate_ocr_cache,
)


def _image(array):
    return Image.fromarray(array.astype(np.uint8), mode="L")


def _result_box(left, top, right, bottom):
    return {"bbox": [left, top, right, bottom], "translated": "ข้อความ"}


def test_latest_frame_buffer_overwrites_old_frame():
    buffer = LatestFrameBuffer()
    first = buffer.publish("first", {"left": 0, "top": 0, "width": 10, "height": 10})
    second = buffer.publish("second", {"left": 5, "top": 6, "width": 20, "height": 20})

    assert second.sequence == first.sequence + 1
    assert buffer.latest() == second
    assert buffer.latest().image == "second"


def test_region_fingerprint_ignores_animation_outside_text_box():
    previous = np.zeros((120, 160), dtype=np.uint8)
    previous[40:60, 50:110] = 220
    current = previous.copy()
    current[0:30, 0:30] = 255
    results = [_result_box(48, 38, 112, 62)]

    fingerprints = build_region_fingerprints(_image(previous), results)
    metrics = compare_region_fingerprints(_image(current), fingerprints)

    assert metrics.changed is False


def test_region_fingerprint_detects_removed_text_panel():
    previous = np.full((120, 160), 25, dtype=np.uint8)
    previous[38:64, 45:115] = 210
    current = np.full((120, 160), 25, dtype=np.uint8)
    results = [_result_box(43, 36, 117, 66)]

    fingerprints = build_region_fingerprints(_image(previous), results)
    metrics = compare_region_fingerprints(_image(current), fingerprints)

    assert metrics.changed is True
    assert metrics.changed_ratio >= 0.10


def test_ocr_cache_reuses_stable_text_regions_despite_background_animation():
    previous = np.zeros((400, 600), dtype=np.uint8)
    previous[150:190, 180:420] = 210
    current = previous.copy()
    current[:, :150] = 100
    results = [_result_box(175, 145, 425, 195)]

    decision = evaluate_ocr_cache(
        previous,
        current,
        results,
        cache_age_seconds=1.0,
    )

    assert decision.reuse is True
    assert decision.regions_changed is False
    assert decision.global_changed_ratio > 0.05


def test_ocr_cache_invalidates_changed_text_region_immediately():
    previous = np.zeros((400, 600), dtype=np.uint8)
    previous[150:190, 180:420] = 210
    current = previous.copy()
    current[150:190, 180:420] = 30
    results = [_result_box(175, 145, 425, 195)]

    decision = evaluate_ocr_cache(
        previous,
        current,
        results,
        cache_age_seconds=0.1,
    )

    assert decision.reuse is False
    assert decision.regions_changed is True


def test_ocr_cache_periodically_rescans_static_scene():
    previous = np.zeros((400, 600), dtype=np.uint8)
    current = previous.copy()
    results = [_result_box(175, 145, 425, 195)]

    decision = evaluate_ocr_cache(
        previous,
        current,
        results,
        cache_age_seconds=6.0,
    )

    assert decision.reuse is False
    assert decision.reason == "periodic_rescan"


def test_empty_result_cache_polls_frequently_for_new_text():
    previous = np.zeros((400, 600), dtype=np.uint8)
    current = previous.copy()

    decision = evaluate_ocr_cache(
        previous,
        current,
        [],
        cache_age_seconds=0.6,
    )

    assert decision.reuse is False
    assert decision.reason == "empty_scene_poll"
