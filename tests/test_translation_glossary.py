import pytest

from overlayr.services.translation_glossary import (
    canonicalize_embedded_translation,
    correct_contextual_ocr_labels,
    detect_translation_domain,
    lookup_translation_override,
)


def test_corrects_ftb_rewards_label_misread_as_fiends():
    corrected = correct_contextual_ocr_labels(
        ["Tasks", "Fiends", "Create a Coke Oven"]
    )

    assert corrected == ["Tasks", "Rewards", "Create a Coke Oven"]


def test_does_not_rewrite_fiends_without_ftb_tasks_label():
    corrected = correct_contextual_ocr_labels(
        ["Fiends", "Defeat every enemy"]
    )

    assert corrected == ["Fiends", "Defeat every enemy"]


def test_does_not_rewrite_fiends_inside_a_sentence():
    corrected = correct_contextual_ocr_labels(
        ["Tasks", "Defeat the fiends"]
    )

    assert corrected == ["Tasks", "Defeat the fiends"]


def test_detects_ftb_quests_domain_from_header_pair():
    assert detect_translation_domain(["Tasks", "Rewards"]) == "ftb_quests"


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


@pytest.mark.parametrize(
    ("source_text", "expected"),
    [
        ("Tasks", "ภารกิจ"),
        ("Rewards", "รางวัล"),
    ],
)
def test_ftb_quests_glossary_uses_stable_header_terms(source_text, expected):
    assert (
        lookup_translation_override(
            source_text,
            source="en",
            target="th",
            domain="ftb_quests",
        )
        == expected
    )


def test_ftb_quest_sentence_uses_canonical_watering_can_term():
    translated = canonicalize_embedded_translation(
        (
            "Fill a wooden watering can with water and use it to water "
            "your plants."
        ),
        "เติมน้ำลงในกระป๋องไม้แล้วใช้รดน้ำต้นไม้",
        source="en",
        target="th",
        domain="generic",
    )

    assert translated == "เติมน้ำลงในบัวรดน้ำไม้แล้วใช้รดน้ำต้นไม้"


def test_embedded_term_is_not_rewritten_without_matching_source_phrase():
    translated = canonicalize_embedded_translation(
        "Store the sample in a wooden can.",
        "เก็บตัวอย่างไว้ในกระป๋องไม้",
        source="en",
        target="th",
        domain="ftb_quests",
    )

    assert translated == "เก็บตัวอย่างไว้ในกระป๋องไม้"
