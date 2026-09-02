"""Decides which of the three target scenes the capture source is producing.

Signals are combined cheapest-first, as laid out in CLAUDE.md section 5:

1. the process name of the captured window - free, and the strongest signal;
2. image heuristics - saturation, white ratio, long straight lines, and how
   periodic the horizontal ink profile is;
3. temporal smoothing - a capture is a stream from one source, so the scene must
   not flip frame to frame.

The classifier runs on a downscaled copy and is only called on frames that
already passed the similarity gate, so it stays off the hot path.
"""
from __future__ import annotations

from collections import Counter, deque
from typing import Deque, Dict, Optional

import cv2
import numpy as np

from ..core.scene import SceneType

# Process names that settle the question on their own.
DOCUMENT_PROCESSES = {
    "acrord32.exe", "acrobat.exe", "sumatrapdf.exe", "winword.exe",
    "foxitreader.exe", "foxitphantompdf.exe", "pdfxedit.exe", "okular.exe",
    "wps.exe", "libreoffice.exe", "soffice.exe", "onenote.exe",
    "notepad.exe", "notepad++.exe", "calibre.exe",
}

COMIC_PROCESSES = {
    "cdisplayex.exe", "honeyview.exe", "mangameeya.exe", "yacreader.exe",
    "comicrack.exe", "neeview.exe",
}

# Browsers and generic viewers say nothing by themselves; the window title and
# the image decide. Listed so they are not mistaken for game executables.
AMBIGUOUS_PROCESSES = {
    "msedge.exe", "chrome.exe", "brave.exe", "firefox.exe", "opera.exe",
    "vivaldi.exe", "photos.exe", "irfanview.exe", "explorer.exe",
}

DOCUMENT_TITLE_HINTS = (".pdf", ".docx", ".doc", ".epub", ".txt")
COMIC_TITLE_HINTS = ("manga", "manhwa", "manhua", "webtoon", "comic",
                     "chapter", ".cbz", ".cbr")

# How many recent frames the smoothed answer looks back over.
HISTORY_LENGTH = 10

# A hint from the curated lists above, or from a file extension in the title,
# is the most reliable signal there is (CLAUDE.md 5A) and outweighs any reading
# of the pixels. Guessing "game" from an unrecognised executable is a far weaker
# claim, so it only breaks ties.
STRONG_HINT_WEIGHT = 4.5
WEAK_HINT_WEIGHT = 1.0

# Longest edge the heuristics work on. Small enough to stay cheap, large enough
# that text rows still register in the projection profile.
ANALYSIS_WIDTH = 480


def weighted_process_hint(process_name=None, title=None):
    """Scene implied by the source window, paired with how much it counts."""
    name = (process_name or "").lower().strip()
    text = (title or "").lower()

    if name in DOCUMENT_PROCESSES:
        return SceneType.DOCUMENT, STRONG_HINT_WEIGHT
    if name in COMIC_PROCESSES:
        return SceneType.COMIC, STRONG_HINT_WEIGHT

    if any(hint in text for hint in COMIC_TITLE_HINTS):
        return SceneType.COMIC, STRONG_HINT_WEIGHT
    if any(hint in text for hint in DOCUMENT_TITLE_HINTS):
        return SceneType.DOCUMENT, STRONG_HINT_WEIGHT

    if name and name not in AMBIGUOUS_PROCESSES:
        # An unrecognised application is more often a game than anything else,
        # but a page of text on screen says far more than the file name does.
        return SceneType.GAME_UI, WEAK_HINT_WEIGHT
    return None, 0.0


def process_hint(process_name=None, title=None):
    """Scene implied by the executable and window title of the source, if any."""
    return weighted_process_hint(process_name, title)[0]


def _prepare(image) -> np.ndarray:
    """Return a BGR numpy array scaled down to ANALYSIS_WIDTH."""
    array = np.asarray(image)
    if array.ndim == 2:
        array = cv2.cvtColor(array, cv2.COLOR_GRAY2BGR)
    elif array.shape[2] == 4:
        array = cv2.cvtColor(array, cv2.COLOR_RGBA2BGR)
    elif array.shape[2] == 3:
        array = cv2.cvtColor(array, cv2.COLOR_RGB2BGR)

    height, width = array.shape[:2]
    if width > ANALYSIS_WIDTH:
        scale = ANALYSIS_WIDTH / float(width)
        array = cv2.resize(array, (ANALYSIS_WIDTH, max(1, int(height * scale))),
                           interpolation=cv2.INTER_AREA)
    return array


