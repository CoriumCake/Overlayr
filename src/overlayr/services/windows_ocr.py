import asyncio
import os
import cv2
import numpy as np
import math
import sys
import tempfile
import time
from collections import Counter
from typing import List, Tuple, Any, Dict, Optional
from PIL import Image

from winsdk.windows.media.ocr import OcrEngine
from winsdk.windows.graphics.imaging import BitmapDecoder, SoftwareBitmap
from winsdk.windows.storage import StorageFile, FileAccessMode
from difflib import SequenceMatcher

try:
    from .ocr_preprocessor import preprocess_image_for_ocr, detect_speech_bubbles, detect_text_regions_dbnet
    HAS_OPENCV = True
except ImportError:
    print("Warning: ocr_preprocessor not found. Advanced processing disabled.")
    HAS_OPENCV = False

ROTATION_PADDING = 150 # Padding to prevent text clipping during rotation
RAW_MODE = False

class TextLine:
    def __init__(self, text, words, source=None):
        self.text = text
        self.words = words # List of Rect objects
        self.sources = {source} if source else set()

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

async def run_ocr(image_source, language, technique='baseline'):
    try:
        lang = __import__("winsdk.windows.globalization").windows.globalization.Language(language)
        ocr_engine = OcrEngine.try_create_from_language(lang)
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
            
    if not bitmap:
        if isinstance(image_source, str):
            bitmap = await get_software_bitmap_from_file(image_source)
        elif not HAS_OPENCV:
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
            final_lines.append(best_line_obj)
            continue
            
        # Sort by raw length (desc), then by frequency (desc)
        best_clean = sorted(text_counts.keys(), key=lambda k: (len(text_to_orig[k].text), text_counts[k]), reverse=True)[0]
        
        best_line_obj = text_to_orig[best_clean]
        best_line_obj.sources = all_sources
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

def group_text_lines(final_lines, image):
    """
    Groups text lines based on:
    1. Speech Bubble Containment (DBNet Regions)
    2. Word-by-word region assignment + Line Splitting
    3. Column-Based Clustering
    4. Proximity (if not in bubble)
    """
    if not HAS_OPENCV: return [{"lines": final_lines, "type": "page"}]

    # 1. Detect Text Regions
    text_regions = detect_text_regions_dbnet(image)
    
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
    
    async def extract_text(self, image: Image.Image) -> List[Tuple[List[List[int]], str, float]]:
        """
        Pool-All → Spatial Clustering OCR pipeline.
        1. Run active techniques in parallel
        2. Pool all raw results into a single list
        3. cluster_text_lines() to deduplicate and select representatives
        Returns standard format: [(bbox, text, conf), ...]
        """
        start_total = time.perf_counter()
        
        # Convert PIL to CV2 BGR for opencv ops in `run_ocr`
        cv_img = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        
        # --- Step 1: Run active techniques in parallel ---
        tasks = []
        tech_names = []
        for tech in self.ACTIVE_TECHNIQUES:
            tasks.append(run_ocr(cv_img, self._language, technique=tech))
            tech_names.append(tech)
        
        results = await asyncio.gather(*tasks)
        
        def process_result(res, tech, scale, processed, mode='auto'):
            if res:
                 shape = processed.shape if processed is not None else cv_img.shape
                 return correct_geometry(res, tech, scale, shape, matrix_mode=mode)
            return []

        # --- Step 2: Pool all raw lines ---
        all_raw_lines = []
        for i, tech in enumerate(tech_names):
            res, scale, processed, duration = results[i]
            if res:
                lines = process_result(res, tech, scale, processed)
                all_raw_lines.extend(lines)
        
        if not all_raw_lines:
            end_total = time.perf_counter()
            print(f"Windows OCR (Pool-All) Finished: {end_total - start_total:.2f}s (no results)")
            return []
        
        # --- Step 3: Spatial clustering (deduplication + representative selection) ---
        final_lines = cluster_text_lines(all_raw_lines)
        
        # --- Step 4: Grouping & Region Splitting (DBNet) ---
        grouped_results = group_text_lines(final_lines, cv_img)
        
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
            
            output_results.append((bbox, line.text, 0.95))
            
        end_total = time.perf_counter()
        print(f"Windows OCR (Pool-All, {len(self.ACTIVE_TECHNIQUES)} Techniques) Finished: {end_total - start_total:.2f}s")
        
        return output_results
    
    def warmup(self):
        pass
