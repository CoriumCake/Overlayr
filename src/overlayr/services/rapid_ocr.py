import numpy as np
from PIL import Image
from typing import List, Tuple, Optional, Callable
import asyncio

class RapidOCREngine:
    """
    RapidOCR engine wrapper. Follows the same interface as WindowsOCREngine.
    """
    def __init__(self):
        print("RapidOCREngine: Init start...", flush=True)
        try:
            from rapidocr_onnxruntime import RapidOCR
            self.engine = RapidOCR()
            self._is_available = True
            print("RapidOCREngine: Successfully initialized.", flush=True)
        except ImportError:
            print("RapidOCREngine: rapidocr_onnxruntime not installed.", flush=True)
            self.engine = None
            self._is_available = False
        
        self._language = "en"
        self._status_callback: Optional[Callable] = None

    def set_callback(self, callback: Callable):
        self._status_callback = callback

    def set_language(self, lang_code: str):
        # RapidOCR generally uses a unified model for multiple languages,
        # but we track language state here as expected by AsyncEngine.
        self._language = lang_code
        print(f"RapidOCREngine: Language set to {lang_code}", flush=True)

    def warmup(self):
        if not self._is_available:
            return
        print("RapidOCREngine: Warming up...", flush=True)
        try:
            # Run a small dummy image
            dummy = np.zeros((100, 100, 3), dtype=np.uint8)
            self.engine(dummy)
            print("RapidOCREngine: Warmup complete.", flush=True)
        except Exception as e:
            print(f"RapidOCREngine Warmup Error: {e}", flush=True)

    async def extract_text(self, image: Image.Image) -> List[Tuple[List[List[int]], str, float]]:
        """
        Executes OCR on the image.
        Returns a list of (bbox, text, confidence) where bbox is [[x,y], [x,y], [x,y], [x,y]].
        """
        if not self._is_available:
            print("RapidOCREngine is not available.", flush=True)
            return []

        # Convert PIL to CV2 BGR
        img_cv = np.array(image.convert('RGB'))
        img_cv = img_cv[:, :, ::-1].copy()

        # Run OCR in a background thread to prevent blocking async loop
        results = await asyncio.to_thread(self._run_ocr_sync, img_cv)
        return results

    def _run_ocr_sync(self, img_cv: np.ndarray) -> List[Tuple[List[List[int]], str, float]]:
        try:
            ocr_result, elapse = self.engine(img_cv)
        except Exception as e:
            print(f"RapidOCREngine extraction error: {e}", flush=True)
            return []

        formatted_results = []
        if ocr_result:
            for item in ocr_result:
                # item format: [box, text, confidence]
                box = item[0]
                text = item[1]
                conf = item[2]
                formatted_results.append((box, text, float(conf)))
                
        return formatted_results
