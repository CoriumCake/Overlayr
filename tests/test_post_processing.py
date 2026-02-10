"""Tests for TextPostProcessor — regex cleaning and custom rules."""
import pytest
from overlayr.core.post_processing import TextPostProcessor


@pytest.fixture
def processor():
    """TextPostProcessor without SymSpell dictionary (no external deps needed)."""
    return TextPostProcessor(dictionary_path="nonexistent")


class TestCleanText:
    def test_empty_string(self, processor):
        assert processor.clean_text("") == ""

    def test_removes_isolated_symbols(self, processor):
        result = processor.clean_text("Hello | World")
        assert "|" not in result
        assert "Hello" in result
        assert "World" in result

    def test_fixes_ligatures(self, processor):
        assert "fi" in processor.clean_text("ﬁnd")
        assert "fl" in processor.clean_text("ﬂow")

    def test_strips_whitespace(self, processor):
        assert processor.clean_text("  Hello  ") == "Hello"


class TestCustomRules:
    def test_z0_to_30(self, processor):
        assert processor._apply_custom_rules("Z0") == "30"
        assert processor._apply_custom_rules("ZO") == "30"

    def test_year_correction(self, processor):
        result = processor._apply_custom_rules("Y3AR")
        assert result == "YEAR"

    def test_zero_to_o_adjacent_to_letter(self, processor):
        result = processor._apply_custom_rules("H0ME")
        assert result == "HOME"

    def test_zero_not_replaced_near_digits(self, processor):
        # "10th" — the 0 is adjacent to a digit, should NOT become O
        result = processor._apply_custom_rules("10th")
        assert "0" in result  # Should remain as 0


class TestCorrectText:
    def test_empty_returns_empty(self, processor):
        assert processor.correct_text("") == ""

    def test_applies_custom_rules_before_correction(self, processor):
        # Even without SymSpell, custom rules should still apply
        result = processor.correct_text("H0ME")
        assert "home" in result.lower()


class TestFuzzyMatch:
    def test_empty_custom_terms_returns_original(self, processor):
        assert processor.fuzzy_match_custom("Hello") == "Hello"

    def test_short_text_returns_original(self, processor):
        processor.custom_terms = ["He"]
        assert processor.fuzzy_match_custom("He") == "He"
