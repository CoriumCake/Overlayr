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
    from .ocr_preprocessor import preprocess_image_for_ocr, detect_speech_bubbles
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
    def __init__(self, x, y, w, h):
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
            new_rect = Rect(min_x / scale, min_y / scale, (max_x - min_x) / scale, (max_y - min_y) / scale)
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
                     print(f"[DEBUG-MERGE] MERGED (Duplicate): '{new_text}' -> '{ex_text}'", flush=True)
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
                 print(f"[DEBUG-MERGE] MERGED (Overlap/Sim): '{new_text}' -> '{ex_text}'", flush=True)
                 ex_line.sources.update(new_line.sources)
                 if len(new_text) > len(ex_text):
                     if ex_text in new_text:
                         ex_line.text = new_text
                     elif similarity > 0.4:
                         ex_line.text = new_text
                 is_new = False
                 break
        if is_new:
            print(f"[DEBUG-MERGE] KEPT (NEW): '{new_text}'", flush=True)
            merged.append(new_line)
    return merged

def calculate_match_score(candidates, references):
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
                    from difflib import SequenceMatcher
                    r_text = r_line.text.strip()
                    b_text = b_line.text.strip()
                    similarity = SequenceMatcher(None, r_text, b_text).ratio()
                    if similarity < 0.4:
                        print(f"[DEBUG-MERGE] DISCARDED (Bad Geometry): '{r_text}' overlap with '{b_text}'", flush=True)
                        keep = False
                        break
                    if r_text in b_text or similarity > 0.8:
                        print(f"[DEBUG-MERGE] DISCARDED (Redundant Geometry): '{r_text}' in '{b_text}'", flush=True)
                        keep = False
                        break
        if keep: filtered.append(r_line)
    return filtered

def group_text_lines(final_lines, image):
    """
    Groups text lines based on proximity if speech bubbles cannot be detected.
    Re-adapted from WinOCR to just return grouping.
    """
    if not HAS_OPENCV: return [{"lines": final_lines, "type": "page"}]

    # Fallback to proximity clustering
    ungrouped = list(final_lines)
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

    # Simplified deduction for brevity, let's keep it simple for now as we just need basic block merging
    # as engine.py handles Manga mode merges anyway.
    
    final_groups = []
    for i, lines in enumerate(proximity_groups):
        if lines:
            lines.sort(key=lambda l: min([w.y for w in l.words]) if l.words else 0)
            final_groups.append({"type": "block", "id": i, "lines": lines})
            
    final_groups.sort(key=lambda g: min([w.y for w in g["lines"][0].words]) if g["lines"] and g["lines"][0].words else 0)
    return final_groups


class WindowsOCREngine:
    """
    Advanced Windows 10/11 OCR combining 10 image processing techniques.
    Adapted from the experimental WinOCR script.
    """
    
    def __init__(self):
        self._language = "en"
        self._status_callback = None
        print("WindowsOCREngine (Advanced) initialized.")

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
        Executes parallel OCR using up to 10 techniques and merges results.
        Returns standard format: [(bbox, text, conf), ...]
        """
        start_total = time.perf_counter()
        
        # Convert PIL to CV2 BGR for opencv ops in `run_ocr`
        cv_img = cvImage = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        
        task1 = run_ocr(cv_img, self._language, technique='baseline')
        task2 = run_ocr(cv_img, self._language, technique='threshold')
        task3 = run_ocr(cv_img, self._language, technique='inverted')
        task4 = run_ocr(cv_img, self._language, technique='thicken')
        task5 = run_ocr(cv_img, self._language, technique='shear')
        task6 = run_ocr(cv_img, self._language, technique='rotate')
        task7 = run_ocr(cv_img, self._language, technique='rotate_cw')
        task8 = run_ocr(cv_img, self._language, technique='shear_right')
        task9 = run_ocr(cv_img, self._language, technique='simple_threshold')
        task10 = run_ocr(cv_img, self._language, technique='clahe')
        
        # Concurrent execution
        results = await asyncio.gather(task1, task2, task3, task4, task5, task6, task7, task8, task9, task10)
        
        def process_result(res, tech, scale, processed, mode='auto'):
            if res:
                 shape = processed.shape if processed is not None else cv_img.shape
                 lines = correct_geometry(res, tech, scale, shape, matrix_mode=mode)
                 print(f"[DEBUG-OCR] Preprocessing '{tech}' found {len(lines)} lines:", flush=True)
                 for idx, line in enumerate(lines):
                     if line.words:
                         lx, ly = min(w.x for w in line.words), min(w.y for w in line.words)
                         print(f"  -> [{idx}] '{line.text}' at ({lx:.1f}, {ly:.1f})", flush=True)
                 return lines
            return []

        # Reconstruct and merge based on WinOCR logic
        lines1 = process_result(results[0][0], 'baseline', results[0][1], results[0][2])
        final_lines = list(lines1)
        
        for i, tech in enumerate(['threshold', 'inverted', 'thicken', None, None, None, None, 'simple_threshold', 'clahe']):
            if tech and results[i][0]:
                final_lines = merge_text_lines(final_lines, process_result(results[i][0], tech, results[i][1], results[i][2]))
                
        reference_lines = list(final_lines) 
        
        # if results[7][0]: # shear_right
        #     shear_r_lines = process_result(results[7][0], 'shear_right', results[7][1], results[7][2])
        #     filtered_shear_r = filter_redundant_geometry(final_lines, shear_r_lines, technique_name="Shear_Right")
        #     final_lines = merge_text_lines(final_lines, filtered_shear_r)
        #     
        # reference_lines = list(final_lines)
        #     
        # if results[4][0]: # shear
        #     shear_lines = process_result(results[4][0], 'shear', results[4][1], results[4][2])
        #     filtered_shear = filter_redundant_geometry(final_lines, shear_lines, technique_name="Shear")
        #     final_lines = merge_text_lines(final_lines, filtered_shear)
            
        # Rotate (Tech 6 & 7) - Temporarily Disabled
        # for idx_res, tech_name in [(5, 'rotate'), (6, 'rotate_cw')]:
        #     if results[idx_res][0]:
        #         lines_fwd = process_result(results[idx_res][0], tech_name, results[idx_res][1], results[idx_res][2], mode='forward')
        #         lines_inv = process_result(results[idx_res][0], tech_name, results[idx_res][1], results[idx_res][2], mode='inverse')
        #         
        #         score_fwd = calculate_match_score(lines_fwd, reference_lines)
        #         score_inv = calculate_match_score(lines_inv, reference_lines)
        #         
        #         if score_fwd > score_inv * 1.2:
        #             best_score, best_lines = score_fwd, lines_fwd
        #         else:
        #             best_score, best_lines = score_inv, lines_inv
        #             
        #         threshold = max(3, len(reference_lines) * 0.20)
        #         if best_score >= threshold:
        #             filtered_lines = filter_redundant_geometry(final_lines, best_lines, technique_name=tech_name)
        #             final_lines = merge_text_lines(final_lines, filtered_lines)
                    
        final_lines.sort(key=lambda line: line.words[0].y if line.words else 0)
        
        # Convert TextLines back to required format: (bbox, text, conf)
        output_results = []
        for line in final_lines:
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
        print(f"Windows OCR (10-Tech Pipeline) Finished: {end_total - start_total:.2f}s")
        
        return output_results
    
    def warmup(self):
        pass
