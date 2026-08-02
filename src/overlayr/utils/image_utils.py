from PIL import Image, ImageChops
import math
import operator
from functools import reduce

def is_different(image1: Image.Image, image2: Image.Image, threshold: float = 5.0) -> bool:
    """
    Compare two images and return True if they are different beyond a threshold.
    """
    if image1 is None or image2 is None:
        return True
    
    if image1.size != image2.size:
        return True
        
    # Convert to grayscale for faster comparison
    img1 = image1.convert('L')
    img2 = image2.convert('L')
    
    # Calculate RMS difference
    h = ImageChops.difference(img1, img2).histogram()
    
    # Calculate rms
    sum_of_squares = sum(value * ((idx % 256) ** 2) for idx, value in enumerate(h))
    rms = math.sqrt(sum_of_squares / float(image1.size[0] * image1.size[1]))
    
    return rms > threshold

def calculate_similarity(image1: Image.Image, image2: Image.Image) -> float:
    """
    Returns a similarity score between 0.0 and 100.0.
    100.0 means identical.
    """
    if image1 is None or image2 is None:
        return 0.0
    
    if image1.size != image2.size:
        # For simplicity, if they aren't the same size, we just say 0% similar
        return 0.0
        
    img1 = image1.convert('L')
    img2 = image2.convert('L')
    
    h = ImageChops.difference(img1, img2).histogram()
    sum_of_squares = sum(value * ((idx % 256) ** 2) for idx, value in enumerate(h))
    
    # Avoid division by zero
    if image1.size[0] * image1.size[1] == 0:
        return 0.0
        
    rms = math.sqrt(sum_of_squares / float(image1.size[0] * image1.size[1]))
    
    # Map RMS (0 to 255) to Similarity (100 to 0)
    similarity = max(0.0, 100.0 * (1.0 - (rms / 255.0)))
    return similarity
