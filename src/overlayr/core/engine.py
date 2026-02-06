import asyncio
from typing import Optional, Tuple, Callable, List, Dict, Any
from PIL import Image
import numpy as np
from ..services.windows_ocr import WindowsOCREngine
from ..services.paddle_ocr import PaddleOCREngine
from ..services.google_translate import GoogleTranslateService
from ..services.validation_service import ValidationService
from ..utils.singleton import SingletonMeta
from PIL import ImageEnhance, ImageFilter

import cv2
from .post_processing import TextPostProcessor

class AsyncEngine(metaclass=SingletonMeta):
    """
    Orchestrates OCR and Translation asynchronously.
    Pipeline: Resize (2x) -> FNLM -> AdaptiveThresh -> CCA -> Close -> PaddleOCR.
    """
    def __init__(self) -> None:
        print("AsyncEngine: Init start...", flush=True)
        self.ocr_service = WindowsOCREngine()
        # Ensure we have a default language. Paddle needs 'en', 'japan', 'korean', 'ch'
        # self.ocr_service = PaddleOCREngine(lan='en') 
        print("AsyncEngine: Windows OCR initialized.", flush=True)
        self.translator_service = GoogleTranslateService()
        print("AsyncEngine: Google Translate initialized.", flush=True)
        self.post_processor = TextPostProcessor()
        print("AsyncEngine: PostProcessor initialized.", flush=True)
        self._is_running = False
        self._loop_task: Optional[asyncio.Task] = None

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
        debug_dir = r"d:\NotWork\NewWork\Overlayr\ProcessingImge"
        if not os.path.exists(debug_dir): os.makedirs(debug_dir)
        timestamp = int(time.time() * 1000)
        
        # TOGGLE: Set to False to disable debug image saving
        SAVE_DEBUG_IMAGES = False

        def log_and_save(name, img_cv, suffix):
            nonlocal last_time
            now = time.perf_counter()
            print(f"[DEBUG] Preprocessing - {name}: {(now - last_time)*1000:.2f} ms", flush=True)
            last_time = now
            
            # Save debug image
            if SAVE_DEBUG_IMAGES:
                try:
                    fname = f"step_{suffix}_{timestamp}.png"
                    # cv2.imwrite expects BGR or Grayscale.
                    cv2.imwrite(os.path.join(debug_dir, fname), img_cv)
                except Exception as e:
                    print(f"[DEBUG] Save Fail ({name}): {e}", flush=True)

        # Convert PIL to CV2 (BGR)
        scale_factor = 2.5
        padding = 50
        open_cv_image = np.array(image.convert('RGB')) 
        open_cv_image = open_cv_image[:, :, ::-1].copy()
        
        # 1. Upscale 250% (Bicubic)
        # Resize directly on the color image for better interpolation quality before greyscale
        processed_cv = cv2.resize(open_cv_image, None, fx=2.5, fy=2.5, interpolation=cv2.INTER_CUBIC)
        log_and_save("Upscale 2.5x", processed_cv, "01_upscale")

        # 2. Grayscale & CLAHE
        gray = cv2.cvtColor(processed_cv, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
        processed_cv = clahe.apply(gray)
        log_and_save("Grayscale & CLAHE", processed_cv, "02_clahe")

        # 3. Denoise: Median Blur (k=5)
        # Applied after upscale/enhance to remove noise amplified by CLAHE 
        processed_cv = cv2.medianBlur(processed_cv, 5)
        log_and_save("Median Blur (Denoise)", processed_cv, "03_denoise")
        
        # 4. Adaptive Thresholding (C=15) - High contrast separation
        processed_cv = cv2.adaptiveThreshold(processed_cv, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 15)
        log_and_save("Adaptive Threshold", processed_cv, "04_threshold")

        # 5. Clean Noise: CCA + NumPy LUT Optimization
        # Invert for CCA (Need white text on black background)
        processed_cv = cv2.bitwise_not(processed_cv)
        
        # Connected Components
        num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(processed_cv, connectivity=8)
        
        # Filtering using NumPy LUT (Lookup Table) - Fastest method
        min_area = 10
        
        # stats[:, 4] is area. Index 0 is background.
        areas = stats[:, cv2.CC_STAT_AREA]
        
        # Create a LUT where valid labels = 255, others = 0
        lut = np.zeros(num_labels, dtype=np.uint8)
        
        # Get indices of valid components (area >= min_area), skipping background (index 0)
        valid_indices = np.where(areas >= min_area)[0]
        if 0 in valid_indices:
             valid_indices = valid_indices[valid_indices != 0]
             
        lut[valid_indices] = 255
        
        # Apply LUT
        processed_cv = lut[labels]
        log_and_save("Clean Noise (CCA+LUT)", processed_cv, "05_clean_cca")
        
        # 6. Final Invert (Back to Black text on White BG for OCR)
        processed_cv = cv2.bitwise_not(processed_cv)
        
        # 7. Padding
        processed_cv = cv2.copyMakeBorder(processed_cv, padding, padding, padding, padding, cv2.BORDER_CONSTANT, value=[255,255,255])
        log_and_save("Final Invert & Padding", processed_cv, "06_final")
        
        # Convert back to PIL
        processed_pil = Image.fromarray(processed_cv)
        
        # Execute OCR
        results = await self.ocr_service.extract_text(processed_pil)
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
        
        for bbox_points, text, conf in results:
            if conf < 0.35: continue 
            
            # 1. First, try to correct the text (Spell Check / Dictionary)
            # This helps fix "garbage" that might otherwise be filtered out.
            corrected_text = self.post_processor.correct_text(text)
            
            # Debug change
            if corrected_text != text:
                 print(f"[DEBUG] Speller Corrected: '{text}' -> '{corrected_text}'", flush=True)
                 text = corrected_text

            # 2. Then, Validate (Filter)
            is_valid = ValidationService.is_valid_text(text, self.source)
            if not is_valid:
                print(f"[DEBUG] Filtered out text: '{text}' (Conf: {conf})", flush=True)
                continue
            else:
                 print(f"[DEBUG] Accepted text: '{text}' (Conf: {conf})", flush=True)
            
            pts = np.array(bbox_points)
            left = int(np.min(pts[:, 0]))
            top = int(np.min(pts[:, 1]))
            right = int(np.max(pts[:, 0]))
            bottom = int(np.max(pts[:, 1]))
            
            left = max(0, left); top = max(0, top)
            right = min(w, right); bottom = min(h, bottom)

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
        # Move this BEFORE validation check so we can see what's happening even if no text is found
        try:
            import os
            from PIL import ImageDraw, ImageFont
            
            # Use the preprocessed image (processed_pil) which is Upscaled + Padded
            debug_img = processed_pil.convert("RGB")
            draw = ImageDraw.Draw(debug_img)
            
            # Load a font
            try:
                font = ImageFont.truetype("arial.ttf", 24)
            except:
                font = ImageFont.load_default()

            # We can only draw if we have merged_metadata.
            # But if metadata is empty, we haven't merged yet.
            # Let's try to merge what we have (if anything) or just save the raw image.
            
            items_to_draw = []
            if metadata:
                 # If we have valid metadata, merge and draw it
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
                    text_label = item["original"]
                    draw.text((l, max(0, t - 30)), text_label, fill="blue", font=font)
                except:
                   pass
            
            debug_dir = r"d:\NotWork\NewWork\Overlayr\ProcessingImge"
            if not os.path.exists(debug_dir): os.makedirs(debug_dir)
            
            timestamp = int(time.time() * 1000)
            save_path = os.path.join(debug_dir, f"proc_grouped_{timestamp}.png")
            
            if SAVE_DEBUG_IMAGES:
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

        # Batch Translate via Google (or skip)
        if skip_translation:
             translated_texts = texts_to_translate
        else:
             translated_texts = await self.translator_service.translate(texts_to_translate)
        
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
            # Improved Line Merging
            # 1. Sort by Y
            elements.sort(key=lambda x: x["bbox"][1])
            
            merged = []
            visited = [False] * len(elements)
            
            for i in range(len(elements)):
                if visited[i]: continue
                group = [elements[i]]
                visited[i] = True
                
                # Look for horizontal neighbors
                changed = True
                while changed:
                    changed = False
                    for j in range(len(elements)):
                         if visited[j]: continue
                         
                         # Check against any item in group
                         is_close = False
                         for existing in group:
                             b1 = existing["bbox"]
                             b2 = elements[j]["bbox"]
                             
                             # Strict vertical alignment check (Must be same "line")
                             # Centers must represent the same physical line
                             cy1 = (b1[1]+b1[3])/2
                             cy2 = (b2[1]+b2[3])/2
                             
                             # If vertical center diff is small (< 15px)
                             if abs(cy1 - cy2) < 15:
                                 # And horizontal gap is not too wide (e.g. < 200px)
                                 # This handles spaces between words, giving a full sentence
                                 h_gap = max(0, min(abs(b1[2]-b2[0]), abs(b2[2]-b1[0])))
                                 
                                 # Or check overlap if they are badly segmented
                                 overlap_w = max(0, min(b1[2], b2[2]) - max(b1[0], b2[0]))
                                 
                                 if h_gap < 150 or overlap_w > 0:
                                     is_close = True
                                     break
                         
                         if is_close:
                             group.append(elements[j])
                             visited[j] = True
                             changed = True
                
                merged.append(merge_cluster(group))
            return merged

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

