import asyncio
from typing import Optional, Tuple, Callable, List, Dict, Any
from PIL import Image
import numpy as np
from ..services.windows_ocr import WindowsOCREngine
from ..services.rapid_ocr import RapidOCREngine
from ..services.google_translate import GoogleTranslateService
from ..services.validation_service import ValidationService
from ..utils.singleton import SingletonMeta
from PIL import ImageEnhance, ImageFilter

import cv2
from .post_processing import TextPostProcessor

class AsyncEngine(metaclass=SingletonMeta):
    """
    Orchestrates OCR and Translation asynchronously.
    Pipeline: Resize (2x) -> FNLM -> AdaptiveThresh -> CCA -> Close -> RapidOCR.
    """
    def __init__(self) -> None:
        print("AsyncEngine: Init start...", flush=True)
        # self.ocr_service = WindowsOCREngine()
        self.ocr_service = RapidOCREngine()
        print("AsyncEngine: RapidOCR initialized.", flush=True)
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
        target_width = 1600 # Optimal width for OCR
        scale_factor = 1.0
        
        if w > target_width:
            # Downscale (Speed up!)
            scale_factor = target_width / w
            new_w = int(w * scale_factor)
            new_h = int(h * scale_factor)
            processed_cv = cv2.resize(open_cv_image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
            log_and_save(f"Smart Scale (Down {scale_factor:.2f}x)", processed_cv, "01_scale")
        elif w < 800:
            # Upscale (Accuracy for small text)
            scale_factor = 2.0
            new_w = int(w * scale_factor)
            new_h = int(h * scale_factor)
            processed_cv = cv2.resize(open_cv_image, (new_w, new_h), interpolation=cv2.INTER_CUBIC)
            log_and_save(f"Smart Scale (Up {scale_factor:.2f}x)", processed_cv, "01_scale")
        else:
             # Keep original
             scale_factor = 1.0
             processed_cv = open_cv_image
             log_and_save("Smart Scale (1.0x)", processed_cv, "01_scale")
             
        # 2. Grayscale & CLAHE
        gray = cv2.cvtColor(processed_cv, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
        processed_cv = clahe.apply(gray)
        # log_and_save("Grayscale & CLAHE", processed_cv, "02_clahe")

        # --- AUTO-INVERT (Fix for White Text on Dark Background) ---
        # Calculate mean brightness to detect Dark Mode / Inverted text
        mean_brightness = np.mean(processed_cv)
        is_dark_bg = mean_brightness < 127 # If mostly dark, assume white text
        
        if is_dark_bg:
             processed_cv = cv2.bitwise_not(processed_cv)
             log_and_save("Auto-Invert (Dark BG)", processed_cv, "02a_invert")


        # 3. Denoise: Median Blur (Adaptive)
        # Only blur if we upscaled (likely noisy) or image is smallish to avoid destroying details on high-res downscaled
        # 3. Denoise: Median Blur (Adaptive)
        if scale_factor > 1.5:
             processed_cv = cv2.medianBlur(processed_cv, 3)

        # 4. Adaptive Thresholding
        # Use a lower C (8) to be more inclusive
        thresh_cv = cv2.adaptiveThreshold(processed_cv, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 8)
        
        # --- DUAL POLARITY MASKING (Fix for Mixed Text) ---
        # We need to capture both Dark Text (in light area) AND Light Text (in dark area).
        # thresh_cv: 255=Light/BG, 0=Dark/Text (Logic A) OR 255=Text, 0=Dark/BG (Logic B)
        #
        # 1. Candidate Mask for DARK Text (Black on White)
        # In this case, Text < LocalMean -> 0 in thresh_cv.
        # So we want 0 values.
        mask_dark_candidates = cv2.bitwise_not(thresh_cv) # 255 where Text is Dark
        
        # 2. Candidate Mask for LIGHT Text (White on Black)
        # In this case, Text > LocalMean -> 255 in thresh_cv.
        # So we want 255 values.
        mask_light_candidates = thresh_cv # 255 where Text is Light
        
        # Function to Filter Noise & Huge BG using CCA
        def filter_mask(mask, min_area=10, max_area_pct=0.6):
            num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
            
            areas = stats[:, cv2.CC_STAT_AREA]
            total_area = mask.shape[0] * mask.shape[1]
            max_area = total_area * max_area_pct
            
            # Create LUT: 0=Ignore, 255=Keep
            lut = np.zeros(num_labels, dtype=np.uint8)
            
            # Valid indices: Area >= min AND Area <= max 
            # AND not background (checking index 0 usually sufficient if BG is largest, but we explicitly check area too)
            valid_indices = np.where((areas >= min_area) & (areas <= max_area))[0]
            
            # Exclude label 0 (background/unconnected void) if it was selected? 
            # Actually label 0 is "the zeros" in the mask. We are analyzing the "ones". 
            # So label 0 is the "void" of the mask. We don't need to filter it, it's already 0.
            # But we must ensure the "background blob" of the image isn't selected as text.
            
            if len(valid_indices) > 0:
                lut[valid_indices] = 255
                
            return lut[labels]

        # Filter both
        mask_dark = filter_mask(mask_dark_candidates)
        mask_light = filter_mask(mask_light_candidates)

        # Merge Masks (Union)
        # Ideally they shouldn't overlap (thresh is binary), but CCA stats might differ.
        # But actually we treat them differently.
        
        # 6. Hybrid Construction (Normalize everything to Dark Text on White BG)
        
        # Base: White Canvas
        final_cv = np.full_like(processed_cv, 255)
        
        # A. Apply Dark Text (Keep Original Grayscale)
        # Where mask_dark is white -> Copy source pixel
        dark_text_pixels = cv2.bitwise_and(processed_cv, processed_cv, mask=mask_dark)
        # We need to put these into final_cv. 
        # final_cv is 255. We want to overwrite with dark_text_pixels where mask is set.
        # Logic: (Final & ~Mask) | (Source & Mask)
        # Since Final is all 1s: (~Mask) | (Source & Mask)
        
        fg_dark = cv2.bitwise_and(processed_cv, processed_cv, mask=mask_dark)
        bg_mask_dark_inv = cv2.bitwise_not(mask_dark)
        final_cv = cv2.bitwise_and(final_cv, final_cv, mask=bg_mask_dark_inv) # Clear holes
        final_cv = cv2.add(final_cv, fg_dark) # Fill text
        
        # B. Apply Light Text (INVERT Original Grayscale -> Becomes Dark)
        # Where mask_light is white -> Copy INVERTED source pixel
        src_inv = cv2.bitwise_not(processed_cv)
        fg_light = cv2.bitwise_and(src_inv, src_inv, mask=mask_light)
        
        bg_mask_light_inv = cv2.bitwise_not(mask_light)
        final_cv = cv2.bitwise_and(final_cv, final_cv, mask=bg_mask_light_inv) # Clear holes 
        # Note: If overlaps occur, this might overwrite dark text. 
        # But thresh and ~thresh are disjoint. So masks shouldn't overlap pixels.
        
        final_cv = cv2.add(final_cv, fg_light)

        log_and_save("Dual-Polarity Output", final_cv, "06_dual_final")
        
        # 7. Padding
        padding_color = [255,255,255] if len(final_cv.shape) == 3 else 255
        final_cv = cv2.copyMakeBorder(final_cv, padding, padding, padding, padding, cv2.BORDER_CONSTANT, value=padding_color)
        log_and_save("Final Hybrid (Gray+Mask)", final_cv, "06_final")
        
        # Convert back to PIL
        # Convert back to PIL
        processed_pil = Image.fromarray(final_cv)
        
        # Execute OCR
        ocr_start = time.perf_counter()
        results = await self.ocr_service.extract_text(processed_pil)
        ocr_dur = (time.perf_counter() - ocr_start) * 1000
        print(f"[DEBUG] OCR Processing: {ocr_dur:.2f} ms", flush=True)
        t1 = time.time()
        
        # Scale back coordinates & Post-Process
        final_results = []
        for bbox_points, text, conf in results:
            # Post-Process Text
            cleaned_text = self.post_processor.clean_text(text)
            final_text = self.post_processor.correct_text(cleaned_text)
            
            if not final_text: continue
            
            # Scale coordinates back to original image
            # x_orig = (x_ocr - padding) / scale
            scaled_bbox = []
            for point in bbox_points:
                x = (point[0] - padding) / scale_factor
                y = (point[1] - padding) / scale_factor
                scaled_bbox.append([int(x), int(y)])
                
            final_results.append((scaled_bbox, final_text, conf))
            
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
        try:
            import os
            from PIL import ImageDraw, ImageFont
            
            debug_img = processed_pil.convert("RGB")
            draw = ImageDraw.Draw(debug_img)
            
            try:
                font = ImageFont.truetype("arial.ttf", 24)
            except:
                font = ImageFont.load_default()

            # Draw Valid Items (BLUE)
            items_to_draw = []
            if metadata:
                 merged = self._merge_text_blocks(metadata, mode=merge_mode)
                 items_to_draw = merged
            
            for item in items_to_draw:
                bbox = item["bbox"] 
                l = (bbox[0] * scale_factor) + padding
                t = (bbox[1] * scale_factor) + padding
                r = (bbox[2] * scale_factor) + padding
                b = (bbox[3] * scale_factor) + padding
                
                draw.rectangle([l, t, r, b], outline="blue", width=4)
                try:
                    draw.text((l, max(0, t - 30)), item["original"], fill="blue", font=font)
                except: pass

            # Draw Filtered Items (RED)
            for item in filtered_metadata:
                bbox = item["bbox"]
                l = (bbox[0] * scale_factor) + padding
                t = (bbox[1] * scale_factor) + padding
                r = (bbox[2] * scale_factor) + padding
                b = (bbox[3] * scale_factor) + padding
                
                # Dashed or Thinner line for rejected? Just Red.
                draw.rectangle([l, t, r, b], outline="red", width=2)
                try:
                    # Draw text in red
                    draw.text((l, max(0, t - 30)), f"[X] {item['original']}", fill="red", font=font)
                except: pass
            
            debug_dir = os.path.join(os.getcwd(), "debug_images")
            if SAVE_FINAL_DEBUG and not os.path.exists(debug_dir): 
                os.makedirs(debug_dir, exist_ok=True)
            
            timestamp = int(time.time() * 1000)
            save_path = os.path.join(debug_dir, f"proc_grouped_{timestamp}.png")
            
            if SAVE_FINAL_DEBUG:
                debug_img.save(save_path)
                print(f"[DEBUG] Saved debug image to: {save_path}", flush=True)
            
        except Exception as e:
             print(f"Failed to save debug image: {e}", flush=True)

        if not metadata:
            return [], raw_text

        # Merge spatially close blocks (Real logic for return)
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

