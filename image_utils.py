import cv2
import numpy as np
from PIL import Image

def get_image_difference(image1: Image.Image, image2: Image.Image) -> float:
    """
    Calculates the structural similarity or simple difference between two images.
    Returns a score indicating how different they are.
    """
    if image1 is None or image2 is None:
        return 1.0

    # Convert to grayscale numpy arrays
    img1 = cv2.cvtColor(np.array(image1), cv2.COLOR_RGB2GRAY)
    img2 = cv2.cvtColor(np.array(image2), cv2.COLOR_RGB2GRAY)

    if img1.shape != img2.shape:
        # If sizes differ, they are definitely different
        # Resize img2 to match img1 for comparison to avoid crash, or just return max diff
        img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))

    # Compute absolute difference
    diff = cv2.absdiff(img1, img2)
    
    # Calculate mean difference
    score = np.mean(diff)
    
    return score

def is_different(image1: Image.Image, image2: Image.Image, threshold: float = 2.0) -> bool:
    """
    Returns True if the images are significantly different.
    Threshold is the mean pixel difference (0-255).
    """
    diff = get_image_difference(image1, image2)
    # print(f"Diff score: {diff}") # Debug
    return diff > threshold
