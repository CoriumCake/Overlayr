import re
import os
from typing import List, Optional
try:
    from symspellpy import SymSpell, Verbosity
    from fuzzywuzzy import process, fuzz
except ImportError:
    SymSpell = None
    process = None
    fuzz = None
    print("Warning: post-processing dependencies not found. Install fuzzywuzzy, python-Levenshtein, symspellpy.")

class TextPostProcessor:
    """
    Handles advanced post-processing of OCR text:
    1. Regex Cleaning (Artifact removal)
    2. SymSpell Correction (Fast Dictionary)
    3. Fuzzy Matching (Fallback)
    """
    def __init__(self, dictionary_path: Optional[str] = None):
        self.sym_spell = None
        if SymSpell:
            self.sym_spell = SymSpell(max_dictionary_edit_distance=2, prefix_length=7)
            
            # Load dictionary if provided, otherwise try to find default in package
            dict_path = dictionary_path
            
            if not dict_path:
                try:
                    from importlib import resources
                    # Modern replacement for pkg_resources using importlib.resources (Python 3.9+)
                    dict_path = str(resources.files("symspellpy").joinpath("frequency_dictionary_en_82_765.txt"))
                except (ImportError, AttributeError):
                    dict_path = "frequency_dictionary_en_82_765.txt"

            if dict_path and os.path.exists(dict_path):
                if not self.sym_spell.load_dictionary(dict_path, term_index=0, count_index=1):
                    print(f"SymSpell: Failed to load dictionary from {dict_path}")
                else:
                    print(f"SymSpell: Loaded dictionary from {dict_path}")
            else:
                 print(f"SymSpell: Dictionary not found at {dict_path}. Correction disabled.")

        # Custom dictionary for specific game/manga terms
        self.custom_terms = [] 

    def clean_text(self, text: str) -> str:
        """
        Basic heuristic and regex cleaning.
        Removes common OCR artifacts like isolated symbols.
        """
        if not text: return ""
        
        # 1. Remove isolated special characters often mistaken for I/l/1 or noise
        # e.g. " | " " _ "
        cleaned = re.sub(r'\s+[-_|+=\\\/]+\s+', ' ', text)
        
        # 2. Fix common ligatures or spacing issues (basic)
        cleaned = cleaned.replace("ﬁ", "fi").replace("ﬂ", "fl")
        
        # 3. Strip leading/trailing non-alphanumeric noise
        # This removes things like ".Hello" -> "Hello" but keeps "Mr."
        # cleaned = re.sub(r'^[^a-zA-Z0-9]+', '', cleaned) 
        # cleaned = re.sub(r'[^a-zA-Z0-9.!?]+$', '', cleaned)
        
        return cleaned.strip()

    def _apply_custom_rules(self, text: str) -> str:
        """
        Hardcoded rules for common misreadings.
        """
        # Z0 -> 30 (Start of string or word boundary)
        text = re.sub(r'(?i)\bZ[0O]\b', '30', text)  # Z0, z0, ZO, zo -> 30
        text = re.sub(r'(?i)\bZ[0O]-', '30-', text)  # Z0-YEAR -> 30-YEAR
        
        # OlD -> OLD (Handle 0LD, O1D, etc.)
        # Use lookbehind for word boundary OR hyphen
        text = re.sub(r'(?i)(?<=[\s\b-])[0O][l1I]D\b', 'OLD', text)
        text = re.sub(r'(?i)\bY[E3][A4]R\b', 'YEAR', text) # Y3AR -> YEAR
        
        # FIX: "OH...I" read backwards as "I"""HO"
        text = re.sub(r'(?i)I"+HO', "OH...I", text)

        # Generic 0 -> O Rule:
        # If 0 is adjacent to a letter and NOT adjacent to another digit, treat as O.
        # Covers: BEDR00M, H0ME, L00K, 0K, 0LD
        # Excludes: 10th, 2000, Area 51
        def replace_zeros(match):
            return 'O' * len(match.group())
            
        text = re.sub(r'(?<=[a-zA-Z])0+(?!\d)|(?<!\d)0+(?=[a-zA-Z])', replace_zeros, text)
        
        return text

    def _apply_ocr_corrections(self, text: str) -> str:
        """
        Regex-based OCR misreading corrections.
        Ported from WindowOCR-Refining/ocr_postprocessor.py.
        """
        if not text: return text

        # 1. PONT -> DONT
        text = re.sub(r'\bPONT\b', 'DONT', text)
        
        # 2. PO -> DO (standalone)
        text = re.sub(r'\bPO\b', 'DO', text)
        
        # 3. 1 -> I before specific verbs
        text = re.sub(r"(^|\s)1(?=\s+(?:AM|CAN\'?T|DON\'?T|WAS|WILL|HAVE|HAD|SHOULD|COULD|MUST)\b)", r'\1I', text, flags=re.IGNORECASE)

        # 4. 15 -> IS in context
        text = re.sub(r'\bNAME\s+15\b', 'NAME IS', text)
        text = re.sub(r'\b15(?=\s+(?:THAT|THIS|THE|IT|WHAT|THERE)\b)', 'IS', text)
        
        # 4.1: Standalone "15" with no other digits -> IS
        digit_count = sum(c.isdigit() for c in text)
        if digit_count == 2 and "15" in text.split(): 
            text = re.sub(r'\b15\b', 'IS', text)
        
        # 5. € -> G
        text = text.replace('€', 'G')

        # 5.1 Characters the engine substitutes inside comic lettering, anchored
        # between two letters so ordinary punctuation is left alone. These are
        # the cases the dictionary-guarded repair cannot reach, because the word
        # they belong to is often a name the dictionary has never heard of.
        # Counted across the sample pages: "/" for "I" 13 times, "7" for "T" 3.
        text = re.sub(r'(?<=[A-Za-z])[/|](?=[A-Za-z])', 'I', text)
        text = re.sub(r'(?<=[A-Za-z])7(?=[A-Za-z])', 'T', text)
        text = re.sub(r'(?<=[A-Za-z])\$(?=[A-Za-z])', 'S', text)
        text = text.replace('Ø', 'O').replace('ø', 'o')

        # A slash or pipe standing alone is the letter I, not punctuation.
        text = re.sub(r'(?<!\S)[/|](?!\S)', 'I', text)
        
        # 6. Specific corrections
        text = re.sub(r'\bBUT 1\b', 'BUT I', text, flags=re.IGNORECASE)
        text = re.sub(r'\bTHAN 1\b', 'THAN I', text, flags=re.IGNORECASE)
        text = re.sub(r'\b1 G0T\b', 'I GOT', text, flags=re.IGNORECASE)
        text = re.sub(r'\b0Nce\b', 'ONce', text)
        text = re.sub(r'\beo\b', 'Go', text)
        text = text.replace('•••', '. . .')
        text = re.sub(r'\$ELF\b', 'SELF', text, flags=re.IGNORECASE)
        
        # Generic 0 -> O at word start
        text = re.sub(r'\b0(?=[A-Za-z]{2,})', 'O', text)

        # 7. OJT -> OUT
        text = re.sub(r'\bOJT\b', 'OUT', text)
        
        # 8. 60 -> GO at start
        if text.startswith("60 "):
            text = "GO " + text[3:]
        elif text == "60":
            text = "GO"
            
        # 9. N& -> NG
        text = re.sub(r'N&', 'NG', text)
        
        # 10. Strip leading/trailing non-alphanumeric artifacts
        text = re.sub(r'^[\/\|\)\(\\]+\s*', '', text)
        text = re.sub(r'\s*[\/\|\)\(\\]+$', '', text)
        
        # 11. 1 at start -> I
        text = re.sub(r'^1(?=\s|$)', 'I', text)
        
        # 12. 5 mixed with letters -> S
        def fix_5_to_S(match):
            word = match.group(0)
            if re.search(r'[A-Za-z]', word):
                return word.replace('5', 'S')
            return word
        text = re.sub(r'\w+', fix_5_to_S, text)

        return text

    def correct_text(self, text: str) -> str:
        """
        Apply SymSpell correction then Fuzzy fallback.
        """
        if not text: return text

        # 0. OCR-specific corrections (regex rules)
        text = self._apply_ocr_corrections(text)

        # 0b. Undo the letter shapes this OCR engine confuses in comic fonts.
        text = fix_confusions(text)
        
        # 1. Custom Rules (character-level substitutions)
        text = self._apply_custom_rules(text)
        
        if not self.sym_spell:
            return text

        # Split into words for SymSpell? 
        # SymSpell supports compound splitting, but let's try word-by-word or full phrase lookup first.
        # For mixed content, SymSpell's lookup_compound is powerful.
        
        suggestions = self.sym_spell.lookup_compound(text, max_edit_distance=2)

        if suggestions:
            corrected = suggestions[0].term
            return self._accept_correction(text, corrected)

        return text

    @staticmethod
    def _accept_correction(original: str, corrected: str) -> str:
        """Return the correction, or the original when the change went too far.

        ``lookup_compound`` does not only fix spelling: it splits and joins
        words to make the sentence fit the dictionary. That is the wrong trade
        here. It turned an item count of "100" into "a of", and the name
        "TAKAKURA" into "taka kura" - both of which read as a loss to anyone
        following the story, while the misspelling they replaced did not.

        So two things have to survive a correction: the numbers, and the number
        of words. Fixing the letters inside a word is welcome; restructuring the
        sentence around it is not.
        """
        digits_before = re.findall(r"\d+", original)
        if digits_before and re.findall(r"\d+", corrected) != digits_before:
            return original

        if len(original.split()) != len(corrected.split()):
            return original

        return corrected

    def fuzzy_match_custom(self, text: str) -> str:
        """
        Fallback fuzzy matching against a custom term list.
        """
        if not self.custom_terms or not process:
            return text
            
        # If text is very short, fuzzy matching might be dangerous
        if len(text) < 3: return text
        
        # Find best match
        best_match, score = process.extractOne(text, self.custom_terms, scorer=fuzz.ratio)
        
        if score > 85: # High confidence threshold
            return best_match
            
        return text




