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
