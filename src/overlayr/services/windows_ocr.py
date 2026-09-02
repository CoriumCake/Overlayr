import asyncio
import os
import cv2
import numpy as np
import math
import sys
import time
from collections import Counter
from typing import List, Tuple, Any, Dict, Optional
from PIL import Image

from winsdk.windows.globalization import Language
from winsdk.windows.media.ocr import OcrEngine
from winsdk.windows.graphics.imaging import (
    BitmapAlphaMode, BitmapDecoder, BitmapPixelFormat, SoftwareBitmap)
from winsdk.windows.storage import StorageFile, FileAccessMode
from winsdk.windows.storage.streams import DataWriter
from difflib import SequenceMatcher

from ..core.scene import SceneType
from .lexicon import dictionary_ratio
from .neural_ocr import (CROP_PADDING, NeuralLineReader, choose_reading,
                         is_suspect)
from .ocr_profile import get_profile, prepare_region
from .region_provider import find_regions

try:
    from .ocr_preprocessor import (preprocess_image_for_ocr, detect_speech_bubbles,
                                   detect_text_regions_dbnet, warmup_dbnet)
    HAS_OPENCV = True
except ImportError:
    print("Warning: ocr_preprocessor not found. Advanced processing disabled.")
    HAS_OPENCV = False

    def warmup_dbnet():
        """Nothing to warm up when the detector could not be imported."""

ROTATION_PADDING = 150 # Padding to prevent text clipping during rotation
RAW_MODE = False

class TextLine:
    def __init__(self, text, words, source=None):
        self.text = text
        self.words = words # List of Rect objects
        self.sources = {source} if source else set()
        # Share of the reads covering this spot that agreed on this text.
        # A line no other read contradicts stays at 1.0.
        self.confidence = 1.0
        self.region = None

class Rect:
    def __init__(self, text, x, y, w, h):
        self.text = text
        self.x, self.y, self.width, self.height = x, y, w, h

async def get_software_bitmap_from_file(file_path):
    try:
        abs_path = os.path.abspath(file_path)
        if not os.path.exists(abs_path):
            return None
        file = await StorageFile.get_file_from_path_async(abs_path)
        stream = await file.open_async(FileAccessMode.READ)
        decoder = await BitmapDecoder.create_async(stream)
        software_bitmap = await decoder.get_software_bitmap_async()
        return software_bitmap
    except Exception as e:
        print(f"Error loading image: {e}")
        return None

def get_software_bitmap_from_array(bgr_image):
    """Build a SoftwareBitmap straight from a BGR numpy array, with no disk I/O.

    Windows OCR accepts Bgra8; the alpha channel is filled opaque so
    PREMULTIPLIED and straight alpha are equivalent here.
    """
    try:
        if bgr_image.ndim == 2:
            bgra = cv2.cvtColor(bgr_image, cv2.COLOR_GRAY2BGRA)
        elif bgr_image.shape[2] == 4:
            bgra = bgr_image
        else:
            bgra = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2BGRA)
        bgra = np.ascontiguousarray(bgra)
        height, width = bgra.shape[:2]

        writer = DataWriter()
        writer.write_bytes(bgra.tobytes())
        buffer = writer.detach_buffer()
        return SoftwareBitmap.create_copy_from_buffer(
            buffer, BitmapPixelFormat.BGRA8, width, height, BitmapAlphaMode.PREMULTIPLIED)
    except Exception as e:
        print(f"Error building SoftwareBitmap from memory: {e}")
        return None


def sauvola_threshold(gray, window: int = 25, k: float = 0.2, r: float = 128.0):
    """Binarise with a threshold computed per neighbourhood rather than per image.

    Sauvola's rule: a pixel is ink when it is darker than the local mean by an
    amount that shrinks where the local contrast is low. On a comic page that
    matters because screentone, a shaded panel and a white bubble all want
    different cut-offs, and Otsu can only choose one.

    Built from box filters, which are separable and run in constant time per
    pixel whatever the window size - so this costs about as much as any of the
    other techniques rather than being the expensive one.
    """
    if window % 2 == 0:
        window += 1

    source = gray.astype(np.float32)
    size = (window, window)
    mean = cv2.boxFilter(source, -1, size, normalize=True,
                         borderType=cv2.BORDER_REFLECT)
    mean_square = cv2.boxFilter(source * source, -1, size, normalize=True,
                                borderType=cv2.BORDER_REFLECT)
    deviation = cv2.sqrt(cv2.max(mean_square - mean * mean, 0))

    threshold = mean * (1.0 + k * (deviation / r - 1.0))
    return np.where(source > threshold, 255, 0).astype(np.uint8)


