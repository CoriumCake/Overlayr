"""Low-cost frame coordination and change detection for the OCR hot path."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Any, Iterable, Mapping, Optional, Sequence

import cv2
import numpy as np
from PIL import Image


@dataclass(frozen=True)
class FramePacket:
    """The newest captured frame and its monotonically increasing sequence."""

    sequence: int
    image: Any
    bounds: Mapping[str, int]
    captured_at: float


class LatestFrameBuffer:
    """Thread-safe single-slot buffer that never builds a stale frame backlog."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._sequence = 0
        self._latest: Optional[FramePacket] = None

    def publish(self, image: Any, bounds: Mapping[str, int]) -> FramePacket:
        with self._lock:
            self._sequence += 1
            packet = FramePacket(
                sequence=self._sequence,
                image=image,
                bounds=dict(bounds),
                captured_at=monotonic(),
            )
            self._latest = packet
            return packet

    def latest(self) -> Optional[FramePacket]:
        with self._lock:
            return self._latest

    def clear(self) -> None:
        with self._lock:
            self._latest = None


@dataclass(frozen=True)
class RegionFingerprint:
    bbox: tuple[int, int, int, int]
    frame_size: tuple[int, int]
    gray: np.ndarray


@dataclass(frozen=True)
class RegionChangeMetrics:
    changed: bool
    changed_ratio: float
    mean_delta: float


@dataclass(frozen=True)
class OCRCacheDecision:
    reuse: bool
    reason: str
    global_changed_ratio: float
    regions_changed: bool


def _to_gray(image: Any) -> np.ndarray:
    if isinstance(image, Image.Image):
        return np.asarray(image.convert("L"))

    array = np.asarray(image)
    if array.ndim == 2:
        return array.astype(np.uint8, copy=False)
    if array.ndim == 3 and array.shape[2] >= 3:
        return cv2.cvtColor(array[:, :, :3], cv2.COLOR_BGR2GRAY)
    raise ValueError("Unsupported image shape")


def _normalize_box(
    bbox: Sequence[Any],
    width: int,
    height: int,
    padding: int,
) -> Optional[tuple[int, int, int, int]]:
    if len(bbox) != 4:
        return None
    left, top, right, bottom = (int(round(value)) for value in bbox)
    left = max(0, left - padding)
    top = max(0, top - padding)
    right = min(width, right + padding)
    bottom = min(height, bottom + padding)
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _bounded_preview(gray: np.ndarray, max_side: int) -> np.ndarray:
    height, width = gray.shape[:2]
    scale = min(1.0, max_side / max(1, width, height))
    resized_width = max(8, int(round(width * scale)))
    resized_height = max(8, int(round(height * scale)))
    if (resized_width, resized_height) != (width, height):
        gray = cv2.resize(
            gray,
            (resized_width, resized_height),
            interpolation=cv2.INTER_AREA,
        )
    return cv2.GaussianBlur(gray, (3, 3), 0)


def build_frame_preview(image: Any, *, max_side: int = 192) -> np.ndarray:
    """Build a compact structural preview once for repeated cache decisions."""
    return _bounded_preview(_to_gray(image), max_side)


def build_region_fingerprints(
    image: Any,
    results: Iterable[Mapping[str, Any]],
    *,
    padding: int = 2,
    max_side: int = 160,
) -> tuple[RegionFingerprint, ...]:
    """Compress translated text regions for inexpensive future comparisons."""
    gray = _to_gray(image)
    height, width = gray.shape[:2]
    fingerprints = []

    for result in results:
        bbox = result.get("bbox")
        if not bbox:
            continue
        normalized = _normalize_box(bbox, width, height, padding)
        if normalized is None:
            continue
        left, top, right, bottom = normalized
        preview = _bounded_preview(gray[top:bottom, left:right], max_side)
        fingerprints.append(
            RegionFingerprint(
                bbox=normalized,
                frame_size=(width, height),
                gray=preview.copy(),
            )
        )

    return tuple(fingerprints)


