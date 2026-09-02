"""Tests for which detector model is loaded and when it is allowed to run.

Two things here have gone wrong before and would go wrong silently again.

The probability cut-off belongs to the model, not to the algorithm: 0.15 was
swept for the larger net and costs the lite net real recall, while 0.11 is the
lite net's own best value. A model swapped in without its threshold reads worse
and nothing in the pipeline complains, so the pairing is pinned down here.

And the detector is a manga model. It has no business being run over a game
frame or a page - both of which arrive with regions from a much cheaper finder -
yet a `None` slipping through used to buy one full detector run per frame for
nothing (CLAUDE.md 9.30).
"""
import numpy as np
import pytest

from overlayr.core.scene import SceneType
from overlayr.services import ocr_preprocessor as pp
from overlayr.services.windows_ocr import Rect, TextLine, group_text_lines


class TestModelRegistry:
    def test_the_lite_model_leads(self):
        assert pp.DBNET_MODELS[0][0] == "Detec_Manga-English_lite.onnx"

    def test_every_model_carries_its_own_threshold(self):
        for name, threshold in pp.DBNET_MODELS:
            assert name.endswith(".onnx")
            assert 0.0 < threshold < 1.0

    def test_the_two_models_do_not_share_a_threshold(self):
        """Sharing one would mean the sweep never happened for the second."""
        thresholds = [threshold for _, threshold in pp.DBNET_MODELS]
        assert len(set(thresholds)) == len(thresholds)


class TestDetectorScopedToComics:
    """`group_text_lines` may only reach for the detector on a comic page."""

    def _lines(self):
        return [TextLine("HELLO", [Rect("HELLO", 10, 10, 60, 20)], source="baseline")]

    def _image(self):
        return np.zeros((80, 120, 3), dtype=np.uint8)

    @pytest.fixture
    def detector_calls(self, monkeypatch):
        calls = []

        def spy(image, model_path=None):
            calls.append(image)
            return []

        monkeypatch.setattr("overlayr.services.windows_ocr.detect_text_regions_dbnet", spy)
        return calls

    def test_a_comic_with_no_regions_falls_back_to_the_detector(self, detector_calls):
        group_text_lines(self._lines(), self._image(), None, scene=SceneType.COMIC)

        assert len(detector_calls) == 1

    @pytest.mark.parametrize("scene", [SceneType.GAME_UI, SceneType.DOCUMENT, None])
    def test_every_other_scene_leaves_the_detector_alone(self, detector_calls, scene):
        group_text_lines(self._lines(), self._image(), None, scene=scene)

        assert detector_calls == []

    def test_regions_that_were_passed_in_are_never_re_detected(self, detector_calls):
        """Detection is the expensive step; paying for it twice is the bug."""
        contour = np.array([[[0, 0]], [[100, 0]], [[100, 60]], [[0, 60]]], dtype=np.int32)

        group_text_lines(self._lines(), self._image(), [contour], scene=SceneType.COMIC)

        assert detector_calls == []

    def test_lines_survive_when_the_detector_is_skipped(self, detector_calls):
        """Skipping detection must cost regions, never text."""
        groups = group_text_lines(self._lines(), self._image(), None,
                                  scene=SceneType.GAME_UI)

        grouped = [line.text for group in groups for line in group["lines"]]
        assert grouped == ["HELLO"]


class TestProviderChain:
    """DirectML first, CPU behind it, and a loud line whenever one is skipped."""

    def test_directml_leads(self):
        assert pp.DBNET_PROVIDERS[0] == "DmlExecutionProvider"

    def test_cpu_is_the_last_resort(self):
        """A machine with no usable DX12 device still has to read comics."""
        assert pp.DBNET_PROVIDERS[-1] == "CPUExecutionProvider"

    def test_the_first_working_provider_is_used(self, monkeypatch):
        tried = []

        def fake_session(path, providers):
            tried.append(providers[0])
            return f"session on {providers[0]}"

        monkeypatch.setattr(pp.ort, "InferenceSession", fake_session)

        assert pp._make_dbnet_session("model.onnx") == "session on DmlExecutionProvider"
        assert tried == ["DmlExecutionProvider"]

    def test_a_broken_provider_falls_through_to_the_next(self, monkeypatch):
        tried = []

        def fake_session(path, providers):
            tried.append(providers[0])
            if providers[0] != "CPUExecutionProvider":
                raise RuntimeError("no DX12 device")
            return "cpu session"

        monkeypatch.setattr(pp.ort, "InferenceSession", fake_session)

        assert pp._make_dbnet_session("model.onnx") == "cpu session"
        assert tried == list(pp.DBNET_PROVIDERS)

    def test_no_usable_provider_raises_rather_than_returning_nothing(self, monkeypatch):
        def fake_session(path, providers):
            raise RuntimeError("nope")

        monkeypatch.setattr(pp.ort, "InferenceSession", fake_session)

        with pytest.raises(RuntimeError, match="No usable onnxruntime provider"):
            pp._make_dbnet_session("model.onnx")

    def test_warmup_survives_a_missing_model(self, monkeypatch):
        """Start-up must not die because the detector could not be opened."""
        monkeypatch.setattr(pp, "detect_text_regions_dbnet",
                            lambda image: (_ for _ in ()).throw(RuntimeError("gone")))

        pp.warmup_dbnet()  # must not raise
