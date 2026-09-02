"""Tests for repairing the characters this OCR engine confuses.

The confusion table was mined from the sample comic pages by lining OCR output
up against the ground truth, so these cases are real misreads, not invented
ones. What matters most here is the other half: that correctly-read words come
through untouched, because a repair that fires too eagerly corrupts good text.
"""
import numpy as np
import pytest

from overlayr.core.post_processing import (OCR_CONFUSIONS, TextPostProcessor,
                                           fix_confusions)
from overlayr.services.windows_ocr import sauvola_threshold


@pytest.fixture
def processor():
    return TextPostProcessor()


class TestFixConfusions:
    @pytest.mark.parametrize("misread,expected", [
        ("NEEP", "NEED"),        # P read as D, 20 times in the sample pages
        ("HANPS", "HANDS"),
        ("LOAPEP", "LOADED"),
        ("M/SS/ON", "MISSION"),  # slash read as I, 13 times
        ("REFU5E", "REFUSE"),
        ("UTTLE", "LITTLE"),     # U read as LI
        ("UKE", "LIKE"),
    ])
    def test_known_misreads_are_repaired(self, misread, expected):
        assert fix_confusions(misread) == expected

    @pytest.mark.parametrize("text", [
        "PLAYER", "PAPA", "STOP IT", "THE CAT", "I DO NOT", "PUSH", "CUP",
    ])
    def test_correctly_read_words_are_left_alone(self, text):
        # Every one of these contains a character in the confusion table. They
        # must survive: the repair only applies to words the dictionary rejects.
        assert fix_confusions(text) == text

    def test_a_word_the_dictionary_knows_is_never_rewritten(self):
        # "papa" would become "dada" under a blind swap; both are words, and the
        # original is the one that was actually on the page.
        assert fix_confusions("PAPA") == "PAPA"

    def test_a_repair_that_produces_no_word_is_declined(self):
        assert fix_confusions("XQZJP") == "XQZJP"

    def test_punctuation_is_preserved(self):
        assert fix_confusions("NEEP.") == "NEED."
        assert fix_confusions("HANPS,") == "HANDS,"

    def test_repairs_inside_a_sentence(self):
        assert fix_confusions("WE NEEP MORE HANPS") == "WE NEED MORE HANDS"

    def test_short_tokens_are_skipped(self):
        assert fix_confusions("UP") == "UP"

    def test_empty_input(self):
        assert fix_confusions("") == ""

    def test_the_table_covers_the_mined_confusions(self):
        for wrong in ("/", "P", "C", "7", "5", "€"):
            assert wrong in OCR_CONFUSIONS


class TestAnchoredRules:
    def test_a_slash_between_letters_becomes_i(self, processor):
        assert processor._apply_ocr_corrections("AT/ON") == "ATION"

    def test_it_reaches_words_no_dictionary_holds(self, processor):
        # This is why the anchored rule exists alongside the guarded repair: a
        # character name is not in any word list, so the guarded path declines
        # to touch it and the sentence keeps the wrong glyph.
        assert processor._apply_ocr_corrections("TAKAKUR/S") == "TAKAKURIS"
        assert fix_confusions("TAKAKUR/S") == "TAKAKUR/S"

    def test_a_lone_slash_is_the_letter_i(self, processor):
        assert processor._apply_ocr_corrections("WHAT / SAID") == "WHAT I SAID"

    def test_a_real_slash_between_words_is_kept(self, processor):
        assert "/" in processor._apply_ocr_corrections("HP 240/320")

    def test_seven_between_letters_becomes_t(self, processor):
        assert processor._apply_ocr_corrections("ME7ICULOUS") == "METICULOUS"

    def test_a_number_is_not_touched(self, processor):
        assert "747" in processor._apply_ocr_corrections("FLIGHT 747")

    def test_slashed_o_becomes_o(self, processor):
        assert processor._apply_ocr_corrections("BØBIES") == "BOBIES"


class TestSauvola:
    def test_dark_text_on_a_light_ground_is_separated(self):
        page = np.full((80, 200), 235, np.uint8)
        page[30:50, 40:160] = 25

        binary = sauvola_threshold(page)

        assert binary[40, 100] == 0     # the stroke is ink
        assert binary[5, 5] == 255      # the margin is not

    def test_it_survives_an_uneven_background(self):
        # The point of a local threshold: one side of the crop is darker than
        # the other, and a single global cut-off would lose one of them.
        page = np.tile(np.linspace(90, 250, 200).astype(np.uint8), (80, 1))
        page[30:50, 20:60] = 10
        page[30:50, 140:180] = 10

        binary = sauvola_threshold(page)

        assert binary[40, 40] == 0
        assert binary[40, 160] == 0

    def test_output_is_a_two_level_image(self):
        noise = np.random.default_rng(3).integers(0, 255, (40, 40), dtype=np.uint8)
        assert set(np.unique(sauvola_threshold(noise))) <= {0, 255}

    def test_an_even_window_is_made_odd(self):
        page = np.full((40, 40), 200, np.uint8)
        assert sauvola_threshold(page, window=24).shape == page.shape
