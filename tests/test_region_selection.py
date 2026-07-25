import pytest

from overlayr.core.region_selection import (
    local_selection_to_screen,
    normalize_drag_selection,
    selection_to_roi,
)


def test_drag_selection_supports_reverse_direction():
    selection = normalize_drag_selection(
        start=(700, 500),
        end=(100, 200),
        canvas_size=(1920, 1080),
    )

    assert selection == {
        "left": 100,
        "top": 200,
        "width": 600,
        "height": 300,
    }


def test_drag_selection_clamps_to_source_canvas():
    selection = normalize_drag_selection(
        start=(-50, -20),
        end=(2100, 1200),
        canvas_size=(1920, 1080),
    )

    assert selection == {
        "left": 0,
        "top": 0,
        "width": 1920,
        "height": 1080,
    }


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ((10, 10), (10, 100)),
        ((10, 10), (17, 17)),
        ((400, 300), (395, 298)),
    ],
)
def test_drag_selection_rejects_tiny_or_empty_regions(start, end):
    assert (
        normalize_drag_selection(
            start=start,
            end=end,
            canvas_size=(1920, 1080),
            minimum_size=8,
        )
        is None
    )


def test_local_selection_maps_to_negative_monitor_coordinates():
    absolute = local_selection_to_screen(
        {
            "left": 100,
            "top": 50,
            "width": 800,
            "height": 400,
        },
        {
            "left": -2560,
            "top": 120,
            "width": 2560,
            "height": 1440,
        },
    )

    assert absolute == {
        "left": -2460,
        "top": 170,
        "width": 800,
        "height": 400,
    }


def test_screen_selection_converts_to_capture_roi():
    roi = selection_to_roi(
        {
            "left": 480,
            "top": 270,
            "width": 960,
            "height": 540,
        },
        {
            "left": 0,
            "top": 0,
            "width": 1920,
            "height": 1080,
        },
    )

    assert roi == pytest.approx((0.25, 0.25, 0.5, 0.5))


def test_roi_intersects_selection_with_source_bounds():
    roi = selection_to_roi(
        {
            "left": -2100,
            "top": 100,
            "width": 500,
            "height": 500,
        },
        {
            "left": -1920,
            "top": 0,
            "width": 1920,
            "height": 1080,
        },
    )

    assert roi == pytest.approx((0.0, 100 / 1080, 320 / 1920, 500 / 1080))


def test_roi_rejects_selection_outside_source():
    assert (
        selection_to_roi(
            {
                "left": 2000,
                "top": 100,
                "width": 500,
                "height": 500,
            },
            {
                "left": 0,
                "top": 0,
                "width": 1920,
                "height": 1080,
            },
        )
        is None
    )
