"""Loading of the harness dataset.

Two layouts are read (see eval_data/README.md):

    eval_data/
        game_ui/    shot_01.png   shot_01.gt.txt   [shot_01.meta.json]
        document/   ...
        comic/      ...

or a flat folder where the scene is part of the file name, which is the
convention the TestingImage/ folder already uses:

    TestingImage/  EN-COMIC-15.png   EN-COMIC-15_Answer.txt

The ground-truth file is plain UTF-8, one expected text line per line. Blank
lines and lines starting with ``#`` are ignored, so the file can be annotated.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..core.scene import SceneType

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".webp")

# Directory name -> scene. Directory names mirror the SceneType values.
SCENE_DIRS = {
    "game_ui": SceneType.GAME_UI,
    "document": SceneType.DOCUMENT,
    "comic": SceneType.COMIC,
}

# Reasonable source language per scene when no .meta.json says otherwise.
DEFAULT_LANG = {
    SceneType.GAME_UI: "en",
    SceneType.DOCUMENT: "en",
    SceneType.COMIC: "ja",
}

# Scene words recognised in a flat folder`s file names. A web page is scored as
# a document: it is running text on a light page, which is the closest of the
# three target scenes.
NAME_SCENE_HINTS = {
    "COMIC": SceneType.COMIC,
    "MANGA": SceneType.COMIC,
    "GAME": SceneType.GAME_UI,
    "DOCUMENT": SceneType.DOCUMENT,
    "WEBSITE": SceneType.DOCUMENT,
}

# Language prefixes used in those same file names.
NAME_LANG_HINTS = {
    "EN": "en", "JP": "ja", "JA": "ja", "KO": "ko", "TH": "th", "ZH": "zh-CN",
}

# Ground truth may sit beside the image under either name.
GT_SUFFIXES = (".gt.txt", "_Answer.txt")


@dataclass
class Sample:
    """One image plus the text a perfect run would produce from it."""

    name: str
    image_path: str
    scene: SceneType
    lines: List[str]
    lang: str = "en"
    meta: Dict[str, object] = field(default_factory=dict)
    # Lines grouped as the annotation separated them: one bubble, paragraph or
    # panel per group. Empty when the file used no blank lines.
    groups: List[List[str]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def parse_ground_truth(raw: str) -> List[str]:
    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines


def parse_ground_truth_groups(raw: str) -> List[List[str]]:
    """Split the ground truth into the blocks a blank line separates.

    A blank line in the annotation marks the end of one speech bubble, one
    paragraph, one panel of dialogue - which makes it a free record of the
    correct grouping, and the only way to measure the mistake CLAUDE.md
    section 1 calls worse than misreading a character.
    """
    groups: List[List[str]] = []
    current: List[str] = []

    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not stripped:
            if current:
                groups.append(current)
                current = []
            continue
        current.append(stripped)

    if current:
        groups.append(current)
    return groups


def _load_meta(path: str) -> Dict[str, object]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"Harness: ignoring unreadable meta file {path}: {exc}")
        return {}


def scene_from_filename(stem: str) -> Optional[SceneType]:
    """Scene named inside a file stem such as ``EN-COMIC-15``."""
    for token in stem.upper().replace("_", "-").split("-"):
        if token in NAME_SCENE_HINTS:
            return NAME_SCENE_HINTS[token]
    return None


def lang_from_filename(stem: str) -> Optional[str]:
    """Language named inside a file stem such as ``EN-COMIC-15``."""
    for token in stem.upper().replace("_", "-").split("-"):
        if token in NAME_LANG_HINTS:
            return NAME_LANG_HINTS[token]
    return None


def _find_ground_truth(directory: str, stem: str) -> Optional[str]:
    for suffix in GT_SUFFIXES:
        path = os.path.join(directory, f"{stem}{suffix}")
        if os.path.exists(path):
            return path
    return None


def _build_sample(directory: str, entry: str, sample_scene: SceneType,
                  name: str) -> Optional[Sample]:
    stem = os.path.splitext(entry)[0]
    image_path = os.path.join(directory, entry)

    gt_path = _find_ground_truth(directory, stem)
    if gt_path is None:
        print(f"Harness: no ground truth for {image_path}, skipping.")
        return None

    with open(gt_path, "r", encoding="utf-8") as handle:
        raw = handle.read()
    lines = parse_ground_truth(raw)
    if not lines:
        print(f"Harness: ground truth {gt_path} is empty, skipping.")
        return None

    meta = _load_meta(os.path.join(directory, f"{stem}.meta.json"))
    lang = meta.get("lang") or lang_from_filename(stem) or DEFAULT_LANG[sample_scene]
    return Sample(name=name, image_path=image_path, scene=sample_scene,
                  lines=lines, lang=str(lang), meta=meta,
                  groups=parse_ground_truth_groups(raw))


def load_dataset(root: str, scene: Optional[SceneType] = None) -> List[Sample]:
    """Collect every annotated image under ``root``, in either layout.

    Images without a ground-truth sibling are skipped with a warning: an
    unannotated image cannot be scored, and silently counting it as empty would
    poison the averages.
    """
    samples: List[Sample] = []
    if not os.path.isdir(root):
        return samples

    # Layout 1: one directory per scene.
    for dir_name, dir_scene in sorted(SCENE_DIRS.items()):
        if scene is not None and dir_scene is not scene:
            continue
        scene_dir = os.path.join(root, dir_name)
        if not os.path.isdir(scene_dir):
            continue

        for entry in sorted(os.listdir(scene_dir)):
            if os.path.splitext(entry)[1].lower() not in IMAGE_EXTENSIONS:
                continue
            stem = os.path.splitext(entry)[0]
            sample = _build_sample(scene_dir, entry, dir_scene, f"{dir_name}/{stem}")
            if sample:
                samples.append(sample)

    # Layout 2: a flat folder naming the scene in each file.
    for entry in sorted(os.listdir(root)):
        if os.path.splitext(entry)[1].lower() not in IMAGE_EXTENSIONS:
            continue
        stem = os.path.splitext(entry)[0]
        file_scene = scene_from_filename(stem)
        if file_scene is None:
            print(f"Harness: cannot tell the scene of {entry} from its name, skipping.")
            continue
        if scene is not None and file_scene is not scene:
            continue
        sample = _build_sample(root, entry, file_scene, stem)
        if sample:
            samples.append(sample)

    return samples
