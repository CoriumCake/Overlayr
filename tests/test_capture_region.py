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


def test_full_source_capture_ignores_existing_roi(monkeypatch):
    captured_bounds = None

    class FakeMSS:
        monitors = [
            {"left": 0, "top": 0, "width": 8, "height": 6},
            {"left": 0, "top": 0, "width": 8, "height": 6},
        ]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def grab(self, bounds):
            nonlocal captured_bounds
            captured_bounds = dict(bounds)
            size = (bounds["width"], bounds["height"])
            return SimpleNamespace(
                size=size,
                bgra=b"\x00\x00\x00\xff" * (size[0] * size[1]),
            )

    monkeypatch.setattr(
        "overlayr.core.capture_engine.mss.mss",
        lambda: FakeMSS(),
    )
    engine = _capture_engine_without_initializing_wgc()
    engine.mode = "screen"
    engine.target = {
        "id": 0,
        "bounds": {"left": 0, "top": 0, "width": 8, "height": 6},
    }
    engine.roi = (0.25, 0.25, 0.5, 0.5)

    image, bounds = engine.capture_full_source()

    assert image.size == (8, 6)
    assert bounds == {"left": 0, "top": 0, "width": 8, "height": 6}
    assert captured_bounds == bounds