def _row_profile_periodicity(gray: np.ndarray) -> float:
    """How regularly ink rows repeat down the image - text lines score high.

    Autocorrelation of the per-row ink profile; documents have evenly spaced
    baselines, game HUDs and comic panels do not.
    """
    ink = (gray < 160).sum(axis=1).astype(np.float32)
    ink -= ink.mean()
    if ink.size < 16 or not np.any(ink):
        return 0.0

    correlation = np.correlate(ink, ink, mode="full")[ink.size - 1:]
    if correlation[0] <= 0:
        return 0.0
    correlation = correlation / correlation[0]

    # Ignore the first few lags (self-overlap) and look for a real repeat peak.
    search = correlation[4:max(5, gray.shape[0] // 2)]
    return float(search.max()) if search.size else 0.0


def _ink_spread(gray: np.ndarray, cell: int = 16) -> float:
    """How much of the frame is covered by solid dark area.

    Measured over the whole set of test captures, this is the strongest single
    separator between the three scenes, because it distinguishes thin strokes
    from filled area. Text on a page is thin: even a dense page of running text
    leaves each cell mostly light. Comic art and screentone fill cells solidly,
    and a dark game frame fills nearly all of them.
    """
    height, width = gray.shape
    rows, cols = height // cell, width // cell
    if rows < 2 or cols < 2:
        return 0.0
    dark = (gray < 100).astype(np.float32)
    grid = dark[:rows * cell, :cols * cell].reshape(rows, cell, cols, cell)
    return float((grid.mean(axis=(1, 3)) > 0.03).mean())


# Thresholds below come from measuring the sample captures in TestingImage:
# ink spread runs 0.03-0.21 for document pages, 0.51-0.90 for comic pages, and
# above 0.92 for a frame whose background is dark throughout.
DOCUMENT_INK_SPREAD = 0.30
COMIC_INK_SPREAD = (0.35, 0.92)


def image_scores(image) -> Dict[SceneType, float]:
    """Weighted votes from the image alone. Higher is more likely."""
    bgr = _prepare(image)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    saturation = float(cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)[:, :, 1].mean())
    white_ratio = float((gray > 200).mean())
    dark_ratio = float((gray < 60).mean())
    mid_ratio = float(((gray >= 60) & (gray <= 200)).mean())
    periodicity = _row_profile_periodicity(gray)
    ink_spread = _ink_spread(gray)

    edges = cv2.Canny(gray, 60, 180)
    min_length = max(40, gray.shape[1] // 6)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=90,
                            minLineLength=min_length, maxLineGap=6)
    line_count = 0 if lines is None else len(lines)

    # Printed pages are greyscale whether they hold text or art, so colour only
    # ever argues for a game frame - never between the two paper scenes.
    is_greyscale = saturation < 30
    scores = {scene: 0.0 for scene in SceneType}

    if saturation > 60:
        scores[SceneType.GAME_UI] += 2.5
    if dark_ratio > 0.5 and not is_greyscale:
        scores[SceneType.GAME_UI] += 1.5
    if white_ratio < 0.15 and not is_greyscale:
        scores[SceneType.GAME_UI] += 1.0
    if ink_spread > COMIC_INK_SPREAD[1]:
        # Dark from edge to edge: no printed page looks like this, and saying so
        # explicitly keeps a colourless dark frame from winning on a tie.
        scores[SceneType.GAME_UI] += 1.0

    # Thin strokes on a light ground: running text rather than art.
    if ink_spread < DOCUMENT_INK_SPREAD:
        scores[SceneType.DOCUMENT] += 2.5
    if white_ratio > 0.85:
        scores[SceneType.DOCUMENT] += 1.0
    if periodicity > 0.7:
        scores[SceneType.DOCUMENT] += 0.5

    if is_greyscale:
        if COMIC_INK_SPREAD[0] <= ink_spread <= COMIC_INK_SPREAD[1]:
            scores[SceneType.COMIC] += 2.0
        # Panel borders and bubble outlines make many long straight lines.
        if line_count > 20:
            scores[SceneType.COMIC] += 1.0
        # Screentone and shading live in the greys a printed page does not use.
        if mid_ratio > 0.15:
            scores[SceneType.COMIC] += 0.5

    return scores


class SceneClassifier:
    """Stateful per-source classifier with temporal smoothing."""

    def __init__(self, history_length: int = HISTORY_LENGTH):
        self._history: Deque[SceneType] = deque(maxlen=history_length)
        self._override: Optional[SceneType] = None
        self._last: SceneType = SceneType.GAME_UI

    def set_override(self, scene: Optional[SceneType]) -> None:
        """Lock the scene to a user choice, or pass None to return to auto."""
        self._override = scene
        if scene is not None:
            self._history.clear()
            self._last = scene

    @property
    def override(self) -> Optional[SceneType]:
        return self._override

    @property
    def current(self) -> SceneType:
        """Last answer, without touching an image."""
        return self._override or self._last

    def reset(self) -> None:
        """Clear history - call when the capture source changes."""
        self._history.clear()

    def classify_frame(self, image, process_name=None, title=None) -> SceneType:
        """Single-frame answer with no smoothing."""
        scores = image_scores(image)
        hint, weight = weighted_process_hint(process_name, title)
        if hint is not None:
            scores[hint] += weight
        return max(scores.items(), key=lambda item: item[1])[0]

    def classify(self, image, process_name=None, title=None) -> SceneType:
        """Smoothed answer: majority vote over the recent frames.

        An explicit user override short-circuits everything.
        """
        if self._override is not None:
            return self._override

        self._history.append(self.classify_frame(image, process_name, title))
        counts = Counter(self._history)
        top_count = max(counts.values())
        winners = [scene for scene, count in counts.items() if count == top_count]

        # Keep the current scene on a tie so the answer does not oscillate.
        self._last = self._last if self._last in winners else winners[0]
        return self._last
