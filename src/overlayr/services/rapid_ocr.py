import logging
import time
import threading
from typing import List, Tuple, Any
import numpy as np
from PIL import Image

class RapidOCREngine:
    """
    OCR engine using RapidOCR (ONNX Runtime).
    Fast and lightweight, supports multiple languages.
    """
    
    def __init__(self):
        self._engine = None
        self._status_callback = None
        print("RapidOCREngine: Initialized (Lazy loading).")

    def set_callback(self, callback):
        self._status_callback = callback
        
    def _get_engine(self):
        """Lazy load RapidOCR to avoid startup delay."""
        if self._engine is None:
            if self._status_callback: self._status_callback("Loading RapidOCR Model...")
            
            try:
                from rapidocr_onnxruntime import RapidOCR
                # Initialize with default English/General model
                # RapidOCR automatically downloads models if not present
                print("RapidOCREngine: Loading ONNX models...", flush=True)
                self._engine = RapidOCR() 
                print("RapidOCREngine: Models loaded.", flush=True)
                if self._status_callback: self._status_callback("RapidOCR Ready")
            except Exception as e:
                print(f"RapidOCREngine: Initialization failed: {e}", flush=True)
                raise e
                
        return self._engine
    
    def warmup(self):
        """Triggers model loading."""
        threading.Thread(target=self._get_engine, daemon=True).start()

    def set_language(self, lang_code: str) -> None:
        """
        RapidOCR usually handles general models well. 
        Specific language switching might require reloading with different det/rec models 
        if using custom models, but the default often covers English/Numbers well.
        For now, we rely on the default model.
        """
        pass

    async def extract_text(self, image: Image.Image) -> List[Tuple[List[List[int]], str, float]]:
        """
        Extract text from image using RapidOCR.
        Returns list of (bbox_points, text, confidence).
        """
        import asyncio
        loop = asyncio.get_running_loop()
        
        # RapidOCR accepts numpy array (BGR or RGB)
        # It handles conversion internally, but robust way is passing numpy
        img_np = np.array(image)
        
        def _run_ocr():
            engine = self._get_engine()
            # engine(img) returns result, elapse
            # result is a list of [box, text, score]
            result, elapse = engine(img_np)
            return result
            
        try:
            # Run in executor
            result_list = await loop.run_in_executor(None, _run_ocr)
            
            if not result_list:
                return []
                
            output = []
            # result_list format: [ [[x1,y1],[x2,y2],[x3,y3],[x4,y4]], "text", 0.95 ]
            for item in result_list:
                box = item[0]
                text = item[1]
                conf = float(item[2])
                
                # Ensure box points are int
                box_int = [[int(p[0]), int(p[1])] for p in box]
                
                output.append((box_int, text, conf))
                
            return output
            
        except Exception as e:
            print(f"RapidOCR Error: {e}")
            import traceback
            traceback.print_exc()
            return []
