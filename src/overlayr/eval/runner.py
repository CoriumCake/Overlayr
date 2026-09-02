"""Runs the OCR pipeline over the harness dataset and scores the output.

Import-safe on any platform: the Windows OCR engine is only imported when a run
actually starts, so metrics and dataset code stay unit-testable.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Sequence

from ..core.scene import SceneType
from .dataset import Sample, load_dataset
from .metrics import cer, group_separation, line_recall, tokenize, word_recall

# An OCR function takes a PIL image, a language code and the scene, and returns
# the OCR result tuples the engine produces: (bbox_points, text, confidence).
OcrFn = Callable[..., Awaitable[Sequence[tuple]]]

# Accuracy floor from CLAUDE.md section 1: nothing may go missing. These are the
# thresholds a run must clear before effort moves from accuracy to speed.
PASS_LINE_RECALL = 0.95
PASS_WORD_RECALL = 0.90

# The grouping metric only means something once blocks exist, so it is only
# required of the pipeline stage.
PASS_SEPARATION = 0.95

# The overlay stage asks the opposite question of every other stage: after the
# translation is drawn, how much of the *source* text can still be read? What
# is left is what shows around the edges of the paint, so the floor is on how
# much disappeared (CLAUDE.md 9.24).
PASS_COVERAGE = 0.90


@dataclass
class SampleResult:
    name: str
    scene: str
    line_recall: float
    word_recall: float
    cer: float
    separation: float
    extra_ratio: float
    duration_ms: float
    ocr_line_count: int
    ground_truth_line_count: int
    missing_lines: List[str] = field(default_factory=list)
    stage: str = "ocr"
    # Overlay stage only: the share of source words the drawing hid.
    coverage: float = 0.0

    @property
    def passed(self) -> bool:
        """Whether this sample cleared the accuracy floor for its stage.

        Line recall is only a fair test of the OCR stage. Once the grouper has
        run there are no lines left to count - a bubble that was correctly
        joined into one block cannot match the four ground-truth lines it came
        from, so scoring it that way punishes exactly the behaviour section 7
        asks for. Measured on the comic pages: turning merging off moved line
        recall from 0.684 to 0.796 without changing word recall at all, which is
        the proof that nothing was ever lost.

        So the pipeline stage is judged on whether the words survived and
        whether they landed in the right block instead.
        """
        if self.stage == "overlay":
            return self.coverage >= PASS_COVERAGE
        if self.stage == "pipeline":
            return (self.word_recall >= PASS_WORD_RECALL
                    and self.separation >= PASS_SEPARATION)
        return (self.line_recall >= PASS_LINE_RECALL
                and self.word_recall >= PASS_WORD_RECALL)


@dataclass
class SceneSummary:
    scene: str
    samples: int
    line_recall: float
    word_recall: float
    cer: float
    separation: float
    extra_ratio: float
    duration_ms: float
    coverage: float
    passed: bool


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def reading_order(ocr_result: Sequence[tuple]) -> List[str]:
    """Sort raw OCR boxes into top-to-bottom, left-to-right reading order.

    The OCR stage returns fragments in whatever order its technique pool
    finished in, and a word split off from its line is not a loss of meaning -
    the grouper puts it back. Scoring against the unsorted list would measure
    the pool ordering instead of the reading, so the boxes are banded into rows
    by vertical overlap first.
    """
    boxes = []
    for bbox_points, text, _ in ocr_result:
        if not text:
            continue
        ys = [point[1] for point in bbox_points]
        xs = [point[0] for point in bbox_points]
        boxes.append((min(ys), max(ys), min(xs), text))
    if not boxes:
        return []

    heights = sorted(bottom - top for top, bottom, _, _ in boxes)
    tolerance = max(1.0, heights[len(heights) // 2] * 0.6)

    boxes.sort(key=lambda box: box[0])
    rows: List[List[tuple]] = [[boxes[0]]]
    for box in boxes[1:]:
        if box[0] - rows[-1][0][0] <= tolerance:
            rows[-1].append(box)
        else:
            rows.append([box])

    ordered = []
    for row in rows:
        row.sort(key=lambda box: box[2])
        ordered.extend(text for _, _, _, text in row)
    return ordered


def _missing_lines(sample: Sample, ocr_lines: Sequence[str]) -> List[str]:
    """Ground-truth lines that no OCR line covered - the "ตกหล่น" list."""
    missing = []
    for line in sample.lines:
        if line_recall([line], ocr_lines) < 1.0:
            missing.append(line)
    return missing


async def _default_ocr_fn_factory(use_scene: bool = True):
    """Build the OCR function the harness measures.

    ``use_scene`` off reads every sample with the legacy whole-frame profile,
    which is how a region-first change is compared against what came before.
    """
    from ..services.windows_ocr import WindowsOCREngine

    engine = WindowsOCREngine()
    engine.warmup()

    async def run(image, lang: str, scene=None):
        engine.set_language(lang)
        return await engine.extract_text(image, scene=scene if use_scene else None)

    return run


async def _pipeline_fn_factory(use_scene: bool = True):
    """Measure the whole pipeline: OCR, then filtering and grouping.

    What reaches the overlay is not what OCR returned - ValidationService drops
    text it distrusts and the merger reshapes the rest. Measuring only the OCR
    stage would miss a filter that quietly throws real lines away.
    """
    from ..core.engine import AsyncEngine
    from ..core.scene import merge_mode_for

    engine = AsyncEngine()
    engine.warmup()

    async def run(image, lang: str, scene=None):
        await engine.update_languages(lang, "th")
        merge_mode = merge_mode_for(scene) if scene else "paragraph"
        blocks, _ = await engine.process_image(
            image, skip_translation=True, merge_mode=merge_mode,
            scene=scene if use_scene else None)
        results = []
        for block in blocks:
            left, top, right, bottom = block["bbox"]
            results.append((
                [[left, top], [right, top], [right, bottom], [left, bottom]],
                block["original"], 1.0,
            ))
        return results

    return run


async def _overlay_fn_factory(use_scene: bool = True,
                              render_dir: Optional[str] = None):
    """Translate, draw the result over the frame, and read the frame again.

    The lines this returns are what an OCR engine can still make out *after*
    the overlay has painted, so scoring them against the source ground truth
    measures leftover: whatever the paint failed to cover. Translation runs for
    real here rather than being skipped, because drawing the source text back
    onto itself would cover nothing and score perfectly.
    """
    from ..core.engine import AsyncEngine
    from ..core.scene import merge_mode_for
    from .overlay_stage import render_blocks, save_render

    engine = AsyncEngine()
    engine.warmup()

    async def run(image, lang: str, scene=None, name: str = "frame"):
        await engine.update_languages(lang, "th")
        merge_mode = merge_mode_for(scene) if scene else "paragraph"
        blocks, _ = await engine.process_image(
            image, skip_translation=False, merge_mode=merge_mode,
            scene=scene if use_scene else None)

        rendered = render_blocks(image, blocks)
        save_render(rendered, render_dir, name)

        engine.ocr_service.set_language(lang)
        return await engine.ocr_service.extract_text(
            rendered, scene=scene if use_scene else None)

    return run


def _accepted_arg_count(ocr_fn: OcrFn) -> int:
    """How many positional arguments an OCR function will take.

    The signature is read rather than discovered by calling and catching
    TypeError: that older approach also swallowed a TypeError raised *inside*
    the function and silently retried it with fewer arguments, which is exactly
    the kind of quiet failure CLAUDE.md section 10 rules out.
    """
    try:
        parameters = inspect.signature(ocr_fn).parameters.values()
    except (TypeError, ValueError):
        return 2

    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in parameters):
        return 4
    return sum(1 for p in parameters
               if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                             inspect.Parameter.POSITIONAL_OR_KEYWORD))


async def _call_ocr(ocr_fn: OcrFn, image, sample: Sample):
    """Call an OCR function with as many of (image, lang, scene, name) as it takes.

    Test doubles are simpler when they can ignore the scene entirely.
    """
    args = [image, sample.lang, sample.scene, sample.name]
    return await ocr_fn(*args[:max(2, _accepted_arg_count(ocr_fn))])


async def run_samples(samples: Sequence[Sample],
                      ocr_fn: Optional[OcrFn] = None,
                      use_scene: bool = True,
                      stage: str = "ocr",
                      render_dir: Optional[str] = None) -> List[SampleResult]:
    """Score every sample. ``stage`` decides which floor they are held to."""
    from PIL import Image

    if ocr_fn is None:
        if stage == "overlay":
            ocr_fn = await _overlay_fn_factory(use_scene, render_dir)
        elif stage == "pipeline":
            ocr_fn = await _pipeline_fn_factory(use_scene)
        else:
            ocr_fn = await _default_ocr_fn_factory(use_scene)

    results: List[SampleResult] = []
    for sample in samples:
        with Image.open(sample.image_path) as handle:
            image = handle.convert("RGB")

        start = time.perf_counter()
        ocr_result = await _call_ocr(ocr_fn, image, sample)
        duration_ms = (time.perf_counter() - start) * 1000.0

        ocr_lines = reading_order(ocr_result)
        hypothesis = " ".join(ocr_lines)

        reference_tokens = len(tokenize(sample.text))
        leftover = word_recall(sample.text, hypothesis)
        results.append(SampleResult(
            name=sample.name,
            scene=sample.scene.value,
            line_recall=line_recall(sample.lines, ocr_lines),
            word_recall=word_recall(sample.text, hypothesis),
            cer=cer(sample.text, hypothesis),
            separation=group_separation(sample.groups, ocr_lines),
            extra_ratio=(len(tokenize(hypothesis)) / reference_tokens
                         if reference_tokens else 0.0),
            duration_ms=duration_ms,
            ocr_line_count=len(ocr_lines),
            ground_truth_line_count=len(sample.lines),
            missing_lines=_missing_lines(sample, ocr_lines),
            stage=stage,
            coverage=(1.0 - leftover) if stage == "overlay" else 0.0,
        ))
    return results


def summarize(results: Sequence[SampleResult]) -> Dict[str, SceneSummary]:
    summaries: Dict[str, SceneSummary] = {}
    for scene in list(SceneType) + [None]:
        key = scene.value if scene else "overall"
        subset = [r for r in results if scene is None or r.scene == scene.value]
        if not subset:
            continue
        summaries[key] = SceneSummary(
            scene=key,
            samples=len(subset),
            line_recall=_mean([r.line_recall for r in subset]),
            word_recall=_mean([r.word_recall for r in subset]),
            cer=_mean([r.cer for r in subset]),
            separation=_mean([r.separation for r in subset]),
            extra_ratio=_mean([r.extra_ratio for r in subset]),
            duration_ms=_mean([r.duration_ms for r in subset]),
            coverage=_mean([r.coverage for r in subset]),
            passed=all(r.passed for r in subset),
        )
    return summaries


def format_report(results: Sequence[SampleResult],
                  summaries: Dict[str, SceneSummary],
                  baseline: Optional[dict] = None) -> str:
    lines = []
    stage = results[0].stage if results else "ocr"
    # At the overlay stage 'word' is leftover source text, so the column that
    # matters is how much of it the drawing hid.
    fourth = "cover" if stage == "overlay" else "sep"
    header = (f"{'sample':<34}{'line':>8}{'word':>8}{fourth:>8}{'CER':>8}"
              f"{'extra':>8}{'ms':>9}  miss")
    lines.append(header)
    lines.append("-" * len(header))
    for result in results:
        flag = " " if result.passed else "!"
        fourth_value = result.coverage if stage == "overlay" else result.separation
        lines.append(
            f"{flag}{result.name:<33}"
            f"{result.line_recall:>8.3f}{result.word_recall:>8.3f}"
            f"{fourth_value:>8.3f}"
            f"{result.cer:>8.3f}{result.extra_ratio:>8.2f}{result.duration_ms:>9.0f}"
            f"  {len(result.missing_lines)}"
        )

    lines.append("")
    lines.append(f"{'scene':<34}{'line':>8}{'word':>8}{fourth:>8}{'CER':>8}"
                 f"{'extra':>8}{'ms':>9}  n")
    lines.append("-" * len(header))
    base_scenes = (baseline or {}).get("summary", {})
    for key, summary in summaries.items():
        summary_fourth = summary.coverage if stage == "overlay" else summary.separation
        row = (f" {key:<33}{summary.line_recall:>8.3f}{summary.word_recall:>8.3f}"
               f"{summary_fourth:>8.3f}"
               f"{summary.cer:>8.3f}{summary.extra_ratio:>8.2f}"
               f"{summary.duration_ms:>9.0f}  {summary.samples}")
        base = base_scenes.get(key)
        if base:
            delta = summary.line_recall - base["line_recall"]
            row += f"   line {delta:+.3f}"
            if "separation" in base:
                row += f"  sep {summary.separation - base['separation']:+.3f}"
            row += " vs baseline"
        lines.append(row)

    overall = summaries.get("overall")
    if overall:
        verdict = "PASS" if overall.passed else "BELOW FLOOR"
        lines.append("")
        if overall.extra_ratio > 1.3:
            lines.append("CER is inflated: the OCR read roughly "
                         f"{overall.extra_ratio:.1f}x the annotated text, so most "
                         "of the error is text the ground truth does not cover.")

        if stage == "overlay":
            lines.append("At this stage 'word' is the source text an OCR pass "
                         "could still read through the drawing, so lower is "
                         "better; 'cover' is what disappeared.")
            lines.append(f"Coverage floor (cover>={PASS_COVERAGE:.2f}): {verdict}")
        elif stage == "pipeline":
            lines.append("Ignore the 'line' column at this stage: the grouper has "
                         "merged lines into blocks on purpose, so a ground-truth "
                         "line no longer has a block to match one-to-one.")
            lines.append(f"Accuracy floor (word>={PASS_WORD_RECALL:.2f}, "
                         f"sep>={PASS_SEPARATION:.2f}): {verdict}")
        else:
            lines.append(f"Accuracy floor (line>={PASS_LINE_RECALL:.2f}, "
                         f"word>={PASS_WORD_RECALL:.2f}): {verdict}")
        if not overall.passed:
            failed = [r.name for r in results if not r.passed]
            lines.append(f"Below floor: {', '.join(failed)}")
    return "\n".join(lines)


def run(data_root: str,
        scene: Optional[SceneType] = None,
        out_path: Optional[str] = None,
        baseline_path: Optional[str] = None,
        ocr_fn: Optional[OcrFn] = None,
        use_scene: bool = True,
        stage: str = "ocr",
        render_dir: Optional[str] = None) -> int:
    """Entry point used by scripts/run_harness.py. Returns a process exit code."""
    samples = load_dataset(data_root, scene)
    if not samples:
        print(f"No annotated samples found under {data_root}.")
        print("Add images plus .gt.txt files - see eval_data/README.md.")
        return 2

    results = asyncio.run(run_samples(samples, ocr_fn, use_scene, stage,
                                      render_dir))
    summaries = summarize(results)

    baseline = None
    if baseline_path and os.path.exists(baseline_path):
        with open(baseline_path, "r", encoding="utf-8") as handle:
            baseline = json.load(handle)

    print(format_report(results, summaries, baseline))

    if out_path:
        payload = {
            "samples": [asdict(r) for r in results],
            "summary": {k: asdict(v) for k, v in summaries.items()},
        }
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        print(f"\nWrote {out_path}")

    overall = summaries.get("overall")
    return 0 if overall and overall.passed else 1
