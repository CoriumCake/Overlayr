"""Which OCR techniques to run, chosen by scene (CLAUDE.md section 6).

Every technique costs a full pass over the image, so a profile only lists the
ones that earn their place for that scene. Two passes are described:

* ``full_frame`` runs over the whole capture and is the safety net - if the
  region detector misses an area, this is what still reads it. Dropping text is
  the one failure CLAUDE.md rules out, so this list is never empty.
* ``region`` runs over each region separately, which is the point of
  region-first OCR: a crop can be normalised for its own contrast instead of
  sharing one threshold with the rest of the frame.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import cv2
import numpy as np

from ..core.scene import SceneType

# Windows OCR is weak on small glyphs, so a crop is enlarged until its text is
# tall enough to read. The techniques already upscale 2x; this is the extra
# factor applied on top for crops that are still small.
TARGET_REGION_HEIGHT = 96
MAX_EXTRA_SCALE = 3.0


@dataclass(frozen=True)
class OcrProfile:
    full_frame: Tuple[str, ...]
    region: Tuple[str, ...] = ()
    normalize_polarity: bool = False
    upscale_small_regions: bool = True
    # Whether a second, differently-trained reader re-reads the lines this
    # profile is least sure of. Only comics ask for it: the errors it fixes are
    # collisions between drawn letter shapes, and it costs time the other two
    # scenes have no reason to spend (CLAUDE.md 9.28).
    uses_neural_reader: bool = False

    @property
    def uses_regions(self) -> bool:
        return bool(self.region)


# Kept for a capture whose scene is unknown: the four techniques the engine ran
# on every frame before profiles existed.
LEGACY_PROFILE = OcrProfile(
    full_frame=("baseline", "thicken", "simple_threshold", "clahe"),
)

# Every list below was chosen by measuring it against the TestingImage captures
# with scripts/run_harness.py, one technique at a time. Change one only with a
# harness run that shows the change is an improvement.
PROFILES = {
    # Game text is light on dark as often as the reverse, and sits on moving,
    # patterned backgrounds - so each panel is normalised for its own polarity.
    # Reading the panels instead of the frame took the sample from 1454ms to
    # 464ms and raised word recall from 0.789 to 0.895.
    SceneType.GAME_UI: OcrProfile(
        full_frame=("baseline", "clahe"),
        region=("clahe", "simple_threshold"),
        normalize_polarity=True,
    ),
    # A page is already high contrast, and section 6 warns that processing it
    # further makes it worse - so the region pass stays minimal and the column
    # bands mainly save time by cutting the margins out. 1798ms to 897ms at the
    # same recall.
    SceneType.DOCUMENT: OcrProfile(
        full_frame=("baseline", "thicken", "simple_threshold", "clahe"),
        region=("baseline",),
        upscale_small_regions=False,
    ),
    # Inside a bubble the text is black on white and plain Otsu is enough; the
    # work that matters for comics is cutting the bubble out of the art first.
    # Despeckling the whole frame removes screentone that would otherwise break
    # up the glyphs sitting on it.
    SceneType.COMIC: OcrProfile(
        full_frame=("baseline", "thicken", "simple_threshold", "clahe", "despeckle"),
        region=("simple_threshold", "clahe"),
        normalize_polarity=True,
        uses_neural_reader=True,
    ),
}

# Measured and left out on purpose. `sauvola` (a local threshold) and `tophat`
# (morphology that lifts thin strokes off the art behind them) are both
# implemented and tested, and section 6 suggests them - but on the sample pages
# neither one earns its cost:
#
#   comic   line 0.794 -> 0.797 with sauvola, word 0.721 -> 0.719
#   game    word 0.895 either way; sauvola in place of the threshold drops it
#           to 0.789
#   document  no change at all
#
# Section 1 is explicit that a technique which does not improve accuracy is
# time spent for nothing. They stay available for a future scene or a harder
# set of pages; do not add them back without a harness run that shows a gain.


def get_profile(scene) -> OcrProfile:
    """Profile for a scene, or the legacy full-frame profile when unknown."""
    return PROFILES.get(scene, LEGACY_PROFILE)


def is_light_on_dark(gray: np.ndarray) -> bool:
    """Whether the text in a crop is lighter than what surrounds it.

    Windows OCR leans clearly towards dark text on a light ground, so a crop
    that is the other way round is inverted before being read. The test
    compares the middle of the crop, where text usually sits, against its
    border, which is usually background.
    """
    height, width = gray.shape[:2]
    if height < 6 or width < 6:
        return False

    margin_y = max(1, height // 5)
    margin_x = max(1, width // 5)
    middle = gray[margin_y:height - margin_y, margin_x:width - margin_x]
    if middle.size == 0:
        return False

    border = np.concatenate([
        gray[:margin_y, :].ravel(), gray[height - margin_y:, :].ravel(),
        gray[:, :margin_x].ravel(), gray[:, width - margin_x:].ravel(),
    ])
    if border.size == 0:
        return False

    # The background dominates both samples, so compare the extremes rather
    # than the means: what differs is the ink, not the ground.
    return float(np.percentile(middle, 90)) > float(np.percentile(border, 90)) + 25


def prepare_region(crop: np.ndarray, profile: OcrProfile):
    """Normalise one region for reading. Returns the crop and its extra scale.

    The scale is returned rather than applied to the result here because the
    caller has to map coordinates back to the full frame anyway.
    """
    if crop.size == 0:
        return crop, 1.0

    prepared = crop
    if profile.normalize_polarity:
        gray = (prepared if prepared.ndim == 2
                else cv2.cvtColor(prepared, cv2.COLOR_BGR2GRAY))
        if is_light_on_dark(gray):
            prepared = cv2.bitwise_not(prepared)

    scale = 1.0
    if profile.upscale_small_regions:
        height = prepared.shape[0]
        if 0 < height < TARGET_REGION_HEIGHT:
            scale = min(MAX_EXTRA_SCALE, TARGET_REGION_HEIGHT / float(height))
            prepared = cv2.resize(prepared, None, fx=scale, fy=scale,
                                  interpolation=cv2.INTER_CUBIC)

    return prepared, scale
