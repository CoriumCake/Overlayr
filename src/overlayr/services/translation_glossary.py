"""Small, context-aware overrides for terms machine translation cannot disambiguate."""

from __future__ import annotations

from typing import Iterable, Optional

_MINECRAFT_EN_TH = {
    "chest": "หีบ",
    "trapped chest": "หีบกับดัก",
    "ender chest": "หีบเอนเดอร์",
    "minecraft": "Minecraft",
}


def _normalize_text(text: str) -> str:
    return " ".join(text.casefold().split())


def detect_translation_domain(texts: Iterable[str]) -> str:
    """Infer a narrow domain from labels visible in the current OCR frame."""
    if isinstance(texts, str):
        texts = [texts]

    normalized = tuple(_normalize_text(text) for text in texts if text)
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
    if domain != "minecraft":
        return None
    return _MINECRAFT_EN_TH.get(_normalize_text(text))
