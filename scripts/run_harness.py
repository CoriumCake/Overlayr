"""Score the OCR pipeline against the annotated sample set.

    .venv/Scripts/python.exe scripts/run_harness.py
    .venv/Scripts/python.exe scripts/run_harness.py --scene comic
    .venv/Scripts/python.exe scripts/run_harness.py --out eval_results/after.json \
        --baseline eval_results/baseline.json

Exit code 0 means every sample cleared the accuracy floor, 1 means at least one
did not, 2 means there was nothing to measure.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from overlayr.core.scene import SceneType  # noqa: E402
from overlayr.eval import runner  # noqa: E402
from overlayr.utils.console import make_output_safe  # noqa: E402

make_output_safe()

REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default=os.path.join(REPO_ROOT, "eval_data"),
                        help="dataset root (default: eval_data/)")
    parser.add_argument("--scene", choices=[s.value for s in SceneType],
                        help="only measure one scene")
    parser.add_argument("--out", help="write the run as JSON to this path")
    parser.add_argument("--baseline", help="compare against a previous --out file")
    parser.add_argument("--legacy", action="store_true",
                        help="read every sample with the old whole-frame "
                             "profile, ignoring scene and regions")
    parser.add_argument("--stage", choices=("ocr", "pipeline", "overlay"),
                        default="ocr",
                        help="'ocr' measures what the OCR engine returned; "
                             "'pipeline' measures what survives filtering and "
                             "grouping, which is what reaches the overlay; "
                             "'overlay' translates, draws the result over the "
                             "frame and reads it again, to check the source "
                             "text is actually hidden")
    parser.add_argument("--render-dir",
                        help="with --stage overlay, save each drawn frame here "
                             "so the numbers can be eyeballed")
    args = parser.parse_args()

    scene = SceneType(args.scene) if args.scene else None
    return runner.run(args.data, scene=scene, out_path=args.out,
                      baseline_path=args.baseline, use_scene=not args.legacy,
                      stage=args.stage, render_dir=args.render_dir)


if __name__ == "__main__":
    sys.exit(main())
