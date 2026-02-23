import asyncio
from typing import Optional, Tuple, Callable, List, Dict, Any
from PIL import Image
import numpy as np
from ..services.windows_ocr import WindowsOCREngine
from ..services.google_translate import GoogleTranslateService
from ..services.validation_service import ValidationService
from ..utils.singleton import SingletonMeta
from PIL import ImageEnhance, ImageFilter

import cv2
from .post_processing import TextPostProcessor

class AsyncEngine(metaclass=SingletonMeta):
    """
    Orchestrates OCR and Translation asynchronously.
    Pipeline: Screen Capture -> Grayscale -> CLAHE -> OCR.
    """
    def __init__(self) -> None:
        print("AsyncEngine: Init start...", flush=True)
        self.ocr_service = WindowsOCREngine()
        print("AsyncEngine: Windows OCR initialized.", flush=True)
        self.translator_service = GoogleTranslateService()
        print("AsyncEngine: Google Translate initialized.", flush=True)
        self.post_processor = TextPostProcessor()
        print("AsyncEngine: PostProcessor initialized.", flush=True)
        self._is_running = False
        self._loop_task: Optional[asyncio.Task] = None
        
        # Translation Cache: LRU (OrderedDict)
        # Key: source_text, Value: translated_text
        from collections import OrderedDict
        self._translation_cache = OrderedDict()
        self.CACHE_SIZE = 100
        
        self._loop_counter = 0

    def set_status_callback(self, callback):
        """Pass UI status callback to services."""
        if hasattr(self.ocr_service, 'set_callback'):
            self.ocr_service.set_callback(callback)
        
        # Current language state
        self.source = "en"
        self.target = "th"

    def set_loop(self, loop):
        self._loop = loop

    def _handle_task_result(self, future):
        try:
            future.result()
        except Exception as e:
            print(f"Async Task Error: {e}", flush=True)

    def warmup(self):
        # Trigger model load / connection
        self.ocr_service.warmup()
        self.translator_service.warmup()
        print("AsyncEngine: Warmup complete.", flush=True)

    async def update_languages(self, source: str, target: str):
        self.source = source
        self.target = target
        
        # Update translator (async)
        await self.translator_service.load_model(source, target)
        
        # Update Windows OCR language
        # Windows OCR Engine handles mapping internally (e.g. ja->ja, zh->zh-Hans)
        # We just pass the code.
        # Common UI codes: "Japanese" (ja), "English" (en), "Chinese" (zh-CN)
        # self.ocr_service.set_language(source) 
        # Actually better to extract the lang code part if source is "ja" or "en" etc.
        # Assuming source is just the code "en", "ja", etc. from the UI.
        # Let's verify what UI sends. 
        # UI sends values from the dict: "ja", "en", "ko", "th", "zh-CN"
        
        self.ocr_service.set_language(source)
             
        # But for this task, let's assume default 'en' or user setting matches
        
        # Store for internal use (noise filtering logic)
        self.source = source
        self.target = target

    async def process_image(self, image: Image.Image, step_callback: Optional[Callable] = None, skip_translation: bool = True, merge_mode: str = "paragraph") -> Tuple[List[Dict[str, Any]], str]:
        """
        Runs OCR and translates text using fast Windows+Google APIs.
        Supports step_callback(stage_name, data) for debugging flow.
        merge_mode: 'paragraph' (group lines) or 'line' (separate lines)
        """
        import time
        t0 = time.time()
        
        # --- NEW PIPELINE (Optimized: MedianBlur > Gray/CLAHE > Upscale > Bin > CCA LUT) ---
        import time
        import os
        
        # Helper to log time and save debug images
        last_time = time.perf_counter()
        debug_dir = os.path.join(os.getcwd(), "debug_images")
        # if not os.path.exists(debug_dir): os.makedirs(debug_dir) # Only create if needed
        timestamp = int(time.time() * 1000)
        
        # TOGGLE: Debug Image Saving
        SAVE_INTERMEDIATE_DEBUG = False
        SAVE_FINAL_DEBUG = False

        if SAVE_INTERMEDIATE_DEBUG or SAVE_FINAL_DEBUG:
            if not os.path.exists(debug_dir):
                os.makedirs(debug_dir, exist_ok=True)

        def log_and_save(name, img_cv, suffix):
            nonlocal last_time
            now = time.perf_counter()
            print(f"[DEBUG] Preprocessing - {name}: {(now - last_time)*1000:.2f} ms", flush=True)
            last_time = now
            
            # Save debug image
            if SAVE_INTERMEDIATE_DEBUG:
                try:
                    fname = f"step_{suffix}_{timestamp}.png"
                    # cv2.imwrite expects BGR or Grayscale.
                    cv2.imwrite(os.path.join(debug_dir, fname), img_cv)
                except Exception as e:
                    print(f"[DEBUG] Save Fail ({name}): {e}", flush=True)

        # Convert PIL to CV2 (BGR)
        padding = 50
        open_cv_image = np.array(image.convert('RGB')) 
        open_cv_image = open_cv_image[:, :, ::-1].copy()
        
        # 1. Smart Scaling (Optimize for Speed/Accuracy)
        w, h = image.size
        # The new WindowsOCREngine handles its own preprocessing (Grayscale, CLAHE, etc.)
        # so we just pass the original PIL image to it.
        
        # Execute OCR
        ocr_start = time.perf_counter()
        results = await self.ocr_service.extract_text(image)
        ocr_dur = (time.perf_counter() - ocr_start) * 1000
        print(f"[DEBUG] OCR Processing: {ocr_dur:.2f} ms", flush=True)
        t1 = time.time()
        
        # Post-Process Text
        final_results = []
        for bbox_points, text, conf in results:
            cleaned_text = self.post_processor.clean_text(text)
            final_text = self.post_processor.correct_text(cleaned_text)
            
            if not final_text: continue
            
            final_results.append((bbox_points, final_text, conf))
            
        results = final_results
        
        # DEBUG CALLBACK: OCR DONE
        if step_callback:
            await step_callback("OCR", results)
        
        if not results:
            return [], ""
            
        texts_to_translate = []
        metadata = []
        
        # Capture all raw text before filtering for debugging
        raw_parts = [t for _, t, _ in results]
        raw_text = f"[{len(results)} raw items] {' | '.join(raw_parts)}"[:500] 
        
        # Prepare batch
        w, h = image.size # Original size
        
        filtered_metadata = [] # Store rejected items for debug

        for bbox_points, text, conf in results:
            if conf < 0.35: continue 
            
            # ... (Spell Check) ...
            corrected_text = self.post_processor.correct_text(text)
            if corrected_text != text:
                 print(f"[DEBUG] Speller Corrected: '{text}' -> '{corrected_text}'", flush=True)
                 text = corrected_text

            # ... (Calculate coordinates common for both) ...
            pts = np.array(bbox_points)
            left = int(np.min(pts[:, 0])); top = int(np.min(pts[:, 1]))
            right = int(np.max(pts[:, 0])); bottom = int(np.max(pts[:, 1]))
            left = max(0, left); top = max(0, top); right = min(w, right); bottom = min(h, bottom)

            # 2. Then, Validate (Filter)
            is_valid = ValidationService.is_valid_text(text, self.source)
            
            if not is_valid:
                print(f"[DEBUG] Filtered out text: '{text}' (Conf: {conf})", flush=True)
                # Store filtered item
                filtered_metadata.append({
                    "bbox": [left, top, right, bottom],
                    "original": text
                })
                continue
            else:
                 print(f"[DEBUG] Accepted text: '{text}' (Conf: {conf})", flush=True)
            
            try:
                region = image.crop((left, top, right, bottom))
                if region.width == 0 or region.height == 0: continue
                np_region = np.array(region)
                avg_color = np.mean(np_region, axis=(0,1))
                bg_color_hex = '#%02x%02x%02x' % (int(avg_color[0]), int(avg_color[1]), int(avg_color[2]))
            except: 
                bg_color_hex = "#000000"

            texts_to_translate.append(text)
            metadata.append({
                "bbox": [left, top, right, bottom],
                "original": text,
                "bg_color": bg_color_hex
            })

        # --- DEBUG SAVE: Processed Image + Grouped Boxes ---
        # Removed as WindowsOCREngine handles its own debug images if needed.

        if not metadata:
            return [], raw_text

        # In WinOCR, the engine has already handled merging spatially close blocks and overlapping geometry
        # but the returns are just flat boxes. We keep this to group them into paragraphs if requested.
        merged_metadata = self._merge_text_blocks(metadata, mode=merge_mode)  
        # DEBUG CALLBACK: MERGE DONE
        if step_callback:
            await step_callback("MERGED", merged_metadata)
        
        texts_to_translate = [m["original"] for m in merged_metadata]

        # --- Translation Caching (Last 2 Loops) ---
        self._loop_counter += 1
        current_loop = self._loop_counter
        
        final_translations = [None] * len(texts_to_translate)
        missing_indices = []
        missing_texts = []
        
        # 1. Check Cache
        for i, text in enumerate(texts_to_translate):
            if text in self._translation_cache:
                trans = self._translation_cache[text]
                # Move to end (Recently Used)
                self._translation_cache.move_to_end(text)
                
                final_translations[i] = trans
                print(f"[DEBUG] CACHED: '{text}'", flush=True)
                continue
            
            # If miss
            missing_indices.append(i)
            missing_texts.append(text)

        # 2. Batch Translate Missing
        if missing_texts:
            print(f"[DEBUG] TRANSLATING (New/Expired): {missing_texts}", flush=True)
            if skip_translation:
                 new_results = missing_texts
            else:
                 new_results = await self.translator_service.translate(missing_texts)
                 if isinstance(new_results, str): new_results = [new_results]
            
            for i, result in enumerate(new_results):
                original_idx = missing_indices[i]
                final_translations[original_idx] = result
                
                # Update Cache (LRU)
                text_key = missing_texts[i]
                self._translation_cache[text_key] = result
                self._translation_cache.move_to_end(text_key)
                
                # Prune if exceeding size
                while len(self._translation_cache) > self.CACHE_SIZE:
                    self._translation_cache.popitem(last=False) # Pop first (Oldest)

        translated_texts = final_translations

        # 3. Prune Old Cache (> 2 loops ago) - REMOVED for LRU Strategy
        # keys_to_remove = [k for k, v in self._translation_cache.items() if current_loop - v[1] > 2]
        # for k in keys_to_remove:
        #     del self._translation_cache[k]
        
        # DEBUG CALLBACK: TRANSLATE DONE
        if step_callback:
            await step_callback("TRANSLATED", translated_texts)
        
        # Recombine
        final_results = []
        if isinstance(translated_texts, str): translated_texts = [translated_texts]
        
        for i, meta in enumerate(merged_metadata):
            meta["translated"] = translated_texts[i]
            final_results.append(meta)
            
        t2 = time.time()
        print(f"OCR: {t1-t0:.2f}s | Translate: {t2-t1:.2f}s | Items: {len(final_results)}", flush=True)
        
        return final_results, raw_text

    def _merge_text_blocks(self, elements: List[Dict[str, Any]], mode: str = "paragraph") -> List[Dict[str, Any]]:
        """
        Merges text boxes using clustering logic adapted from WindowsOCRTool.
        mode='paragraph': Groups vertically adjacent lines (Manga/Comic style).
        mode='line': Merges only horizontal fragments, strict on vertical.
        """
        if not elements: return []
        
        # Helper to get bbox values
        def get_vals(el):
            b = el["bbox"]
            # b = [L, T, R, B]
            w = b[2] - b[0]
            h = b[3] - b[1]
            return b[0], b[1], w, h

        # Helper to merge a list of elements into one metadata dict
        def merge_cluster(cluster_items):
            # Sort by Y to maintain reading order within block
            cluster_items.sort(key=lambda x: x["bbox"][1])
            
            # Combine text
            full_text = " ".join([item["original"] for item in cluster_items])
            
            # Combine bbox
            all_l = [i["bbox"][0] for i in cluster_items]
            all_t = [i["bbox"][1] for i in cluster_items]
            all_r = [i["bbox"][2] for i in cluster_items]
            all_b = [i["bbox"][3] for i in cluster_items]
            
            new_bbox = [min(all_l), min(all_t), max(all_r), max(all_b)]
            
            # Use background color of the first item (topmost)
            bg_color = cluster_items[0]["bg_color"]
            
            return {
                "bbox": new_bbox,
                "original": full_text,
                "bg_color": bg_color
            }

        # ---- DUPLICATE / NESTED FILTERING (Common for all modes, but critical for Manga) ----
        # Remove small boxes that are fully contained within larger boxes
        # (e.g. RapidOCR detecting a whole paragraph AND a line inside it)
        if len(elements) > 1:
            elements.sort(key=lambda x: (x["bbox"][2]-x["bbox"][0]) * (x["bbox"][3]-x["bbox"][1]), reverse=True) # Sort by Area Desc
            filtered = []
            
            for i, item in enumerate(elements):
                is_nested = False
                ix, iy, iw, ih = get_vals(item)
                item_area = iw * ih
                
                for kept in filtered:
                    kx, ky, kw, kh = get_vals(kept)
                    
                    # Check if item is inside kept
                    # Overlap Box
                    oxa = max(ix, kx); oxb = min(ix+iw, kx+kw)
                    oya = max(iy, ky); oyb = min(iy+ih, ky+kh)
                    
                    ow = max(0, oxb - oxa)
                    oh = max(0, oyb - oya)
                    overlap_area = ow * oh
                    
                    # If overlap is > 50% of item's area, it's nested/redundant
                    if item_area > 0 and overlap_area / item_area > 0.50:
                        is_nested = True
                        
                        # CRITICAL FIX: Ensure no text is lost!
                        # Check if the small item's text is already in the kept item
                        small_text = item['original'].strip()
                        big_text = kept['original']
                        
                        # Simple substring check (or fuzzy?)
                        # 1. Exact or Substring
                        if small_text in big_text:
                            # Already present perfectly (e.g. redundant detection)
                            # print(f"[DEBUG] Removed redundant nested text: '{small_text}'")
                            pass 
                        else:
                            # Text MIGHT be missing or different
                            # Heuristic: If similarity is high, ignore.
                            # But if completely different, APPEND it.
                            # For safety, let's append if it's not a substring.
                            # But avoid duplication like "Hello World Hello"
                            
                            # Check words
                            small_words = set(small_text.split())
                            big_words = set(big_text.split())
                            
                            # If most words are already there, skip
                            common = small_words.intersection(big_words)
                            if len(common) / len(small_words) > 0.8:
                                # 80% of words overlap -> Likely duplicate
                                pass 
                            else:
                                # Append missing text to big box
                                # print(f"[DEBUG] Merging nested text: '{small_text}' into '{big_text}'")
                                kept['original'] += " " + small_text
                                
                        break
                
                if not is_nested:
                    filtered.append(item)
            
            elements = filtered

        if mode == "line":
            # --- IMPROVED DOCUMENT MODE: Smart Paragraphs ---
            # 1. First, merge HORIZONTAL fragments into strict lines (as old logic did partially)
            elements.sort(key=lambda x: x["bbox"][1])
            
            lines = []
            visited = [False] * len(elements)
            
            # Step 1: Form Lines
            for i in range(len(elements)):
                if visited[i]: continue
                line_group = [elements[i]]
                visited[i] = True
                
                changed = True
                while changed:
                    changed = False
                    for j in range(len(elements)):
                        if visited[j]: continue
                        
                        # Check strictly horizontal
                        is_same_line = False
                        for existing in line_group:
                             b1 = existing["bbox"]
                             b2 = elements[j]["bbox"]
                             
                             # Vertical center check
                             cy1 = (b1[1]+b1[3])/2
                             cy2 = (b2[1]+b2[3])/2
                             
                             if abs(cy1 - cy2) < 15: # Same line
                                 # Horizontal proximity
                                 h_gap = max(0, min(abs(b1[2]-b2[0]), abs(b2[2]-b1[0])))
                                 overlap_w = max(0, min(b1[2], b2[2]) - max(b1[0], b2[0]))
                                 
                                 if h_gap < 150 or overlap_w > 0:
                                     is_same_line = True
                                     break
                        
                        if is_same_line:
                            line_group.append(elements[j])
                            visited[j] = True
                            changed = True
                
                # Merge fragments into a single "Line" object
                lines.append(merge_cluster(line_group))

            # Step 2: Merge Lines into Paragraphs (But respect Headers/Sections)
            if not lines: return []
            
            lines.sort(key=lambda x: x["bbox"][1])
            paragraphs = []
            
            current_para = [lines[0]]
            
            for i in range(1, len(lines)):
                prev = current_para[-1]
                curr = lines[i]
                
                # Metrics
                # 1. Vertical Gap
                gap = curr["bbox"][1] - prev["bbox"][3]
                
                # 2. Height difference (Header vs Body?)
                h_prev = prev["bbox"][3] - prev["bbox"][1]
                h_curr = curr["bbox"][3] - curr["bbox"][1]
                avg_h = (h_prev + h_curr) / 2
                
                # 3. Font Size Ratio
                ratio = max(h_prev, h_curr) / (min(h_prev, h_curr) + 0.1)

                # 4. Horizontal Overlap (Column Check)
                # Overlap X range
                p_x1, p_x2 = prev["bbox"][0], prev["bbox"][2]
                c_x1, c_x2 = curr["bbox"][0], curr["bbox"][2]
                
                overlap_x = max(0, min(p_x2, c_x2) - max(p_x1, c_x1))
                # Horizontal Gap (if no overlap)
                if overlap_x > 0:
                    h_gap = 0
                else:
                    h_gap = max(0, c_x1 - p_x2, p_x1 - c_x2)
                
                # Decisions:
                is_same_para = False
                
                # CONDITION A: Must align horizontally (Same column)
                # Significant overlap OR very small horizontal gap (indentation/bullet)
                # Let's say overlap > 0 OR h_gap < 50 (to allow small bullets)
                is_same_column = (overlap_x > 0) or (h_gap < 50)
                
                if is_same_column:
                    # CONDITION B: Must be vertically close (Same paragraph flow)
                    # Allow gap up to 1.5x height for same paragraph
                    if gap < avg_h * 1.5:
                        # CONDITION C: Similar Font Size (Body text)
                        if ratio < 1.3:
                            is_same_para = True
                        else:
                            # Font size changed -> Header vs Content
                            is_same_para = False
                    else:
                        # Large vertical gap -> New paragraph
                        is_same_para = False
                else:
                    # Different Column -> New paragraph/block
                    is_same_para = False
                
                if is_same_para:
                    current_para.append(curr)
                else:
                    # Flush current para
                    paragraphs.append(merge_cluster(current_para))
                    current_para = [curr]
            
            # Flush last
            if current_para:
                paragraphs.append(merge_cluster(current_para))
                
            return paragraphs

        # ---- PARAGRAPH / MANGA MODE (Clustering) ----
        # 1. Sort by Y initially
        elements.sort(key=lambda x: x["bbox"][1])
        
        clusters = []
        
        for item in elements:
            x, y, w, h = get_vals(item)
            added = False
            
            for cluster in clusters:
                # Calculate cluster bbox
                cl = [i["bbox"][0] for i in cluster]
                ct = [i["bbox"][1] for i in cluster]
                cr = [i["bbox"][2] for i in cluster]
                cb = [i["bbox"][3] for i in cluster] # Bottom of each item
                
                c_min_x = min(cl); c_max_x = max(cr)
                c_min_y = min(ct); c_max_y = max(cb) # Max bottom
                
                # Check horizontal overlap
                # item x range: [x, x+w]
                # cluster x range: [c_min_x, c_max_x]
                item_r = x + w
                
                overlap_w = max(0, min(item_r, c_max_x) - max(x, c_min_x))
                
                # Significant X overlap (> 0 is basically enough for bubbles)
                if overlap_w > 0:
                    # Check vertical distance
                    # Dist from bottom of cluster to top of item
                    # We need the bottom-most point of the cluster effectively
                    
                    v_dist = max(0, y - c_max_y)
                    
                    # Heuristic: If v_dist is small relative to line height
                    # item height: h
                    if v_dist < h * 2.0:
                        cluster.append(item)
                        added = True
                        break
            
            if not added:
                clusters.append([item])
                
        # 2. Sort Clusters (Row-Column Sort)
        # Helper to get top-left of cluster
        def get_cluster_pos(c):
            ts = [i["bbox"][1] for i in c]
            ls = [i["bbox"][0] for i in c]
            return min(ls), min(ts)

        # Sort by Y first
        clusters.sort(key=lambda c: get_cluster_pos(c)[1])
        
        sorted_clusters = []
        row_threshold = 100 # If Y diff < 100, treat as same row (Matched WindowsOCRTool)
        
        while clusters:
            current = clusters.pop(0)
            cx, cy = get_cluster_pos(current)
            
            row = [current]
            others = []
            
            for other in clusters:
                ox, oy = get_cluster_pos(other)
                if abs(oy - cy) < row_threshold:
                    row.append(other)
                else:
                    others.append(other)
            
            # Sort row by X
            row.sort(key=lambda c: get_cluster_pos(c)[0])
            sorted_clusters.extend(row)
            clusters = others
            
        # 3. Final Merge
        final_results = []
        for cluster in sorted_clusters:
            final_results.append(merge_cluster(cluster))
            
        return final_results
                        


    def stop(self) -> None:
        self._is_running = False
        if self._loop_task:
            self._loop_task.cancel()

