"""Tests for dataset loading and the harness runner.

A fake OCR function stands in for Windows OCR so the harness itself can be
tested on any machine.
"""
import asyncio
import json
import os

from PIL import Image

from overlayr.core.scene import SceneType
from overlayr.eval.dataset import load_dataset, parse_ground_truth
from overlayr.eval.runner import (format_report, reading_order, run_samples,
                                  summarize)


def _write_sample(root, scene_dir, name, lines, meta=None):
    directory = os.path.join(root, scene_dir)
    os.makedirs(directory, exist_ok=True)
    Image.new("RGB", (20, 20), (255, 255, 255)).save(os.path.join(directory, f"{name}.png"))
    with open(os.path.join(directory, f"{name}.gt.txt"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    if meta:
        with open(os.path.join(directory, f"{name}.meta.json"), "w", encoding="utf-8") as handle:
            json.dump(meta, handle)


def _box(left, top, width=100, height=20):
    return [[left, top], [left + width, top],
            [left + width, top + height], [left, top + height]]


def _ocr_returning(lines):
    """Fake OCR: one box per line, stacked down the image."""
    async def fake_ocr(image, lang):
        return [(_box(0, i * 30), text, 0.95) for i, text in enumerate(lines)]
    return fake_ocr


class TestParseGroundTruth:
    def test_drops_comments_and_blank_lines(self):
        raw = "# heading comment\n\nfirst line\n  second line  \n\n"
        assert parse_ground_truth(raw) == ["first line", "second line"]


class TestLoadDataset:
    def test_reads_samples_per_scene(self, tmp_path):
        root = str(tmp_path)
        _write_sample(root, "comic", "page01", ["hello", "there"])
        _write_sample(root, "document", "page01", ["paragraph"])

        samples = load_dataset(root)

        assert [s.name for s in samples] == ["comic/page01", "document/page01"]
        assert samples[0].scene is SceneType.COMIC
        assert samples[0].lines == ["hello", "there"]

    def test_default_language_comes_from_scene(self, tmp_path):
        root = str(tmp_path)
        _write_sample(root, "comic", "page01", ["hello"])
        assert load_dataset(root)[0].lang == "ja"

    def test_meta_file_overrides_language(self, tmp_path):
        root = str(tmp_path)
        _write_sample(root, "comic", "page01", ["hello"], meta={"lang": "ko"})
        assert load_dataset(root)[0].lang == "ko"

    def test_image_without_ground_truth_is_skipped(self, tmp_path):
        root = str(tmp_path)
        directory = os.path.join(root, "game_ui")
        os.makedirs(directory)
        Image.new("RGB", (10, 10)).save(os.path.join(directory, "orphan.png"))

        assert load_dataset(root) == []

    def test_scene_filter(self, tmp_path):
        root = str(tmp_path)
        _write_sample(root, "comic", "page01", ["hello"])
        _write_sample(root, "document", "page01", ["paragraph"])

        samples = load_dataset(root, scene=SceneType.DOCUMENT)

        assert len(samples) == 1
        assert samples[0].scene is SceneType.DOCUMENT

    def test_missing_root_is_not_an_error(self, tmp_path):
        assert load_dataset(str(tmp_path / "nope")) == []


class TestReadingOrder:
    def test_sorts_fragments_top_to_bottom_left_to_right(self):
        # Windows OCR returns fragments in whatever order its pool finished in.
        scrambled = [
            (_box(360, 50), "jumps over the", 0.95),
            (_box(50, 50), "The quick brown", 0.95),
            (_box(50, 120), "second line", 0.95),
            (_box(308, 50), "fox", 0.95),
        ]

        assert reading_order(scrambled) == [
            "The quick brown", "fox", "jumps over the", "second line",
        ]

    def test_slightly_offset_boxes_stay_on_one_row(self):
        boxes = [
            (_box(200, 263), "second", 0.95),
            (_box(50, 261), "first", 0.95),
        ]
        assert reading_order(boxes) == ["first", "second"]

    def test_empty_input(self):
        assert reading_order([]) == []

    def test_blank_text_is_dropped(self):
        assert reading_order([(_box(0, 0), "", 0.95)]) == []


class TestRunSamples:
    def test_perfect_read_scores_full_marks(self, tmp_path):
        root = str(tmp_path)
        _write_sample(root, "game_ui", "shot01", ["Press Start", "New Game"])
        samples = load_dataset(root)

        results = asyncio.run(run_samples(samples, _ocr_returning(["Press Start", "New Game"])))

        assert len(results) == 1
        assert results[0].line_recall == 1.0
        assert results[0].word_recall == 1.0
        assert results[0].cer == 0.0
        assert results[0].missing_lines == []
        assert results[0].passed

    def test_dropped_line_is_reported(self, tmp_path):
        root = str(tmp_path)
        _write_sample(root, "game_ui", "shot01", ["Press Start", "New Game"])
        samples = load_dataset(root)

        results = asyncio.run(run_samples(samples, _ocr_returning(["Press Start"])))

        assert results[0].line_recall == 0.5
        assert results[0].missing_lines == ["New Game"]
        assert not results[0].passed

    def test_summary_groups_by_scene_and_overall(self, tmp_path):
        root = str(tmp_path)
        _write_sample(root, "game_ui", "shot01", ["alpha"])
        _write_sample(root, "comic", "page01", ["alpha"])
        samples = load_dataset(root)

        results = asyncio.run(run_samples(samples, _ocr_returning(["alpha"])))
        summaries = summarize(results)

        assert set(summaries) == {"game_ui", "comic", "overall"}
        assert summaries["overall"].samples == 2
        assert summaries["overall"].passed

    # Short bubble lines, one word of which the OCR misread. Unmerged, each line
    # still matches on character similarity; merged into one block, the same
    # line has to be found word by word and the misread word sinks it.
    BUBBLE_LINES = ["alpha beta", "gamma delta", "epsilon zeta",
                    "eta theta", "iota kappa", "lambda mu"]
    MERGED_BLOCK = ("alpha beta gamma delta epsilon zeta "
                    "eta theta iota kappa lambda nu")

    def test_the_pipeline_stage_is_not_judged_on_line_recall(self, tmp_path):
        # After grouping there are no lines left to count: a bubble correctly
        # merged into one block cannot match the lines it came from, so holding
        # it to line recall punishes the behaviour section 7 asks for.
        root = str(tmp_path)
        _write_sample(root, "comic", "page01", self.BUBBLE_LINES)
        samples = load_dataset(root)

        merged = _ocr_returning([self.MERGED_BLOCK])
        results = asyncio.run(run_samples(samples, merged, stage="pipeline"))

        assert results[0].line_recall < 0.95   # the line test would fail it
        assert results[0].word_recall >= 0.90  # yet the words are all there
        assert results[0].passed               # so the sample passes

    def test_the_pipeline_stage_still_fails_on_lost_words(self, tmp_path):
        root = str(tmp_path)
        _write_sample(root, "comic", "page01", ["alpha beta", "gamma delta"])
        samples = load_dataset(root)

        results = asyncio.run(run_samples(samples, _ocr_returning(["alpha beta"]),
                                          stage="pipeline"))

        assert not results[0].passed

    def test_the_ocr_stage_is_still_judged_on_line_recall(self, tmp_path):
        root = str(tmp_path)
        _write_sample(root, "comic", "page01", self.BUBBLE_LINES)
        samples = load_dataset(root)

        merged = _ocr_returning([self.MERGED_BLOCK])
        results = asyncio.run(run_samples(samples, merged, stage="ocr"))

        assert not results[0].passed

    def test_report_mentions_failing_sample(self, tmp_path):
        root = str(tmp_path)
        _write_sample(root, "comic", "page01", ["alpha", "beta"])
        samples = load_dataset(root)

        results = asyncio.run(run_samples(samples, _ocr_returning(["alpha"])))
        report = format_report(results, summarize(results))

        assert "comic/page01" in report
        assert "BELOW FLOOR" in report
