"""Tests for image_utils — RMS image comparison."""
import pytest
from PIL import Image
from overlayr.utils.image_utils import (frame_signature, is_different,
                                        signature_similarity)


class TestIsDifferent:
    def test_identical_images_are_not_different(self):
        img = Image.new("RGB", (100, 100), color=(128, 128, 128))
        assert is_different(img, img) is False

    def test_none_image_is_different(self):
        img = Image.new("RGB", (100, 100))
        assert is_different(None, img) is True
        assert is_different(img, None) is True
        assert is_different(None, None) is True

    def test_different_sizes_are_different(self):
        img1 = Image.new("RGB", (100, 100))
        img2 = Image.new("RGB", (200, 200))
        assert is_different(img1, img2) is True

    def test_black_vs_white_is_different(self):
        img1 = Image.new("RGB", (100, 100), color=(0, 0, 0))
        img2 = Image.new("RGB", (100, 100), color=(255, 255, 255))
        assert is_different(img1, img2) is True

    def test_slight_difference_below_threshold(self):
        """Two nearly identical images should not be flagged as different."""
        img1 = Image.new("RGB", (100, 100), color=(128, 128, 128))
        img2 = Image.new("RGB", (100, 100), color=(130, 130, 130))
        assert is_different(img1, img2, threshold=10.0) is False

    def test_custom_threshold(self):
        img1 = Image.new("RGB", (100, 100), color=(0, 0, 0))
        img2 = Image.new("RGB", (100, 100), color=(255, 255, 255))
        # Even very different images pass with high enough threshold
        assert is_different(img1, img2, threshold=999.0) is False


class TestFrameSignature:
    """The cheap stand-in used to compare frames while the pipeline is busy."""

    def test_a_signature_has_a_fixed_size_whatever_the_frame(self):
        wide = frame_signature(Image.new("RGB", (1920, 1080)))
        small = frame_signature(Image.new("RGB", (320, 240)))

        assert wide.shape == small.shape

    def test_no_image_has_no_signature(self):
        assert frame_signature(None) is None

    def test_the_same_frame_scores_full_similarity(self):
        signature = frame_signature(Image.new("RGB", (400, 300), (90, 90, 90)))

        assert signature_similarity(signature, signature) == pytest.approx(100.0)

    def test_black_against_white_scores_zero(self):
        black = frame_signature(Image.new("RGB", (400, 300), (0, 0, 0)))
        white = frame_signature(Image.new("RGB", (400, 300), (255, 255, 255)))

        assert signature_similarity(black, white) == pytest.approx(0.0)

    def test_a_missing_signature_is_never_similar(self):
        signature = frame_signature(Image.new("RGB", (400, 300)))

        assert signature_similarity(None, signature) == 0.0
        assert signature_similarity(signature, None) == 0.0

    def test_a_small_change_stays_similar(self):
        """A cursor or a blinking caret must not read as a new page."""
        base = Image.new("RGB", (400, 300), (255, 255, 255))
        nudged = base.copy()
        nudged.paste(Image.new("RGB", (8, 8), (0, 0, 0)), (10, 10))

        similarity = signature_similarity(frame_signature(base),
                                          frame_signature(nudged))

        assert similarity > 90.0
