import easyocr
import numpy as np
from PIL import Image

class OCREngine:
    def __init__(self, languages=['ja', 'en'], gpu=True):
        print("Initializing EasyOCR...")
        # Initialize the reader. This might take a while on first run as it downloads models.
        self.reader = easyocr.Reader(languages, gpu=gpu)
        print("EasyOCR initialized.")

    def extract_text(self, image: Image.Image) -> str:
        """
        Extracts text from a PIL Image.
        """
        try:
            # Convert PIL Image to numpy array (OpenCV format) which EasyOCR expects
            img_np = np.array(image)
            
            # detail=0 returns just the list of text strings found
            result = self.reader.readtext(img_np, detail=0, paragraph=True)
            
            return "\n".join(result)
        except Exception as e:
            print(f"Error during OCR: {e}")
            return ""

if __name__ == "__main__":
    # Test stub
    engine = OCREngine()
    print("OCR Engine ready.")