# Characters this OCR engine confuses on comic lettering, together with what
# they should have been. Every entry was mined from the sample pages by lining
# OCR output up against the ground truth and counting substitutions, so the
# table describes what actually goes wrong rather than what might:
#
#   / -> I  (13)   M/SS/ON, ACC/PEN?ALLY, RECONC/L/-
#   P -> D  (20)   NEEP, HANPS, ANP, LOAPEP
#   C -> G   (8)
#   7 -> T   (3)   ME77CUZOUS
#   5 -> S         REFU5E
#   U -> LI        UTTLE, UKE
#
# Nothing here fires on its own: a swap is only kept when it turns a word the
# dictionary rejects into one it accepts, so a correctly-read "P" is never
# touched.
OCR_CONFUSIONS = {
    "/": "I", "|": "I", "!": "I",
    "p": "d", "P": "D",
    "c": "g", "C": "G",
    "7": "T", "5": "S", "0": "O", "1": "I", "8": "B", "6": "G",
    "€": "G", "£": "E", "ø": "o", "Ø": "O", "§": "S", "$": "S",
    "u": "li", "U": "LI",
    "rn": "m", "RN": "M", "cl": "d", "CL": "D",
    "vv": "w", "VV": "W",
}

# Characters that may sit inside a word without ending it. Without these, a
# misread like "M/SS/ON" is split into fragments and never repaired.
_TOKEN_CHARS = re.compile(r"[^\W_]|[/|!€£$§ø Ø]", re.UNICODE)

