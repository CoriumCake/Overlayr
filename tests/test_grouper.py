"""Tests for per-scene grouping.

Grouping is the failure CLAUDE.md section 1 ranks above every other: a bubble
merged with its neighbour is translated as a sentence nobody wrote, and unlike a
misread character the reader cannot recover from it. So most of what is pinned
down here is what must NOT be merged.
"""
import pytest

from overlayr.core.grouper import (ComicGrouper, DocumentGrouper, GameUiGrouper,
                                   LegacyGrouper, filter_nested, get_grouper,
                                   merge_cluster)
from overlayr.core.scene import SceneType
from overlayr.services.region_provider import Region


def element(text, left, top, right, bottom):
    return {"bbox": [left, top, right, bottom], "original": text,
            "bg_color": "#000000"}


def texts(blocks):
    return [block["original"] for block in blocks]


class TestMergeCluster:
    def test_joins_text_top_to_bottom(self):
        block = merge_cluster([element("second", 0, 40, 100, 60),
                               element("first", 0, 0, 100, 20)])
        assert block["original"] == "first second"

    def test_box_covers_every_member(self):
        block = merge_cluster([element("a", 10, 20, 50, 40),
                               element("b", 30, 60, 90, 80)])
        assert block["bbox"] == [10, 20, 90, 80]


class TestFilterNested:
    def test_a_line_inside_a_block_is_dropped(self):
        outer = element("hello there friend", 0, 0, 200, 100)
        inner = element("hello there", 10, 10, 100, 40)
        assert texts(filter_nested([outer, inner])) == ["hello there friend"]

    def test_text_the_outer_box_lacks_is_kept(self):
        # Dropping the inner box outright would lose the words it alone read.
        outer = element("hello there", 0, 0, 200, 100)
        inner = element("completely different words", 10, 10, 100, 40)
        result = filter_nested([outer, inner])
        assert len(result) == 1
        assert "completely different words" in result[0]["original"]

    def test_separate_boxes_are_untouched(self):
        a = element("one", 0, 0, 50, 20)
        b = element("two", 300, 300, 350, 320)
        assert len(filter_nested([a, b])) == 2


class TestComicGrouper:
    def test_one_bubble_is_one_block(self):
        bubble = Region(0, 0, 200, 200, kind="bubble")
        lines = [element("NO WAY", 20, 20, 180, 50),
                 element("I REFUSE", 20, 60, 180, 90)]

        blocks = ComicGrouper([bubble]).group(lines)

        assert texts(blocks) == ["NO WAY I REFUSE"]

    def test_two_bubbles_never_merge_however_close(self):
        # The bubbles nearly touch; distance alone would fuse them.
        left_bubble = Region(0, 0, 200, 100, kind="bubble")
        right_bubble = Region(0, 105, 200, 200, kind="bubble")
        lines = [element("MISS AYASE", 20, 20, 180, 60),
                 element("THE CURSE", 20, 120, 180, 160)]

        blocks = ComicGrouper([left_bubble, right_bubble]).group(lines)

        assert len(blocks) == 2

    def test_a_line_just_outside_its_bubble_still_belongs_to_it(self):
        # Detected outlines hug the text, so a line often lands a few pixels
        # outside. Counting it as region-less would let it merge elsewhere.
        bubble = Region(0, 0, 200, 100, kind="bubble")
        neighbour = Region(0, 130, 200, 220, kind="bubble")
        lines = [element("INSIDE", 20, 20, 180, 50),
                 element("JUST OUT", 20, 104, 180, 126),
                 element("NEIGHBOUR", 20, 150, 180, 180)]

        blocks = ComicGrouper([bubble, neighbour]).group(lines)

        assert "INSIDE JUST OUT" in texts(blocks)

    def test_lines_in_no_region_still_group_when_stacked(self):
        lines = [element("A CAPTION", 20, 20, 180, 50),
                 element("CONTINUES", 20, 55, 180, 85)]

        assert texts(ComicGrouper([]).group(lines)) == ["A CAPTION CONTINUES"]

    def test_lines_in_no_region_stay_apart_when_far(self):
        lines = [element("TOP TEXT", 20, 20, 180, 50),
                 element("FAR BELOW", 20, 400, 180, 430)]

        assert len(ComicGrouper([]).group(lines)) == 2


