import sys
import os

# Add src to path
sys.path.append(os.path.join(os.getcwd(), 'src'))

from overlayr.services.validation_service import ValidationService

test_cases = [
    ("Hello world", "en", True),
    ("{}[][]=+", "en", False),
    ("bcdfgh", "en", False),
    ("NOhh5#nf51üm6", "en", False),
    ("(0.0)", "en", False),
    ("0724", "en", False),
    ("nn5finm", "en", False),
    ("ucuK,j", "en", False),
    ("auz«uusa. n.", "en", False),
    ("hianime.to/watch/one-piece-100?ep=214", "en", False),
    ("http://google.com", "en", False),
    ("www.example.net", "en", False),
]

for text, lang, expected in test_cases:
    result = ValidationService.is_valid_text(text, lang)
    status = "PASS" if result == expected else "FAIL"
    print(f"[{status}] text='{text}' lang='{lang}' expected={expected} actual={result}")
