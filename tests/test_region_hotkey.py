import queue
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from overlayr.ui.main_window import MainWindow


def _main_window_without_tk():
    return object.__new__(MainWindow)


def test_alt_t_hotkey_queues_ui_action(monkeypatch):
    callback = None

    def add_hotkey(shortcut, registered_callback, **options):
        nonlocal callback
        assert shortcut == "alt+t"
        assert options == {
            "suppress": True,
            "trigger_on_release": True,
        }
        callback = registered_callback
        return "hotkey-token"

    fake_keyboard = SimpleNamespace(add_hotkey=add_hotkey)
    monkeypatch.setitem(__import__("sys").modules, "keyboard", fake_keyboard)
    window = _main_window_without_tk()
    window._ui_action_queue = queue.Queue()
    window._hotkey_handle = None

    window._register_hotkeys()
    callback()

    assert window._hotkey_handle == "hotkey-token"
    assert window._ui_action_queue.get_nowait() == "select_region"


def test_selected_area_updates_live_capture_roi():
    window = _main_window_without_tk()
    window._region_selector = object()
    window._region_selection_active = True
    window._active_roi = None
    window._pending_start_roi = None
    window.is_streaming = True
    window.capture_engine = Mock()
    window.lbl_status = Mock()
    window._reset_pipeline_for_area_change = Mock()
    window._set_area_button_active = Mock()
    window.toggle_stream = Mock()

    window._apply_region_selection(
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

    expected_roi = pytest.approx((0.25, 0.25, 0.5, 0.5))
    assert window._active_roi == expected_roi
    assert window._pending_start_roi == expected_roi
    window.capture_engine.set_roi.assert_called_once_with(expected_roi)
    window._reset_pipeline_for_area_change.assert_called_once_with()
    window._set_area_button_active.assert_called_once_with(True)
    window.toggle_stream.assert_not_called()


def test_selected_area_starts_translation_when_capture_is_stopped():
    window = _main_window_without_tk()
    window._region_selector = object()
    window._region_selection_active = True
    window._active_roi = None
    window._pending_start_roi = None
    window.is_streaming = False
    window.capture_engine = Mock()
    window.lbl_status = Mock()
    window._reset_pipeline_for_area_change = Mock()
    window._set_area_button_active = Mock()
    window.toggle_stream = Mock()

    window._apply_region_selection(
        {
            "left": 100,
            "top": 100,
            "width": 800,
            "height": 400,
        },
        {
            "left": 0,
            "top": 0,
            "width": 1920,
            "height": 1080,
        },
    )

    window.capture_engine.set_roi.assert_not_called()
    window.toggle_stream.assert_called_once_with()


def test_stale_full_source_result_cannot_replace_selected_area():
    window = _main_window_without_tk()
    window.is_streaming = True
    window._stream_generation = 4
    window._capture_revision = 8
    window._overlay_tracking_lock = threading.Lock()
    window._minimum_render_sequence = 0
    window.overlay = Mock()

    window._update_overlay_safe(
        results=[{"translated": "old full-screen result"}],
        raw_text="",
        x=0,
        y=0,
        geometry="1920x1080+0+0",
        stream_generation=4,
        capture_revision=7,
    )

    window.overlay.set_geometry.assert_not_called()
    window.overlay.show_results.assert_not_called()
