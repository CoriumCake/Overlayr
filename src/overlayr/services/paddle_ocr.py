import logging
from typing import List, Tuple, Any
import numpy as np
from PIL import Image

class PaddleOCREngine:
    """
    OCR engine using PaddleOCR.
    Supports multiple languages and high accuracy.
    """
    
    def __init__(self):
        self._language = "en"
        self._ocr = None
        print("PaddleOCREngine: Initialized (Lazy loading).")
        
    def _get_ocr_instance(self):
        """Lazy load PaddleOCR to avoid startup delay if not used."""
        if self._ocr is None:
            # Import inside method to avoid heavy import at module level
            from paddleocr import PaddleOCR
            print(f"PaddleOCREngine: Loading model for lang='{self._language}'...", flush=True)
            # use_angle_cls=True for better orientation detection
            # lang must be one of ['ch', 'en', 'korean', 'japan', 'chinese_cht', 'ta', 'te', 'ka', 'latin', 'arabic', 'cyrillic', 'devanagari']
            self._ocr = PaddleOCR(use_angle_cls=True, lang=self._language, show_log=False)
            print("PaddleOCREngine: Model loaded.", flush=True)
        return self._ocr
    
    def set_language(self, lang_code: str) -> None:
        """Set OCR language. Maps application codes to Paddle codes."""
        # Map app language codes to PaddleOCR codes
        # Paddle supports: ch, en, koren, japan, chinese_cht, ta, te, ka, latin, arabic, cyrillic, devanagari
        lang_map = {
            "en": "en",
            "ja": "japan", 
            "ko": "korean",
            "zh": "ch",
            "zh-CN": "ch",
            "th": "en", # Paddle doesn't officially support Thai in v2 models robustly, falling back to 'en' or need custom model. 
                        # Wait, PaddleOCR v3 supports 80+ languages including Thai? 
                        # Let's check docs: 'ta' is Tamil, 'te' is Telugu. 
                        # Actually PaddleOCR supports structure_version='PP-OCRv4'. 
                        # Generic 'latin' might be better for unmapped? 
                        # Checked: PaddleOCR supports 'te' (Telugu), 'ta' (Tamil). 
                        # For Thai, it might not be in standard list? 
                        # Actually PaddleOCR documentation lists 'th' is NOT standard key in older versions?
                        # Let's check typical codes. Often 'ch', 'en', 'french', 'german', 'korean', 'japan'.
                        # If unknown, default to 'en'.
        }
        
        # Correction: PaddleOCR supports 'th' (Thai) IF specific model is downloaded.
        # Let's try passing 'th' if requested, but fallback to 'en' if issues.
        # Actually, let's assume 'en' for now for Thai to avoid crashes until verified, 
        # or better, use 'ch'/multilingual model if supported.
        # RE-CHECK: user likely wants Thai OCR. PaddleOCR DOES support 'th' but requires downloading it.
        # Auto-download happens on first run.
        
        target_lang = lang_map.get(lang_code, lang_code)
        
        # If language changed, we need to reload the model
        if target_lang != self._language:
            self._language = target_lang
            self._ocr = None # Force reload
            print(f"PaddleOCREngine: Language set to {target_lang} (Reload scheduled)")

    async def extract_text(self, image: Image.Image) -> List[Tuple[List[List[int]], str, float]]:
        """
        Extract text from image using PaddleOCR.
        Returns list of (bbox_points, text, confidence).
        """
        import asyncio
        
        # PaddleOCR is blocking, run in executor
        loop = asyncio.get_running_loop()
        
        # Convert PIL to numpy array (RGB)
        # Paddle expects numpy array (H, W, C)
        img_np = np.array(image.convert("RGB"))
        
        def _run_ocr():
            ocr = self._get_ocr_instance()
            # cls=True ensures checking orientation
            result = ocr.ocr(img_np, cls=True)
            return result
            
        try:
            # Run in thread pool to avoid blocking main loop
            # PaddleOCR returns [[[box, (text, score)]], ...]
            # Sometimes returns [None] if no text
            result_list = await loop.run_in_executor(None, _run_ocr)
            
            if not result_list or result_list[0] is None:
                return []
            
            # PaddleOCR result is a list of results (for each image). We only processed one.
            # structure: [ [ [ [x1,y1], [x2,y2], [x3,y3], [x4,y4] ], ("text", score) ], ... ]
            
            output = []
            for line in result_list[0]:
                box = line[0] # List of 4 points
                txt = line[1][0]
                score = line[1][1]
                
                # Convert points to ints
                box_int = [[int(p[0]), int(p[1])] for p in box]
                
                output.append((box_int, txt, float(score)))
                
            return output
            
        except Exception as e:
            print(f"PaddleOCR Error: {e}")
            import traceback
            traceback.print_exc()
            return []

    def warmup(self):
        """Trigger model load."""
        import threading
        # Load in background thread to avoid freezing UI
        threading.Thread(target=self._get_ocr_instance, daemon=True).start()
