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

    @staticmethod
    def _has_asiatic_chars(text: str) -> bool:
        """Checks for Thai, CJK, etc."""
        for c in text:
            code = ord(c)
            # Thai
            if 0x0E00 <= code <= 0x0E7F: return True
            # CJK
            if 0x4E00 <= code <= 0x9FFF: return True
            # Hiragana/Katakana
            if 0x3040 <= code <= 0x30FF: return True
            # Hangul
            if 0xAC00 <= code <= 0xD7AF: return True
        return False

    @staticmethod
    def is_valid_text(text: str, source_lang: str) -> bool:
        """
        Determines if a piece of text is likely real content rather than noise.
        """
        text = text.strip()
        
        # Basic length and sanity checks
        length = len(text)
        if not text or length < 2 or length > 500:
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
            # English should NOT contain Asiatic characters (Thai, CJK)
            if ValidationService._has_asiatic_chars(text):
                return False
            
            # Check for non-ASCII characters (Stricter)
            # Allow widely used latin supplements but reject others
            non_standard = sum(1 for c in text if ord(c) > 383 and c not in "“”‘’\"…")
            if non_standard > 0:
                return False

            # Existing vowels check
            if letter_count > 0:
                # Exception for Honorifics/Abbreviations (Mr., Mrs., Dr., etc.)
                clean_upper = text.replace('.', '').upper()
                honorifics = {"MR", "MRS", "MS", "DR", "ST", "JR", "SR", "MT", "SGT", "CAPT"}
                
                if clean_upper in honorifics:
                    pass # Allow valid abbreviations even without vowels
                else:
                    vowel_count = sum(1 for c in text if c in ValidationService.VOWELS)
                    if vowel_count == 0 and length > 2:
                        return False
                        
                # Relaxed density check: allow short phrases with punctuation (e.g. "OH...I")
                # Only enforce 60% letters if text is longer than 8 chars (was 5)
                if letter_count < length * 0.6 and length > 8:
                    return False
            
            # Use langdetect for longer text to be sure
            if length > 20:
                try:
                    from langdetect import detect
                    lang = detect(text)
                    # If detected as Thai/CJK/etc, reject. 
                    if lang in ['th', 'ja', 'ko', 'zh-cn', 'zh-tw', 'vi']:
                        return False
                except Exception: pass

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
