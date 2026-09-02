"""Tests for scene detection.

The images are synthesised to carry the traits CLAUDE.md section 5B lists for
each scene, and are drawn with real glyphs rather than solid bars: the
classifier separates a page of text from a page of art by how solidly ink fills
the frame, so text painted as filled rectangles would not be a document at all.

Thresholds were fitted against the captures in TestingImage/, which are not part
of the repository; these fixtures stand in for them so the suite stays
self-contained.
"""
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from overlayr.core.scene import (SCENE_CHOICES, SceneType, label_for,
                                 merge_mode_for, resolve_saved_scene,
                                 scene_from_label)
from overlayr.services.scene_classifier import SceneClassifier, process_hint

BODY_TEXT = ("the quick brown fox jumps over the lazy dog while the "
             "translation overlay renders every line of text on screen ")


def _font(size):
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


@pytest.fixture
def document_image():
    page = Image.new("RGB", (760, 980), (250, 250, 250))
    draw = ImageDraw.Draw(page)
    font = _font(15)
    for i in range(46):
        draw.text((45, 30 + i * 20), BODY_TEXT[:78], fill=(20, 20, 20), font=font)
    return page


@pytest.fixture
def two_column_document_image():
    page = Image.new("RGB", (760, 980), (252, 252, 252))
    draw = ImageDraw.Draw(page)
    font = _font(14)
    for i in range(46):
        draw.text((40, 30 + i * 20), BODY_TEXT[:36], fill=(25, 25, 25), font=font)
        draw.text((400, 30 + i * 20), BODY_TEXT[10:46], fill=(25, 25, 25), font=font)
    return page


@pytest.fixture
def game_image():
    frame = np.zeros((720, 1280, 3), np.uint8)
    frame[:, :, 0] = 30
    frame[:, :, 1] = 90
    frame[:, :, 2] = 200
    frame[540:660, 100:1180] = (20, 20, 40)
    image = Image.fromarray(frame)
    draw = ImageDraw.Draw(image)
    font = _font(26)
    draw.text((140, 570), "Place the order in the kitchen", fill=(250, 250, 250), font=font)
    draw.text((140, 610), "Continue.", fill=(250, 250, 250), font=font)
    return image


@pytest.fixture
def dark_game_image():
    frame = np.full((720, 1280, 3), 16, np.uint8)
    frame[:, :, 2] = 46  # the blue cast a dark game frame usually carries
    frame[600:700, 60:600] = (40, 40, 90)
    frame[30:60, 30:300] = (200, 40, 40)
    image = Image.fromarray(frame)
    draw = ImageDraw.Draw(image)
    draw.text((80, 630), "HP 240 / 320   MP 88", fill=(240, 240, 240), font=_font(22))
    return image


@pytest.fixture
def comic_image():
    """A page of art and screentone with speech text in two bubbles."""
    rng = np.random.default_rng(7)
    page = np.full((1000, 700, 3), 250, np.uint8)

    # Panel art: solid dark shapes and a screentone field.
    page[40:480, 40:330] = 40
    page[120:300, 360:660] = 90
    tone = (rng.random((480, 300)) < 0.45) * 255
    page[520:1000, 40:340] = np.dstack([tone.astype(np.uint8)] * 3)
    page[540:900, 380:660] = 70

    # Panel borders.
    for x in (8, 348, 684):
        page[:, x:x + 5] = 0
    for y in (8, 496, 992):
        page[y:y + 5, :] = 0

    image = Image.fromarray(page)
    draw = ImageDraw.Draw(image)
    font = _font(20)
    for cx, cy in ((430, 60), (120, 620)):
        draw.ellipse((cx - 90, cy - 40, cx + 90, cy + 40), fill=(255, 255, 255),
                     outline=(0, 0, 0), width=3)
        draw.text((cx - 70, cy - 22), "NO WAY!", fill=(0, 0, 0), font=font)
        draw.text((cx - 70, cy + 2), "I REFUSE", fill=(0, 0, 0), font=font)
    return image


class TestSceneEnum:
    def test_dropdown_offers_auto_plus_three_scenes(self):
        assert SCENE_CHOICES == ["Auto", "Game", "Document", "Comic"]

    def test_labels_round_trip(self):
        for scene in SceneType:
            assert scene_from_label(label_for(scene)) is scene

    def test_auto_has_no_scene(self):
        assert scene_from_label("Auto") is None

    def test_documents_are_grouped_by_line(self):
        assert merge_mode_for(SceneType.DOCUMENT) == "line"

    def test_bubbles_and_dialogue_are_grouped_as_blocks(self):
        assert merge_mode_for(SceneType.COMIC) == "paragraph"
        assert merge_mode_for(SceneType.GAME_UI) == "paragraph"


