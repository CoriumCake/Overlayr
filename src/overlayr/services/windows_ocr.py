import asyncio
import os
import cv2
import numpy as np
import math
from collections import Counter
from shapely.geometry import Polygon
from difflib import SequenceMatcher
import sys
import tempfile
import time
from winsdk.windows.media.ocr import OcrEngine
from winsdk.windows.graphics.imaging import BitmapDecoder, SoftwareBitmap
from winsdk.windows.storage import StorageFile, FileAccessMode
from winsdk.windows.storage.streams import IRandomAccessStream
import winsdk.windows.globalization
try:
    from .ocr_preprocessor import preprocess_image_for_ocr, detect_text_regions, detect_speech_bubbles, apply_cuts_to_original, detect_text_regions_dbnet
    import cv2
    import numpy as np
    HAS_OPENCV = True
except ImportError:
    HAS_OPENCV = False

ROTATION_PADDING = 150 # Padding to prevent text clipping during rotation
RAW_MODE = False # Default: False. Use --raw to enable.


async def get_software_bitmap_from_file(file_path):
    """
    Load an image file and convert it to a SoftwareBitmap.
    """
    try:
        # Get the file using Windows Storage API
        # Note: winsdk.windows.storage.StorageFile.get_file_from_path_async requires absolute path
        abs_path = os.path.abspath(file_path)
        if not os.path.exists(abs_path):
            print(f"File not found: {abs_path}")
            return None
            
        file = await StorageFile.get_file_from_path_async(abs_path)
        
        # Open the file
        stream = await file.open_async(FileAccessMode.READ)
        
        # Create a decoder
        decoder = await BitmapDecoder.create_async(stream)
        
        # Get the software bitmap
        software_bitmap = await decoder.get_software_bitmap_async()
        
        return software_bitmap
    except Exception as e:
        print(f"Error loading image: {e}")
        return None

