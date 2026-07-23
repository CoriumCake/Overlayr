import sys
sys.path.append('src')
from overlayr.core.post_processing import TextPostProcessor

processor = TextPostProcessor()

test_cases = [
    'Y3AR',
    'Y4AR',
    'H0ME',
    '0LD',
    'O1D',
    'YE5',
    '1 CAN',
    'PON\'T',
    'Hello | World _ Test + = \\ ... \" !'
]

print('--- TextPostProcessor Test ---')
for text in test_cases:
    # SymSpell disabled for raw testing
    cleaned = processor.clean_text(text)
    corrected = processor._apply_custom_rules(cleaned)
    print(f'Original: {text} -> Corrected: {corrected}')
