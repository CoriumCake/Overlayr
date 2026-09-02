"""A second reader for lines Windows OCR is unsure of.

Windows OCR is trained on the type you find in documents and interfaces. Comic
lettering is drawn, and its letters collide in ways print does not: D against P,
C against G, G against a euro sign. Phase 5 established that these are the
errors that remain, and that no amount of thresholding fixes them - the
contrast was never the problem, the letter shapes are.

So this adds a reader that learned from a different corpus. It does not replace
Windows OCR (CLAUDE.md section 3) and it is not run over the frame: it re-reads
individual lines that have already been found and read, and its answer is
accepted only when the dictionary says it is at least as much like language as
the reading it would replace.

Two things keep it inside the time budget:

* only suspect lines are re-read - a line made entirely of dictionary words has
  nothing to gain and is skipped, which on the sample pages is two thirds of
  them;
* crops are batched by width, because cost scales with the padded width of the
  batch rather than with the text in it (measured: 7ms per line at 96px wide,
  35ms at 320px).
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional, Sequence, Tuple

import numpy as np

try:
    import cv2
    import onnxruntime as ort
    HAS_RUNTIME = True
except ImportError:  # pragma: no cover - exercised only on a broken install
    HAS_RUNTIME = False

from .lexicon import dictionary_ratio

MODEL_NAME = "Rec_PPOCRv5.onnx"
DICT_NAME = "Rec_PPOCRv5_dict.txt"

# Same chain and reasoning as the detector: DirectML where the machine has a
# DX12 device, CPU where it does not.
PROVIDERS = ("DmlExecutionProvider", "CPUExecutionProvider")

# The recogniser reads a strip of fixed height; width follows the aspect ratio.
INPUT_HEIGHT = 48
MAX_WIDTH = 320

# Crops are padded up to one of these widths so that a batch shares a shape.
# Without buckets every batch would pad to MAX_WIDTH and pay for blank space:
# a batch of 16 costs 112ms at 96 wide and 565ms at 320.
WIDTH_BUCKETS = (64, 96, 128, 160, 224, MAX_WIDTH)

BATCH_SIZE = 16

# A line whose words are all in the dictionary has nothing to gain from a
# second opinion, and skipping it is what pays for the ones that do.
SUSPECT_BELOW = 1.0

# Readings this unsure are noise whatever the dictionary makes of them.
MIN_CONFIDENCE = 0.60

# Pixels of margin added around a line before it is re-read. The boxes come
# from Windows OCR and hug the glyphs; this recogniser was trained on crops
# with a little air around them and reads them better for it. Measured on the
# comic pages: word recall 0.785 with no margin, 0.798 at 4px, 0.797 at 8px.
CROP_PADDING = 4


def _model_dir_candidates():
    root = os.path.join(os.path.dirname(__file__), '..', '..', '..')
    yield os.path.join(root, 'Model')
    yield os.path.join(root, 'assets', 'models')
    yield os.path.join(getattr(sys, '_MEIPASS', root), 'Model')


def _find(name: str) -> Optional[str]:
    for folder in _model_dir_candidates():
        path = os.path.abspath(os.path.join(folder, name))
        if os.path.exists(path):
            return path
    return None


class NeuralLineReader:
    """Reads single lines of text with the PP-OCRv5 recognition model.

    Loading is lazy and failure is not fatal: without this reader the pipeline
    is exactly what it was before, which is a working pipeline. Dropping text
    is the one outcome CLAUDE.md rules out, and that is not what happens here.
    """

    def __init__(self):
        self._session = None
        self._charset: List[str] = []
        self._unavailable = False

    # -- loading ----------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._load() is not None

    def _load(self):
        if self._session is not None or self._unavailable:
            return self._session
        if not HAS_RUNTIME:
            self._unavailable = True
            print("Neural reader: onnxruntime or opencv missing; "
                  "Windows OCR readings stand on their own.", flush=True)
            return None

        model_path, dict_path = _find(MODEL_NAME), _find(DICT_NAME)
        if not model_path or not dict_path:
            self._unavailable = True
            print(f"Neural reader: {MODEL_NAME} or {DICT_NAME} not found; "
                  "Windows OCR readings stand on their own.", flush=True)
            return None

        for provider in PROVIDERS:
            try:
                session = ort.InferenceSession(model_path, providers=[provider])
            except Exception as e:
                print(f"Neural reader: {provider} unavailable "
                      f"({type(e).__name__}: {e}); trying the next one.", flush=True)
                continue

            with open(dict_path, encoding="utf-8") as handle:
                characters = handle.read().splitlines()
            # Index 0 is the CTC blank and the trailing entry is the space the
            # dictionary file leaves implicit.
            self._charset = ["", *characters, " "]
            self._session = session
            print(f"Neural reader: loaded {MODEL_NAME} on "
                  f"{session.get_providers()[0]} "
                  f"({len(self._charset)} classes)", flush=True)
            return session

        self._unavailable = True
        print("Neural reader: no usable onnxruntime provider; "
              "Windows OCR readings stand on their own.", flush=True)
        return None

    def warmup(self) -> None:
        """Pay the session load and first-run compile before a user waits."""
        session = self._load()
        if session is None:
            return
        try:
            blank = np.zeros((INPUT_HEIGHT, WIDTH_BUCKETS[0], 3), dtype=np.uint8)
            self.read([blank])
        except Exception as e:
            print(f"Neural reader warmup failed, the first page will be "
                  f"slower: {type(e).__name__}: {e}", flush=True)

    # -- reading ----------------------------------------------------------

    @staticmethod
    def _bucket_for(width: int) -> int:
        for bucket in WIDTH_BUCKETS:
            if width <= bucket:
                return bucket
        return MAX_WIDTH

    def _prepare(self, crop) -> Tuple[Optional[np.ndarray], int]:
        """Scale one crop to the input height and normalise it."""
        if crop is None or getattr(crop, "size", 0) == 0:
            return None, 0
        if crop.ndim == 2:
            crop = cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR)

        height, width = crop.shape[:2]
        if height < 1 or width < 1:
            return None, 0

        scaled_width = int(round(width * INPUT_HEIGHT / height))
        scaled_width = max(8, min(MAX_WIDTH, scaled_width))
        resized = cv2.resize(crop, (scaled_width, INPUT_HEIGHT))
        normalised = (resized.astype(np.float32) / 255.0 - 0.5) / 0.5
        return np.transpose(normalised, (2, 0, 1)), scaled_width

    def _decode(self, probabilities: np.ndarray) -> Tuple[str, float]:
        """Collapse one CTC output into text plus how sure it was."""
        indices = probabilities.argmax(axis=1)
        scores = probabilities.max(axis=1)

        characters, kept, previous = [], [], -1
        for index, score in zip(indices, scores):
            if index != previous and index != 0:
                if index < len(self._charset):
                    characters.append(self._charset[index])
                    kept.append(score)
            previous = index

        text = "".join(characters).strip()
        return text, float(np.mean(kept)) if kept else 0.0

    def read(self, crops: Sequence) -> List[Tuple[str, float]]:
        """Read a list of line images. Returns (text, confidence) per crop."""
        session = self._load()
        results: List[Tuple[str, float]] = [("", 0.0)] * len(crops)
        if session is None or not len(crops):
            return results

        prepared = [self._prepare(crop) for crop in crops]
        pending = [(i, tensor, self._bucket_for(width))
                   for i, (tensor, width) in enumerate(prepared) if tensor is not None]
        # Grouping by bucket keeps every batch at the narrowest shape that fits
        # all of its members.
        pending.sort(key=lambda item: item[2])

        for start in range(0, len(pending), BATCH_SIZE):
            chunk = pending[start:start + BATCH_SIZE]
            bucket = max(item[2] for item in chunk)
            batch = np.zeros((len(chunk), 3, INPUT_HEIGHT, bucket), dtype=np.float32)
            for row, (_, tensor, _) in enumerate(chunk):
                usable = min(bucket, tensor.shape[2])
                batch[row, :, :, :usable] = tensor[:, :, :usable]

            try:
                output = session.run(None, {"x": batch})[0]
            except Exception as e:
                print(f"Neural reader failed on a batch of {len(chunk)}: "
                      f"{type(e).__name__}: {e}", flush=True)
                continue

            for row, (index, _, _) in enumerate(chunk):
                results[index] = self._decode(output[row])

        return results


def is_suspect(text: str) -> bool:
    """Whether a reading is worth a second opinion.

    A line the dictionary fully recognises is left alone; anything else - a
    misread word, a name, a shout - is a candidate.
    """
    if not text or not text.strip():
        return False
    return dictionary_ratio(text) < SUSPECT_BELOW


def _same_script(neural_text: str, windows_text: str) -> bool:
    """Whether the second reading stays in the writing system of the first.

    The recognition model carries an 18,000 character multilingual vocabulary,
    so a crop it cannot make sense of comes back as CJK or a stray symbol - a
    confident-looking answer in the wrong alphabet. Nothing downstream would
    notice: the text is simply sent to the translator as if it were the line.
    """
    if windows_text.isascii():
        return neural_text.isascii()
    return True


def choose_reading(windows_text: str, neural_text: str, confidence: float) -> str:
    """Pick between the two readings of one line.

    The dictionary decides, because that is the signal that separates the two
    error modes: `HANDS` is a word and `HANPS` is not, and no amount of pixel
    evidence tells the two apart once the glyphs really do look alike.

    The second reading wins ties - it is preferred unless the dictionary likes
    it *less*. That is not the obvious way round, since Windows OCR reads these
    pages better on its own, but it is what measured best: taking the second
    reading on a tie scored 0.819 against 0.795 for keeping the first. Only
    lines Windows OCR was already unsure of get this far, and among those a tie
    means neither is obviously wrong, where the differently-trained reader is
    the better bet.

    Confidence barely matters here: sweeping the floor from 0 to 0.8 moved the
    score by 0.003. It is kept only to throw out noise.
    """
    neural_text = (neural_text or "").strip()
    if not neural_text or confidence < MIN_CONFIDENCE:
        return windows_text
    if not _same_script(neural_text, windows_text):
        return windows_text
    if dictionary_ratio(neural_text) < dictionary_ratio(windows_text):
        return windows_text
    return neural_text
