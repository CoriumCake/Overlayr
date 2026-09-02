"""Tests for the capture loop, now that it is separable from the window.

None of this was reachable while it lived inside MainWindow: the similarity
gate, the scene handover, and the rule that a stopped stream draws nothing all
needed a Tk window to exist before they could be exercised at all.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from overlayr.core.scene import SceneType
from overlayr.core.stream_controller import StreamController, _geometry_of
from overlayr.utils.image_utils import frame_signature


class FakeView:
    """Records what the controller asked the interface to do."""

    def __init__(self):
        self.results = []
        self.geometries = []
        self.statuses = []
        self.hidden = 0
        self.scheduled_after = []

    def schedule(self, callback):
        callback()

    def schedule_after(self, delay_ms, callback):
        # Recorded rather than run: running it would recurse forever.
        self.scheduled_after.append((delay_ms, callback))

    def show_results(self, results, geometry, visible=True):
        self.results.append((results, geometry, visible))

    def set_geometry(self, geometry):
        self.geometries.append(geometry)

    def hide_overlay(self):
        self.hidden += 1

    def set_status(self, text):
        self.statuses.append(text)


BOUNDS = {"width": 800, "height": 600, "left": 100, "top": 50}


def image(colour=(255, 255, 255), size=(64, 64)):
    return Image.new("RGB", size, colour)


@pytest.fixture
def view():
    return FakeView()


@pytest.fixture
def controller(view):
    engine = MagicMock()
    engine.process_image = AsyncMock(return_value=([], ""))
    return StreamController(view, MagicMock(), engine, MagicMock())


class TestGeometry:
    def test_geometry_string(self):
        assert _geometry_of(BOUNDS) == "800x600+100+50"


class TestSimilarityGate:
    def test_the_first_frame_is_always_processed(self, controller):
        assert controller._is_repeat_frame(image()) is False

    def test_an_identical_frame_is_skipped(self, controller):
        frame = image()
        controller.last_frame = frame
        assert controller._is_repeat_frame(frame) is True

    def test_a_different_frame_is_processed(self, controller):
        controller.last_frame = image((255, 255, 255))
        assert controller._is_repeat_frame(image((0, 0, 0))) is False


class TestScene:
    def test_an_override_stops_the_classifier_from_running(self, controller):
        controller.set_scene_override(SceneType.COMIC)
        controller.scene_classifier.classify = MagicMock()

        controller._classify_scene(image())

        controller.scene_classifier.classify.assert_not_called()

    def test_an_override_sets_the_merge_mode(self, controller):
        controller.set_scene_override(SceneType.DOCUMENT)

        assert controller.current_scene is SceneType.DOCUMENT
        assert controller.current_merge_mode == "line"

    def test_a_detected_scene_is_reported_to_the_user(self, controller, view):
        controller.current_scene = SceneType.GAME_UI
        controller.scene_classifier.classify = MagicMock(return_value=SceneType.COMIC)

        controller._classify_scene(image())

        assert controller.current_scene is SceneType.COMIC
        assert "Detected: Comic" in view.statuses

    def test_the_same_scene_again_is_not_reported(self, controller, view):
        controller.current_scene = SceneType.COMIC
        controller.scene_classifier.classify = MagicMock(return_value=SceneType.COMIC)

        controller._classify_scene(image())

        assert view.statuses == []

    def test_a_failing_classifier_does_not_stop_the_stream(self, controller):
        controller.scene_classifier.classify = MagicMock(side_effect=RuntimeError("boom"))

        controller._classify_scene(image())  # must not raise

        assert controller.current_scene is not None

    def test_changing_source_clears_the_scene_history(self, controller):
        controller.scene_classifier.classify(image())
        controller.last_frame = image()

        controller.set_source({"type": "window", "process": "game.exe"})

        assert len(controller.scene_classifier._history) == 0
        assert controller.last_frame is None


class TestProcessFrame:
    def test_results_reach_the_overlay(self, controller, view):
        blocks = [{"bbox": [0, 0, 10, 10], "original": "hi", "translated": "สวัสดี"}]
        controller.async_engine.process_image = AsyncMock(return_value=(blocks, ""))
        controller.is_streaming = True

        asyncio.run(controller.process_frame(image(), BOUNDS))

        assert view.results == [(blocks, "800x600+100+50", True)]

    def test_the_scene_is_passed_to_the_engine(self, controller):
        controller.is_streaming = True
        controller.set_scene_override(SceneType.COMIC)

        asyncio.run(controller.process_frame(image(), BOUNDS))

        kwargs = controller.async_engine.process_image.call_args.kwargs
        assert kwargs["scene"] is SceneType.COMIC
        assert kwargs["merge_mode"] == "paragraph"

    def test_a_stopped_stream_draws_nothing(self, controller, view):
        controller.is_streaming = False

        asyncio.run(controller.process_frame(image(), BOUNDS))

        assert view.results == []

    def test_an_engine_failure_does_not_propagate(self, controller, view):
        controller.is_streaming = True
        controller.async_engine.process_image = AsyncMock(side_effect=RuntimeError("boom"))

        asyncio.run(controller.process_frame(image(), BOUNDS))

        assert view.results == []


class TestLifecycle:
    def test_start_needs_a_source(self, controller):
        assert controller.start() is False
        assert controller.is_streaming is False

    def test_stop_hides_the_overlay(self, controller, view):
        controller.is_streaming = True

        controller.stop()

        assert controller.is_streaming is False
        assert view.hidden == 1

    def test_visibility_checks_stop_with_the_stream(self, controller, view):
        controller.is_streaming = False

        controller.check_visibility()

        assert view.scheduled_after == []

    def test_visibility_reschedules_itself_while_running(self, controller, view):
        controller.is_streaming = True

        controller.check_visibility()

        assert len(view.scheduled_after) == 1


class TestStaleness:
    """What happens to a frame the screen has already moved past.

    The pipeline takes seconds, and for all of them the picture it is working
    from can stop being what the user is looking at. Publishing that answer
    anyway is not merely late: the Replace overlay paints it over whatever is
    there now, hiding the new text under a translation of the old.
    """

    def test_a_frame_is_not_stale_before_one_is_dispatched(self, controller):
        assert controller._is_stale(image()) is False

    def test_the_same_picture_is_not_stale(self, controller):
        frame = image((128, 128, 128))
        controller._pending_signature = frame_signature(frame)

        assert controller._is_stale(frame) is False

    def test_a_changed_picture_is_stale(self, controller):
        controller._pending_signature = frame_signature(image((255, 255, 255)))

        assert controller._is_stale(image((0, 0, 0))) is True

    def test_watching_clears_the_overlay_once_the_screen_moves(self, controller, view):
        controller._pending_signature = frame_signature(image((255, 255, 255)))
        controller.capture_engine.capture.return_value = (image((0, 0, 0)), BOUNDS)

        controller._watch_while_busy()

        assert view.results == [([], None, False)]

    def test_watching_leaves_a_still_screen_alone(self, controller, view):
        frame = image((200, 200, 200))
        controller._pending_signature = frame_signature(frame)
        controller.capture_engine.capture.return_value = (frame, BOUNDS)

        controller._watch_while_busy()

        assert view.results == []

    def test_watching_retires_the_frame_being_translated(self, controller):
        controller._frame_seq = 7
        controller._pending_signature = frame_signature(image((255, 255, 255)))
        controller.capture_engine.capture.return_value = (image((0, 0, 0)), BOUNDS)

        controller._watch_while_busy()

        assert controller.is_current(7) is False

    def test_watching_does_nothing_with_nothing_in_flight(self, controller, view):
        controller._pending_signature = None

        controller._watch_while_busy()

        controller.capture_engine.capture.assert_not_called()
        assert view.results == []

    def test_a_failed_capture_does_not_stop_the_stream(self, controller, view):
        controller._pending_signature = frame_signature(image())
        controller.capture_engine.capture.side_effect = RuntimeError("device lost")

        controller._watch_while_busy()

        assert view.results == []


class TestGenerationCounter:
    def test_the_frame_in_flight_is_current(self, controller):
        controller._frame_seq = 3

        assert controller.is_current(3) is True
        assert controller.is_current(2) is False

    def test_a_late_result_is_not_drawn(self, controller, view):
        blocks = [{"bbox": [0, 0, 10, 10], "original": "hi", "translated": "สวัสดี"}]
        controller.async_engine.process_image = AsyncMock(return_value=(blocks, ""))
        controller.is_streaming = True
        controller._frame_seq = 5

        asyncio.run(controller.process_frame(image(), BOUNDS, seq=4))

        assert view.results == []

    def test_a_current_result_is_drawn(self, controller, view):
        blocks = [{"bbox": [0, 0, 10, 10], "original": "hi", "translated": "สวัสดี"}]
        controller.async_engine.process_image = AsyncMock(return_value=(blocks, ""))
        controller.is_streaming = True
        controller._frame_seq = 5

        asyncio.run(controller.process_frame(image(), BOUNDS, seq=5))

        assert view.results == [(blocks, "800x600+100+50", True)]

    def test_changing_source_retires_whatever_is_in_flight(self, controller):
        controller._frame_seq = 2

        controller.set_source({"type": "window", "id": 1})

        assert controller.is_current(2) is False

    def test_translation_is_offered_a_way_out(self, controller):
        """The engine is told how to ask whether the answer is still wanted."""
        controller.is_streaming = True
        controller._frame_seq = 1

        asyncio.run(controller.process_frame(image(), BOUNDS, seq=1))

        should_continue = controller.async_engine.process_image.call_args.kwargs["should_continue"]
        assert should_continue() is True

        controller._frame_seq = 2
        assert should_continue() is False

    def test_a_stopped_stream_stops_the_translation(self, controller):
        controller.is_streaming = True

        asyncio.run(controller.process_frame(image(), BOUNDS, seq=1))

        should_continue = controller.async_engine.process_image.call_args.kwargs["should_continue"]
        controller.is_streaming = False
        assert should_continue() is False
