"""Tests for region finding.

Regions decide which parts of a frame get read on their own terms, and in
Phase 3 they will also decide what may not be merged together - so what matters
here is that a region covers the text it should and stops where the next one
starts.
"""
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from overlayr.core.scene import SceneType
from overlayr.services.region_provider import (MAX_REGIONS, Region,
                                               find_document_regions,
                                               find_game_ui_regions,
                                               find_regions, merge_overlapping)


def _font(size):
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _bgr(image: Image.Image) -> np.ndarray:
    return np.array(image)[:, :, ::-1].copy()


@pytest.fixture
def two_column_page():
    page = Image.new("RGB", (800, 600), (252, 252, 252))
    draw = ImageDraw.Draw(page)
    font = _font(14)
    for i in range(20):
        draw.text((60, 40 + i * 24), "left column text here", fill=(20, 20, 20), font=font)
        draw.text((470, 40 + i * 24), "right column text here", fill=(20, 20, 20), font=font)
    return _bgr(page)


@pytest.fixture
def game_frame():
    frame = np.zeros((720, 1280, 3), np.uint8)
    frame[:, :, 0] = 40
    frame[:, :, 1] = 110
    frame[:, :, 2] = 190
    frame[560:680, 120:1160] = (20, 20, 45)
    image = Image.fromarray(frame[:, :, ::-1])
    draw = ImageDraw.Draw(image)
    font = _font(28)
    draw.text((160, 590), "Place the order in the kitchen", fill=(250, 250, 250), font=font)
    draw.text((40, 40), "HP 240", fill=(255, 255, 255), font=font)
    return _bgr(image)


class TestRegion:
    def test_geometry(self):
        region = Region(10, 20, 110, 70)
        assert region.width == 100
        assert region.height == 50
        assert region.area == 5000
        assert region.box == (10, 20, 110, 70)

    def test_crop_matches_the_box(self):
        image = np.arange(100 * 200, dtype=np.uint8).reshape(100, 200)
        assert Region(20, 10, 60, 40).crop(image).shape == (30, 40)

    def test_contains_point_uses_the_box_without_a_polygon(self):
        region = Region(0, 0, 50, 50)
        assert region.contains_point(25, 25) is True
        assert region.contains_point(75, 25) is False

    def test_contains_point_uses_the_polygon_when_there_is_one(self):
        # A triangle filling the lower-left half of a 100x100 box: the top-right
        # corner is inside the box but outside the shape.
        triangle = np.array([[0, 0], [0, 100], [100, 100]],
                            dtype=np.int32).reshape(-1, 1, 2)
        region = Region(0, 0, 100, 100, polygon=triangle)
        assert region.contains_point(10, 90) is True
        assert region.contains_point(90, 10) is False

    def test_to_contour_without_a_polygon_is_the_corners(self):
        contour = Region(1, 2, 3, 4).to_contour()
        assert contour.shape == (4, 1, 2)


class TestMergeOverlapping:
    def test_a_box_inside_another_is_absorbed(self):
        outer = Region(0, 0, 100, 100)
        inner = Region(10, 10, 40, 40)
        assert merge_overlapping([outer, inner]) == [outer]

    def test_separate_boxes_are_left_alone(self):
        a = Region(0, 0, 40, 40)
        b = Region(200, 200, 240, 240)
        assert len(merge_overlapping([a, b])) == 2

    def test_merging_keeps_the_union(self):
        a = Region(0, 0, 100, 100)
        b = Region(50, 50, 150, 150)
        merged = merge_overlapping([a, b], threshold=0.2)
        assert len(merged) == 1
        assert merged[0].box == (0, 0, 150, 150)


class TestDocumentRegions:
    def test_two_columns_are_split(self, two_column_page):
        regions = find_document_regions(two_column_page)
        assert len(regions) == 2

    def test_the_split_falls_in_the_gutter(self, two_column_page):
        left, right = sorted(find_document_regions(two_column_page),
                             key=lambda r: r.left)
        assert left.right < right.left

    def test_a_single_column_stays_one_region(self):
        page = Image.new("RGB", (600, 500), (250, 250, 250))
        draw = ImageDraw.Draw(page)
        font = _font(15)
        for i in range(16):
            draw.text((50, 30 + i * 26), "one column of running text",
                      fill=(20, 20, 20), font=font)
        assert len(find_document_regions(_bgr(page))) == 1

    def test_a_blank_page_yields_nothing(self):
        blank = np.full((200, 300, 3), 255, np.uint8)
        assert find_document_regions(blank) == []


class TestGameRegions:
    def test_finds_the_dialogue_box_and_the_hud(self, game_frame):
        regions = find_game_ui_regions(game_frame)
        assert 2 <= len(regions) <= MAX_REGIONS

    def test_regions_do_not_cover_the_whole_frame(self, game_frame):
        regions = find_game_ui_regions(game_frame)
        covered = sum(r.area for r in regions)
        assert covered < game_frame.shape[0] * game_frame.shape[1] * 0.6

    def test_a_region_covers_the_dialogue_text(self, game_frame):
        # The dialogue sits around y=590, x=160.
        regions = find_game_ui_regions(game_frame)
        assert any(r.contains_point(200, 600) for r in regions)

    def test_regions_stay_inside_the_frame(self, game_frame):
        height, width = game_frame.shape[:2]
        for region in find_game_ui_regions(game_frame):
            assert 0 <= region.left < region.right <= width
            assert 0 <= region.top < region.bottom <= height


class TestFindRegions:
    def test_dispatches_by_scene(self, two_column_page):
        assert len(find_regions(two_column_page, SceneType.DOCUMENT)) == 2

    def test_unknown_scene_asks_for_the_whole_frame(self, two_column_page):
        assert find_regions(two_column_page, None) == []
