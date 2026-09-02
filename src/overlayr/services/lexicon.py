"""A word list used to judge which of several OCR readings looks like language.

Windows OCR returns no confidence (CLAUDE.md section 3), so when two techniques
disagree about the same patch of pixels something else has to break the tie.
Asking how many of the words are real words is the cheapest such test, and it is
what section 6 names as the first tie-breaker.

The list is the frequency dictionary that ships with symspellpy, read directly
as a set of terms: only membership is needed here, so building a full SymSpell
index would cost time and memory for nothing.
"""
from __future__ import annotations

import os
import re
from typing import Optional, Set

_WORDS: Optional[Set[str]] = None
_LOAD_FAILED = False

# Only Latin words can be looked up; CJK text has no equivalent list here, and
# scoring it against an English dictionary would always return zero.
_LATIN_WORD = re.compile(r"[a-z]{2,}")


def _dictionary_path() -> Optional[str]:
    try:
        from importlib import resources
        path = resources.files("symspellpy").joinpath(
            "frequency_dictionary_en_82_765.txt")
        return str(path)
    except Exception as e:
        print(f"Lexicon: cannot locate the symspellpy dictionary: "
              f"{type(e).__name__}: {e}")
        return None


def _load() -> Set[str]:
    global _WORDS, _LOAD_FAILED
    if _WORDS is not None:
        return _WORDS
    if _LOAD_FAILED:
        return set()

    path = _dictionary_path()
    if not path or not os.path.exists(path):
        print("Lexicon: word list unavailable; readings will be ranked without it.")
        _LOAD_FAILED = True
        return set()

    words = set()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                term = line.split(" ", 1)[0].strip()
                if term:
                    words.add(term)
    except OSError as e:
        print(f"Lexicon: failed to read {path}: {e}")
        _LOAD_FAILED = True
        return set()

    _WORDS = words
    return _WORDS


def is_word(token: str) -> bool:
    return token.lower() in _load()


def dictionary_ratio(text: str) -> float:
    """Share of the Latin words in ``text`` that are real words.

    Text with no Latin words - a Japanese line, a row of numbers - scores a
    neutral 0.5 so this test neither favours nor punishes it, leaving the
    decision to the tie-breakers behind it.
    """
    tokens = _LATIN_WORD.findall(text.lower())
    if not tokens:
        return 0.5

    words = _load()
    if not words:
        return 0.5

    found = sum(1 for token in tokens if token in words)
    return found / len(tokens)
