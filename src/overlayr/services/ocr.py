import easyocr
import numpy as np
from PIL import Image
import threading
from typing import List, Optional
from ..utils.singleton import SingletonMeta

class OCREngine(metaclass=SingletonMeta):
    _reader: Optional[easyocr.Reader] = None
    _lock: threading.Lock = threading.Lock()

    def __init__(self, languages: List[str] = ['ja', 'en'], gpu: bool = True) -> None:
        pass # Lazy init checks _reader in extract_text

    def set_languages(self, languages: List[str]):
        """Re-initialize reader with new languages."""
        print(f"Switching OCR languages to: {languages}...", flush=True)
        with self._lock:
            try:
                self._reader = easyocr.Reader(languages, gpu=True)
                print("OCR languages updated.", flush=True)
            except Exception as e:
                print(f"Error switching languages: {e}")
                # Fallback to default if fails?
                self._reader = easyocr.Reader(['ja', 'en'], gpu=True)

    def warmup(self):
        """Pre-initialize the reader to avoid delay on first use."""
        if self._reader is not None: return
        print("Warming up OCR Engine...", flush=True)
        with self._lock:
            if self._reader is None:
                 try:
                    self._reader = easyocr.Reader(['ja', 'en'], gpu=True)
                    print("OCR Engine warmed up.", flush=True)
                 except Exception as e:
                    print(f"OCR Warmup Failed: {e}")

    def extract_text(self, image: Image.Image) -> List[tuple]:
        """
        Extracts text from a PIL Image ensuring thread safety for the reader.
        Returns list of (bbox, text, prob).
        """
        if self._reader is None:
            # Fallback re-init if something went wrong, though singleton should prevent this
            with self._lock:
                 if self._reader is None:
                    self._reader = easyocr.Reader(['ja', 'en'], gpu=True)

        try:
            # Convert PIL Image to numpy array (OpenCV format)
            img_np = np.array(image)
            
            with self._lock:
                # detail=1 returns (bbox, text, conf)
                # paragraph=False gives us individual lines/blocks which is better for overlay
                result = self._reader.readtext(img_np, detail=1, paragraph=False)
            
            return result
        except Exception as e:
            print(f"Error during OCR: {e}")
            return []
