"""Layout rules shared by the Tk overlay and the harness renderer."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from overlayr.core.overlay_render import (DEFAULT_OVERLAY_STYLE,  # noqa: E402
                                          MIN_FONT_SIZE, STYLE_CARD,
                                          STYLE_REPLACE, base_font_size,
                                          fit_font_size, resolve_saved_style,
                                          wrap_text, wrap_width_for)


def fake_measure(chars_per_line=None):
    """A measurer with predictable metrics: every glyph is `size` wide."""
    def measure(text, size, wrap):
        per_line = chars_per_line or max(1, wrap // max(1, size))
        lines = max(1, -(-len(text) // per_line))
        width = min(len(text), per_line) * size
        return width, lines * size
    return measure


class TestStyleSetting:
    def test_default_is_replace(self):
        assert DEFAULT_OVERLAY_STYLE == STYLE_REPLACE

    def test_known_styles_pass_through(self):
        assert resolve_saved_style(STYLE_CARD) == STYLE_CARD
        assert resolve_saved_style(STYLE_REPLACE) == STYLE_REPLACE

    def test_missing_setting_falls_back(self):
        assert resolve_saved_style(None) == DEFAULT_OVERLAY_STYLE

    def test_stale_setting_falls_back_rather_than_raising(self):
        """A settings file from an older build must not stop the overlay."""
        assert resolve_saved_style("Fancy") == DEFAULT_OVERLAY_STYLE


class TestBaseFontSize:
    def test_measured_line_height_is_preferred(self):
        assert base_font_size("hello", 400, 60, line_height=40) == 36

    def test_box_is_used_when_ocr_reported_no_line_height(self):
        size = base_font_size("hello", 400, 60)
        assert MIN_FONT_SIZE <= size <= 48

    def test_result_is_clamped(self):
        assert base_font_size("x", 10000, 10000, line_height=5000) <= 80
        assert base_font_size("x", 1, 1, line_height=1) >= MIN_FONT_SIZE


class TestFitFontSize:
    def test_text_that_already_fits_keeps_its_size(self):
        size = fit_font_size("hi", 400, 60, fake_measure(), line_height=40)
        assert size == 36

    def test_long_translation_shrinks_to_stay_inside_the_box(self):
        """Thai runs longer than English; the text has to shrink, not spill."""
        text = "คำแปลที่ยาวกว่าต้นฉบับมาก" * 4
        size = fit_font_size(text, 200, 40, fake_measure(), line_height=30)
        assert size < 27
        width, height = fake_measure()(text, size, wrap_width_for(200))
        assert width <= 200 and height <= 40

    def test_a_single_unbreakable_word_shrinks_on_width_alone(self):
        """Wrapping cannot help here, so only the font size can - otherwise the
        block paints over whatever sits beside it. The box is tall enough that
        only the width can be what forces the shrink."""
        size = fit_font_size("A" * 20, 400, 1000, fake_measure(chars_per_line=20),
                             line_height=30)
        assert size == 20

    def test_shrinking_stops_at_the_floor(self):
        size = fit_font_size("A" * 5000, 20, 10, fake_measure(chars_per_line=5000))
        assert size == MIN_FONT_SIZE


class TestWrapText:
    def test_words_are_packed_up_to_the_width(self):
        lines = wrap_text("one two three four", 10, 100,
                          lambda text, size: len(text) * 10)
        assert lines == ["one two", "three four"]

    def test_a_word_wider_than_the_line_is_kept_whole(self):
        lines = wrap_text("supercalifragilistic", 10, 50,
                          lambda text, size: len(text) * 10)
        assert lines == ["supercalifragilistic"]

    def test_existing_newlines_are_respected(self):
        lines = wrap_text("a\nb", 10, 1000, lambda text, size: len(text) * 10)
        assert lines == ["a", "b"]

    def test_empty_text_yields_one_empty_line(self):
        assert wrap_text("", 10, 100, lambda text, size: 0) == [""]


def test_wrap_width_leaves_padding_and_never_goes_to_zero():
    assert wrap_width_for(100) < 100
    assert wrap_width_for(1) >= 1
