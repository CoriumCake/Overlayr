"""Runs the capture loop: grab a frame, decide what it is, translate, draw.

Split out of MainWindow, which had grown to around 1,300 lines with the whole
pipeline inline. The controller owns everything about a running stream - the
worker thread, the similarity gate, the scene state, the frame in flight - and
knows nothing about widgets: it reaches the interface through the small view
protocol below.

That separation is what makes the loop testable at all. It also keeps the two
threads honest: the loop runs on its own thread, the engine on the asyncio loop,
and every touch of the interface goes through ``view.schedule``.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Optional

from ..services.scene_classifier import SceneClassifier
from ..utils.image_utils import (calculate_similarity, frame_signature,
                                 signature_similarity)
from .scene import SceneType, label_for, merge_mode_for

# Frames more alike than this are skipped: the cheapest work is the work not
# done, and re-translating an unchanged frame is pure waste (CLAUDE.md 1).
SIMILARITY_SKIP_PERCENT = 85.0

# How different the screen has to get, while a frame is still being translated,
# before that frame counts as describing something no longer on screen. Set
# apart from the gate above and deliberately more forgiving: the gate decides
# whether new work is worth starting, this decides whether work already done is
# worth showing, and throwing away a good translation over a cursor blink would
# be worse than the staleness it avoids.
STALE_SIMILARITY_PERCENT = 70.0

# How long to wait before looking again after skipping a near-identical frame.
IDLE_SLEEP_SECONDS = 0.2

# Target frame interval, about 30fps.
FRAME_INTERVAL_SECONDS = 0.033

# How often the overlay re-checks whether it should be visible.
VISIBILITY_INTERVAL_MS = 100


class StreamView:
    """What the controller needs from the interface.

    MainWindow implements these; a test can implement them with a list.
    """

    def schedule(self, callback) -> None:
        """Run a callable on the interface thread."""
        raise NotImplementedError

    def show_results(self, results, geometry, visible: bool) -> None:
        raise NotImplementedError

    def set_geometry(self, geometry: str) -> None:
        raise NotImplementedError

    def hide_overlay(self) -> None:
        raise NotImplementedError

    def set_status(self, text: str) -> None:
        raise NotImplementedError


class StreamController:
    """Owns a running capture-translate-draw loop."""

    def __init__(self, view: StreamView, capture_engine, async_engine, loop,
                 scene_override: Optional[SceneType] = None):
        self.view = view
        self.capture_engine = capture_engine
        self.async_engine = async_engine
        self.loop = loop

        self.is_streaming = False
        self.source = None
        self.last_frame = None
        self.last_overlay_data = None
        self.is_target_active = True

        # Which frame the overlay is allowed to be showing. Every dispatch
        # takes the next number, and results arriving under an older one are
        # dropped: a translation of the previous page painted over the current
        # one is worse than no overlay at all, because the Replace style covers
        # the new text with it (CLAUDE.md 9.27).
        self._frame_seq = 0
        # Signature of the frame currently being translated, so the loop can
        # tell whether that frame still describes what is on screen.
        self._pending_signature = None

        self.scene_classifier = SceneClassifier()
        self.scene_classifier.set_override(scene_override)
        self.current_scene = self.scene_classifier.current
        self.current_merge_mode = merge_mode_for(self.current_scene)

    # -- scene ------------------------------------------------------------

    def set_scene_override(self, scene: Optional[SceneType]) -> None:
        """Lock the scene to the user's choice, or None to go back to auto."""
        self.scene_classifier.set_override(scene)
        if scene is not None:
            self._apply_scene(scene)

    def set_source(self, source) -> None:
        """Point the controller at a capture source.

        A different source is a different stream, so the scene history is
        cleared: frames of the last window must not vote on this one.
        """
        self.source = source
        self.scene_classifier.reset()
        self.last_frame = None
        self.last_overlay_data = None
        self._pending_signature = None
        self._frame_seq += 1

    def _apply_scene(self, scene: SceneType, detected: bool = False) -> None:
        if scene == self.current_scene:
            return

        self.current_scene = scene
        self.current_merge_mode = merge_mode_for(scene)
        print(f"Scene: {scene.value} (merge mode: {self.current_merge_mode})", flush=True)

        if detected:
            label = label_for(scene)
            self.view.schedule(lambda: self.view.set_status(f"Detected: {label}"))

    def _classify_scene(self, image) -> None:
        """Re-read the scene from a frame that just passed the similarity gate."""
        if self.scene_classifier.override is not None:
            return

        source = self.source or {}
        try:
            scene = self.scene_classifier.classify(
                image,
                process_name=source.get("process"),
                title=source.get("original_title") or source.get("name"),
            )
        except Exception as e:
            print(f"Scene classification failed: {type(e).__name__}: {e}", flush=True)
            return

        self._apply_scene(scene, detected=True)

    # -- lifecycle --------------------------------------------------------

    def start(self) -> bool:
        """Begin streaming. Returns False when there is nothing to capture."""
        if self.is_streaming or not self.source:
            return False

        self.is_streaming = True
        self.capture_engine.set_source(self.source.get("type"), self.source)
        threading.Thread(target=self._run, daemon=True).start()
        self.check_visibility()
        return True

    def stop(self) -> None:
        self.is_streaming = False
        self.view.hide_overlay()

    def check_visibility(self) -> None:
        """Keep the overlay in step with whether the target is on screen.

        Runs apart from the OCR loop so toggling is instant rather than waiting
        for the current frame to finish.
        """
        if not self.is_streaming:
            return

        # The overlay currently stays up when the user switches windows.
        is_active = True
        previously_active = self.is_target_active
        self.is_target_active = is_active

        if is_active != previously_active:
            if not is_active:
                self.view.show_results([], None, False)
            elif self.last_overlay_data:
                results, geometry = self.last_overlay_data
                self.view.show_results(results, geometry, True)

        self.view.schedule_after(VISIBILITY_INTERVAL_MS, self.check_visibility)

    # -- the loop ---------------------------------------------------------

    def _run(self) -> None:
        print("Stream loop started.", flush=True)
        pending = None
        last_bounds = None

        while self.is_streaming:
            if pending is not None:
                if not pending.done():
                    # Still translating the previous frame. Keep watching the
                    # screen anyway: the whole pipeline takes seconds, and going
                    # blind for all of them is what let a finished translation
                    # land on a page that had already been scrolled away.
                    if last_bounds:
                        self._schedule_geometry(last_bounds)
                    self._watch_while_busy()
                    time.sleep(0.05)
                    continue
                pending = None

            started = time.time()
            try:
                result = self.capture_engine.capture()
                image, bounds = result if result else (None, None)

                if image and bounds:
                    last_bounds = bounds
                    if self._is_repeat_frame(image):
                        time.sleep(IDLE_SLEEP_SECONDS)
                        continue

                    self.last_frame = image
                    # The frame differs enough to be worth processing, which is
                    # exactly when the scene is worth re-checking (CLAUDE.md 5C).
                    self._classify_scene(image)

                    self._frame_seq += 1
                    self._pending_signature = frame_signature(image)
                    pending = asyncio.run_coroutine_threadsafe(
                        self.process_frame(image, bounds, seq=self._frame_seq),
                        self.loop)
                    pending.add_done_callback(self._log_future_error)

            except Exception as e:
                print(f"Stream error: {type(e).__name__}: {e}", flush=True)

            time.sleep(max(0, FRAME_INTERVAL_SECONDS - (time.time() - started)))

        self.view.schedule(self.view.hide_overlay)
        print("Stream loop stopped.", flush=True)

    def _watch_while_busy(self) -> None:
        """Check whether the frame being translated is still on screen.

        Runs on the loop thread rather than a watcher of its own so that the
        capture engine keeps a single caller: the WGC frame pool is drained,
        not shared, and two threads pulling from it would race.

        When the screen has moved on, the overlay is cleared at once instead of
        waiting out the rest of the pipeline. Showing nothing is the honest
        answer for a page whose translation is not ready (CLAUDE.md 1).
        """
        if self._pending_signature is None:
            return

        try:
            result = self.capture_engine.capture()
            image, _ = result if result else (None, None)
            if image is None:
                return

            if self._is_stale(image):
                # Dropping the signature stops this from firing again for the
                # same frame; the sequence number is what actually keeps the
                # late result off the screen.
                self._pending_signature = None
                self._frame_seq += 1
                self.last_frame = None
                self.last_overlay_data = None
                self.view.schedule(lambda: self.view.show_results([], None, False))
        except Exception as e:
            print(f"Staleness check failed: {type(e).__name__}: {e}", flush=True)

    def _is_stale(self, image) -> bool:
        """Whether the screen has moved away from the frame being translated."""
        if self._pending_signature is None:
            return False
        current = frame_signature(image)
        return (signature_similarity(current, self._pending_signature)
                < STALE_SIMILARITY_PERCENT)

    def is_current(self, seq: int) -> bool:
        """Whether results tagged with this sequence number may still be drawn."""
        return seq == self._frame_seq

    def _is_repeat_frame(self, image) -> bool:
        if self.last_frame is None:
            return False
        return calculate_similarity(image, self.last_frame) > SIMILARITY_SKIP_PERCENT

    def _schedule_geometry(self, bounds) -> None:
        geometry = _geometry_of(bounds)
        self.view.schedule(lambda: self.view.set_geometry(geometry))

    @staticmethod
    def _log_future_error(future) -> None:
        try:
            future.result()
        except Exception as e:
            print(f"Frame processing failed: {type(e).__name__}: {e}", flush=True)

    # -- one frame --------------------------------------------------------

    async def process_frame(self, image, bounds, seq=None) -> None:
        """Translate one captured frame and hand the blocks to the overlay.

        ``seq`` is the sequence number the frame was dispatched under. Results
        carrying an old one are discarded rather than drawn.
        """
        if not self.async_engine:
            return

        geometry = _geometry_of(bounds)

        async def step_callback(stage, data):
            if stage == "MERGED":
                self.view.schedule(lambda: self.view.set_geometry(geometry))

        # Translation is the expensive half of the pipeline and it runs after
        # the reading is done, which makes the gap between them the last chance
        # to notice that nobody will ever see the result.
        def still_wanted():
            return self.is_streaming and (seq is None or self.is_current(seq))

        try:
            results, _ = await self.async_engine.process_image(
                image,
                step_callback=step_callback,
                skip_translation=False,
                merge_mode=self.current_merge_mode,
                scene=self.current_scene,
                should_continue=still_wanted,
            )
        except Exception as e:
            print(f"Frame processing error: {type(e).__name__}: {e}", flush=True)
            return

        if not still_wanted():
            return

        self._publish(results, geometry)

        # Yield so the loop thread can pick up the next frame immediately.
        await asyncio.sleep(0)

    def _publish(self, results, geometry) -> None:
        """Hand a set of blocks to the overlay and remember them."""
        visible = self.is_target_active
        self.last_overlay_data = (results, geometry)
        self._pending_signature = None
        self.view.schedule(
            lambda: self.view.show_results(results, geometry, visible))


def _geometry_of(bounds) -> str:
    return (f"{bounds['width']}x{bounds['height']}"
            f"+{bounds['left']}+{bounds['top']}")
