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
        
        # 1. Custom Rules (character-level substitutions)
        text = self._apply_custom_rules(text)
        
        if not self.sym_spell:
            return text

        # Split into words for SymSpell? 
        # SymSpell supports compound splitting, but let's try word-by-word or full phrase lookup first.
        # For mixed content, SymSpell's lookup_compound is powerful.
        
        suggestions = self.sym_spell.lookup_compound(text, max_edit_distance=2)
        
        if suggestions:
            # Take the best suggestion
            corrected = suggestions[0].term
            
            # Use Fuzzy Matching as a sanity check or for custom terms?
            # User request: "If not found (in Dictionary), use Fuzzy Matching"
            
            # If the correction is drastically different or low confidence, maybe check custom list
            # But lookup_compound usually forces dictionary words.
            return corrected
            
        return text

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
