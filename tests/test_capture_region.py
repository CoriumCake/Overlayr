from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image

from overlayr.core.capture_engine import CaptureEngine


def _capture_engine_without_initializing_wgc():
    return object.__new__(CaptureEngine)


def test_screen_source_bounds_use_persisted_physical_monitor(monkeypatch):
    class FakeMSS:
        monitors = [{"left": -1920, "top": 0, "width": 3840, "height": 1080}]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(
        "overlayr.core.capture_engine.mss.mss",
        lambda: FakeMSS(),
    )
    engine = _capture_engine_without_initializing_wgc()
    target = {
        "id": 1,
        "bounds": {
            "left": -2560,
            "top": 120,
            "width": 2560,
            "height": 1440,
        },
    }

    assert engine.get_source_bounds("screen", target) == target["bounds"]


def test_window_source_bounds_match_wgc_frame_dimensions(monkeypatch):
    engine = _capture_engine_without_initializing_wgc()
    target = {
        "id": 321,
        "window_obj": SimpleNamespace(
            left=-1928,
            top=-8,
            width=1936,
            height=1048,
        ),
    }
    engine.mode = "window"
    engine.target = target
    engine.wgc = Mock()
    engine.wgc.get_latest_frame.return_value = Image.new("RGB", (1920, 1009))
    monkeypatch.setattr(
        engine,
        "_get_visible_window_bounds",
        lambda _hwnd: None,
    )
    monkeypatch.setattr(
        engine,
        "_get_window_bounds",
        lambda _hwnd: {
            "left": -1928,
            "top": -8,
            "width": 1936,
            "height": 1048,
        },
    )
    monkeypatch.setattr(
        engine,
        "_get_client_bounds",
        lambda _hwnd: {
            "left": -1920,
            "top": 23,
            "width": 1920,
            "height": 1009,
        },
    )

    assert engine.get_source_bounds("window", target) == {
        "left": -1920,
        "top": 23,
        "width": 1920,
        "height": 1009,
    }
