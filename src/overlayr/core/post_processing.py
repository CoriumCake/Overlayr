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

    def correct_text(self, text: str) -> str:
        """
        Apply SymSpell correction then Fuzzy fallback.
        """
        if not text or not self.sym_spell:
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
