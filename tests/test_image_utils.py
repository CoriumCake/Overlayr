"""Tests for image_utils — RMS image comparison."""
import pytest
from PIL import Image
from overlayr.utils.image_utils import is_different


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
