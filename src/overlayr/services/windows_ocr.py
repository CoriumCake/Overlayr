import asyncio
from typing import List, Tuple, Any
from PIL import Image
import io

class WindowsOCREngine:
    """
    Fast OCR using Windows 10/11 built-in OCR via winocr package.
    Much faster than EasyOCR (~0.1s vs 2s).
    """
    
    def __init__(self):
        self._language = "en"
        print("WindowsOCREngine initialized.")
    
    def set_language(self, lang_code: str) -> None:
        """Set OCR language. Common codes: en, ja, ko, zh, th"""
        lang_map = {
            "en": "en",
            "ja": "ja", 
            "ko": "ko",
            "zh": "zh-Hans",
            "zh-CN": "zh-Hans",
            "th": "th",
            "vi": "vi",
        }
        self._language = lang_map.get(lang_code, lang_code)
        print(f"OCR language set to: {self._language}")
    
    async def extract_text(self, image: Image.Image) -> List[Tuple[List[List[int]], str, float]]:
        """
        Extract text from image using Windows OCR.
        Returns list of (bbox_points, text, confidence).
        """
        try:
            import winocr
            
            # Run OCR - winocr.recognize_pil returns WinRT OcrResult
            result = await winocr.recognize_pil(image, lang=self._language)
            
            # Parse results - result.lines is a WinRT collection
            results = []
            for line in result.lines:
                text = line.text
                if not text or not text.strip():
                    continue
                
                # Get bounding box from words
                words = list(line.words)
                if words:
                    # Calculate line bounding box from words
                    all_rects = []
                    for word in words:
                        rect = word.bounding_rect
                        all_rects.append((rect.x, rect.y, rect.width, rect.height))
                    
                    if all_rects:
                        left = min(r[0] for r in all_rects)
                        top = min(r[1] for r in all_rects)
                        right = max(r[0] + r[2] for r in all_rects)
                        bottom = max(r[1] + r[3] for r in all_rects)
                        
                        # Format as corner points (like EasyOCR)
                        bbox = [
                            [left, top],
                            [right, top],
                            [right, bottom],
                            [left, bottom]
                        ]
                        
                        results.append((bbox, text, 0.95))
            
            return results
            
        except Exception as e:
            print(f"Windows OCR error: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def warmup(self):
        """Pre-initialize is not needed for winocr."""
        pass
