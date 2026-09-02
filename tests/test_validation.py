"""Tests for ValidationService — text filtering and noise rejection."""
import pytest
from overlayr.services.validation_service import ValidationService


class TestValidEnglish:
    def test_basic_sentences(self):
        assert ValidationService.is_valid_text("Hello world", "en") is True
        assert ValidationService.is_valid_text("This is a test sentence.", "en") is True

    def test_short_valid_words(self):
        assert ValidationService.is_valid_text("OK", "en") is True
        assert ValidationService.is_valid_text("Hi!", "en") is True

    def test_honorifics_without_vowels(self):
        """Abbreviations like Mr, Dr should pass even without vowels."""
        assert ValidationService.is_valid_text("Mr", "en") is True
        assert ValidationService.is_valid_text("Dr", "en") is True


class TestNoiseRejection:
    def test_high_symbol_density(self):
        assert ValidationService.is_valid_text("{}[][]=+", "en") is False

    def test_vowel_free_words_are_kept(self):
        # Comics are full of real text with no vowels. Rejecting it lost whole
        # speech bubbles, which CLAUDE.md section 1 counts as the worst failure
        # there is - so consonant runs are let through even though it means
        # some genuine noise gets through with them.
        assert ValidationService.is_valid_text("HMM", "en") is True
        assert ValidationService.is_valid_text("PSST", "en") is True
        assert ValidationService.is_valid_text("TSK", "en") is True

    def test_stray_odd_characters_do_not_reject_a_line(self):
        # A stylised G read as a currency sign should not cost the whole line.
        assert ValidationService.is_valid_text("€ROUNP RULES", "en") is True

    def test_mostly_foreign_text_is_still_rejected(self):
        assert ValidationService.is_valid_text("日本語のテキスト", "en") is False

    def test_coordinates_noise(self):
        assert ValidationService.is_valid_text("(0.0)", "en") is False
        assert ValidationService.is_valid_text("0724", "en") is False

    def test_mixed_noise(self):
        # Pure digits should be rejected
        assert ValidationService.is_valid_text("0724", "en") is False


class TestURLFiltering:
    def test_http_urls_rejected(self):
        assert ValidationService.is_valid_text("https://example.com", "en") is False

    def test_domain_patterns_rejected(self):
        assert ValidationService.is_valid_text("manga.net/chapter1", "en") is False


class TestLengthBounds:
    def test_single_letter_is_kept(self):
        # "I" and "A" are words, and a letter split off from its line is about
        # to be rejoined by the merge step - dropping it here breaks the line.
        assert ValidationService.is_valid_text("a", "en") is True
        assert ValidationService.is_valid_text("I", "en") is True

    def test_single_symbol_is_rejected(self):
        assert ValidationService.is_valid_text("|", "en") is False
        assert ValidationService.is_valid_text("7", "en") is False

    def test_too_long(self):
        assert ValidationService.is_valid_text("A" * 501, "en") is False

    def test_empty_string(self):
        assert ValidationService.is_valid_text("", "en") is False


class TestAsiaticLanguages:
    def test_japanese_text_accepted(self):
        assert ValidationService.is_valid_text("こんにちは", "ja") is True

    def test_thai_requires_thai_chars(self):
        assert ValidationService.is_valid_text("Hello", "th") is False
        assert ValidationService.is_valid_text("สวัสดี", "th") is True

    def test_english_rejects_asiatic_chars(self):
        assert ValidationService.is_valid_text("こんにちは", "en") is False


class TestAsiaticCharDetection:
    def test_thai_detected(self):
        assert ValidationService._has_asiatic_chars("สวัสดี") is True

    def test_japanese_detected(self):
        assert ValidationService._has_asiatic_chars("こんにちは") is True

    def test_korean_detected(self):
        assert ValidationService._has_asiatic_chars("안녕하세요") is True

    def test_ascii_not_detected(self):
        assert ValidationService._has_asiatic_chars("Hello") is False
