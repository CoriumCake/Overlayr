from overlayr.utils.display_utils import (
    format_tk_geometry,
    parse_physical_geometry,
    resolve_monitor_bounds,
    select_capture_bounds,
    should_show_window_overlay,
    tk_pixel_font,
)


def test_geometry_supports_monitor_left_of_primary_display():
    geometry = format_tk_geometry({
        "left": -2560,
        "top": 0,
        "width": 2560,
        "height": 1440,
    })

    assert geometry == "2560x1440-2560+0"


def test_negative_geometry_is_preserved_as_absolute_physical_position():
    bounds = parse_physical_geometry("1920x1009-1920+23")

    assert bounds == {
        "left": -1920,
        "top": 23,
        "width": 1920,
        "height": 1009,
    }


def test_tk_font_size_is_physical_pixels():
    assert tk_pixel_font("Segoe UI", 24, "normal") == (
        "Segoe UI",
        -24,
        "normal",
    )


def test_monitor_selection_prefers_persisted_physical_bounds():
    target = {
        "id": 0,
        "bounds": {
            "left": 1920,
            "top": -360,
            "width": 2560,
            "height": 1440,
        },
    }
    # Deliberately different order/bounds to reproduce provider disagreement.
    monitors = [
        {"left": 0, "top": 0, "width": 4480, "height": 1440},
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
    ]

    assert resolve_monitor_bounds(target, monitors) == target["bounds"]


def test_wgc_capture_prefers_exact_visible_dwm_frame():
    selected = select_capture_bounds(
        2560,
        1368,
        {"left": 0, "top": 0, "width": 2560, "height": 1368},
        {"left": -11, "top": -11, "width": 2582, "height": 1390},
        {"left": 0, "top": 34, "width": 2560, "height": 1334},
    )

    assert selected == {
        "left": 0,
        "top": 0,
        "width": 2560,
        "height": 1368,
    }


def test_wgc_capture_uses_client_area_when_frame_matches_it():
    selected = select_capture_bounds(
        1920,
        1009,
        None,
        {"left": -1928, "top": -8, "width": 1936, "height": 1048},
        {"left": -1920, "top": 23, "width": 1920, "height": 1009},
    )

    assert selected == {
        "left": -1920,
        "top": 23,
        "width": 1920,
        "height": 1009,
    }


def test_overlay_remains_visible_when_focus_moves_to_other_monitor():
    visible = should_show_window_overlay(
        target_exists=True,
        target_visible=True,
        target_minimized=False,
        foreground_is_target=False,
        target_monitor=101,
        foreground_monitor=202,
    )

    assert visible is True


def test_overlay_hides_when_another_window_covers_target_monitor():
    visible = should_show_window_overlay(
        target_exists=True,
        target_visible=True,
        target_minimized=False,
        foreground_is_target=False,
        target_monitor=101,
        foreground_monitor=101,
    )

    assert visible is False


def test_overlay_hides_for_minimized_target():
    visible = should_show_window_overlay(
        target_exists=True,
        target_visible=True,
        target_minimized=True,
        foreground_is_target=False,
        target_monitor=101,
        foreground_monitor=202,
    )

    assert visible is False