def compare_region_fingerprints(
    current_image: Any,
    fingerprints: Iterable[RegionFingerprint],
    *,
    pixel_threshold: int = 24,
    changed_ratio_threshold: float = 0.10,
    mean_delta_threshold: float = 8.0,
) -> RegionChangeMetrics:
    """Detect whether any previously translated source region has changed."""
    fingerprints = tuple(fingerprints)
    if not fingerprints:
        return RegionChangeMetrics(False, 0.0, 0.0)

    current_gray = None
    if isinstance(current_image, Image.Image):
        width, height = current_image.size
    else:
        current_gray = _to_gray(current_image)
        height, width = current_gray.shape[:2]
    max_ratio = 0.0
    max_mean = 0.0

    for fingerprint in fingerprints:
        if fingerprint.frame_size != (width, height):
            return RegionChangeMetrics(True, 1.0, 255.0)

        left, top, right, bottom = fingerprint.bbox
        if current_gray is None:
            crop = np.asarray(
                current_image.crop((left, top, right, bottom)).convert("L")
            )
        else:
            crop = current_gray[top:bottom, left:right]
        if crop.size == 0:
            return RegionChangeMetrics(True, 1.0, 255.0)

        crop = cv2.resize(
            crop,
            (fingerprint.gray.shape[1], fingerprint.gray.shape[0]),
            interpolation=cv2.INTER_AREA,
        )
        crop = cv2.GaussianBlur(crop, (3, 3), 0)
        diff = cv2.absdiff(fingerprint.gray, crop)
        changed_ratio = float(np.count_nonzero(diff >= pixel_threshold)) / diff.size
        mean_delta = float(np.mean(diff))
        max_ratio = max(max_ratio, changed_ratio)
        max_mean = max(max_mean, mean_delta)

        if (
            changed_ratio >= changed_ratio_threshold
            and mean_delta >= mean_delta_threshold
        ) or mean_delta >= mean_delta_threshold * 2.25:
            return RegionChangeMetrics(True, max_ratio, max_mean)

    return RegionChangeMetrics(False, max_ratio, max_mean)


def _global_change_metrics(
    previous_gray: np.ndarray,
    current_gray: np.ndarray,
    *,
    previous_preview: Optional[np.ndarray] = None,
    current_preview: Optional[np.ndarray] = None,
    max_side: int = 192,
    pixel_threshold: int = 18,
) -> tuple[float, float]:
    if previous_gray.shape != current_gray.shape:
        return 1.0, 255.0

    if previous_preview is None:
        previous_preview = _bounded_preview(previous_gray, max_side)
    if current_preview is None:
        current_preview = _bounded_preview(current_gray, max_side)
    elif current_preview.shape != previous_preview.shape:
        current_preview = cv2.resize(
            current_preview,
            (previous_preview.shape[1], previous_preview.shape[0]),
            interpolation=cv2.INTER_AREA,
        )
    diff = cv2.absdiff(previous_preview, current_preview)
    changed_ratio = float(np.count_nonzero(diff >= pixel_threshold)) / diff.size
    return changed_ratio, float(np.mean(diff))


def evaluate_ocr_cache(
    previous_gray: Optional[np.ndarray],
    current_gray: np.ndarray,
    previous_results: Iterable[Mapping[str, Any]],
    *,
    cache_age_seconds: float,
    previous_preview: Optional[np.ndarray] = None,
    current_preview: Optional[np.ndarray] = None,
    previous_fingerprints: Optional[Iterable[RegionFingerprint]] = None,
    dynamic_cooldown_seconds: float = 2.5,
    stable_rescan_seconds: float = 5.0,
    empty_poll_seconds: float = 0.4,
    structural_change_threshold: float = 0.025,
) -> OCRCacheDecision:
    """Decide whether expensive OCR can reuse its last translated result.

    Existing text regions are the primary signal, so animation elsewhere in a
    game does not invalidate the cache. A low-resolution global comparison and
    bounded TTL still discover newly appearing text outside those regions.
    """
    if previous_gray is None or previous_gray.shape != current_gray.shape:
        return OCRCacheDecision(False, "different_frame_shape", 1.0, True)

    previous_results = tuple(previous_results)
    global_ratio, _ = _global_change_metrics(
        previous_gray,
        current_gray,
        previous_preview=previous_preview,
        current_preview=current_preview,
    )

    fingerprints = (
        tuple(previous_fingerprints)
        if previous_fingerprints is not None
        else build_region_fingerprints(previous_gray, previous_results)
    )
    region_metrics = compare_region_fingerprints(current_gray, fingerprints)
    if region_metrics.changed:
        return OCRCacheDecision(
            False,
            "translated_region_changed",
            global_ratio,
            True,
        )

    if not previous_results:
        reuse = (
            cache_age_seconds < empty_poll_seconds
            and global_ratio < structural_change_threshold
        )
        return OCRCacheDecision(
            reuse,
            "empty_scene_cooldown" if reuse else "empty_scene_poll",
            global_ratio,
            False,
        )

    if cache_age_seconds < dynamic_cooldown_seconds:
        return OCRCacheDecision(
            True,
            "translated_regions_stable",
            global_ratio,
            False,
        )

    if (
        global_ratio < structural_change_threshold
        and cache_age_seconds < stable_rescan_seconds
    ):
        return OCRCacheDecision(
            True,
            "static_scene",
            global_ratio,
            False,
        )

    return OCRCacheDecision(
        False,
        "periodic_rescan",
        global_ratio,
        False,
    )
