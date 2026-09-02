"""Finds the areas of a frame that should be read separately.

CLAUDE.md section 4 calls for region-first OCR: no single threshold suits a
bright HUD and a dark dialogue box in the same frame, so each area is cut out
and normalised on its own. Each scene needs a different notion of an area:

* GAME_UI  - panels, found from where text-like detail clusters;
* DOCUMENT - column bands, found from the vertical gaps between columns;
* COMIC    - text regions, found with the DBNet detector.

Regions are also what Phase 3 will use to forbid merging across a bubble, a
column, or a panel, so each one carries its polygon when it has a shape more
specific than its bounding box.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import cv2
import numpy as np

from ..core.scene import SceneType

# A region smaller than this cannot hold readable text and is dropped.
MIN_REGION_WIDTH = 24
MIN_REGION_HEIGHT = 12

# Upper bound on regions per frame. Each one costs an OCR call, and a frame
# broken into dozens of pieces means the detector failed rather than that the
# frame really holds that many separate texts.
MAX_REGIONS = 24

# Regions are padded outwards so a glyph is never clipped at the edge.
REGION_PADDING = 6


@dataclass(frozen=True)
class Region:
    """An axis-aligned area of the frame, in full-frame pixel coordinates."""

    left: int
    top: int
    right: int
    bottom: int
    kind: str = "region"
    polygon: Optional[np.ndarray] = None

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def box(self):
        return self.left, self.top, self.right, self.bottom

    def crop(self, image: np.ndarray) -> np.ndarray:
        return image[self.top:self.bottom, self.left:self.right]

    def to_contour(self) -> np.ndarray:
        """OpenCV contour for this region: its polygon, or its corners."""
        if self.polygon is not None:
            return self.polygon
        corners = [[self.left, self.top], [self.right, self.top],
                   [self.right, self.bottom], [self.left, self.bottom]]
        return np.array(corners, dtype=np.int32).reshape(-1, 1, 2)

    def contains_point(self, x: float, y: float) -> bool:
        """Whether a point falls inside this region.

        Uses the polygon when there is one, so a point in the corner of a
        bubble`s bounding box but outside the bubble itself is excluded.
        """
        if self.polygon is not None:
            return cv2.pointPolygonTest(self.polygon, (float(x), float(y)), False) >= 0
        return self.left <= x <= self.right and self.top <= y <= self.bottom


def _clip(region: Region, width: int, height: int) -> Region:
    return Region(
        left=max(0, min(region.left, width)),
        top=max(0, min(region.top, height)),
        right=max(0, min(region.right, width)),
        bottom=max(0, min(region.bottom, height)),
        kind=region.kind,
        polygon=region.polygon,
    )


def _pad(region: Region, amount: int = REGION_PADDING) -> Region:
    return Region(region.left - amount, region.top - amount,
                  region.right + amount, region.bottom + amount,
                  region.kind, region.polygon)


def _is_usable(region: Region) -> bool:
    return (region.width >= MIN_REGION_WIDTH
            and region.height >= MIN_REGION_HEIGHT)


def _overlap_ratio(a: Region, b: Region) -> float:
    """Intersection over the area of the smaller region."""
    x_overlap = max(0, min(a.right, b.right) - max(a.left, b.left))
    y_overlap = max(0, min(a.bottom, b.bottom) - max(a.top, b.top))
    intersection = x_overlap * y_overlap
    smaller = min(a.area, b.area)
    return intersection / smaller if smaller else 0.0


def merge_overlapping(regions: List[Region], threshold: float = 0.6) -> List[Region]:
    """Fuse regions that mostly cover each other, keeping the union box.

    Detectors routinely return one box per line and another around the block;
    reading both would spend an OCR call to produce a duplicate.
    """
    merged: List[Region] = []
    for region in sorted(regions, key=lambda r: r.area, reverse=True):
        for i, existing in enumerate(merged):
            if _overlap_ratio(region, existing) >= threshold:
                merged[i] = Region(
                    min(existing.left, region.left), min(existing.top, region.top),
                    max(existing.right, region.right), max(existing.bottom, region.bottom),
                    existing.kind,
                    existing.polygon if existing.area >= region.area else region.polygon,
                )
                break
        else:
            merged.append(region)
    return merged


def _finalise(regions: List[Region], width: int, height: int) -> List[Region]:
    """Pad, clip, drop the unusable, merge duplicates, and cap the count."""
    prepared = [_clip(_pad(r), width, height) for r in regions]
    prepared = [r for r in prepared if _is_usable(r)]
    prepared = merge_overlapping(prepared)
    prepared.sort(key=lambda r: r.area, reverse=True)
    prepared = prepared[:MAX_REGIONS]
    # Reading order: top to bottom, then left to right.
    prepared.sort(key=lambda r: (r.top, r.left))
    return prepared


def _to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def text_detail_mask(gray: np.ndarray) -> np.ndarray:
    """Binary mask of stroke-like detail, whatever the polarity of the text.

    A morphological gradient responds to the edge of a stroke rather than to its
    colour, so light text on a dark panel and dark text on a light one both
    show up - which matters because a game frame usually holds both.
    """
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    gradient = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, kernel)
    _, mask = cv2.threshold(gradient, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return mask


def find_game_ui_regions(image: np.ndarray) -> List[Region]:
    """Panels of a game frame, taken as the clusters of text-like detail.

    Text in a game is sparse and grouped: a dialogue box, a name plate, a few
    HUD counters. Joining detail horizontally first turns glyphs into lines, and
    a second, taller join turns stacked lines into the panel they sit in.
    """
    gray = _to_gray(image)
    height, width = gray.shape[:2]
    mask = text_detail_mask(gray)

    line_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (max(9, width // 60), 3))
    lines = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, line_kernel)

    panel_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (max(15, width // 40), max(9, height // 40)))
    panels = cv2.morphologyEx(lines, cv2.MORPH_CLOSE, panel_kernel)

    count, _, stats, _ = cv2.connectedComponentsWithStats(panels, connectivity=8)
    regions = []
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        # A component covering most of the frame is background texture, not a
        # panel; reading it region-first would be the full-frame pass again.
        if w > width * 0.97 and h > height * 0.97:
            continue
        if area < 150:
            continue
        regions.append(Region(int(x), int(y), int(x + w), int(y + h), kind="panel"))

    return _finalise(regions, width, height)


def find_document_regions(image: np.ndarray) -> List[Region]:
    """Column bands of a page, taken from the vertical gaps between columns.

    Without this a paragraph merges across the gutter and the whole block is
    translated out of context, which CLAUDE.md section 1 counts as worse than
    misreading a character.
    """
    gray = _to_gray(image)
    height, width = gray.shape[:2]

    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    profile = (ink > 0).sum(axis=0).astype(np.float32)
    if not profile.any():
        return []

    # A gutter is a run of columns holding almost no ink. Requiring it to be
    # reasonably wide keeps the space between two words from splitting a line.
    empty = profile <= max(1.0, profile.max() * 0.01)
    min_gutter = max(8, width // 50)

    bands = []
    start = None
    run = 0
    for x in range(width):
        if empty[x]:
            run += 1
            if run == min_gutter and start is not None:
                bands.append((start, x - min_gutter + 1))
                start = None
        else:
            run = 0
            if start is None:
                start = x
    if start is not None:
        bands.append((start, width))

    regions = []
    for left, right in bands:
        column = ink[:, left:right]
        rows = np.flatnonzero((column > 0).any(axis=1))
        if rows.size == 0:
            continue
        # Trim to the ink so page margins are not sent to the OCR engine.
        regions.append(Region(left, int(rows[0]), right, int(rows[-1]) + 1,
                              kind="column"))

    return _finalise(regions, width, height)


def find_comic_regions(image: np.ndarray) -> List[Region]:
    """Text regions of a comic page, from the DBNet detector.

    The detector returns a polygon per text area, which is kept alongside the
    box: a bubble is round, and Phase 3 needs the shape to tell whether two
    lines really share one bubble.
    """
    from .ocr_preprocessor import detect_text_regions_dbnet

    gray = _to_gray(image)
    height, width = gray.shape[:2]

    bgr = image if image.ndim == 3 else cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    try:
        polygons = detect_text_regions_dbnet(bgr)
    except Exception as e:
        print(f"Comic region detection failed, falling back to detail clusters: "
              f"{type(e).__name__}: {e}")
        return find_game_ui_regions(image)

    regions = []
    for polygon in polygons:
        points = np.asarray(polygon).reshape(-1, 2)
        if points.size == 0:
            continue
        x, y, w, h = cv2.boundingRect(points.astype(np.int32))
        regions.append(Region(x, y, x + w, y + h, kind="bubble",
                              polygon=points.astype(np.int32).reshape(-1, 1, 2)))

    if not regions:
        return []
    return _finalise(regions, width, height)


FINDERS = {
    SceneType.GAME_UI: find_game_ui_regions,
    SceneType.DOCUMENT: find_document_regions,
    SceneType.COMIC: find_comic_regions,
}


def find_regions(image: np.ndarray, scene: SceneType) -> List[Region]:
    """Regions of the frame to read separately, chosen by scene.

    An empty list is a valid answer and means the caller should read the whole
    frame - which is what happens when a detector finds nothing it trusts.
    """
    finder = FINDERS.get(scene)
    if finder is None:
        return []
    try:
        return finder(image)
    except Exception as e:
        print(f"Region detection failed for {scene.value}: "
              f"{type(e).__name__}: {e}")
        return []
