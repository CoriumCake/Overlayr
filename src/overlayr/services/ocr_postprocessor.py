import re

def correct_misreadings(text):
    """
    Correct common OCR misreadings based on specific rules.
    """
    if not text:
        return text

    original_text = text
    
    # Rule 1: PONT -> DONT (P before O)
    # Context: "PONT" -> "DONT"
    # Also "1 PONT" -> "I DON'T" (handled by 1->I rule combined)
    # Regex: replace PONT with DONT
    text = re.sub(r'\bPONT\b', 'DONT', text)
    
    # Rule 2: PO -> DO
    # Context: "TO PO" -> "TO DO"
    text = re.sub(r'\bPO\b', 'DO', text)
    
    # Rule 3: 1 -> I (Start of sentence/before verb)
    # Context: "1 CAN'T" -> "I CAN'T", "1 AM" -> "I AM", "1 DON'T" -> "I DON'T"
    # Matches "1" followed by specific verbs/contractions
    text = re.sub(r'(^|\s)1(?=\s+(?:AM|CAN\'?T|DON\'?T|WAS|WILL|HAVE|HAD|SHOULD|COULD|MUST)\b)', r'\1I', text, flags=re.IGNORECASE)

    # Rule 4: 15 -> IS
    # Context: "NAME 15" -> "NAME IS", "15 THAT" -> "IS THAT"
    text = re.sub(r'\bNAME\s+15\b', 'NAME IS', text)
    text = re.sub(r'\b15(?=\s+(?:THAT|THIS|THE|IT|WHAT|THERE)\b)', 'IS', text)
    
    # Generalized Rule 4.1: Single "15" -> "IS" if no other digits are nearby in the text block
    # Check if text contains other digits (excluding the "15" instance itself)
    # Actually, we process line by line. If a line is "15", we check if there are other digits.
    # But wait, "15" could be "Chapter 15".
    # User Request: "if number 15 is alone and no other numbers nearby"
    # Let's count digits.
    digit_count = sum(c.isdigit() for c in text)
    if digit_count == 2 and "15" in text.split(): 
        # Only digits are '1' and '5' from '15', and '15' is a standalone word.
        # Replace strictly whole word '15' with 'IS'
        text = re.sub(r'\b15\b', 'IS', text)
    
    # Rule 5: € -> G
    # Context: "€OBLINS" -> "GOBLINS"
    text = text.replace('€', 'G')
    
    # Rule 6: Specific User Corrections (2026-02-18)
    # BUT 1 -> BUT I
    text = re.sub(r'\bBUT 1\b', 'BUT I', text, flags=re.IGNORECASE)
    # THAN 1 -> THAN I
    text = re.sub(r'\bTHAN 1\b', 'THAN I', text, flags=re.IGNORECASE)
    # 1 G0T -> I GOT (also 0->O in G0T)
    text = re.sub(r'\b1 G0T\b', 'I GOT', text, flags=re.IGNORECASE)
    # 0Nce -> ONce (0 at start of word)
    text = re.sub(r'\b0Nce\b', 'ONce', text) 
    # eo -> Go (Assuming 'eo' is a misread for 'Go' in specific contexts like 'Go Ahead', or generally if standalone)
    text = re.sub(r'\beo\b', 'Go', text)
    # ••• -> . . .
    text = text.replace('•••', '. . .')
    # $ELF -> SELF
    text = re.sub(r'\$ELF\b', 'SELF', text, flags=re.IGNORECASE)
    
    # Generic 0 -> O in words? (Optional, if 0Nce is pattern)
    # Maybe 0 at start: \b0(?=[A-Za-z])
    text = re.sub(r'\b0(?=[A-Za-z]{2,})', 'O', text) # e.g. 0Nce -> ONce, 0THERS -> OTHERS

    # --- Rules for EN-4 (2026-02-20) ---
    
    # 7. Fix 'OJT' -> 'OUT'
    text = re.sub(r'\bOJT\b', 'OUT', text)
    
    # 8. Fix '60' -> 'GO' at start of line
    if text.startswith("60 "):
        text = "GO " + text[3:]
    elif text == "60":
        text = "GO"
        
    # 9. Fix '&' -> 'G' at end of word (e.g. SOMETHIN& -> SOMETHING)
    # Only if preceded by N? "SOMETHIN&", "CLOSIN&"
    text = re.sub(r'N&', 'NG', text)
    
    # 10. Strip leading/trailing non-alphanumeric artifacts
    # e.g. "/ BUT J )" -> "BUT J" (Wait, J should be I)
    # Be careful not to strip "I..." or "...YOU"
    # Allow dots, but strip slashes, parenthesis, bars
    
    # Strip leading artifacts
    text = re.sub(r'^[\/\|\)\(\\]+\s*', '', text)
    # Strip trailing artifacts
    text = re.sub(r'\s*[\/\|\)\(\\]+$', '', text)
    
    # 11. Fix 'SORTev' -> 'SORTED' (EN-4 specific due to masking)
    text = re.sub(r'\b4?\s*SORTev\b', 'SORTED', text, flags=re.IGNORECASE)
    
    # 12. Fix "YOU'RE NOT" artifacts (EN-4)
    if "YOU'RE NOT" in text:
        # Strip garbage before it (e.g. "LZ--kh SORT" I YOU'RE NOT")
        text = re.sub(r'^.*(?=YOU\'RE NOT)', '', text)

    return text
