import logging
from typing import List, Tuple, Any
import numpy as np
from PIL import Image

class EasyOCREngine:
    """
    OCR engine using EasyOCR.
    Supports many languages, good accuracy, but slower than built-in Windows OCR.
    """
    
    def __init__(self):
        self._language = "en"
        self._reader = None
        self._status_callback = None
        print("EasyOCREngine: Initialized (Lazy loading).")

    def set_callback(self, callback):
        self._status_callback = callback
        
    def _get_reader(self):
        """Lazy load EasyOCR to avoid startup delay."""
        if self._reader is None:
            if self._status_callback: self._status_callback(f"Loading EasyOCR Model ({self._language})...")
            # Import inside method
            import easyocr
            print(f"EasyOCREngine: Loading model for lang='{self._language}'...", flush=True)
            
            # EasyOCR expects list of languages e.g. ['en', 'th']
            langs = ['en']
            if self._language != 'en':
                langs.append(self._language)
                
            # gpu=True/False depending on availability. Let's auto-detect.
            self._reader = easyocr.Reader(langs, gpu=True, verbose=False)
            print("EasyOCREngine: Model loaded.", flush=True)
            if self._status_callback: self._status_callback("EasyOCR Ready")
        return self._reader
    
    def set_language(self, lang_code: str) -> None:
        """Set OCR language."""
        # Map app codes to EasyOCR codes
        # EasyOCR supports: 'en', 'th', 'ch_sim', 'ja', 'ko' etc.
        lang_map = {
            "en": "en",
            "ja": "ja", 
            "ko": "ko",
            "zh": "ch_sim",
            "zh-CN": "ch_sim",
            "th": "th",
        }
        
        target_lang = lang_map.get(lang_code, "en")
        
        if target_lang != self._language:
            self._language = target_lang
            self._reader = None # Force reload
            print(f"EasyOCREngine: Language set to {target_lang} (Reload scheduled)")

    async def extract_text(self, image: Image.Image) -> List[Tuple[List[List[int]], str, float]]:
        """
        Extract text from image using EasyOCR.
        Returns list of (bbox_points, text, confidence).
        """
        import asyncio
        
        loop = asyncio.get_running_loop()
        
        # EasyOCR expects numpy array or bytes
        img_np = np.array(image)
        
        def _run_ocr():
            reader = self._get_reader()
            # detail=1 gives bounding boxes
            return reader.readtext(img_np, detail=1)
            
        try:
            # Run in executor (thread pool)
            result_list = await loop.run_in_executor(None, _run_ocr)
            
            output = []
            for item in result_list:
                # item format: ([[x1, y1], [x2, y2], [x3, y3], [x4, y4]], 'text', confidence)
                box = item[0]
                text = item[1]
                conf = float(item[2])
                
                # Convert points to int lists
                box_int = [[int(p[0]), int(p[1])] for p in box]
                
                output.append((box_int, text, conf))
                
            return output
            
        except Exception as e:
            print(f"EasyOCR Error: {e}")
            import traceback
            traceback.print_exc()
            return []

    def warmup(self):
        """Trigger model load."""
        import threading
        threading.Thread(target=self._get_reader, daemon=True).start()
