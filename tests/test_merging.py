from overlayr.core.engine import AsyncEngine


def test_merge_text_blocks_horizontal():
    engine = AsyncEngine()
    elements = [
        {"bbox": [10, 10, 50, 20], "original": "Hello", "bg_color": "#000"},
        {"bbox": [60, 10, 100, 20], "original": "world", "bg_color": "#000"}
    ]
    merged = engine._merge_text_blocks(elements, mode="line")
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


def test_paragraph_merges_short_orphaned_final_line():
    engine = object.__new__(AsyncEngine)
    paragraph = {
        "bbox": [8, 205, 746, 306],
        "original": (
            "Crops planted in such poor conditions will take some time to "
            "grow. To speed up the process, fill a wooden watering can with "
            "water and use it to water your plants, causing them to grow much"
        ),
        "bg_color": "#202428",
        "text_color": "#ffffff",
        "source_boxes": [
            [8, 205, 714, 227],
            [8, 231, 741, 253],
            [8, 257, 728, 279],
            [8, 283, 746, 306],
        ],
        "line_height": 22,
        "glyph_height": 20,
    }
    orphan = {
        "bbox": [8, 311, 91, 334],
        "original": "faster.",
        "bg_color": "#202428",
        "text_color": "#ffb300",
        "source_boxes": [[8, 311, 91, 334]],
        "line_height": 23,
        "glyph_height": 20,
    }

    merged = engine._merge_text_blocks(
        [paragraph, orphan],
        mode="paragraph",
        dbnet_regions=[],
    )

    assert len(merged) == 1
    assert merged[0]["original"].endswith("grow much faster.")
    assert merged[0]["bbox"] == [8, 205, 746, 334]
    assert len(merged[0]["source_boxes"]) == 5


def test_paragraph_does_not_merge_independent_short_labels():
    engine = object.__new__(AsyncEngine)
    tasks = {
        "bbox": [143, 72, 225, 99],
        "original": "Tasks",
        "bg_color": "#202428",
        "source_boxes": [[143, 72, 225, 99]],
    }
    rewards = {
        "bbox": [499, 72, 624, 99],
        "original": "Rewards",
        "bg_color": "#202428",
        "source_boxes": [[499, 72, 624, 99]],
    }

    merged = engine._merge_text_blocks(
        [tasks, rewards],
        mode="paragraph",
        dbnet_regions=[],
    )

    assert [item["original"] for item in merged] == ["Tasks", "Rewards"]
