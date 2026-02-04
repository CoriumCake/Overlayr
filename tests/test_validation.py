import pytest
from overlayr.services.validation_service import ValidationService

def test_validation_valid_english():
    assert ValidationService.is_valid_text("Hello world", "en") is True
    assert ValidationService.is_valid_text("This is a test sentence.", "en") is True

def test_validation_noise():
    # High symbol density
    assert ValidationService.is_valid_text("{}[][]=+", "en") is False
    # No vowels in English
    assert ValidationService.is_valid_text("bcdfgh", "en") is False
    # Mixed gibberish
    assert ValidationService.is_valid_text("NOhh5#nf51üm6", "en") is False
    # Cases from screenshot
    assert ValidationService.is_valid_text("(0.0)", "en") is False
    assert ValidationService.is_valid_text("0724", "en") is False
    assert ValidationService.is_valid_text("nn5finm", "en") is False
    assert ValidationService.is_valid_text("ucuK,j", "en") is False
    assert ValidationService.is_valid_text("auz«uusa. n.", "en") is False

def test_validation_short_long():
    assert ValidationService.is_valid_text("a", "en") is False
    assert ValidationService.is_valid_text("A" * 501, "en") is False

def test_validation_non_ascii_en():
    # Noise with accents in English source
    assert ValidationService.is_valid_text("Héllò", "en") is False
    # But allowed in other languages (logic might need expansion for Ja/Ko, but for now it's simple)
    assert ValidationService.is_valid_text("こんにちは", "ja") is True
