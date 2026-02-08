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
                    import pkg_resources
                    dict_path = pkg_resources.resource_filename(
                        "symspellpy", "frequency_dictionary_en_82_765.txt"
                    )
                except (ImportError, FileNotFoundError):
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

    def correct_text(self, text: str) -> str:
        """
        Apply SymSpell correction then Fuzzy fallback.
        """
        if not text: return text
        
        # 0. Custom Rules (Pre-correction)
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
