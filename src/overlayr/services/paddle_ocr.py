import numpy as np
from PIL import Image
from typing import List, Tuple, Any

try:
    from paddleocr import PaddleOCR
except ImportError:
    PaddleOCR = None
    print("PaddleOCR not installed.")

class PaddleOCREngine:
    def __init__(self, lan='en'): # 'en', 'ch', 'japan', 'korean'
        if not PaddleOCR:
            raise ImportError("paddleocr module is not installed. Please pip install paddleocr paddlepaddle.")
        
        # Initialize PaddleOCR
        # use_angle_cls=True to handle rotated text (common in manga)
        # enable_mkldnn=False to fix "ConvertPirAttribute2RuntimeAttribute" crash
        print(f"Initializing PaddleOCR with lang={lan}...")
        self.ocr = PaddleOCR(use_angle_cls=True, lang=lan, enable_mkldnn=False)
        print("PaddleOCR initialized.")
        
    def set_language(self, lang_code: str):
        """
        Switch OCR language. This re-initializes the heavy PaddleOCR model.
        lang_code: 'en', 'ch', 'japan', 'korean'
        """
        if not hasattr(self, 'current_lang'):
             self.current_lang = 'en' # Default
             
        if lang_code == self.current_lang:
            return

        print(f"PaddleOCR: Switching language from {self.current_lang} to {lang_code}...")
        try:
            self.ocr = PaddleOCR(use_angle_cls=True, lang=lang_code, enable_mkldnn=False)
            self.current_lang = lang_code
            print(f"PaddleOCR: Switched to {lang_code}.")
        except Exception as e:
            print(f"PaddleOCR: Init failed for {lang_code}: {e}")

    async def extract_text(self, image: Image.Image) -> List[Tuple[List[List[int]], str, float]]:
        """
        Extract text from PIL Image.
        Returns: List of (bbox_points, text, confidence)
        bbox_points: [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
        """
        # Convert PIL to Numpy
        # PaddleOCR expects 3-channel BGR image.
        # If input is Grayscale (L), convert to RGB first.
        if image.mode != 'RGB':
            image = image.convert('RGB')
            
        img_np = np.array(image)
        
        # Convert RGB to BGR
        img_np = img_np[:, :, ::-1].copy()
        
        # Debug check
        # print(f"DEBUG: Paddle Input Shape: {img_np.shape}", flush=True)

        # Run OCR
        # result structure varies by version/detection.
        # Typically: [ [ [ [x1,y1],..], (text, conf) ], ... ] (List of lists)
        try:
            result = self.ocr.ocr(img_np)
        except Exception as e:
            print(f"PaddleOCR internal error: {e}")
            return []
            
        output = []
        if result is None:
            return output
            
        # Debug print to see structure if fails
        # print(f"DEBUG PADDLE RESULT: {result}", flush=True)

        for idx, res in enumerate(result):
            if not res: continue # empty result for this image in batch
            
            for line in res:
                # Expecting line to be a list/tuple of size 2: [bbox, (text, conf)]
                if not line or len(line) < 2:
                    continue
                    
                bbox = line[0] 
                text_info = line[1]
                
                if not text_info or len(text_info) < 2:
                    continue
                    
                text = text_info[0]
                conf = text_info[1]
                
                # Ensure bbox is ints
                try:
                    bbox_int = [[int(pt[0]), int(pt[1])] for pt in bbox]
                    output.append((bbox_int, text, conf))
                except:
                    continue
                
        return output

    def warmup(self):
        # Dummy run?
        pass
