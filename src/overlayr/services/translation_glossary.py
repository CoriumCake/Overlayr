"""Small, context-aware overrides for terms machine translation cannot disambiguate."""

from __future__ import annotations

import re
from typing import Iterable, Optional

_MINECRAFT_EN_TH = {
    "chest": "หีบ",
    "trapped chest": "หีบกับดัก",
    "ender chest": "หีบเอนเดอร์",
    "minecraft": "Minecraft",
}

_FTB_QUESTS_EN_TH = {
    "tasks": "ภารกิจ",
    "rewards": "รางวัล",
    "wooden watering can": "บัวรดน้ำไม้",
}

_EMBEDDED_EN_TH = {
    "wooden watering can": {
        "canonical": "บัวรดน้ำไม้",
        "aliases": (
            "กระป๋องไม้",
            "กระป๋องรดน้ำไม้",
            "กระป๋องรดน้ำที่ทำจากไม้",
            "บัวรดน้ำที่ทำจากไม้",
        ),
    },
}

# RapidOCR can confuse the compact FTB pixel glyphs in "Rewards" with
# "Fiends". This alias is intentionally activated only when the companion
# "Tasks" header is present in the same frame.
_FTB_QUESTS_OCR_LABEL_ALIASES = {
    "fiends": "Rewards",
}


def _normalize_text(text: str) -> str:
    return " ".join(text.casefold().split())


def correct_contextual_ocr_labels(texts: Iterable[str]) -> list[str]:
    """Correct known pixel-font label errors only in their matching UI."""
    labels = [texts] if isinstance(texts, str) else list(texts)
    normalized = {
        _normalize_text(text)
        for text in labels
        if text
    }
    if "tasks" not in normalized:
        return labels
    if not normalized.intersection(_FTB_QUESTS_OCR_LABEL_ALIASES):
        return labels

    return [
        _FTB_QUESTS_OCR_LABEL_ALIASES.get(_normalize_text(text), text)
        if text
        else text
        for text in labels
    ]


def detect_translation_domain(texts: Iterable[str]) -> str:
    """Infer a narrow domain from labels visible in the current OCR frame."""
    if isinstance(texts, str):
        texts = [texts]

    normalized = tuple(_normalize_text(text) for text in texts if text)
    labels = set(normalized)
    if {"tasks", "rewards"}.issubset(labels):
        return "ftb_quests"
    if any("minecraft" in text for text in normalized):
        return "minecraft"
    return "generic"


def lookup_translation_override(
    text: str,
    *,
    source: str,
    target: str,
    domain: str,
) -> Optional[str]:
    """Return an exact canonical term, leaving sentences to the translator."""
    normalized_source = source.casefold().split("-", 1)[0]
    normalized_target = target.casefold().split("-", 1)[0]
    if normalized_source not in {"en", "english"}:
        return None
    if normalized_target not in {"th", "thai"}:
        return None
    normalized_text = _normalize_text(text)
    if domain == "ftb_quests":
        return _FTB_QUESTS_EN_TH.get(normalized_text)
    if domain == "minecraft":
        return _MINECRAFT_EN_TH.get(normalized_text)
    return None


def canonicalize_embedded_translation(
    source_text: str,
    translated_text: str,
    *,
    source: str,
    target: str,
    domain: str,
) -> str:
    """Stabilize known terms inside otherwise naturally translated sentences."""
    normalized_source_language = source.casefold().split("-", 1)[0]
    normalized_target_language = target.casefold().split("-", 1)[0]
    if normalized_source_language not in {"en", "english"}:
        return translated_text
    if normalized_target_language not in {"th", "thai"}:
        return translated_text

    normalized_source_text = _normalize_text(source_text)
    result = translated_text
    for source_term, term in _EMBEDDED_EN_TH.items():
        source_pattern = rf"(?<!\w){re.escape(source_term)}(?!\w)"
        if re.search(source_pattern, normalized_source_text) is None:
            continue
        canonical = term["canonical"]
        for alias in term["aliases"]:
            result = result.replace(alias, canonical)
    return result
