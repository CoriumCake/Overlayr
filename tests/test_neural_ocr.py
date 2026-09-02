"""Tests for the second reader and, mostly, for when it is allowed to win.

The reader itself is a model; what needs pinning down is the arbitration around
it. It reads these pages worse than Windows OCR does on average (0.54 against
0.73 word recall measured on the comic set) and is only worth having because it
is wrong about *different* lines. Every guard below is what keeps that trade
positive - remove one and the pipeline gets a confident second opinion with no
reason to trust it.
"""
import numpy as np
import pytest

from overlayr.core.scene import SceneType
from overlayr.services.neural_ocr import (CROP_PADDING, MIN_CONFIDENCE,
                                          NeuralLineReader, choose_reading,
                                          is_suspect)
from overlayr.services.ocr_profile import get_profile


class TestSuspectLines:
    """Which lines are worth spending a second read on."""

    def test_a_line_of_real_words_is_left_alone(self):
        assert is_suspect("THE GROUND RULES") is False

    def test_a_misread_word_makes_a_line_suspect(self):
        assert is_suspect("€ROUNP RULES") is True

    def test_empty_text_is_not_worth_reading_again(self):
        assert is_suspect("") is False
        assert is_suspect("   ") is False


class TestChooseReading:
    """The dictionary decides which of the two readings survives."""

    def test_a_real_word_beats_a_misread_one(self):
        assert choose_reading("HANPS", "HANDS", 0.9) == "HANDS"

    def test_a_misread_word_never_replaces_a_real_one(self):
        assert choose_reading("HANDS", "HANPS", 0.99) == "HANDS"

    def test_the_second_reading_wins_a_tie(self):
        """Measured: taking it on a tie scored 0.819, keeping the first 0.795.

        Only lines Windows OCR was already unsure of reach this function, so a
        tie means neither reading is obviously wrong - and among those, the
        differently-trained reader is the better bet.
        """
        assert choose_reading("HANDS", "GROUND", 0.99) == "GROUND"

    def test_an_unsure_reading_is_ignored_however_good_it_looks(self):
        assert choose_reading("HANPS", "HANDS", MIN_CONFIDENCE - 0.01) == "HANPS"

    def test_an_empty_reading_changes_nothing(self):
        assert choose_reading("HANPS", "", 0.99) == "HANPS"
        assert choose_reading("HANPS", "   ", 0.99) == "HANPS"

    def test_another_alphabet_is_rejected_outright(self):
        """The model carries 18,000 characters and will reach for them.

        A crop it cannot read comes back as CJK with high confidence, and
        nothing downstream would notice: the text just goes to the translator
        as though it were the line.
        """
        assert choose_reading("HANPS", "漢字テスト", 0.99) == "HANPS"

    def test_a_non_ascii_source_may_be_answered_in_kind(self):
        """The guard is about switching scripts, not about banning one."""
        assert choose_reading("日本", "日本語", 0.99) == "日本語"


class TestProfileWiring:
    def test_only_comics_pay_for_a_second_read(self):
        assert get_profile(SceneType.COMIC).uses_neural_reader is True
        assert get_profile(SceneType.GAME_UI).uses_neural_reader is False
        assert get_profile(SceneType.DOCUMENT).uses_neural_reader is False
        assert get_profile(None).uses_neural_reader is False


class TestReaderMechanics:
    """Batching and shaping, which are what keep this inside the time budget."""

    def test_crops_are_padded_up_to_a_bucket_not_to_the_maximum(self):
        reader = NeuralLineReader()
        assert reader._bucket_for(50) == 64
        assert reader._bucket_for(100) == 128
        assert reader._bucket_for(1000) == 320

    def test_a_crop_becomes_a_normalised_channel_first_tensor(self):
        reader = NeuralLineReader()
        tensor, width = reader._prepare(np.full((24, 96, 3), 255, dtype=np.uint8))

        assert tensor.shape == (3, 48, width)
        assert width == 192
        assert tensor.max() == pytest.approx(1.0)

    def test_a_grayscale_crop_is_accepted(self):
        reader = NeuralLineReader()
        tensor, _ = reader._prepare(np.zeros((20, 60), dtype=np.uint8))

        assert tensor.shape[0] == 3

    def test_an_empty_crop_yields_no_tensor(self):
        reader = NeuralLineReader()
        assert reader._prepare(None) == (None, 0)
        assert reader._prepare(np.zeros((0, 10, 3), dtype=np.uint8)) == (None, 0)

    def test_reading_nothing_asks_the_model_nothing(self):
        reader = NeuralLineReader()
        assert reader.read([]) == []

    def test_an_unavailable_model_returns_blanks_rather_than_failing(self):
        """Without the model the pipeline is what it was before: working."""
        reader = NeuralLineReader()
        reader._unavailable = True

        assert reader.read([np.zeros((20, 60, 3), dtype=np.uint8)]) == [("", 0.0)]

    def test_a_failing_batch_leaves_the_other_readings_alone(self, monkeypatch):
        reader = NeuralLineReader()

        class Boom:
            def run(self, *args, **kwargs):
                raise RuntimeError("device removed")

        monkeypatch.setattr(reader, "_load", lambda: Boom())
        crops = [np.zeros((20, 60, 3), dtype=np.uint8)] * 3

        assert reader.read(crops) == [("", 0.0)] * 3


def test_the_crop_margin_is_small_enough_not_to_swallow_a_neighbour():
    """Measured best at 4px; a large margin would pull in the next line."""
    assert 0 < CROP_PADDING <= 8
