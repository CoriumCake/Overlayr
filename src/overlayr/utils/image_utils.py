from PIL import Image, ImageChops
import math
import operator
import numpy as np
from functools import reduce

# Width and height a frame is reduced to before frames are compared. The gate
# only has to answer "did this change", and a thumbnail answers it: at 1920x1080
# `calculate_similarity` costs about 12ms against 3ms here, which matters
# because this now runs while the pipeline is busy rather than between frames.
SIGNATURE_SIZE = (160, 90)

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


def frame_signature(image: Image.Image):
    """A small grayscale array standing in for a frame when comparing frames.

    Kept apart from `calculate_similarity` rather than replacing it: the 85%
    threshold that decides whether a frame is worth reading was tuned against
    that function at full resolution, and quietly changing what it measures
    would move the gate without anyone choosing to.
    """
    if image is None:
        return None
    return np.asarray(image.convert("L").resize(SIGNATURE_SIZE, Image.BILINEAR),
                      dtype=np.int16)


def signature_similarity(first, second) -> float:
    """Similarity of two signatures on the same 0-100 scale as full frames."""
    if first is None or second is None:
        return 0.0
    if first.shape != second.shape:
        return 0.0

    # Squared in float, not in the signature's own integer type: a full
    # black-to-white difference squares to 65025, which wraps negative in
    # int16 and turns the square root below into a domain error.
    difference = first.astype(np.float32) - second.astype(np.float32)
    rms = math.sqrt(float(np.mean(difference ** 2)))
    return max(0.0, 100.0 * (1.0 - (rms / 255.0)))
