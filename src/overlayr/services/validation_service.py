from typing import Set
import re

class ValidationService:
    """
    Provides logic to filter and validate OCR text detections.
    """
    
    SYMBOLS: Set[str] = set(r"{}[]|=+-_/\<>*^#~€£¥•©®™()[].,:;!?'\"")
    VOWELS: Set[str] = set("aeiouAEIOUyY")
    
    # regex for URLs, domains, and common UI artifacts
    UI_PATTERNS = [
        re.compile(r'https?://\S+', re.I),           # http/https
        re.compile(r'\S+\.com\S*', re.I),            # .com
        re.compile(r'\S+\.to\S*', re.I),             # .to (common for anime sites)
        re.compile(r'\S+\.net\S*', re.I),            # .net
        re.compile(r'\S+\.org\S*', re.I),            # .org
        re.compile(r'\S+/\S+/\S+', re.I),            # path-like strings
        re.compile(r'[a-z0-9]+\.[a-z]{2,}/', re.I),  # site.something/
    ]

    # Share of a string that has to be foreign to its source language before it
    # is treated as noise. A stray character is an OCR slip inside real text,
    # not a reason to throw the whole line away.
    FOREIGN_CHAR_LIMIT = 0.35

    @staticmethod
    def _is_asiatic(c: str) -> bool:
        code = ord(c)
        return (0x0E00 <= code <= 0x0E7F        # Thai
                or 0x4E00 <= code <= 0x9FFF     # CJK
                or 0x3040 <= code <= 0x30FF     # Hiragana / Katakana
                or 0xAC00 <= code <= 0xD7AF)    # Hangul

    @staticmethod
    def _has_asiatic_chars(text: str) -> bool:
        """Checks for Thai, CJK, etc."""
        return any(ValidationService._is_asiatic(c) for c in text)

    @staticmethod
    def _asiatic_ratio(text: str) -> float:
        stripped = [c for c in text if not c.isspace()]
        if not stripped:
            return 0.0
        return sum(1 for c in stripped if ValidationService._is_asiatic(c)) / len(stripped)

    @staticmethod
    def is_valid_text(text: str, source_lang: str) -> bool:
        """
        Determines if a piece of text is likely real content rather than noise.

        The filter is deliberately conservative (CLAUDE.md section 1): it cuts
        only what is obviously junk - symbols, bare numbers, URLs. Dropping a
        line that carried meaning breaks the reader`s train of thought, which
        the project counts as worse than an clumsy translation, so anything that
        might be words is let through.
        """
        text = text.strip()

        # Basic length and sanity checks
        length = len(text)
        if not text or length > 500:
            return False

        # A lone letter is usually a real word ("I", "A") or part of one that
        # the merge step is about to rejoin; a lone symbol never is.
        if length == 1 and not text.isalpha():
            return False


        # UI/URL Filtering
        # Exclude technical strings, URLs, and browser UI elements
        for pattern in ValidationService.UI_PATTERNS:
            if pattern.search(text):
                return False
            
        # 1. Symbol and Digit Density Check
        digit_count = sum(1 for c in text if c.isdigit())
        symbol_count = sum(1 for c in text if c in ValidationService.SYMBOLS)
        letter_count = sum(1 for c in text if c.isalpha())
        
        # If it's mostly symbols or digits, it's probably noise (coordinates, random artifacts)
        # Exception: short numbers like "10" or "1:00" might be valid, but "(0.0)" or "0724" are usually noise here.
        # If it's mostly symbols or digits, it's probably noise (coordinates, random artifacts)
        # Exception: short numbers like "10" or "1:00" might be valid, but "(0.0)" or "0724" are usually noise here.
        if (symbol_count + digit_count) > length * 0.7 and length < 10:
            return False
            
        if symbol_count > length * 0.5:
            return False
            
        # 2. Source-Specific Logic
        if source_lang == 'en':
            # Mostly CJK or Thai in an English capture is noise, but a single
            # stray glyph inside a real sentence is an OCR slip worth keeping.
            if ValidationService._asiatic_ratio(text) > ValidationService.FOREIGN_CHAR_LIMIT:
                return False

            # A comic or game font sends back the odd unusual character - the
            # currency sign an OCR engine sees in a stylised G, an accent in a
            # name. Reject only when such characters dominate the string.
            non_standard = sum(1 for c in text if ord(c) > 383 and c not in "“”‘’\"…")
            if non_standard > max(1, length * 0.4):
                return False

            # Letter density: a long string that is mostly punctuation is noise.
            if letter_count > 0 and letter_count < length * 0.6 and length > 8:
                return False

            # The vowel test and the langdetect call that used to sit here were
            # removed: comics are full of vowel-free real text (HMM, TSK, PSST)
            # and language detection on shouted, stylised lines is unreliable.

        elif source_lang == 'th':
            # Thai MUST contain some Thai characters
            if not ValidationService._has_asiatic_chars(text):
                return False

        # 3. Mixed Gibberish Improved
        if letter_count > 0 and (digit_count > 0 or symbol_count > 0):
            # If it's short and mixed without spaces, it's noise
            if ' ' not in text and length < 15:
                # Relaxed: Allow if it has enough vowels or looks like a code
                pass 
                
            # Even with spaces, if there are many symbols/digits relative to letters, it's noise
            if (digit_count + symbol_count) > letter_count * 2 and length < 20:
                return False
                
        # 4. Pure Digits or Symbols Check
        if (digit_count + symbol_count) == length and length < 10:
            # Random numbers or symbol strings like "0724" or "!!!..." are usually noise
            # Exception: we might want to allow some, but in this context they are almost always artifacts
            return False
            
        # 5. Low Alpha Ratio for longer strings
        if letter_count < length * 0.5 and length > 10:
            return False
                
        return True
