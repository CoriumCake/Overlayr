import sys
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image

from overlayr.core.capture_engine import CaptureEngine, _is_solid_white


def _engine_with_window_frames(frames):
    engine = object.__new__(CaptureEngine)
    engine.mode = "window"
    engine.roi = None
    engine.target = {
        "id": 123,
        "window_obj": SimpleNamespace(left=10, top=20, width=100, height=80),
    }
    engine.wgc = SimpleNamespace(get_latest_frame=Mock(side_effect=frames))
    return engine


def test_solid_white_detection_is_strict():
    assert _is_solid_white(Image.new("RGB", (4, 4), "white"))
    assert not _is_solid_white(Image.new("RGB", (4, 4), (254, 255, 255)))


def test_capture_retries_transient_white_wgc_frame(monkeypatch):
    white = Image.new("RGB", (100, 80), "white")
    valid = Image.new("RGB", (100, 80), (20, 40, 60))
    engine = _engine_with_window_frames([white, valid])
    monkeypatch.setitem(sys.modules, "pygetwindow", SimpleNamespace())
    monkeypatch.setattr("overlayr.core.capture_engine.time.sleep", Mock())

    image, bounds = engine.capture()

    assert image is valid
    assert bounds == {"left": 10, "top": 20, "width": 100, "height": 80}
    assert engine.wgc.get_latest_frame.call_count == 2


def test_capture_drops_white_wgc_frames_when_no_valid_frame_arrives(monkeypatch):
    engine = _engine_with_window_frames([Image.new("RGB", (100, 80), "white")] * 3)
    monkeypatch.setitem(sys.modules, "pygetwindow", SimpleNamespace())
    sleep = Mock()
    monkeypatch.setattr("overlayr.core.capture_engine.time.sleep", sleep)

    image, bounds = engine.capture()

    assert image is None
    assert bounds is None
    assert engine.wgc.get_latest_frame.call_count == 3
    assert sleep.call_count == 2