class TestGameUiGrouper:
    def test_lines_in_one_panel_join(self):
        panel = Region(0, 500, 1000, 700, kind="panel")
        lines = [element("Place the order", 100, 540, 800, 570),
                 element("in the kitchen", 100, 575, 800, 605)]

        blocks = GameUiGrouper([panel]).group(lines)

        assert texts(blocks) == ["Place the order in the kitchen"]

    def test_a_hud_counter_never_joins_the_dialogue(self):
        hud = Region(0, 0, 200, 60, kind="panel")
        dialogue = Region(0, 500, 1000, 700, kind="panel")
        lines = [element("HP 240", 20, 20, 180, 50),
                 element("Place the order", 100, 540, 800, 570)]

        assert len(GameUiGrouper([hud, dialogue]).group(lines)) == 2

    def test_a_much_larger_font_is_a_separate_block(self):
        # A speaker name or heading is set larger and is its own block.
        panel = Region(0, 0, 1000, 400, kind="panel")
        lines = [element("PLAYER", 100, 40, 400, 110),
                 element("some body text here", 100, 120, 800, 145)]

        assert len(GameUiGrouper([panel]).group(lines)) == 2

    def test_a_wide_vertical_gap_splits_a_panel(self):
        panel = Region(0, 0, 1000, 800, kind="panel")
        lines = [element("first line", 100, 40, 400, 70),
                 element("much later", 100, 400, 400, 430)]

        assert len(GameUiGrouper([panel]).group(lines)) == 2


class TestDocumentGrouper:
    def test_fragments_on_one_baseline_become_one_line(self):
        lines = [element("The quick", 50, 100, 200, 125),
                 element("brown fox", 210, 100, 360, 125)]

        assert texts(DocumentGrouper([]).group(lines)) == ["The quick brown fox"]

    def test_consecutive_lines_become_one_paragraph(self):
        lines = [element("first line of the paragraph", 50, 100, 400, 125),
                 element("second line of the paragraph", 50, 130, 400, 155)]

        assert len(DocumentGrouper([]).group(lines)) == 1

    def test_columns_never_merge(self):
        left = Region(40, 80, 410, 600, kind="column")
        right = Region(430, 80, 800, 600, kind="column")
        lines = [element("left column text", 50, 100, 400, 125),
                 element("right column text", 440, 100, 790, 125)]

        blocks = DocumentGrouper([left, right]).group(lines)

        assert len(blocks) == 2
        assert "left column text right column text" not in texts(blocks)

    def test_a_heading_is_not_swallowed_by_the_paragraph(self):
        lines = [element("A Big Heading", 50, 100, 400, 160),
                 element("body text under it", 50, 170, 400, 195)]

        assert len(DocumentGrouper([]).group(lines)) == 2

    def test_a_wide_gap_starts_a_new_paragraph(self):
        lines = [element("end of one paragraph", 50, 100, 400, 125),
                 element("start of the next one", 50, 300, 400, 325)]

        assert len(DocumentGrouper([]).group(lines)) == 2


class TestReadingOrder:
    def test_left_to_right_by_default(self):
        blocks = [element("right", 400, 100, 500, 130),
                  element("left", 50, 100, 150, 130)]

        assert texts(ComicGrouper([], source_lang="en").group(blocks)) == ["left", "right"]

    def test_japanese_reads_right_to_left(self):
        # Manga is read from the right of the page towards the left.
        blocks = [element("left", 50, 100, 150, 130),
                  element("right", 400, 100, 500, 130)]

        assert texts(ComicGrouper([], source_lang="ja").group(blocks)) == ["right", "left"]

    def test_rows_come_before_columns(self):
        blocks = [element("bottom", 50, 500, 150, 530),
                  element("top right", 400, 100, 500, 130),
                  element("top left", 50, 100, 150, 130)]

        result = texts(ComicGrouper([], source_lang="en").group(blocks))

        assert result == ["top left", "top right", "bottom"]


class TestGetGrouper:
    def test_dispatches_by_scene(self):
        assert isinstance(get_grouper(SceneType.COMIC), ComicGrouper)
        assert isinstance(get_grouper(SceneType.GAME_UI), GameUiGrouper)
        assert isinstance(get_grouper(SceneType.DOCUMENT), DocumentGrouper)

    def test_unknown_scene_keeps_the_old_behaviour(self):
        grouper = get_grouper(None, merge_mode="paragraph")
        assert isinstance(grouper, LegacyGrouper)

    def test_empty_input(self):
        assert get_grouper(SceneType.COMIC).group([]) == []
