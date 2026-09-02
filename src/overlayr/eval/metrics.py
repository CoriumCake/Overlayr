"""Text accuracy metrics for the OCR harness.

Three numbers, matching the three accuracy conditions in CLAUDE.md section 1:

* ``line_recall``  -> "ไม่ตกหล่น": how much of the ground truth was captured at all.
                      This is the primary metric; a dropped line breaks meaning.
* ``word_recall``  -> a finer-grained view of the same question.
* ``cer``          -> character error rate, the classic OCR number. Reported for
                      trend watching only; it is not the pass/fail criterion.

Pure text in, floats out - no Windows dependency, so this module is testable
anywhere.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import List, Sequence

# Each CJK ideograph / kana is its own token; Thai and Latin come in runs.
_CJK = r"぀-ヿ㐀-䶿一-鿿豈-﫿가-힯"
_TOKEN_RE = re.compile(
    rf"[{_CJK}]|[฀-๿]+|[a-z0-9]+(?:'[a-z]+)?",
    re.IGNORECASE,
)

# Punctuation and separators are noise for accuracy purposes: OCR reading a
# full-width comma as a half-width one is not a meaning loss.
_STRIP_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize(text: str) -> str:
    """Fold width, case and punctuation so only meaningful characters remain."""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text).lower()
    return _STRIP_RE.sub("", folded)


def tokenize(text: str) -> List[str]:
    """Split text into comparable tokens (see module docstring for the rules)."""
    if not text:
        return []
    folded = unicodedata.normalize("NFKC", text).lower()
    return _TOKEN_RE.findall(folded)


def levenshtein(a: str, b: str) -> int:
    """Edit distance between two strings, two-row DP."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(
                previous[j] + 1,          # deletion
                current[j - 1] + 1,       # insertion
                previous[j - 1] + (ca != cb),  # substitution
            ))
        previous = current
    return previous[-1]


def cer(reference: str, hypothesis: str) -> float:
    """Character error rate over normalized text. 0.0 is perfect, 1.0 is total loss.

    Capped at 1.0 so a hallucinating run cannot drag an average to nonsense.
    """
    ref = normalize(reference)
    hyp = normalize(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return min(1.0, levenshtein(ref, hyp) / len(ref))


def word_recall(reference: str, hypothesis: str) -> float:
    """Fraction of ground-truth tokens that also appear in the OCR output.

    Multiset based, so a word expected three times and read once scores 1/3.
    Extra tokens are not penalized - that is what ``cer`` is for.
    """
    ref_tokens = Counter(tokenize(reference))
    if not ref_tokens:
        return 1.0
    hyp_tokens = Counter(tokenize(hypothesis))
    found = sum(min(count, hyp_tokens[token]) for token, count in ref_tokens.items())
    return found / sum(ref_tokens.values())


def group_separation(reference_groups: Sequence[Sequence[str]],
                     hypothesis_blocks: Sequence[str]) -> float:
    """Share of produced blocks that did not fuse two ground-truth groups.

    This is the measurement for the failure CLAUDE.md section 1 ranks above
    every other: text merged across a bubble, a column, or a UI panel is
    translated as a sentence nobody wrote, and unlike a misread character the
    reader cannot recover from it.

    A block counts as fusing two groups when it carries words that belong
    distinctively to each of them. Words shared between groups are ignored, so
    two bubbles that both say "the" are not treated as merged; a group with no
    distinctive words at all cannot be detected either way and is skipped.

    Returns 1.0 when nothing was fused, and 1.0 for an empty comparison, so a
    sample with no grouping information neither rewards nor punishes a change.
    """
    groups = [set(tokenize(" ".join(group))) for group in reference_groups]
    groups = [group for group in groups if group]
    if len(groups) < 2 or not hypothesis_blocks:
        return 1.0

    # A word is distinctive if exactly one group uses it.
    counts = Counter()
    for group in groups:
        counts.update(group)
    distinctive = [{token for token in group if counts[token] == 1}
                   for group in groups]

    considered = 0
    fused = 0
    for block in hypothesis_blocks:
        block_tokens = set(tokenize(block))
        if not block_tokens:
            continue
        present = [index for index, marks in enumerate(distinctive)
                   if len(marks & block_tokens) >= 2]
        if not present:
            continue
        considered += 1
        if len(present) >= 2:
            fused += 1

    if considered == 0:
        return 1.0
    return 1.0 - fused / considered


def _similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return 1.0 - levenshtein(a, b) / max(len(a), len(b))


def line_recall(
    reference_lines: Sequence[str],
    hypothesis_lines: Sequence[str],
    threshold: float = 0.7,
) -> float:
    """Fraction of ground-truth lines that survived, whole, into the OCR output.

    Matching happens in two passes:

    1. line to line - a ground-truth line counts as captured when its best match
       among the OCR lines is at least ``threshold`` similar;
    2. word by word - for lines that found no single match, the words are looked
       up in what is left of the OCR output, and the line counts as captured
       when at least ``threshold`` of them are there.

    The second pass exists because OCR routinely splits one line into several
    boxes, and because the boxes of a two-column page interleave. Requiring the
    pieces to stay adjacent would report those lines as lost when every word was
    in fact read - a false alarm that would send later phases chasing nothing.
    Whether the pieces ended up grouped together is a separate question, and
    belongs to a grouping metric rather than to this one.

    Both passes consume what they match, so one OCR line cannot cover two
    different ground-truth lines.
    """
    refs = [line for line in reference_lines if normalize(line)]
    if not refs:
        return 1.0

    hyps = [line for line in hypothesis_lines if normalize(line)]
    normalized_hyps = [normalize(line) for line in hyps]

    available = list(range(len(hyps)))
    captured = [False] * len(refs)

    for i, ref in enumerate(refs):
        normalized_ref = normalize(ref)
        best_idx, best_score = None, 0.0
        for idx in available:
            score = _similarity(normalized_ref, normalized_hyps[idx])
            if score > best_score:
                best_idx, best_score = idx, score
        if best_score >= threshold and best_idx is not None:
            available.remove(best_idx)
            captured[i] = True

    remaining = Counter()
    for idx in available:
        remaining.update(tokenize(hyps[idx]))

    for i, ref in enumerate(refs):
        if captured[i]:
            continue
        tokens = tokenize(ref)
        if not tokens:
            continue
        found = 0
        for token in tokens:
            if remaining[token] > 0:
                remaining[token] -= 1
                found += 1
        if found / len(tokens) >= threshold:
            captured[i] = True

    return sum(captured) / len(refs)
