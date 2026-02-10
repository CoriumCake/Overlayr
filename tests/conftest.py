"""Shared test fixtures for Overlayr tests."""
import sys
import os

# Add src/ to path so 'import overlayr' works without pip install -e .
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from unittest.mock import MagicMock, patch
from PIL import Image
import numpy as np


@pytest.fixture
def sample_image():
    """Create a simple test image (100x100 white)."""
    return Image.new("RGB", (100, 100), color=(255, 255, 255))


@pytest.fixture
def sample_image_dark():
    """Create a simple dark test image (100x100 black)."""
    return Image.new("RGB", (100, 100), color=(0, 0, 0))


@pytest.fixture
def sample_ocr_results():
    """Standard OCR result format: list of (bbox, text, confidence)."""
    return [
        ([[10, 10], [50, 10], [50, 25], [10, 25]], "Hello", 0.95),
        ([[10, 40], [80, 40], [80, 55], [10, 55]], "World", 0.90),
    ]


@pytest.fixture
def mock_status_callback():
    """A mock callable for status updates."""
    return MagicMock()
