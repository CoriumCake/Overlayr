"""Tests for consensus voting and for placing region reads back in the frame.

Windows OCR reports no confidence of its own (CLAUDE.md section 3), so which of
several disagreeing readings wins - and how sure we are of it - is decided here.
Getting it wrong is invisible: the pipeline still produces text, just the wrong
text, which is why these two functions are pinned down by tests.
"""
import pytest

from overlayr.services.region_provider import Region
from overlayr.services.windows_ocr import (Rect, TextLine, cluster_text_lines,
                                           place_lines_in_frame, technique_rank)


def _line(text, x=100, y=100, width=200, height=20, source="baseline"):
    """One TextLine holding a single word box at a given spot."""
    return TextLine(text, [Rect(text, x, y, width, height)], source=source)


class TestConsensusVote:
    def test_the_reading_most_techniques_agree_on_wins(self):
        lines = [
            _line("GROUND RULES", source="baseline"),
            _line("GROUND RULES", source="clahe"),
            _line("€ROUNP RULES LONGER", source="thicken"),
        ]

        result = cluster_text_lines(lines)

        assert len(result) == 1
        assert result[0].text == "GROUND RULES"

    def test_confidence_is_the_share_that_agreed(self):
        lines = [
            _line("hello there", source="baseline"),
            _line("hello there", source="clahe"),
            _line("hetlo thero", source="thicken"),
            _line("hetlo thera", source="simple_threshold"),
        ]

        result = cluster_text_lines(lines)

        assert result[0].confidence == pytest.approx(0.5)

    def test_a_single_unopposed_reading_is_fully_confident(self):
        # A line only one technique could see is what the region pass exists to
        # rescue; scoring it as doubtful would throw it away again.
        result = cluster_text_lines([_line("only one read")])

        assert result[0].confidence == 1.0
        assert result[0].text == "only one read"

    def test_a_tie_is_settled_by_which_reading_looks_like_language(self):
        lines = [
            _line("ground rules", source="baseline"),
            _line("qrouncl ruies", source="thicken"),
        ]

        result = cluster_text_lines(lines)

        assert result[0].text == "ground rules"

    def test_lines_far_apart_are_not_merged(self):
        lines = [_line("first", y=100), _line("second", y=900)]

        result = cluster_text_lines(lines)

        assert len(result) == 2

    def test_every_technique_that_saw_the_line_is_recorded(self):
        lines = [
            _line("same text", source="baseline"),
            _line("same text", source="clahe"),
        ]

        result = cluster_text_lines(lines)

        assert result[0].sources == {"baseline", "clahe"}

    def test_empty_input(self):
        assert cluster_text_lines([]) == []


class TestTechniqueRank:
    def test_the_untouched_image_is_trusted_most(self):
        assert technique_rank(_line("x", source="baseline")) < \
               technique_rank(_line("x", source="thicken"))

    def test_a_region_source_is_ranked_by_its_technique(self):
        assert technique_rank(_line("x", source="clahe@3")) == \
               technique_rank(_line("x", source="clahe"))

    def test_an_unknown_source_ranks_last(self):
        assert technique_rank(_line("x", source="something-else")) > \
               technique_rank(_line("x", source="inverted"))


class TestPlaceLinesInFrame:
    def test_a_region_read_is_offset_to_where_the_region_sits(self):
        region = Region(300, 400, 500, 460)
        lines = [_line("text", x=10, y=5, width=100, height=20)]

        placed = place_lines_in_frame(lines, region, extra_scale=1.0, source="clahe@0")

        word = placed[0].words[0]
        assert (word.x, word.y) == (310, 405)
        assert (word.width, word.height) == (100, 20)

    def test_an_enlarged_crop_is_scaled_back_down(self):
        region = Region(300, 400, 500, 460)
        lines = [_line("text", x=20, y=10, width=200, height=40)]

        placed = place_lines_in_frame(lines, region, extra_scale=2.0, source="clahe@0")

        word = placed[0].words[0]
        assert (word.x, word.y) == (310, 405)
        assert (word.width, word.height) == (100, 20)

    def test_the_region_is_remembered_on_the_line(self):
        region = Region(0, 0, 100, 100)
        placed = place_lines_in_frame([_line("t")], region, 1.0, "clahe@0")

        assert placed[0].region is region
        assert placed[0].sources == {"clahe@0"}
