import pytest

from overlayr.services.translation_glossary import (
    detect_translation_domain,
    lookup_translation_override,
)


def test_detects_minecraft_domain_from_brand_label():
    assert detect_translation_domain(["Chest", "Minecraft"]) == "minecraft"


def test_generic_text_does_not_assume_minecraft_domain():
    assert detect_translation_domain(["Chest pain", "Medical notes"]) == "generic"


@pytest.mark.parametrize(
    ("source_text", "expected"),
    [
        ("Chest", "หีบ"),
        ("  CHEST  ", "หีบ"),
        ("Trapped Chest", "หีบกับดัก"),
        ("Ender Chest", "หีบเอนเดอร์"),
        ("Minecraft", "Minecraft"),
    ],
)
def test_minecraft_glossary_uses_canonical_thai_terms(source_text, expected):
    assert (
        lookup_translation_override(
            source_text,
            source="en",
            target="th",
            domain="minecraft",
        )
        == expected
    )


def test_exact_glossary_does_not_rewrite_longer_sentences():
    assert (
        lookup_translation_override(
            "Chest pain",
            source="en",
            target="th",
            domain="minecraft",
        )
        is None
    )


def test_glossary_does_not_override_generic_translation():
    assert (
        lookup_translation_override(
            "Chest",
            source="en",
            target="th",
            domain="generic",
        )
        is None
    )