class TestSavedSceneSetting:
    def test_no_setting_means_auto(self):
        assert resolve_saved_scene(None, None) is None

    def test_auto_is_auto(self):
        assert resolve_saved_scene("Auto", None) is None

    def test_saved_scene_is_restored(self):
        assert resolve_saved_scene("Comic", None) is SceneType.COMIC

    def test_legacy_document_grouping_migrates_to_document(self):
        assert resolve_saved_scene(None, "Document (Lines)") is SceneType.DOCUMENT

    def test_legacy_manga_grouping_migrates_to_auto(self):
        assert resolve_saved_scene(None, "Group (Manga)") is None

    def test_explicit_auto_beats_the_legacy_setting(self):
        assert resolve_saved_scene("Auto", "Document (Lines)") is None

    def test_unknown_value_falls_back_to_auto(self):
        assert resolve_saved_scene("Spreadsheet", None) is None


class TestProcessHint:
    def test_pdf_reader_means_document(self):
        assert process_hint("SumatraPDF.exe") is SceneType.DOCUMENT

    def test_comic_reader_means_comic(self):
        assert process_hint("cdisplayex.exe") is SceneType.COMIC

    def test_browser_alone_is_undecided(self):
        assert process_hint("chrome.exe") is None

    def test_browser_title_can_decide(self):
        assert process_hint("chrome.exe", "One Piece manga - chapter 1") is SceneType.COMIC
        assert process_hint("chrome.exe", "annual-report.pdf") is SceneType.DOCUMENT

    def test_unknown_executable_leans_game(self):
        assert process_hint("SomeGame.exe") is SceneType.GAME_UI

    def test_no_information_gives_no_hint(self):
        assert process_hint(None, None) is None


class TestImageHeuristics:
    def test_document_page(self, document_image):
        assert SceneClassifier().classify_frame(document_image) is SceneType.DOCUMENT

    def test_two_column_document(self, two_column_document_image):
        assert SceneClassifier().classify_frame(two_column_document_image) is SceneType.DOCUMENT

    def test_colourful_game_frame(self, game_image):
        assert SceneClassifier().classify_frame(game_image) is SceneType.GAME_UI

    def test_dark_game_frame(self, dark_game_image):
        assert SceneClassifier().classify_frame(dark_game_image) is SceneType.GAME_UI

    def test_comic_page(self, comic_image):
        assert SceneClassifier().classify_frame(comic_image) is SceneType.COMIC

    def test_image_outvotes_a_weak_process_hint(self, document_image):
        # An unknown executable only leans game; a page of text overrules it.
        classifier = SceneClassifier()
        assert classifier.classify_frame(document_image, "Unknown.exe") is SceneType.DOCUMENT

    def test_process_hint_decides_an_ambiguous_frame(self):
        blank = Image.fromarray(np.full((400, 400, 3), 128, np.uint8))
        classifier = SceneClassifier()
        assert classifier.classify_frame(blank, "SumatraPDF.exe") is SceneType.DOCUMENT

    def test_a_known_reader_outweighs_the_image(self, document_image):
        # A comic reader showing a text-heavy page is still a comic reader;
        # the curated list is the most reliable signal there is.
        classifier = SceneClassifier()
        assert classifier.classify_frame(document_image, "cdisplayex.exe") is SceneType.COMIC


class TestSmoothing:
    def test_single_odd_frame_does_not_flip_the_scene(self, document_image, game_image):
        classifier = SceneClassifier()
        for _ in range(6):
            classifier.classify(document_image)

        assert classifier.classify(game_image) is SceneType.DOCUMENT

    def test_a_sustained_change_does_flip_the_scene(self, document_image, game_image):
        classifier = SceneClassifier()
        for _ in range(4):
            classifier.classify(document_image)
        for _ in range(8):
            scene = classifier.classify(game_image)

        assert scene is SceneType.GAME_UI

    def test_reset_clears_history(self, document_image, game_image):
        classifier = SceneClassifier()
        for _ in range(6):
            classifier.classify(document_image)

        classifier.reset()

        assert classifier.classify(game_image) is SceneType.GAME_UI


class TestOverride:
    def test_override_wins_over_the_image(self, document_image):
        classifier = SceneClassifier()
        classifier.set_override(SceneType.COMIC)

        assert classifier.classify(document_image) is SceneType.COMIC
        assert classifier.current is SceneType.COMIC

    def test_clearing_the_override_returns_to_auto(self, document_image):
        classifier = SceneClassifier()
        classifier.set_override(SceneType.COMIC)
        classifier.set_override(None)

        assert classifier.classify(document_image) is SceneType.DOCUMENT
