import os
import time
import asyncio
import numpy as np
from PIL import Image

# Import the compiled Rust extension
# Ensure that the extension is accessible (compiled via `maturin develop`)
try:
    import rust_ocr
except ImportError:
    print("[WARNING] rust_ocr module not found. Did you run `maturin develop --release`?")
    rust_ocr = None

class RustOCREngine:
    def __init__(self):
        self.language = "en"
        self._callback = None
        
        if rust_ocr:
            try:
                # Provide a path for ONNX models (e.g. models directory)
                models_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "models")
                os.makedirs(models_dir, exist_ok=True)
                rust_ocr.initialize_ocr(models_dir)
                print("[INFO] Rust RapidOCR Engine Initialized.")
            except Exception as e:
                print(f"[ERROR] Failed to initialize Rust OCR: {e}")

    def set_callback(self, callback):
        self._callback = callback

    def set_language(self, lang_code: str):
        # RapidOCR can auto-detect or uses a single model for multiple languages (usually en/zh).
        # Depending on the model downloaded, you might need to handle this.
        self.language = lang_code
        print(f"RustOCREngine: Language set to {lang_code}", flush=True)

    def warmup(self):
        print("RustOCREngine: Warmup done.", flush=True)

    async def extract_text(self, image: Image.Image):
        """
        Extracts text using the Rust RapidOCR backend.
        image: PIL Image
        Returns: list of (bbox, text, confidence)
        """
        if not rust_ocr:
            print("[ERROR] rust_ocr module is not available.")
            return []
            
        open_cv_image = np.array(image.convert('RGB'))
        
        start_time = time.time()
        
        try:
            # Call Rust function (sync function, so we run it in a thread if it blocks)
            # numpy array is passed directly to Rust
            results = await asyncio.to_thread(rust_ocr.extract_text, open_cv_image)
        except Exception as e:
            print(f"[ERROR] Rust OCR extraction failed: {e}")
            return []
            
        ocr_time = (time.time() - start_time) * 1000
        print(f"[Rust OCR] Extracted {len(results)} items in {ocr_time:.2f} ms")
        
        # Results from Rust are expected as:
        # [( [[x,y], [x,y], [x,y], [x,y]], "text", conf ), ...]
        
        formatted_results = []
        for bbox, text, conf in results:
            # Ensure bbox format is standard for Overlayr
            # [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
            formatted_results.append((bbox, text, conf))
            
        return formatted_results