_TRIM = " \t\n\r.,:;!?\"'()[]{}-–—…"


def _confusion_candidates(token: str):
    """Words reachable by replacing every occurrence of some confused characters.

    Replacing all occurrences at once rather than one position at a time is what
    makes a word like "M/SS/ON" reachable: both slashes are the same mistake,
    made twice. It also keeps the search small - two choices per distinct
    character present, instead of one per position.
    """
    present = [(wrong, right) for wrong, right in OCR_CONFUSIONS.items()
               if wrong in token]
    if not present or len(present) > 6:
        return

    for mask in range(1, 1 << len(present)):
        candidate = token
        for bit, (wrong, right) in enumerate(present):
            if mask & (1 << bit):
                candidate = candidate.replace(wrong, right)
        if candidate != token:
            yield candidate


def fix_confusions(text: str) -> str:
    """Repair words the OCR engine misread as similar-looking characters.

    Only a token the dictionary rejects is touched, and only if a swap turns it
    into one the dictionary accepts. That is what keeps a real "P" from being
    rewritten to "D" - the correction has to earn its place by producing a word.

    This runs before the speller because it fixes the kind of damage the speller
    cannot: "HANPS" is one edit from both "HANDS" and "HANGS", and a frequency
    dictionary has no way to prefer the right one, while the shape of the glyph
    does.
    """
    if not text:
        return text

    from ..services.lexicon import is_word

    repaired = []
    for token in text.split(" "):
        core = token.strip(_TRIM)
        if len(core) < 3 or not any(c.isalpha() or c in "/|!" for c in core):
            repaired.append(token)
            continue
        if is_word(core):
            repaired.append(token)
            continue

        replacement = None
        for candidate in _confusion_candidates(core):
            if is_word(candidate.strip(_TRIM)):
                # Comic text is set in capitals; returning a lower-case word
                # would read as a different one.
                replacement = candidate.upper() if core.isupper() else candidate
                break

        repaired.append(token.replace(core, replacement) if replacement else token)

    return " ".join(repaired)