async def run_ocr(image_source, language=None, technique='baseline'):
    """
    Run OCR on the specified image source (path or SoftwareBitmap/Numpy Array).
    technique: 'baseline' (Scale+Sharpen), 'threshold' (Adaptive), 'inverted' (Invert)
    """
    # Initialize OCR Engine
    if language:
        try:
            lang = winsdk.windows.globalization.Language(language)
            ocr_engine = OcrEngine.try_create_from_language(lang)
        except Exception as e:
            print(f"Error creating OCR engine for language {language}: {e}")
            return None
    else:
        ocr_engine = OcrEngine.try_create_from_user_profile_languages()

    if not ocr_engine:
        print("Error: Could not create OCR Engine.")
        return None, 1.0, None, 0.0

    start_time = time.perf_counter()
    scale_factor = 1.0
    processed_img = None
    bitmap = None
    pre_result = None

    # Processing Logic using OpenCV if available
    if HAS_OPENCV:
        # Load Image into standard Numpy Format
        img = None
        if isinstance(image_source, str):
            img = cv2.imread(image_source)
        elif isinstance(image_source, np.ndarray):
            img = image_source
        
        if img is not None:
             # Ensure Grayscale for processing checks involves Gray logic usually
             if len(img.shape) == 3:
                 gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
             else:
                 gray = img
                 
             # Apply Preprocessing Technique
             if technique == 'baseline':
                 pre_result = preprocess_image_for_ocr(img)
                 
             elif technique == 'threshold':
                 # Adaptive Threshold
                 img_scaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                 binary = cv2.adaptiveThreshold(img_scaled, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
                                                cv2.THRESH_BINARY, 21, 10)
                 processed_img = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
                 scale_factor = 2.0
                 pre_result = (processed_img, scale_factor)

             elif technique == 'simple_threshold':
                 # Otsu
                 img_scaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                 _, binary = cv2.threshold(img_scaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                 processed_img = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
                 scale_factor = 2.0
                 pre_result = (processed_img, scale_factor)

             elif technique == 'clahe':
                 clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
                 enhanced = clahe.apply(gray)
                 img_scaled = cv2.resize(enhanced, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                 processed_img = cv2.cvtColor(img_scaled, cv2.COLOR_GRAY2BGR)
                 scale_factor = 2.0
                 pre_result = (processed_img, scale_factor)

             elif technique == 'inverted':
                 img_scaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                 inverted = cv2.bitwise_not(img_scaled)
                 processed_img = cv2.cvtColor(inverted, cv2.COLOR_GRAY2BGR)
                 scale_factor = 2.0
                 pre_result = (processed_img, scale_factor)

             elif technique == 'thicken':
                 img_scaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                 kernel = np.ones((2,2), np.uint8)
                 eroded = cv2.erode(img_scaled, kernel, iterations=1)
                 processed_img = cv2.cvtColor(eroded, cv2.COLOR_GRAY2BGR)
                 scale_factor = 2.0
                 pre_result = (processed_img, scale_factor)

             elif technique == 'shear':
                 img_scaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                 h, w = img_scaled.shape
                 shear_offset = int(0.2 * h)
                 new_w = w + shear_offset
                 M = np.float32([[1, -0.2, shear_offset], [0, 1, 0]])
                 sheared = cv2.warpAffine(img_scaled, M, (new_w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
                 processed_img = cv2.cvtColor(sheared, cv2.COLOR_GRAY2BGR)
                 scale_factor = 2.0 
                 pre_result = (processed_img, scale_factor)

             elif technique == 'shear_right':
                 img_scaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                 h, w = img_scaled.shape
                 shear_expand = int(0.2 * h)
                 new_w = w + shear_expand
                 M = np.float32([[1, 0.2, 0], [0, 1, 0]])
                 sheared = cv2.warpAffine(img_scaled, M, (new_w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
                 processed_img = cv2.cvtColor(sheared, cv2.COLOR_GRAY2BGR)
                 scale_factor = 2.0
                 pre_result = (processed_img, scale_factor)

             elif technique == 'rotate':
                 img_scaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                 pad = ROTATION_PADDING
                 img_padded = cv2.copyMakeBorder(img_scaled, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
                 h, w = img_padded.shape
                 center = (w // 2, h // 2)
                 M = cv2.getRotationMatrix2D(center, 5, 1.0) 
                 rotated = cv2.warpAffine(img_padded, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
                 processed_img = cv2.cvtColor(rotated, cv2.COLOR_GRAY2BGR)
                 scale_factor = 2.0
                 pre_result = (processed_img, scale_factor)

             elif technique == 'rotate_cw':
                 img_scaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                 pad = ROTATION_PADDING
                 img_padded = cv2.copyMakeBorder(img_scaled, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
                 h, w = img_padded.shape
                 center = (w // 2, h // 2)
                 M = cv2.getRotationMatrix2D(center, -5, 1.0) # CW
                 rotated = cv2.warpAffine(img_padded, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
                 processed_img = cv2.cvtColor(rotated, cv2.COLOR_GRAY2BGR)
                 scale_factor = 2.0
                 pre_result = (processed_img, scale_factor)

    # Convert Pre-processing Result to SoftwareBitmap
    if pre_result and pre_result[0] is not None:
        processed_img, scale_factor = pre_result
        try:
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                tmp_path = tmp.name
            
            cv2.imwrite(tmp_path, processed_img)
            bitmap = await get_software_bitmap_from_file(tmp_path)
            
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception as e:
            print(f"Error handling temp file for OCR: {e}")
            bitmap = None
            
    if bitmap is None:
        # Fallback to original source if no processing or failure
        if isinstance(image_source, str):
            bitmap = await get_software_bitmap_from_file(image_source)
        elif not HAS_OPENCV:
             # If we have an object but no OpenCV, we can't process it.
             # Assume it's a SoftwareBitmap if passed directly?
             # The signature says image_source can be SoftwareBitmap...
             # But we assumed it was numpy array above.
             bitmap = image_source # Try using as is (SoftwareBitmap)

    if bitmap is None:
         return None, 1.0, None, 0.0

    try:
        result = await ocr_engine.recognize_async(bitmap)
        end_time = time.perf_counter()
        duration = end_time - start_time
        return result, scale_factor, processed_img, duration
    except Exception as e:
        print(f"OCR Execution Error: {e}")
        return None, 1.0, None, 0.0

class TextLine:
    def __init__(self, text, words, source=None):
        self.text = text
        self.words = words # List of Rect objects
        self.sources = {source} if source else set()

class Rect:
    def __init__(self, text, x, y, w, h):
        self.text = text
        self.x, self.y, self.width, self.height = x, y, w, h


def correct_geometry(ocr_result, technique, scale, img_shape, matrix_mode='auto'):
    """
    Converts OCR result to a list of TextLine objects with coordinates 
    mapped back to the ORIGINAL image space.
    """
    lines = []
    h, w = img_shape[:2] # Shape of processed image
    
    # Define Matrices based on technique
    M_transform = None
    
    if technique == 'shear':
         # Shear is consistent, no adaptive needed usually
         pass
    elif technique == 'rotate':
        # Tech 6: CCW 5 deg.
        center = (w // 2, h // 2)
        M = cv2.getRotationMatrix2D(center, 5, 1.0)
        M_inv = cv2.invertAffineTransform(M)
        
        if matrix_mode == 'forward': M_transform = M
        elif matrix_mode == 'inverse': M_transform = M_inv
        else: M_transform = M_inv # Default (Inverse)

    elif technique == 'rotate_cw':
        # Tech 7: CW 5 deg (angle = -5).
        center = (w // 2, h // 2)
        Mcw = cv2.getRotationMatrix2D(center, -5, 1.0)
        Mcw_inv = cv2.invertAffineTransform(Mcw)

        if matrix_mode == 'forward': M_transform = Mcw
        elif matrix_mode == 'inverse': M_transform = Mcw_inv
        else: M_transform = Mcw_inv # Default (Inverse)

    for line in ocr_result.lines:
        new_words = []
        for word in line.words:
            rect = word.bounding_rect
            
            # Get corners of the rect in processed image space
            corners = [
                (rect.x, rect.y),
                (rect.x + rect.width, rect.y),
                (rect.x + rect.width, rect.y + rect.height),
                (rect.x, rect.y + rect.height)
            ]
            
            corrected_corners = []
            for (x, y) in corners:
                if technique == 'shear':
                    # Shear with Translation: x' = x - 0.2*y + offset
                    # Inverse: x = x' - offset + 0.2*y
                    # Note: y is same in both spaces (ny = y)
                    # We need 'h' to calc offset. h is from processed image.
                    # img_shape is (h, w) of processed image.
                    h_proc = img_shape[0] 
                    shear_offset = int(0.2 * h_proc)
                    
                    nx = (x - shear_offset) + 0.2 * y
                    ny = y
                    corrected_corners.append((nx, ny))
                elif technique == 'shear_right':
                    # Shear Right: x' = x + 0.2*y
                    # Inverse: x = x' - 0.2*y
                    # No translation offset in forward, BUT max width expanded.
                    # x' is in expanded space.
                    # x = x' - 0.2*y
                    # Do we need to subtract an offset? No, we started at x=0.
                    nx = x - 0.2 * y
                    ny = y
                    corrected_corners.append((nx, ny))
                elif technique in ['rotate', 'rotate_cw'] and M_transform is not None:
                    # Apply Affine Transform to point
                    nx = M_transform[0, 0] * x + M_transform[0, 1] * y + M_transform[0, 2]
                    ny = M_transform[1, 0] * x + M_transform[1, 1] * y + M_transform[1, 2]
                    
                    # Subtract Padding
                    nx -= ROTATION_PADDING
                    ny -= ROTATION_PADDING
                    
                    corrected_corners.append((nx, ny))
                else:
                    corrected_corners.append((x, y))
            
            # Reconstruct Rect from transformed corners
            xs = [p[0] for p in corrected_corners]
            ys = [p[1] for p in corrected_corners]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            
            # Normalize to original scale immediately
            new_rect = Rect(word.text, min_x / scale, min_y / scale, (max_x - min_x) / scale, (max_y - min_y) / scale)
            new_words.append(new_rect)
            
        lines.append(TextLine(line.text, new_words, source=technique))
        
    return lines


def get_line_color(sources):
    """
    Determines the color of the bounding box based on the highest priority technique.
    Priority: Rotate/Rotate_CW > Shear > Inverted > Thicken > Threshold > Baseline
    Colors (BGR):
    - Rotate (+5 deg): Red (0, 0, 255)
    - Rotate CW (-5 deg): Light Red (128, 128, 255)
    - Shear: Purple (255, 0, 255)
    - Inverted: Yellow (0, 255, 255)
    - Thicken: Orange (0, 165, 255)
    - Threshold: Green (0, 255, 0)
    - Baseline: Blue (255, 0, 0)
    """
    if 'rotate' in sources:
        return (0, 0, 255) # Red
    if 'rotate_cw' in sources:
        return (128, 128, 255) # Light Red
    if 'shear_right' in sources:
        return (147, 20, 255) # Pink (Deep Pink)
    if 'shear' in sources:
        return (255, 0, 255) # Purple
    if 'inverted' in sources:
        return (0, 255, 255) # Yellow
    if 'thicken' in sources:
        return (0, 165, 255) # Orange
    if 'threshold' in sources:
        return (0, 255, 0) # Green
    if 'simple_threshold' in sources:
        return (255, 255, 0) # Cyan
    if 'clahe' in sources:
        return (128, 128, 128) # Gray
    return (255, 0, 0) # Blue (Baseline)

def draw_bounding_boxes(image_path, final_lines, processed_img=None, scale_factor=1.0):
    """
    Draws bounding boxes around detected text on the original image.
    Colors determined by detection technique.
    """
    if not HAS_OPENCV:
        return

    try:
        img = cv2.imread(image_path)
        if img is None:
            print(f"Could not load image for visualization: {image_path}")
            return

        # Draw OCR Results with Color Coding
        for line in final_lines:
            color = get_line_color(line.sources)
            for rect in line.words:
                x, y, w, h = int(rect.x), int(rect.y), int(rect.width), int(rect.height)
                cv2.rectangle(img, (x, y), (x + w, y + h), color, 2)

        # Detect and Draw Speech Bubbles (Red)
        bubbles = detect_speech_bubbles(image_path)
        # Filter bubbles that contain text
        valid_bubbles = []
        for cnt in bubbles:
             is_valid = False
             for line in final_lines:
                 for word in line.words:
                     cx = word.x + word.width // 2
                     cy = word.y + word.height // 2
                     if cv2.pointPolygonTest(cnt, (cx, cy), False) >= 0:
                         is_valid = True
                         break
                 if is_valid: break
             if is_valid:
                 valid_bubbles.append(cnt)
                 
        if valid_bubbles:
            cv2.drawContours(img, valid_bubbles, -1, (0, 100, 0), 2)
            
        # Create a new image with sidebar
        h, w = img.shape[:2]
        sidebar_width = 300
        new_w = w + sidebar_width
        
        # Create white canvas
        canvas = np.ones((h, new_w, 3), dtype=np.uint8) * 255
        
        # Place original image on the left
        canvas[0:h, 0:w] = img
        
        # Draw Legend on Sidebar
        legend_x = w + 20
        legend_y = 40
        
        cv2.putText(canvas, "Legend:", (legend_x, legend_y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
        
        legend_items = [
            ("Rotate (+5)", (0, 0, 255)),
            ("Rotate CW (-5)", (128, 128, 255)),
            ("Shear", (255, 0, 255)),
            ("Shear Right", (147, 20, 255)),
            ("Inverted", (0, 255, 255)),
            ("Thicken", (0, 165, 255)),
            ("Threshold", (0, 255, 0)),
            ("Simple Thresh", (255, 255, 0)),
            ("CLAHE", (128, 128, 128)),
            ("Speech Bubble", (0, 100, 0)),
            ("Baseline", (255, 0, 0))
        ]
        
        for name, color in legend_items:
            legend_y += 40
            # Draw color box
            cv2.rectangle(canvas, (legend_x, legend_y - 20), (legend_x + 30, legend_y + 10), color, -1)
            # Draw text
            cv2.putText(canvas, name, (legend_x + 40, legend_y + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)

        output_path = image_path.replace(".png", "_result.png").replace(".jpg", "_result.jpg")
        cv2.imwrite(output_path, canvas)
        print(f"Saved visualization to {output_path}")

    except Exception as e:
        print(f"Visualization Error: {e}")

def merge_text_lines(base_lines, new_lines):
    from difflib import SequenceMatcher
    merged = list(base_lines)
    for new_line in new_lines:
        is_new = True
        new_text = new_line.text.strip()
        xs = [w.x for w in new_line.words]
        ys = [w.y for w in new_line.words]
        if not xs: continue
        nx, ny = min(xs), min(ys)
        nw = max([w.x+w.width for w in new_line.words]) - nx
        nh = max([w.y+w.height for w in new_line.words]) - ny
        
        for ex_line in merged:
             ex_text = ex_line.text.strip()
             
             exs = [w.x for w in ex_line.words]
             eys = [w.y for w in ex_line.words]
             if not exs: continue
             ex = min(exs)
             ey = min(eys)
             ew = max([w.x + w.width for w in ex_line.words]) - ex
             ew = max([w.x + w.width for w in ex_line.words]) - ex
             eh = max([w.y + w.height for w in ex_line.words]) - ey
             
             # RAW MODE: Strict Merge Only
             if False:
                 # Center Distance tolerance to account for bounding box shifts across techniques
                 nx_c, ny_c = nx + nw/2, ny + nh/2
                 ex_c, ey_c = ex + ew/2, ey + eh/2
                 dist = math.sqrt((nx_c - ex_c)**2 + (ny_c - ey_c)**2)
                 
                 # Allow up to 20px or the height of the text box (whichever is larger) to account for drift
                 tolerance = max(20.0, min(nh, eh))
                 
                 if new_text == ex_text and dist < tolerance:
                     ex_line.sources.update(new_line.sources)
                     is_new = False
                     break
                 continue # Skip standard logic in RAW_MODE

             # Check overlap
             x_overlap = max(0, min(ex+ew, nx+nw) - max(ex, nx))
             y_overlap = max(0, min(ey+eh, ny+nh) - max(ey, ny))
             area_overlap = x_overlap * y_overlap
             
             # Calculate vertical distance (gap) if no overlap
             if y_overlap == 0:
                 # Dist between bottom of top and top of bottom
                 y_dist = max(0, max(ny, ey) - min(ny+nh, ey+eh))
             else:
                 y_dist = 0
                 
             min_area = min(ew*eh, nw*nh) # Area of smaller box
             
             # Calculate ratios
             # For strict intersection, area_overlap > 0
             
             similarity = SequenceMatcher(None, new_text, ex_text).ratio()
             
             # Logic for Identical/Highly Similar Text with Geometric Drift
             # If text is nearly perfect (> 0.9) AND X-Overlap is significant (> 40% of smaller width)
             # AND Vertical Gap is small (e.g., < 1.0 * height), we consider it a duplicate.
             
             min_w = min(nw, ew)
             x_overlap_ratio = x_overlap / min_w if min_w > 0 else 0
             avg_h = (nh + eh) / 2
             
             # Overlap Area Ratio
             area_overlap_ratio = area_overlap / min_area if min_area > 0 else 0
             
             is_contained = False
             if min_area > 0 and area_overlap > 0 :
                 is_contained = (area_overlap / (nw*nh) > 0.9) or (area_overlap / (ew*eh) > 0.9)

             should_merge = False
             
             # 1. Standard Area Overlap Merge
             if min_area > 0:
                 if (similarity > 0.8 and area_overlap_ratio > 0.01) or \
                    (similarity > 0.95 and area_overlap_ratio > 0.0) or \
                    (is_contained and similarity > 0.2) or \
                    (area_overlap_ratio > 0.5 and similarity > 0.2):
                     should_merge = True
            
             # 2. Drift Merge (Vertical Gap allowed for High Similarity)
             if not should_merge and similarity > 0.9 and x_overlap_ratio > 0.4:
                 if y_dist < avg_h * 1.5: # Allow gap up to 1.5x height
                     should_merge = True
                     
             if should_merge:
                 # Duplicate found. 
                 # Merge sources
                 ex_line.sources.update(new_line.sources)
                 
                 # Logic: Keep the "Standard" geometry (ex_line) usually. 
                 # But if new_text is "better" (longer), take text.
                 # Updated: Relaxed check for "STUFF, YOUR" vs "YOUR"
                 if len(new_text) > len(ex_text):
                     # If existing text is contained in new text (e.g. "YOUR" in "STUFF, YOUR")
                     if ex_text in new_text:
                         ex_line.text = new_text
                     # Or if they are somewhat similar but new one is longer (e.g. "OKARUN" vs "OKARJN.")
                     elif similarity > 0.4:
                         ex_line.text = new_text
                 
                 is_new = False
                 break
        if is_new:
            merged.append(new_line)
    return merged

def calculate_match_score(candidates, references):
    """
    Calculates a score representing how well 'candidates' align with 'references'.
    Score = Number of lines that have high text similarity AND spatial overlap.
    """
    from difflib import SequenceMatcher
    score = 0
    for cand in candidates:
        cand_text = cand.text.strip()
        c_words = cand.words
        if not c_words: continue
        
        cx_min = min(w.x for w in c_words)
        cy_min = min(w.y for w in c_words)
        cx_max = max(w.x + w.width for w in c_words)
        cy_max = max(w.y + w.height for w in c_words)
        cw, ch = cx_max - cx_min, cy_max - cy_min
        
        match_found = False
        for ref in references:
            ref_text = ref.text.strip()
            similarity = SequenceMatcher(None, cand_text, ref_text).ratio()
            if similarity < 0.8: continue
            
            r_words = ref.words
            if not r_words: continue
            rx_min = min(w.x for w in r_words)
            ry_min = min(w.y for w in r_words)
            rx_max = max(w.x + w.width for w in r_words)
            ry_max = max(w.y + w.height for w in r_words)
            rw, rh = rx_max - rx_min, ry_max - ry_min
            
            # Check for Intersection
            x_overlap = max(0, min(cx_max, rx_max) - max(cx_min, rx_min))
            y_overlap = max(0, min(cy_max, ry_max) - max(cy_min, ry_min))
            area_overlap = x_overlap * y_overlap
            
            min_area = min(cw*ch, rw*rh)
            if min_area > 0:
                 ratio = area_overlap / min_area
                 if ratio > 0.1: # At least 10% overlap + High Text Match
                    match_found = True
                    score += ratio # Use overlap ratio as score weight
                    break
            
    return score

def filter_redundant_geometry(base_lines, geom_lines, technique_name="Geometry"):
    """
    Filters out geometric transform results that overlap with existing base lines but were NOT merged.
    matches:
    - If overlap > 30% AND text similarity < 80% (i.e., not merged), discard.
    - Rationale: If it overlaps spatially but text is different, the Rotation likely caused a drift/misread 
      compared to the robust Baseline/Threshold result.
    """
    filtered = []
    for r_line in geom_lines:
        r_box = r_line.words
        if not r_box: continue
        
        rx_min = min(w.x for w in r_box)
        ry_min = min(w.y for w in r_box)
        rx_max = max(w.x + w.width for w in r_box)
        ry_max = max(w.y + w.height for w in r_box)
        rw, rh = rx_max - rx_min, ry_max - ry_min
        r_area = rw * rh
        
        keep = True
        overlap_info = ""
        
        for b_line in base_lines:
            b_box = b_line.words
            if not b_box: continue
            
            bx_min = min(w.x for w in b_box)
            by_min = min(w.y for w in b_box)
            bx_max = max(w.x + w.width for w in b_box)
            by_max = max(w.y + w.height for w in b_box)
            
            # Intersection
            x_overlap = max(0, min(rx_max, bx_max) - max(rx_min, bx_min))
            y_overlap = max(0, min(ry_max, by_max) - max(ry_min, by_min))
            area_overlap = x_overlap * y_overlap
            
            if r_area > 0 and area_overlap > 0:
                overlap_ratio = area_overlap / r_area
                
                # If significant overlap (> 30%)
                if overlap_ratio > 0.3:
                    
                    from difflib import SequenceMatcher
                    r_text = r_line.text.strip()
                    b_text = b_line.text.strip()
                    similarity = SequenceMatcher(None, r_text, b_text).ratio()
                    
                    # We drop it if it's highly overlapping BUT the text is very different (likely garbage/noise).
                    # A threshold of < 0.4 ensures we KEEP valid text corrections (like "SORRY" vs "50RRY" - sim ~75%) 
                    # but drop complete garbage misreads.
                    if similarity < 0.4:
                        keep = False
                        overlap_info = f"Overlaps '{b_text}' ({overlap_ratio:.1%}) but text diff ({similarity:.1%})"
                        break
                        
                    # NEW: Drop if it's a substring or near-identical to existing baseline text
                    # (e.g. "ANP" overlapping "ANP MAKE" or "OFF." overlapping "OFF.")
                    # merge_text_lines might not merge them if similarity isn't > 0.8, causing duplicates.
                    if r_text in b_text or similarity > 0.8:
                        keep = False
                        overlap_info = f"Redundant substring/duplicate of '{b_text}'"
                        break
        
        if keep:
            filtered.append(r_line)
        else:
            print(f"[{technique_name}] Discarding '{r_line.text}': {overlap_info}")
            
    return filtered

class WindowsOCREngine:
    def __init__(self):
        self._language = "en"
        self._status_callback = None

    def set_callback(self, callback):
        self._status_callback = callback

    def set_language(self, lang_code: str):
        self._language = lang_code
        print(f"WindowsOCREngine: Language set to {lang_code}", flush=True)

    def warmup(self):
        pass

    async def extract_text(self, image):
        """
        Runs 10 techniques concurrently, merges them, and groups by bubble.
        Returns: grouped list or flat list depending on how engine expects.
        Actually Overlayr's AsyncEngine.process_image expects flat list of:
        [(bbox_4pts, text, conf), ...]
        But since we want to group by bubble, we should return the grouped structure,
        OR return a flat list but with a 'group_id' in the tuple?
        Wait, `AsyncEngine.process_image` currently does this:
        ```
        results = await self.ocr_service.extract_text(image)
        for (bbox_points, text, conf) in results:
        ```
        If we want to change grouping, we need to bypass `_merge_text_blocks` in `AsyncEngine`.
        Let's make `extract_text` return a flat list of dicts with all metadata, or modify `engine.py` heavily.
        Let's modify `extract_text` to return the groups natively!
        Wait, `engine.py` also translates text. We need to pass the groups back so `engine.py` can translate and maintain structure.
        """
        start_total = time.perf_counter()

        # Convert PIL to CV2 BGR
        open_cv_image = np.array(image.convert('RGB')) 
        open_cv_image = open_cv_image[:, :, ::-1].copy()
        
        image_source = open_cv_image

        task1 = run_ocr(image_source, language=self._language, technique='baseline')
        task2 = asyncio.sleep(0, result=(None, None, None, 0)) # run_ocr(image_source, technique='threshold')
        task3 = asyncio.sleep(0, result=(None, None, None, 0)) # run_ocr(image_source, technique='inverted')
        task4 = run_ocr(image_source, language=self._language, technique='thicken')
        task5 = asyncio.sleep(0, result=(None, None, None, 0)) # run_ocr(image_source, technique='shear')
        task6 = asyncio.sleep(0, result=(None, None, None, 0)) # run_ocr(image_source, technique='rotate')
        task7 = asyncio.sleep(0, result=(None, None, None, 0)) # run_ocr(image_source, technique='rotate_cw')
        task8 = asyncio.sleep(0, result=(None, None, None, 0)) # run_ocr(image_source, technique='shear_right')
        task9 = run_ocr(image_source, language=self._language, technique='simple_threshold')
        task10 = run_ocr(image_source, language=self._language, technique='clahe')

        results = await asyncio.gather(task1, task2, task3, task4, task5, task6, task7, task8, task9, task10)
        
        (res1, scale1, processed1, time1) = results[0]
        (res2, scale2, processed2, time2) = results[1]
        (res3, scale3, processed3, time3) = results[2]
        (res4, scale4, processed4, time4) = results[3]
        (res5, scale5, processed5, time5) = results[4]
        (res6, scale6, processed6, time6) = results[5]
        (res7, scale7, processed7, time7) = results[6]
        (res8, scale8, processed8, time8) = results[7]
        (res9, scale9, processed9, time9) = results[8]
        (res10, scale10, processed10, time10) = results[9]

        def process_result(res, tech, scale, processed, mode='auto'):
            if res:
                 if processed is not None: shape = processed.shape
                 else: shape = open_cv_image.shape
                 return correct_geometry(res, tech, scale, shape, matrix_mode=mode)
            return []

        lines1 = process_result(res1, 'baseline', scale1, processed1)
        final_lines = list(lines1)
        
        # Disabled in WindowOCR-Refining for speed and redundancy:
        # if res2: final_lines = merge_text_lines(final_lines, process_result(res2, 'threshold', scale2, processed2))
        # if res3: final_lines = merge_text_lines(final_lines, process_result(res3, 'inverted', scale3, processed3))
        
        if res4: final_lines = merge_text_lines(final_lines, process_result(res4, 'thicken', scale4, processed4))
        if res9: final_lines = merge_text_lines(final_lines, process_result(res9, 'simple_threshold', scale9, processed9))
        if res10: final_lines = merge_text_lines(final_lines, process_result(res10, 'clahe', scale10, processed10))

        reference_lines = list(final_lines) 

        # Disabled in WindowOCR-Refining:
        # if res8: 
        #     shear_r_lines = process_result(res8, 'shear_right', scale8, processed8)
        #     filtered_shear_r = filter_redundant_geometry(final_lines, shear_r_lines, technique_name="Shear_Right")
        #     final_lines = merge_text_lines(final_lines, filtered_shear_r)
            
        reference_lines = list(final_lines)
            
        if res5: 
            shear_lines = process_result(res5, 'shear', scale5, processed5)
            filtered_shear = filter_redundant_geometry(final_lines, shear_lines, technique_name="Shear")
            final_lines = merge_text_lines(final_lines, filtered_shear)
        
        if res6:
            lines_fwd = process_result(res6, 'rotate', scale6, processed6, mode='forward')
            lines_inv = process_result(res6, 'rotate', scale6, processed6, mode='inverse')
            score_fwd = calculate_match_score(lines_fwd, reference_lines)
            score_inv = calculate_match_score(lines_inv, reference_lines)
            
            if score_fwd > score_inv * 1.2:
                best_score = score_fwd
                best_lines = lines_fwd
            else:
                best_score = score_inv
                best_lines = lines_inv
                
            threshold = max(3, len(reference_lines) * 0.20)
            if best_score >= threshold:
                filtered_lines = filter_redundant_geometry(final_lines, best_lines, technique_name="Rotate")
                final_lines = merge_text_lines(final_lines, filtered_lines)
                
        if res7:
            lines_fwd = process_result(res7, 'rotate_cw', scale7, processed7, mode='forward')
            lines_inv = process_result(res7, 'rotate_cw', scale7, processed7, mode='inverse')
            score_fwd = calculate_match_score(lines_fwd, reference_lines)
            score_inv = calculate_match_score(lines_inv, reference_lines)
            
            if score_fwd > score_inv * 1.2:
                best_score = score_fwd
                best_lines = lines_fwd
            else:
                best_score = score_inv
                best_lines = lines_inv
                
            threshold = max(3, len(reference_lines) * 0.20)
            if best_score >= threshold:
                filtered_lines = filter_redundant_geometry(final_lines, best_lines, technique_name="Rotate_CW")
                final_lines = merge_text_lines(final_lines, filtered_lines)

        # Convert to Overlayr Format using DBNet Grouping
        print(f'Final Lines before grouping: {len(final_lines)}')
        if final_lines: print(final_lines[0].text)
        final_groups = group_text_lines(final_lines, image_source)
        grouped_results = []
        for group in final_groups:
            g_lines = group["lines"]
            if not g_lines: continue
            
            all_words = []
            for l in g_lines:
                if l.words: all_words.extend(l.words)
                
            if all_words:
                x1 = min([w.x for w in all_words])
                y1 = min([w.y for w in all_words])
                x2 = max([w.x + w.width for w in all_words])
                y2 = max([w.y + w.height for w in all_words])
                bbox = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
            else:
                bbox = [[0,0], [0,0], [0,0], [0,0]]
                
            merged_text = " ".join([l.text for l in g_lines])
            grouped_results.append((bbox, merged_text, 1.0))
            
        return grouped_results


def group_text_lines(final_lines, image_source):
    """
    Groups text lines based on:
    1. Speech Bubble Containment (Strongest)
    2. Proximity (if not in bubble)
    """
    if not HAS_OPENCV: return [{"lines": final_lines, "type": "page"}]

    # 1. Detect Text Regions
    text_regions = detect_text_regions_dbnet(image_source)
    
    # DEBUG: Save visualization of detected Text Regions
    try:
        import os
        debug_img = image_source.copy() if hasattr(image_source, 'copy') else image_source
        if debug_img is not None:
            for i, cnt in enumerate(text_regions):
                cv2.polylines(debug_img, [cnt], isClosed=True, color=(0, 255, 0), thickness=2)
                
                x, y, w, h = cv2.boundingRect(cnt)
                text = str(i)
                font = cv2.FONT_HERSHEY_SIMPLEX
                font_scale = 1.0
                thickness = 2
                
                (text_w, text_h), _ = cv2.getTextSize(text, font, font_scale, thickness)
                y_pos = max(y, text_h + 5) # Ensure it doesn't draw off the top
                cv2.rectangle(debug_img, (x, y_pos - text_h - 5), (x + text_w, y_pos), (0, 0, 255), -1)
                cv2.putText(debug_img, text, (x, y_pos - 5), font, font_scale, (255, 255, 255), thickness)

            import time
            debug_dir = r"d:\NotWork\NewWork\Overlayr\debug_images"
            if not os.path.exists(debug_dir):
                os.makedirs(debug_dir, exist_ok=True)
            timestamp = int(time.time() * 1000)
            debug_out_path = os.path.join(debug_dir, f"dbnet_regions_{timestamp}.png")
            cv2.imwrite(debug_out_path, debug_img)
    except Exception as e:
        print(f"Failed to save debug regions image: {e}")
    
    # 2. Assign to Text Regions
    region_groups = [[] for _ in text_regions]
    ungrouped = []
    
    for line in final_lines:
        assigned = False
        # Center of the line
        if not line.words: # Changed from line['words'] to line.words
            ungrouped.append(line)
            continue
            
        # Refined Assignment Logic: Word-by-Word Split
        word_regions = []
        for w in line.words:
            containing_regions = []
            points_to_check = [
                (w.x, w.y),
                (w.x + w.width, w.y),
                (w.x, w.y + w.height),
                (w.x + w.width, w.y + w.height),
                (w.x + w.width/2, w.y + w.height/2)
            ]
            for i, cnt in enumerate(text_regions):
                is_inside = False
                for pt in points_to_check:
                    if cv2.pointPolygonTest(cnt, pt, False) >= 0:
                        is_inside = True
                        break
                if is_inside:
                    area = cv2.contourArea(cnt)
                    containing_regions.append((i, area))
            
            best_i = -1
            if containing_regions:
                containing_regions.sort(key=lambda x: x[1])
                best_i = containing_regions[0][0]
            
            word_regions.append((w, best_i))
            
        # Group adjacent words by region
        grouped_sublines = []
        current_region = None
        current_words = []
        
        for w, r in word_regions:
            if r != current_region:
                if current_words:
                    grouped_sublines.append((current_region, current_words))
                current_region = r
                current_words = [w]
            else:
                current_words.append(w)
        if current_words:
            grouped_sublines.append((current_region, current_words))
            
        # Create sub-lines and assign
        for r_id, words in grouped_sublines:
            sub_text = " ".join([getattr(w, 'text', '') for w in words]).strip()
            if not sub_text:
                continue
            
            srcs = list(line.sources) if hasattr(line, 'sources') and line.sources else []
            src = srcs[0] if srcs else None
            sub_line = TextLine(sub_text, words, source=src)
            if hasattr(line, 'sources'):
                sub_line.sources = set(line.sources)
                
            if r_id == -1:
                ungrouped.append(sub_line)
            else:
                region_groups[r_id].append(sub_line)
        
    # 3. Cluster Ungrouped by Proximity
    # Simple vertical/horizontal clustering
    # Simple vertical/horizontal clustering
    # We'll use a simple approach: Sort by Y, then merge if close and aligned.
    # Actually, let's keep it simple: A "Block" is a set of lines close to each other.
    
    proximity_groups = []
    while ungrouped:
        seed = ungrouped.pop(0)
        group = [seed]
        
        # Iteratively find close lines
        changed = True
        while changed:
            changed = False
            for i in range(len(ungrouped) - 1, -1, -1):
                candidate = ungrouped[i]
                # Check distance to ANY line in current group
                is_close = False
                for g_line in group:
                    # Calculate distance
                    # Get bounding box of g_line
                    gx = min([w.x for w in g_line.words])
                    gy = min([w.y for w in g_line.words])
                    gw = max([w.x+w.width for w in g_line.words]) - gx
                    gh = max([w.y+w.height for w in g_line.words]) - gy
                    
                    cx = min([w.x for w in candidate.words])
                    cy = min([w.y for w in candidate.words])
                    cw = max([w.x+w.width for w in candidate.words]) - cx
                    ch = max([w.y+w.height for w in candidate.words]) - cy
                    
                    # Horizontal overlap/proximity
                    x_dist = max(0, max(gx, cx) - min(gx+gw, cx+cw))
                    y_dist = max(0, max(gy, cy) - min(gy+gh, cy+ch))
                    
                    # Criteria: Close vertically (< 2x Line Height) AND Aligned Horizontally (Overlap or small gap)
                    avg_h = (gh + ch) / 2
                    if y_dist < avg_h * 2.5: # Vertical proximity
                         # Horizontal: Allow some gap, but mainly check if they look like a paragraph
                         # If x_dist is small (or zero overlap), it's close.
                         # Let's say max X gap < Average Width * 2
                         avg_w = (gw + cw) / 2
                         if x_dist < avg_w * 1.5:
                             is_close = True
                             break
                
                if is_close:
                    group.append(candidate)
                    ungrouped.pop(i)
                    changed = True
        
        proximity_groups.append(group)

    # Format Output
    final_groups = []
    
    def deduplicate_in_group(group_lines):
        if not group_lines: return [], []
        unique = []
        dropped_lines = []
        
        for line in group_lines:
            is_dupe = False
            l_box = line.words
            if not l_box: continue
            lx = min([w.x for w in l_box])
            ly = min([w.y for w in l_box])
            lw = max([w.x+w.width for w in l_box]) - lx
            lh = max([w.y+w.height for w in l_box]) - ly
            l_area = lw * lh
            
            for idx, ex_line in enumerate(unique):
                 if line is ex_line:
                     is_dupe = True
                     break
                     
                 # Check Overlap
                 ex_box = ex_line.words
                 exx = min([w.x for w in ex_box])
                 exy = min([w.y for w in ex_box])
                 exw = max([w.x+w.width for w in ex_box]) - exx
                 exh = max([w.y+w.height for w in ex_box]) - exy
                 ex_area = exw * exh
                 
                 x_overlap = max(0, min(lx+lw, exx+exw) - max(lx, exx))
                 y_overlap = max(0, min(ly+lh, exy+exh) - max(ly, exy))
                 area_overlap = x_overlap * y_overlap
                 
                 min_area = min(l_area, ex_area)
                 min_area = min(l_area, ex_area)
                 ratio = area_overlap / min_area if min_area > 0 else 0
                 
                 # Vertical Overlap (Same line check)
                 min_h = min(lh, exh)
                 y_overlap_ratio = y_overlap / min_h if min_h > 0 else 0
                 
                 # Text Similarity
                 similarity = SequenceMatcher(None, line.text, ex_line.text).ratio()
                 sim_lower = SequenceMatcher(None, line.text.lower(), ex_line.text.lower()).ratio()
                 
                 # Centroid X distance
                 l_cx = lx + lw/2
                 ex_cx = exx + exw/2
                 dist_x = abs(l_cx - ex_cx)
                 # Allow shift up to half the width of the wider box? Or strict?
                 # Shear shift is usually moderate.
                 max_w = max(lw, exw)
                 
                 # print(f"DEBUG: Dedup Check. '{line.text}' vs '{ex_line.text}'")
                 # print(f"DEBUG: Dedup Check. '{line.text}' vs '{ex_line.text}'")
                 # print(f"       Ratio: {ratio:.3f}, Y-ov: {y_overlap_ratio:.2f}, Sim: {similarity:.3f}/{sim_lower:.3f}, Sources: {list(line.sources)} vs {list(ex_line.sources)}")
                 
                 # RAW MODE: Strict Merge Only
                 if False:
                     # Center Distance tolerance to account for bounding box shifts across techniques
                     l_cy = ly + lh/2
                     ex_cy = exy + exh/2
                     l_cx = lx + lw/2
                     ex_cx = exx + exw/2
                     dist = math.sqrt((l_cx - ex_cx)**2 + (l_cy - ex_cy)**2)
                     
                     tolerance = max(20.0, min(lh, exh))
                     
                     clean_line_text = line.text.replace(" ", "")
                     clean_ex_text = ex_line.text.replace(" ", "")
                     
                     is_exact = clean_line_text == clean_ex_text and dist < 50.0  # Allow larger drift for exact matches
                     is_substring = (clean_line_text in clean_ex_text or clean_ex_text in clean_line_text) and dist < max_w * 1.5
                     
                     if (line.text == ex_line.text and dist < tolerance) or is_exact or is_substring:
                         is_dupe = True
                         # Keep the longer text
                         if len(line.text) > len(ex_line.text):
                             line.sources.update(ex_line.sources)
                             unique[idx] = line
                         else:
                             ex_line.sources.update(line.sources)
                         break
                     continue # Skip all other Pruning in RAW_MODE
                 
                 # Strict Merge (High Area Overlap + Identical/High Sim)
                 if min_area > 0 and ratio > 0.5 and sim_lower > 0.8:
                      is_dupe = True
                      ex_line.sources.update(line.sources)
                      dropped_lines.append( (line.text, list(line.sources), f"Strict Merge with '{ex_line.text}'") )
                      # print(f"       -> DUPLICATE MERGED (Strict)!")
                      break
                 
                 # Soft Merge (Same Line + Moderate Sim + Close Proximity)
                 if y_overlap_ratio > 0.5:
                     if sim_lower > 0.85:
                          is_dupe = True
                          ex_line.sources.update(line.sources)
                          dropped_lines.append( (line.text, list(line.sources), f"Soft Merge (High Sim) with '{ex_line.text}'") )
                          # print(f"       -> DUPLICATE MERGED (High Sim, Low Overlap)!")
                          break
                     if sim_lower > 0.4 and dist_x < max_w * 0.8:
                          # Safety: If one is Strong (3+ sources) and other is Weak, AND similarity is low (< 0.7), 
                          # it's likely a collision of distinct words (e.g. "IGNORING" vs "VNORIN?").
                          # Don't merge if it risks destroying the Strong line.
                          
                          n_sources_line = len(line.sources)
                          n_sources_ex = len(ex_line.sources)
                          is_strong_line = n_sources_line >= 3
                          is_weak_ex = n_sources_ex < 3
                          is_strong_ex = n_sources_ex >= 3
                          is_weak_line = n_sources_line < 3
                          
                          # If trying to merge Strong into Weak with low similarity -> ABORT
                          if is_strong_line and is_weak_ex and sim_lower < 0.7:
                               continue
                          
                          # If trying to merge Weak into Strong -> Allow? 
                          # Usually we want to absorb weak into strong.
                          # But this block updates 'ex_line' with 'line' sources and keeps 'ex_line' text.
                          # So if ex_line is Strong, we are fine (we keep Strong text).
                          # If ex_line is Weak and line is Weak, we merge.
                          
                          is_dupe = True
                          ex_line.sources.update(line.sources)
                          dropped_lines.append( (line.text, list(line.sources), f"Soft Merge (Prox) with '{ex_line.text}'") )
                          # print(f"       -> DUPLICATE MERGED (Soft: Line+Sim+Prox)!")
                          break
                          
                 # Weak Line Pruning (Handle Ghost Lines from single techniques like Shear)
                 # If one line is "Strong" (many sources) and other is "Weak" (few sources),
                 # and there is ANY resemblance, text-wise, merge them.
                 n_sources_line = len(line.sources)
                 n_sources_ex = len(ex_line.sources)
                 is_weak_line = n_sources_line < 3
                 is_strong_ex = n_sources_ex >= 3
                 is_weak_ex = n_sources_ex < 3
                 is_strong_line = n_sources_line >= 3
                 
                 # If a weak line (artifacts/partial) is clearly overlapping a strong line, drop it.
                 # "BVT r" (Shear) overlaps "SHOULD" (Strong) by 73%.
                 if (is_weak_line and is_strong_ex) and ratio > 0.6:
                       is_dupe = True
                       ex_line.sources.update(line.sources) # Merge sources (maybe?)
                       dropped_lines.append( (line.text, list(line.sources), f"Weak Containment (Merged into Strong '{ex_line.text}')") )
                       break
                 
                 if (is_weak_ex and is_strong_line) and ratio > 0.6:
                       is_dupe = True
                       line.sources.update(ex_line.sources)
                       # Replace weak ex with strong line (current line)
                       unique[idx] = line
                       dropped_lines.append( (ex_line.text, list(ex_line.sources), f"Weak Containment (Replaced by Strong '{line.text}')") )
                       break
                       
                 # NEW: Weak-Weak Containment Pruning
                 # If TWO weak lines overlap heavily but text differs (e.g., "IT MAKES" vs "IT 4AKE.5"),
                 # they won't trigger strong pruning or standard merge. We must force one to absorb the other.
                 # Also handle cases where Y-drift separates boxes but they are clearly the same line and text.
                 
                 l_cy, ex_cy = ly + lh/2, exy + exh/2
                 l_cx, ex_cx = lx + lw/2, exx + exw/2
                 center_dist = math.sqrt((l_cx - ex_cx)**2 + (l_cy - ex_cy)**2)
                 
                 is_weak_weak_dupe = (ratio > 0.7) or \
                                     (y_overlap_ratio > 0.4 and dist_x < max_w * 0.5 and sim_lower > 0.7) or \
                                     (center_dist < 25.0 and sim_lower > 0.7)
                                     
                 if is_weak_line and is_weak_ex and is_weak_weak_dupe:
                       is_dupe = True
                       # Keep the one with MORE sources, or if equal, the one with MORE text length
                       if n_sources_line > n_sources_ex or (n_sources_line == n_sources_ex and len(line.text) > len(ex_line.text)):
                            line.sources.update(ex_line.sources)
                            unique[idx] = line
                            dropped_lines.append( (ex_line.text, list(ex_line.sources), f"Weak-Weak Containment (Replaced by better weak '{line.text}')") )
                       else:
                            ex_line.sources.update(line.sources)
                            dropped_lines.append( (line.text, list(line.sources), f"Weak-Weak Containment (Merged into better weak '{ex_line.text}')") )
                       break

                  # Merge Weak -> Strong (Text-based)
                 # MUST confirm spatial overlap to avoid merging distinct lines with similar text (e.g. "ME..." vs "...YOU")
                 if (is_weak_line and is_strong_ex) or (is_weak_ex and is_strong_line):
                      # Check for Overlap (Intersection > 10% OR Vertical Overlap on same line)
                      if (min_area > 0 and ratio > 0.1) or (y_overlap_ratio > 0.3 and dist_x < max_w * 0.5):
                          # Lower threshold because ghosts can be very garbled (e.g. 'BVT r' vs 'BUT 1' -> 0.4)
                          if sim_lower > 0.35: 
                               is_dupe = True
                               # Always keep the text of the Strong line?
                               if is_strong_line and is_weak_ex:
                                   # Current line is strong, existing is weak. Replace existing with current.
                                   unique[idx] = line
                                   dropped_lines.append( (ex_line.text, list(ex_line.sources), f"Weak Pruned (Replaced by Strong '{line.text}')") )
                               elif is_weak_line and is_strong_ex:
                                   # Current line is weak, existing is strong. Merge sources into existing.
                                   ex_line.sources.update(line.sources)
                                   dropped_lines.append( (line.text, list(line.sources), f"Weak Pruned (Merged into Strong '{ex_line.text}')") )
                               # print(f"       -> DUPLICATE MERGED (Weak Pruning: Replaced Weak with Strong)!")
                               break
                               
                 # NEW: Substring Override & Exact Match Drift Fallback
                 # If text is identical but drifted far (e.g. 'OFF.' at y=888 vs 'OFF.' at y=871)
                 # Or if one is a pure substring of the other (e.g. '. . .ANP' inside '...ANP ABOUT')
                 if not is_dupe:
                     clean_line_text = line.text.replace(" ", "")
                     clean_ex_text = ex_line.text.replace(" ", "")
                     
                     is_exact = clean_line_text == clean_ex_text and center_dist < 50.0  # Allow larger drift for exact matches in same bubble
                     is_substring = (clean_line_text in clean_ex_text or clean_ex_text in clean_line_text) and center_dist < max_w * 1.5
                     
                     if is_exact or is_substring:
                         is_dupe = True
                         # Keep the longer text / more sources
                         if len(line.text) > len(ex_line.text) or (len(line.text) == len(ex_line.text) and n_sources_line > n_sources_ex):
                             line.sources.update(ex_line.sources)
                             unique[idx] = line
                             dropped_lines.append( (ex_line.text, list(ex_line.sources), f"Substring/Exact Match (Replaced by '{line.text}')") )
                         else:
                             ex_line.sources.update(line.sources)
                             dropped_lines.append( (line.text, list(line.sources), f"Substring/Exact Match (Merged into '{ex_line.text}')") )
                         break
            if not is_dupe:
                unique.append(line)
        return unique, dropped_lines

    # Regions
    total_dropped_lines = []
    
    for i, lines in enumerate(region_groups):
        if not lines: continue
        
        # print(f"DEBUG: Processing Bubble [{i}] with {len(lines)} lines.")
        
        # Deduplicate
        # lines, dropped = deduplicate_in_group(lines)
        # total_dropped_lines.extend(dropped)
        
        if not lines: continue
            
        # Sort lines in bubble top-to-bottom
        lines.sort(key=lambda l: min([w.y for w in l.words]) if l.words else 0)
        
        # Vertical Split Check
        # Calculate average line height
        heights = []
        for l in lines:
            if l.words:
                ly = min([w.y for w in l.words])
                lh = max([w.y+w.height for w in l.words]) - ly
                heights.append(lh)
        avg_h = sum(heights)/len(heights) if heights else 20
        
        # New Logic: Column-Based Clustering
        # Instead of linear pairwise comparison, check if current line fits into ANY existing sub-group (column)
        split_groups = []
        if lines:
            split_groups.append([lines[0]])
            
        for j in range(1, len(lines)):
            curr = lines[j]
            if not curr.words: continue
            
            curr_y_top = min([w.y for w in curr.words])
            curr_x_max = max([w.x+w.width for w in curr.words])
            curr_x_min = min([w.x for w in curr.words])
            
            best_group_idx = -1
            max_overlap = -1
            
            # Check against all existing groups to find the best "column" fit
            for k, group in enumerate(split_groups):
                prev = group[-1]
                if not prev.words: continue
                
                prev_y_bottom = max([w.y+w.height for w in prev.words])
                prev_x_max = max([w.x+w.width for w in prev.words])
                prev_x_min = min([w.x for w in prev.words])
                
                gap = curr_y_top - prev_y_bottom
                
                # Threshold: Vertical Gap must be acceptable
                cutoff = avg_h * 1.0
                
                if gap > cutoff:
                    continue # Too far below this specific column
                    
                # Calculate Horizontal Overlap
                overlap_min = max(prev_x_min, curr_x_min)
                overlap_max = min(prev_x_max, curr_x_max)
                h_overlap = overlap_max - overlap_min
                
                # Must overlap horizontally (Positive) to be in same column
                if h_overlap > 0:
                    # Found a candidate! Pick the one with MOST overlap if multiple
                    if h_overlap > max_overlap:
                        max_overlap = h_overlap
                        best_group_idx = k
            
            if best_group_idx != -1:
                # Add to existing column
                split_groups[best_group_idx].append(curr)
            else:
                # Start new column
                split_groups.append([curr])
        
        for sub_id, subgroup in enumerate(split_groups):
             final_groups.append({"type": "region", "id": f"{i}_{sub_id}" if sub_id > 0 else i, "lines": subgroup})
            
    # Proximity Blocks
    for i, lines in enumerate(proximity_groups):
        if lines:
            lines.sort(key=lambda l: min([w.y for w in l.words]) if l.words else 0)
            final_groups.append({"type": "block", "id": i, "lines": lines})
            
    # Sort groups by top-most line Y
    final_groups.sort(key=lambda g: min([w.y for w in g["lines"][0].words]) if g["lines"] and g["lines"][0].words else 0)
    
    total_lines = sum([len(g["lines"]) for g in final_groups])
    print(f"Total Merged Lines: {total_lines}")
    
    if total_dropped_lines:
        print("-" * 30)
        print(f"DROPPED LINES (Deduplication/Pruning): {len(total_dropped_lines)}")
        print("-" * 30)
        for text, sources, reason in total_dropped_lines:
             src_str = ", ".join(sorted(list(sources)))
             if len(src_str) > 50: src_str = src_str[:47] + "..."
             print(f"  [x] '{text}'")
             print(f"      Reason : {reason}")
             print(f"      Sources: {src_str}")
             print("")
    
    print("-" * 30)
    
    return final_groups


