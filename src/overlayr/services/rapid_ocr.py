import logging
import time
import os
import multiprocessing
from typing import List, Tuple, Any
import numpy as np
from PIL import Image

class RapidOCREngine:
    """
    OCR engine using RapidOCR (ONNX Runtime).
    Optimized for GPU (DirectML) or Multithreaded CPU.
    """
    
    def __init__(self):
        self._engine = None
        self._status_callback = None
        print("RapidOCREngine: Initialized (Lazy loading).")
        
    def warmup(self):
        """Pre-load models."""
        print("RapidOCREngine: Warming up...", flush=True)
        self._get_engine()

    def set_callback(self, callback):
        self._status_callback = callback
        
    def _get_engine(self):
        """Lazy load RapidOCR with optimal settings."""
        if self._engine is None:
            if self._status_callback: self._status_callback("Loading RapidOCR Model...")
            
            try:
                from rapidocr_onnxruntime import RapidOCR
                import onnxruntime as ort
                
                providers = ort.get_available_providers()
                print(f"RapidOCREngine: Available Providers: {providers}", flush=True)
                
                # Check for DML
                if 'DmlExecutionProvider' in providers:
                    print("RapidOCREngine: DirectML detected! Using GPU.", flush=True)
                else:
                    print("RapidOCREngine: DirectML NOT found. Using CPU.", flush=True)

                # Initialize with default config. 
                # ONNX Runtime automatically uses the first available provider (DML if installed).
                self._engine = RapidOCR() 
                
                print("RapidOCREngine: Models loaded.", flush=True)
                if self._status_callback: self._status_callback("RapidOCR Ready")
                
            except Exception as e:
                print(f"RapidOCREngine: Initialization failed: {e}", flush=True)
                raise e
                
        return self._engine
    
    def set_language(self, lang_code: str) -> None:
        pass

    async def extract_text(self, image: Image.Image) -> List[Tuple[List[List[int]], str, float]]:
        import asyncio
        loop = asyncio.get_running_loop()
        
        img_np = np.array(image)
        
        def _run_ocr():
            try:
                engine = self._get_engine()
                # RapidOCR returns: results, elapse_time
                result, elapse = engine(img_np)
                return result
            except Exception as e:
                print(f"RapidOCREngine Extraction Error: {e}")
                import traceback
                traceback.print_exc()
                return None
            
        try:
            result_list = await loop.run_in_executor(None, _run_ocr)
            
            if not result_list:
                return []
                
            output = []
            for item in result_list:
                box = item[0]
                text = item[1]
                conf = float(item[2])
                box_int = [[int(p[0]), int(p[1])] for p in box]
                output.append((box_int, text, conf))
                
            return output
            
        except Exception as e:
            print(f"RapidOCR Async Error: {e}")
            return []
