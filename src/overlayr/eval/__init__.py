"""Quality measurement harness for the OCR pipeline.

Exists so every OCR change in later phases can be answered with a number instead
of an impression. See CLAUDE.md section 1 ("เกณฑ์ความแม่นยำต้องวัดได้").
"""
from .metrics import cer, word_recall, line_recall, normalize, tokenize
from .dataset import Sample, load_dataset

__all__ = [
    "cer",
    "word_recall",
    "line_recall",
    "normalize",
    "tokenize",
    "Sample",
    "load_dataset",
]
