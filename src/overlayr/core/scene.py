"""Scene types - the three situations Overlayr is built for (CLAUDE.md section 2).

Every downstream choice (region finding, OCR technique, grouping rule) is
selected by scene, so the enum lives in core with no dependencies of its own.
"""
from __future__ import annotations

from enum import Enum


class SceneType(str, Enum):
    """What kind of image the capture source is producing."""

    GAME_UI = "game_ui"
    DOCUMENT = "document"
    COMIC = "comic"


# UI labels for the scene dropdown. "Auto" is the classifier; the rest lock it.
AUTO_LABEL = "Auto"

SCENE_LABELS = {
    SceneType.GAME_UI: "Game",
    SceneType.DOCUMENT: "Document",
    SceneType.COMIC: "Comic",
}

LABEL_TO_SCENE = {label: scene for scene, label in SCENE_LABELS.items()}

# The order shown in the dropdown.
SCENE_CHOICES = [AUTO_LABEL] + [SCENE_LABELS[s] for s in SceneType]


def label_for(scene: SceneType) -> str:
    return SCENE_LABELS[scene]


def scene_from_label(label: str) -> "SceneType | None":
    """Map a dropdown label back to a scene. ``Auto`` (or junk) maps to None."""
    return LABEL_TO_SCENE.get(label)


def resolve_saved_scene(saved=None, legacy_grouping_mode=None):
    """Turn the stored settings into a scene lock, or None for Auto.

    ``saved`` is the ``scene_type`` key. When it is missing, the older
    ``grouping_mode`` setting is honoured once so an existing install does not
    silently change behaviour: its "Document (Lines)" option is the Document
    scene, and its manga option becomes Auto.

    An unrecognised value falls back to Auto rather than raising - a stale
    settings file must never leave the user stuck on the wrong scene.
    """
    if saved and saved != AUTO_LABEL:
        scene = scene_from_label(saved)
        if scene is not None:
            return scene
        print(f"Unknown scene_type {saved!r} in settings, falling back to Auto.")
        return None

    if saved is None and legacy_grouping_mode and "Document" in legacy_grouping_mode:
        return SceneType.DOCUMENT
    return None


def uses_spell_correction(scene) -> bool:
    """Whether OCR output for this scene should go through the speller.

    Measured against the sample captures: correction rescues comic text, whose
    stylised lettering the OCR engine mangles, but damages documents and game
    UI, which are read accurately enough that every rewrite is a new mistake
    (document word recall 0.881 with it, 0.920 without).

    An unknown scene keeps correction on, which is what the pipeline did before
    the profiles existed.
    """
    return scene is not SceneType.GAME_UI and scene is not SceneType.DOCUMENT


def merge_mode_for(scene: SceneType) -> str:
    """Grouping mode the engine should use for a scene.

    Documents are read line by line because paragraph clustering merges across
    columns; game dialogue boxes and comic bubbles are read as blocks.
    Phase 3 replaces this mapping with a per-scene Grouper.
    """
    return "line" if scene is SceneType.DOCUMENT else "paragraph"