async def run_ocr(image_source, language, technique='baseline'):
    try:
        # Imported at the top rather than by name here. A string import is
        # invisible to PyInstaller's analysis, so the packaged build shipped
        # without this module and every OCR call failed at this line - while
        # running from source worked perfectly.
        ocr_engine = OcrEngine.try_create_from_language(Language(language))
    except Exception as e:
        print(f"Error creating OCR engine: {e}")
        return None, 1.0, None, 0.0

    if not ocr_engine:
        return None, 1.0, None, 0.0

    start_time = time.perf_counter()
    scale_factor = 1.0
    processed_img = None
    bitmap = None
    pre_result = None

    if HAS_OPENCV:
        img = None
        if isinstance(image_source, str):
            img = cv2.imread(image_source)
        elif isinstance(image_source, np.ndarray):
            img = image_source
        elif isinstance(image_source, Image.Image):
             img = cv2.cvtColor(np.array(image_source), cv2.COLOR_RGB2BGR)

        if img is not None:
             if len(img.shape) == 3:
                 gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
             else:
                 gray = img
                 
             if technique == 'baseline':
                 pre_result = preprocess_image_for_ocr(img)
             elif technique == 'threshold':
                 img_scaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                 binary = cv2.adaptiveThreshold(img_scaled, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 21, 10)
                 processed_img = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
                 scale_factor = 2.0
                 pre_result = (processed_img, scale_factor)
             elif technique == 'simple_threshold':
                 img_scaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                 _, binary = cv2.threshold(img_scaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                 processed_img = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
                 scale_factor = 2.0
                 pre_result = (processed_img, scale_factor)
             elif technique == 'sauvola':
                 # A local threshold, one decision per neighbourhood instead of
                 # one for the whole crop (CLAUDE.md section 6). Otsu picks a
                 # single cut-off and loses lettering that sits over screentone
                 # or a shaded panel, because the page is not one brightness.
                 img_scaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                 binary = sauvola_threshold(img_scaled)
                 processed_img = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
                 scale_factor = 2.0
                 pre_result = (processed_img, scale_factor)
             elif technique == 'tophat':
                 # Keeps thin bright strokes and drops whatever is larger than
                 # the kernel, which is how outlined comic lettering is pulled
                 # off the art behind it.
                 img_scaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                 kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
                 hat = cv2.morphologyEx(img_scaled, cv2.MORPH_BLACKHAT, kernel)
                 _, binary = cv2.threshold(hat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                 processed_img = cv2.cvtColor(cv2.bitwise_not(binary), cv2.COLOR_GRAY2BGR)
                 scale_factor = 2.0
                 pre_result = (processed_img, scale_factor)
             elif technique == 'despeckle':
                 # Screentone dots survive a plain threshold and break up the
                 # glyphs around them; a small median filter removes them while
                 # leaving strokes intact (CLAUDE.md section 6, COMIC).
                 img_scaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                 denoised = cv2.medianBlur(img_scaled, 3)
                 _, binary = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
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

    if pre_result and pre_result[0] is not None:
        processed_img, scale_factor = pre_result
        bitmap = get_software_bitmap_from_array(processed_img)
            
    if not bitmap:
        if isinstance(image_source, str):
            bitmap = await get_software_bitmap_from_file(image_source)
        elif isinstance(image_source, np.ndarray):
            scale_factor = 1.0
            processed_img = None
            bitmap = get_software_bitmap_from_array(image_source)
        elif isinstance(image_source, Image.Image):
            scale_factor = 1.0
            processed_img = None
            bitmap = get_software_bitmap_from_array(
                cv2.cvtColor(np.array(image_source), cv2.COLOR_RGB2BGR))
        else:
            bitmap = image_source

    if not bitmap:
         return None, 1.0, None, 0.0

    try:
        result = await ocr_engine.recognize_async(bitmap)
        end_time = time.perf_counter()
        duration = end_time - start_time
        return result, scale_factor, processed_img, duration
    except Exception as e:
        print(f"OCR Execution Error: {e}")
        return None, 1.0, None, 0.0

def correct_geometry(ocr_result, technique, scale, img_shape, matrix_mode='auto'):
    lines = []
    h, w = img_shape[:2] 
    M_transform = None
    
    if technique == 'shear': pass
    elif technique == 'rotate':
        center = (w // 2, h // 2)
        M = cv2.getRotationMatrix2D(center, 5, 1.0)
        M_inv = cv2.invertAffineTransform(M)
        if matrix_mode == 'forward': M_transform = M
        elif matrix_mode == 'inverse': M_transform = M_inv
        else: M_transform = M_inv 
    elif technique == 'rotate_cw':
        center = (w // 2, h // 2)
        Mcw = cv2.getRotationMatrix2D(center, -5, 1.0)
        Mcw_inv = cv2.invertAffineTransform(Mcw)
        if matrix_mode == 'forward': M_transform = Mcw
        elif matrix_mode == 'inverse': M_transform = Mcw_inv
        else: M_transform = Mcw_inv 

    for line in ocr_result.lines:
        new_words = []
        for word in line.words:
            rect = word.bounding_rect
            corners = [
                (rect.x, rect.y), (rect.x + rect.width, rect.y),
                (rect.x + rect.width, rect.y + rect.height), (rect.x, rect.y + rect.height)]
            corrected_corners = []
            for (x, y) in corners:
                if technique == 'shear':
                    h_proc = img_shape[0] 
                    shear_offset = int(0.2 * h_proc)
                    nx = (x - shear_offset) + 0.2 * y
                    ny = y
                    corrected_corners.append((nx, ny))
                elif technique == 'shear_right':
                    nx = x - 0.2 * y
                    ny = y
                    corrected_corners.append((nx, ny))
                elif technique in ['rotate', 'rotate_cw'] and M_transform is not None:
                    nx = M_transform[0, 0] * x + M_transform[0, 1] * y + M_transform[0, 2]
                    ny = M_transform[1, 0] * x + M_transform[1, 1] * y + M_transform[1, 2]
                    nx -= ROTATION_PADDING
                    ny -= ROTATION_PADDING
                    corrected_corners.append((nx, ny))
                else:
                    corrected_corners.append((x, y))
            
            xs = [p[0] for p in corrected_corners]
            ys = [p[1] for p in corrected_corners]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            new_rect = Rect(word.text, min_x / scale, min_y / scale, (max_x - min_x) / scale, (max_y - min_y) / scale)
            new_words.append(new_rect)
            
        lines.append(TextLine(line.text, new_words, source=technique))
    return lines

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
             eh = max([w.y + w.height for w in ex_line.words]) - ey
             
             if RAW_MODE:
                 nx_c, ny_c = nx + nw/2, ny + nh/2
                 ex_c, ey_c = ex + ew/2, ey + eh/2
                 dist = math.sqrt((nx_c - ex_c)**2 + (ny_c - ey_c)**2)
                 tolerance = max(20.0, min(nh, eh))
                 if new_text == ex_text and dist < tolerance:
                     ex_line.sources.update(new_line.sources)
                     is_new = False
                     break
                 continue

             x_overlap = max(0, min(ex+ew, nx+nw) - max(ex, nx))
             y_overlap = max(0, min(ey+eh, ny+nh) - max(ey, ny))
             area_overlap = x_overlap * y_overlap
             
             y_dist = max(0, max(ny, ey) - min(ny+nh, ey+eh)) if y_overlap == 0 else 0
             min_area = min(ew*eh, nw*nh) 
             similarity = SequenceMatcher(None, new_text, ex_text).ratio()
             
             min_w = min(nw, ew)
             x_overlap_ratio = x_overlap / min_w if min_w > 0 else 0
             avg_h = (nh + eh) / 2
             area_overlap_ratio = area_overlap / min_area if min_area > 0 else 0
             
             is_contained = False
             if min_area > 0 and area_overlap > 0 :
                 is_contained = (area_overlap / (nw*nh) > 0.9) or (area_overlap / (ew*eh) > 0.9)

             should_merge = False
             if min_area > 0:
                 if (similarity > 0.8 and area_overlap_ratio > 0.01) or \
                    (similarity > 0.95 and area_overlap_ratio > 0.0) or \
                    (is_contained and similarity > 0.2) or \
                    (area_overlap_ratio > 0.5 and similarity > 0.2):
                     should_merge = True
             
             if not should_merge and similarity > 0.9 and x_overlap_ratio > 0.4:
                 if y_dist < avg_h * 1.5: should_merge = True
                     
             if should_merge:
                 ex_line.sources.update(new_line.sources)
                 if len(new_text) > len(ex_text):
                     if ex_text in new_text:
                         ex_line.text = new_text
                     elif similarity > 0.4:
                         ex_line.text = new_text
                 is_new = False
                 break
        if is_new:
            merged.append(new_line)
    return merged

# How much each technique is trusted when nothing else separates two readings.
# Baseline is the unaltered image, so it comes first; the rest follow by how
# much they distort the glyphs to get a reading.
TECHNIQUE_TRUST = ('baseline', 'clahe', 'simple_threshold', 'threshold',
                   'thicken', 'inverted')


def technique_rank(line):
    """Position of a line`s technique in TECHNIQUE_TRUST; unknown ranks last."""
    best = len(TECHNIQUE_TRUST)
    for source in getattr(line, 'sources', ()):
        name = str(source).split('@')[0]
        if name in TECHNIQUE_TRUST:
            best = min(best, TECHNIQUE_TRUST.index(name))
    return best


def place_lines_in_frame(lines, region, extra_scale, source):
    """Move lines read from a region crop back into full-frame coordinates.

    ``correct_geometry`` has already undone the scaling a technique applied, so
    what is left is the extra enlargement this crop was given and the offset of
    the region within the frame.
    """
    inverse = 1.0 / extra_scale if extra_scale else 1.0
    placed = []
    for line in lines:
        words = []
        for word in line.words:
            words.append(Rect(
                getattr(word, "text", ""),
                word.x * inverse + region.left,
                word.y * inverse + region.top,
                word.width * inverse,
                word.height * inverse,
            ))
        moved = TextLine(line.text, words, source=source)
        moved.region = region
        placed.append(moved)
    return placed


def cluster_text_lines(all_lines):
    """
    Pool-All → Spatial Clustering algorithm.
    Groups duplicate/overlapping TextLines across techniques into single representatives.
    Uses Text Similarity + IoU + Center Distance for clustering.
    Representative Selection: Longest text wins (not frequency).
    """
    if not all_lines: return []
    
    clusters = []
    
    def get_box(line):
        if not line.words: return 0, 0, 0, 0
        lx = min([w.x for w in line.words])
        ly = min([w.y for w in line.words])
        lw = max([w.x+w.width for w in line.words]) - lx
        lh = max([w.y+w.height for w in line.words]) - ly
        return lx, ly, lw, lh
        
    unprocessed = list(all_lines)
    while unprocessed:
        seed = unprocessed.pop(0)
        group = [seed]
        
        changed = True
        while changed:
            changed = False
            i = 0
            while i < len(unprocessed):
                cand = unprocessed[i]
                cx, cy, cw, ch = get_box(cand)
                if cw == 0 or ch == 0: 
                    i += 1
                    continue
                c_cx, c_cy = cx + cw/2.0, cy + ch/2.0
                
                is_close = False
                for g in group:
                    gx, gy, gw, gh = get_box(g)
                    if gw == 0 or gh == 0: continue
                    g_cx, g_cy = gx + gw/2.0, gy + gh/2.0
                    
                    x_overlap = max(0, min(gx+gw, cx+cw) - max(gx, cx))
                    y_overlap = max(0, min(gy+gh, cy+ch) - max(gy, cy))
                    intersection = x_overlap * y_overlap
                    union = gw*gh + cw*ch - intersection
                    iou = intersection / union if union > 0 else 0
                    
                    dist = math.sqrt((g_cx - c_cx)**2 + (g_cy - c_cy)**2)
                    
                    min_h = min(gh, ch)
                    max_h = max(gh, ch)
                    
                    # 1. Check Text Similarity
                    clean_cand = cand.text.strip()
                    clean_g = g.text.strip()
                    is_same_text = (clean_cand == clean_g) and (len(clean_cand) > 0)
                    
                    # Merge if texts are identical and they are in the same general vicinity
                    if is_same_text and dist < max_h * 2.5:
                        is_close = True
                        break
                        
                    # 2. Strict Geometry Check (Fallback for different texts)
                    if iou > 0.3 or dist < min_h * 0.4:
                        is_close = True
                        break
                        
                if is_close:
                    group.append(cand)
                    unprocessed.pop(i)
                    changed = True
                else:
                    i += 1
        clusters.append(group)
        
    final_lines = []
    for group in clusters:
        all_sources = set()
        for g in group:
            all_sources.update(g.sources)
            
        text_counts = {}
        text_to_orig = {}
        for g in group:
            t = g.text.strip()
            if not t: continue
            
            clean_t = t.replace(" ", "").upper()
            if clean_t not in text_counts:
                text_counts[clean_t] = 0
                text_to_orig[clean_t] = g
            text_counts[clean_t] += 1
            
            # Prefer longer raw text (more punctuation/spaces) for the same normalized content
            if len(t) > len(text_to_orig[clean_t].text):
                text_to_orig[clean_t] = g

        if not text_counts:
            best_line_obj = group[0]
            best_line_obj.sources = all_sources
            best_line_obj.confidence = 0.0
            final_lines.append(best_line_obj)
            continue

        # Consensus vote (CLAUDE.md section 6): the reading most techniques
        # agree on wins, and ties are settled by which reading looks most like
        # language, then by length, then by how much the technique is trusted.
        # Length alone used to decide this, which let one technique`s longest
        # garbled string outrank the reading every other technique produced.
        best_clean = max(text_counts.keys(), key=lambda k: (
            text_counts[k],
            dictionary_ratio(text_to_orig[k].text),
            len(text_to_orig[k].text),
            -technique_rank(text_to_orig[k]),
        ))

        best_line_obj = text_to_orig[best_clean]
        best_line_obj.sources = all_sources
        # Windows OCR reports no confidence of its own (CLAUDE.md section 3), so
        # the score is how many of the reads covering this spot agreed on the
        # winning text. One read on its own is unconfirmed, not wrong: a line
        # only a single technique could see is exactly the line the region pass
        # exists to rescue, so it keeps a usable score rather than a zero.
        total_votes = sum(text_counts.values())
        best_line_obj.confidence = (text_counts[best_clean] / total_votes
                                    if total_votes else 0.0)
        final_lines.append(best_line_obj)

    return final_lines

def calculate_match_score(candidates, references):
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
            
            x_overlap = max(0, min(cx_max, rx_max) - max(cx_min, rx_min))
            y_overlap = max(0, min(cy_max, ry_max) - max(cy_min, ry_min))
            area_overlap = x_overlap * y_overlap
            
            min_area = min(cw*ch, rw*rh)
            if min_area > 0:
                 ratio = area_overlap / min_area
                 if ratio > 0.1: 
                    score += ratio
                    break
    return score

def filter_redundant_geometry(base_lines, geom_lines, technique_name="Geometry"):
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
        
        for b_line in base_lines:
            b_box = b_line.words
            if not b_box: continue
            bx_min = min(w.x for w in b_box)
            by_min = min(w.y for w in b_box)
            bx_max = max(w.x + w.width for w in b_box)
            by_max = max(w.y + w.height for w in b_box)
            
            x_overlap = max(0, min(rx_max, bx_max) - max(rx_min, bx_min))
            y_overlap = max(0, min(ry_max, by_max) - max(ry_min, by_min))
            area_overlap = x_overlap * y_overlap
            
            if r_area > 0 and area_overlap > 0:
                overlap_ratio = area_overlap / r_area
                if overlap_ratio > 0.3:
                    r_text = r_line.text.strip()
                    b_text = b_line.text.strip()
                    similarity = SequenceMatcher(None, r_text, b_text).ratio()
                    if similarity < 0.4:
                        keep = False
                        break
                    if r_text in b_text or similarity > 0.8:
                        keep = False
                        break
        if keep: filtered.append(r_line)
    return filtered

def deduplicate_in_group(group_lines):
    """
    Multi-layer noise pruning within a text group.
    Removes ghost lines from shear/rotate techniques using:
    - Strict Merge (high overlap + high similarity)
    - Soft Merge (same line + moderate similarity)
    - Weak Line Pruning (weak source overlapping strong)
    - Weak Containment (high overlap, regardless of text)
    - Weak-Weak Containment
    - Substring/Exact Match Drift
    """
    if not group_lines: return []
    unique = []
    
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
             max_w = max(lw, exw)
             
             # Strict Merge (High Area Overlap + Identical/High Sim)
             if min_area > 0 and ratio > 0.5 and sim_lower > 0.8:
                  is_dupe = True
                  ex_line.sources.update(line.sources)
                  break
             
             # Soft Merge (Same Line + Moderate Sim + Close Proximity)
             if y_overlap_ratio > 0.5:
                 if sim_lower > 0.85:
                      is_dupe = True
                      ex_line.sources.update(line.sources)
                      break
                 if sim_lower > 0.4 and dist_x < max_w * 0.8:
                      n_sources_line = len(line.sources)
                      n_sources_ex = len(ex_line.sources)
                      # Safety: Don't merge Strong into Weak with low similarity
                      if n_sources_line >= 3 and n_sources_ex < 3 and sim_lower < 0.7:
                           pass  # Skip
                      else:
                           is_dupe = True
                           ex_line.sources.update(line.sources)
                           break
                      
             # Weak Line Pruning
             n_sources_line = len(line.sources)
             n_sources_ex = len(ex_line.sources)
             is_weak_line = n_sources_line < 3
             is_strong_ex = n_sources_ex >= 3
             is_weak_ex = n_sources_ex < 3
             is_strong_line = n_sources_line >= 3
             
             # Weak Containment: Weak heavily overlapping Strong
             if (is_weak_line and is_strong_ex) and ratio > 0.6:
                   is_dupe = True
                   ex_line.sources.update(line.sources)
                   break
             
             if (is_weak_ex and is_strong_line) and ratio > 0.6:
                   is_dupe = True
                   line.sources.update(ex_line.sources)
                   unique[idx] = line
                   break
                   
             # Weak-Weak Containment
             l_cy, ex_cy = ly + lh/2, exy + exh/2
             center_dist = math.sqrt((l_cx - ex_cx)**2 + (l_cy - ex_cy)**2)
             
             is_weak_weak_dupe = (ratio > 0.7) or \
                                 (y_overlap_ratio > 0.4 and dist_x < max_w * 0.5 and sim_lower > 0.7) or \
                                 (center_dist < 25.0 and sim_lower > 0.7)
                                 
             if is_weak_line and is_weak_ex and is_weak_weak_dupe:
                   is_dupe = True
                   if n_sources_line > n_sources_ex or (n_sources_line == n_sources_ex and len(line.text) > len(ex_line.text)):
                        line.sources.update(ex_line.sources)
                        unique[idx] = line
                   else:
                        ex_line.sources.update(line.sources)
                   break

             # Weak -> Strong Text-based merge with spatial confirmation
             if (is_weak_line and is_strong_ex) or (is_weak_ex and is_strong_line):
                  if (min_area > 0 and ratio > 0.1) or (y_overlap_ratio > 0.3 and dist_x < max_w * 0.5):
                      if sim_lower > 0.35: 
                           is_dupe = True
                           if is_strong_line and is_weak_ex:
                               unique[idx] = line
                           elif is_weak_line and is_strong_ex:
                               ex_line.sources.update(line.sources)
                           break
                           
             # Substring/Exact Match Drift
             if not is_dupe:
                 clean_line_text = line.text.replace(" ", "")
                 clean_ex_text = ex_line.text.replace(" ", "")
                 
                 is_exact = clean_line_text == clean_ex_text and center_dist < 50.0
                 is_substring = (clean_line_text in clean_ex_text or clean_ex_text in clean_line_text) and center_dist < max_w * 1.5
                 
                 if is_exact or is_substring:
                     is_dupe = True
                     if len(line.text) > len(ex_line.text) or (len(line.text) == len(ex_line.text) and n_sources_line > n_sources_ex):
                         line.sources.update(ex_line.sources)
                         unique[idx] = line
                     else:
                         ex_line.sources.update(line.sources)
                     break
        if not is_dupe:
            unique.append(line)
    return unique

def group_text_lines(final_lines, image, text_regions=None, scene=None):
    """
    Groups text lines based on:
    1. Speech Bubble Containment (DBNet Regions)
    2. Word-by-word region assignment + Line Splitting
    3. Column-Based Clustering
    4. Proximity (if not in bubble)

    ``text_regions`` are the contours the caller already found. Passing them in
    matters for comics: detection costs a detector run, and doing it a second
    time here would pay for it twice for no new information.

    Finding them here is only worth it for a comic, which is the one scene the
    detector was trained for. A game or a document arrives with regions of its
    own from a far cheaper finder, and when that finder legitimately returns
    nothing there is no reason to spend a manga detector run to disagree.
    """
    if not HAS_OPENCV: return [{"lines": final_lines, "type": "page"}]

    # 1. Detect Text Regions
    if text_regions is None:
        text_regions = (detect_text_regions_dbnet(image)
                        if scene == SceneType.COMIC else [])

    # 2. Assign to Text Regions
    region_groups = [[] for _ in text_regions]
    ungrouped = []
    
    for line in final_lines:
        # Center of the line
        if not line.words: 
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
            sub_line.confidence = getattr(line, 'confidence', 1.0)
            sub_line.region = getattr(line, 'region', None)


            if r_id == -1:
                ungrouped.append(sub_line)
            else:
                region_groups[r_id].append(sub_line)
        
    # 3. Cluster Ungrouped by Proximity
    proximity_groups = []
    while ungrouped:
        seed = ungrouped.pop(0)
        group = [seed]
        
        changed = True
        while changed:
            changed = False
            for i in range(len(ungrouped) - 1, -1, -1):
                candidate = ungrouped[i]
                is_close = False
                for g_line in group:
                    gx = min([w.x for w in g_line.words])
                    gy = min([w.y for w in g_line.words])
                    gw = max([w.x+w.width for w in g_line.words]) - gx
                    gh = max([w.y+w.height for w in g_line.words]) - gy
                    
                    cx = min([w.x for w in candidate.words])
                    cy = min([w.y for w in candidate.words])
                    cw = max([w.x+w.width for w in candidate.words]) - cx
                    ch = max([w.y+w.height for w in candidate.words]) - cy
                    
                    x_dist = max(0, max(gx, cx) - min(gx+gw, cx+cw))
                    y_dist = max(0, max(gy, cy) - min(gy+gh, cy+ch))
                    
                    avg_h = (gh + ch) / 2
                    if y_dist < avg_h * 2.5: 
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
    
    # Regions (Column-Based Clustering)
    for i, lines in enumerate(region_groups):
        if not lines: continue
        
        lines = deduplicate_in_group(lines)
        if not lines: continue
            
        lines.sort(key=lambda l: min([w.y for w in l.words]) if l.words else 0)
        
        heights = []
        for l in lines:
            if l.words:
                ly = min([w.y for w in l.words])
                lh = max([w.y+w.height for w in l.words]) - ly
                heights.append(lh)
        avg_h = sum(heights)/len(heights) if heights else 20
        
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
            
            for k, group in enumerate(split_groups):
                prev = group[-1]
                if not prev.words: continue
                
                prev_y_bottom = max([w.y+w.height for w in prev.words])
                prev_x_max = max([w.x+w.width for w in prev.words])
                prev_x_min = min([w.x for w in prev.words])
                
                gap = curr_y_top - prev_y_bottom
                cutoff = avg_h * 1.0
                
                if gap > cutoff:
                    continue 
                    
                overlap_min = max(prev_x_min, curr_x_min)
                overlap_max = min(prev_x_max, curr_x_max)
                h_overlap = overlap_max - overlap_min
                
                if h_overlap > 0:
                    if h_overlap > max_overlap:
                        max_overlap = h_overlap
                        best_group_idx = k
            
            if best_group_idx != -1:
                split_groups[best_group_idx].append(curr)
            else:
                split_groups.append([curr])
        
        for sub_id, subgroup in enumerate(split_groups):
             final_groups.append({"type": "region", "id": f"{i}_{sub_id}" if sub_id > 0 else i, "lines": subgroup})
            
    # Proximity Blocks
    for i, lines in enumerate(proximity_groups):
        if lines:
            lines = deduplicate_in_group(lines)
            lines.sort(key=lambda l: min([w.y for w in l.words]) if l.words else 0)
            final_groups.append({"type": "block", "id": i, "lines": lines})
            
    final_groups.sort(key=lambda g: min([w.y for w in g["lines"][0].words]) if g["lines"] and g["lines"][0].words else 0)
    
    return final_groups


class WindowsOCREngine:
    """
    Advanced Windows 10/11 OCR using Pool-All → Spatial Clustering pipeline.
    Active techniques: baseline, thicken, simple_threshold, clahe (4 parallel).
    Adapted from WindowOCR-Refining.
    """
    
    # Active techniques for production (optimized for speed ~0.5s)
    ACTIVE_TECHNIQUES = ['baseline', 'thicken', 'simple_threshold', 'clahe']
    
    def __init__(self):
        self._language = "en"
        self._status_callback = None
        self.neural_reader = NeuralLineReader()
        print("WindowsOCREngine (Pool-All Cluster) initialized.")

    def set_callback(self, callback):
        self._status_callback = callback
        if self._status_callback: self._status_callback("Windows OCR Ready")
    
    def set_language(self, lang_code: str) -> None:
        lang_map = {
            "en": "en-US",
            "ja": "ja-JP", 
            "ko": "ko-KR",
            "zh": "zh-Hans-CN",
            "zh-CN": "zh-Hans-CN",
            "th": "th-TH",
            "vi": "vi-VN",
        }
        self._language = lang_map.get(lang_code, lang_code)
        
        # Simplified language extraction
        if "en" in lang_code.lower(): self._language = "en-US"
        elif "ja" in lang_code.lower(): self._language = "ja-JP"
        elif "th" in lang_code.lower(): self._language = "th-TH"
        elif "ko" in lang_code.lower(): self._language = "ko-KR"
        elif "zh" in lang_code.lower(): self._language = "zh-Hans-CN"
            
        print(f"OCR language set to: {self._language}")
    
    async def extract_text(self, image: Image.Image,
                           scene=None) -> List[Tuple[List[List[int]], str, float]]:
        """Read an image and return [(bbox, text, confidence), ...]."""
        results, _ = await self.extract_text_with_regions(image, scene)
        return results

    async def extract_text_with_regions(self, image: Image.Image, scene=None):
        """
        Region-first OCR pipeline (CLAUDE.md section 4).

        Returns the readings together with the regions they were found in. The
        grouper needs those regions to forbid merging across a bubble, a column
        or a panel, and finding them a second time would cost another detector
        run - about 0.7s for a comic page.
        1. Pick the technique profile for the scene
        2. Read the whole frame with the profile's safety-net techniques
        3. Read each detected region separately, normalised for its own contrast
        4. Pool everything and cluster it into one line per position
        Returns standard format: [(bbox, text, conf), ...]
        """
        start_total = time.perf_counter()

        # Convert PIL to CV2 BGR for opencv ops in `run_ocr`
        cv_img = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)

        profile = get_profile(scene)
        regions = find_regions(cv_img, scene) if profile.uses_regions else []

        # --- Step 1: Whole frame, then each region, all in parallel ---
        tasks = []
        # Each entry describes how to map one result back to frame coordinates.
        placements = []

        for tech in profile.full_frame:
            tasks.append(run_ocr(cv_img, self._language, technique=tech))
            placements.append((tech, None, 1.0, cv_img.shape))

        for index, region in enumerate(regions):
            crop = region.crop(cv_img)
            if crop.size == 0:
                continue
            prepared, extra_scale = prepare_region(crop, profile)
            for tech in profile.region:
                tasks.append(run_ocr(prepared, self._language, technique=tech))
                placements.append((f"{tech}@{index}", region, extra_scale,
                                   prepared.shape))

        results = await asyncio.gather(*tasks)

        # --- Step 2: Pool every read into one list of frame-space lines ---
        all_raw_lines = []
        for (source, region, extra_scale, fallback_shape), result in zip(placements, results):
            res, scale, processed, duration = result
            if not res:
                continue
            shape = processed.shape if processed is not None else fallback_shape
            technique = source.split("@")[0]
            lines = correct_geometry(res, technique, scale, shape)
            if region is not None:
                lines = place_lines_in_frame(lines, region, extra_scale, source)
            all_raw_lines.extend(lines)

        if not all_raw_lines:
            end_total = time.perf_counter()
            print(f"Windows OCR (Region-first) Finished: {end_total - start_total:.2f}s (no results)")
            return [], regions

        # --- Step 3: Spatial clustering (deduplication + representative selection) ---
        final_lines = cluster_text_lines(all_raw_lines)

        # --- Step 4: Grouping & Region Splitting ---
        contours = [region.to_contour() for region in regions] if regions else None
        grouped_results = group_text_lines(final_lines, cv_img, contours, scene=scene)

        # Flatten grouped results back to list
        flattened_lines = []
        for group in grouped_results:
            flattened_lines.extend(group["lines"])
        
        # Convert TextLines back to required format: (bbox, text, conf)
        output_results = []
        for line in flattened_lines:
            if not line.words: continue
            
            # Recompute total box for line
            xs = [w.x for w in line.words]
            ys = [w.y for w in line.words]
            xs_max = [w.x + w.width for w in line.words]
            ys_max = [w.y + w.height for w in line.words]
            
            min_x, min_y = min(xs), min(ys)
            max_x, max_y = max(xs_max), max(ys_max)
            
            bbox = [
                [int(min_x), int(min_y)],
                [int(max_x), int(min_y)],
                [int(max_x), int(max_y)],
                [int(min_x), int(max_y)]
            ]
            
            output_results.append((bbox, line.text, getattr(line, "confidence", 1.0)))

        if profile.uses_neural_reader:
            output_results = self._second_opinion(output_results, cv_img)

        end_total = time.perf_counter()
        print(f"Windows OCR ({len(profile.full_frame)} full-frame + "
              f"{len(regions)} regions) Finished: {end_total - start_total:.2f}s")

        return output_results, regions

    def _second_opinion(self, results, cv_img):
        """Re-read the lines the dictionary distrusts, and keep the better one.

        Runs after clustering rather than alongside the techniques, because the
        vote in `cluster_text_lines` counts heads: the profile's techniques are
        all the same engine reading the same pixels, so when they are wrong they
        are wrong together and a single dissenting reader would lose five to one
        no matter how right it was (CLAUDE.md 9.28).
        """
        if not results or not self.neural_reader.available:
            return results

        height, width = cv_img.shape[:2]
        suspects, crops = [], []
        for index, (bbox, text, _conf) in enumerate(results):
            if not is_suspect(text):
                continue
            points = np.array(bbox)
            left, top = max(0, int(points[:, 0].min())), max(0, int(points[:, 1].min()))
            right = min(width, int(points[:, 0].max()))
            bottom = min(height, int(points[:, 1].max()))
            if right <= left or bottom <= top:
                continue
            left = max(0, left - CROP_PADDING)
            top = max(0, top - CROP_PADDING)
            right = min(width, right + CROP_PADDING)
            bottom = min(height, bottom + CROP_PADDING)
            suspects.append(index)
            crops.append(cv_img[top:bottom, left:right])

        if not crops:
            return results

        readings = self.neural_reader.read(crops)
        replaced = 0
        for index, (neural_text, confidence) in zip(suspects, readings):
            bbox, windows_text, conf = results[index]
            chosen = choose_reading(windows_text, neural_text, confidence)
            if chosen != windows_text:
                replaced += 1
                results[index] = (bbox, chosen, conf)

        print(f"[DEBUG] Second opinion: {len(crops)}/{len(results)} lines "
              f"re-read, {replaced} replaced", flush=True)
        return results

    def warmup(self):
        """Pay the one-off costs now, on the caller's background thread.

        Called from AsyncEngine.warmup during start-up, well before a source is
        picked, so the detector's session load lands there rather than in the
        middle of the first frame the user asks for.
        """
        warmup_dbnet()
        self.neural_reader.warmup()
