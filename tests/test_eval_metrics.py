"""Tests for the harness metrics.

The harness is the ruler every later phase measures with, so the ruler itself
has to be checked against known answers.
"""
import pytest

from overlayr.eval.metrics import (cer, levenshtein, line_recall, normalize,
                                   tokenize, word_recall)


class TestNormalize:
    def test_strips_punctuation_and_case(self):
        assert normalize("Hello, World!") == "helloworld"

    def test_folds_full_width(self):
        assert normalize("ＡＢＣ") == "abc"

    def test_empty(self):
        assert normalize("") == ""


class TestTokenize:
    def test_latin_words(self):
        assert tokenize("Press Start to begin") == ["press", "start", "to", "begin"]

    def test_cjk_split_per_character(self):
        assert tokenize("日本語") == ["日", "本", "語"]

    def test_thai_kept_as_run(self):
        assert tokenize("สวัสดี ครับ") == ["สวัสดี", "ครับ"]

    def test_mixed(self):
        assert tokenize("HP 50 体力") == ["hp", "50", "体", "力"]


class TestLevenshtein:
    def test_identical(self):
        assert levenshtein("abc", "abc") == 0

    def test_substitution(self):
        assert levenshtein("abc", "abd") == 1

    def test_empty_side(self):
        assert levenshtein("", "abc") == 3


class TestCer:
    def test_perfect_read(self):
        assert cer("Hello world", "Hello world") == 0.0

    def test_ignores_punctuation_differences(self):
        assert cer("Hello, world!", "Hello world") == 0.0

    def test_one_wrong_character(self):
        assert cer("abcd", "abce") == pytest.approx(0.25)

    def test_missing_output_is_total_loss(self):
        assert cer("some text", "") == 1.0

    def test_capped_at_one_when_output_is_much_longer(self):
        assert cer("hi", "a" * 500) == 1.0

    def test_empty_reference_with_empty_output(self):
        assert cer("", "") == 0.0


class TestWordRecall:
    def test_all_words_found(self):
        assert word_recall("alpha beta", "beta alpha") == 1.0

    def test_half_found(self):
        assert word_recall("alpha beta", "alpha") == pytest.approx(0.5)

    def test_repeats_counted_as_multiset(self):
        assert word_recall("go go go", "go") == pytest.approx(1 / 3)

    def test_extra_words_are_not_penalized(self):
        assert word_recall("alpha", "alpha beta gamma") == 1.0

    def test_empty_reference(self):
        assert word_recall("", "anything") == 1.0


class TestLineRecall:
    def test_exact_lines(self):
        refs = ["first line", "second line"]
        assert line_recall(refs, refs) == 1.0

    def test_missing_line_is_caught(self):
        refs = ["first line", "second line"]
        assert line_recall(refs, ["first line"]) == pytest.approx(0.5)

    def test_small_ocr_errors_still_count_as_captured(self):
        assert line_recall(["press start"], ["prese start"]) == 1.0

    def test_line_split_differently_still_counts(self):
        # OCR merged two ground-truth lines into one block.
        assert line_recall(["hello there", "friend"], ["hello there friend"]) == 1.0

    def test_one_ocr_line_cannot_cover_two_different_lines(self):
        assert line_recall(["alpha text", "beta text"], ["alpha text"]) == pytest.approx(0.5)

    def test_garbage_output_scores_zero(self):
        assert line_recall(["meaningful sentence"], ["zzz"]) == 0.0

    def test_empty_reference(self):
        assert line_recall([], ["anything"]) == 1.0
