import pytest
from overlayr.core.engine import AsyncEngine

def test_merge_text_blocks_horizontal():
    engine = AsyncEngine()
    elements = [
        {"bbox": [10, 10, 50, 20], "original": "Hello", "bg_color": "#000"},
        {"bbox": [60, 10, 100, 20], "original": "world", "bg_color": "#000"}
    ]
    merged = engine._merge_text_blocks(elements)
    assert len(merged) == 1
    assert merged[0]["original"] == "Hello world"
    assert merged[0]["bbox"] == [10, 10, 100, 20]

def test_merge_text_blocks_vertical():
    engine = AsyncEngine()
    elements = [
        {"bbox": [10, 10, 50, 20], "original": "First line", "bg_color": "#000"},
        {"bbox": [10, 30, 50, 40], "original": "Second line", "bg_color": "#000"}
    ]
    merged = engine._merge_text_blocks(elements)
    assert len(merged) == 1
    assert merged[0]["original"] == "First line Second line"
    assert merged[0]["bbox"] == [10, 10, 50, 40]

def test_merge_text_blocks_distant():
    engine = AsyncEngine()
    elements = [
        {"bbox": [10, 10, 50, 20], "original": "Top left", "bg_color": "#000"},
        {"bbox": [400, 400, 450, 410], "original": "Bottom right", "bg_color": "#000"}
    ]
    merged = engine._merge_text_blocks(elements)
    assert len(merged) == 2
