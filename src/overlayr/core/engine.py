import asyncio
from typing import Optional, Tuple, Callable, List, Dict, Any
from PIL import Image
import numpy as np
from ..services.windows_ocr import WindowsOCREngine
from ..services.google_translate import GoogleTranslateService
from ..services.validation_service import ValidationService
from ..utils.singleton import SingletonMeta
from PIL import ImageEnhance, ImageFilter

class AsyncEngine(metaclass=SingletonMeta):
    """
    Orchestrates OCR and Translation asynchronously.
    Now using Windows OCR + Google Translate for maximum speed.
    """
    def __init__(self) -> None:
        print("AsyncEngine: Init start...", flush=True)
        self.ocr_service = WindowsOCREngine()
        print("AsyncEngine: Windows OCR initialized.", flush=True)
        self.translator_service = GoogleTranslateService()
        print("AsyncEngine: Google Translate initialized.", flush=True)
        self._is_running = False
        self._loop_task: Optional[asyncio.Task] = None
        
        # Current language state
        self.source = "en"
        self.target = "th"
        
        print("AsyncEngine: Init done.", flush=True)

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop

    def _handle_task_result(self, future):
        try:
            future.result()
        except Exception as e:
            print(f"Async Task Error: {e}", flush=True)

    def warmup(self):
        """Pre-warm the OCR engine."""
        self.ocr_service.warmup()

    def update_languages(self, source_code: str, target_code: str):
        # Update Translator
        if hasattr(self, 'loop') and self.loop.is_running():
             future = asyncio.run_coroutine_threadsafe(
                self.translator_service.load_model(source_code, target_code),
                self.loop
             )
             future.add_done_callback(self._handle_task_result)
        else:
             print("AsyncEngine: Loop not ready for language update.", flush=True)
        
        # Update OCR language
        self.ocr_service.set_language(source_code)
        
        # Store for internal use (noise filtering logic)
        self.source = source_code
        self.target = target_code

    async def process_image(self, image: Image.Image) -> Tuple[List[Dict[str, Any]], str]:
        """
        Runs OCR and translates text using fast Windows+Google APIs.
        """
        import time
        t0 = time.time()
        
        # Preprocessing: Convert to grayscale and enhance contrast for better OCR
        processed = image.convert("L")
        # Reduced from 2.0 to 1.5 to avoid "hard" edges that create noise
        processed = ImageEnhance.Contrast(processed).enhance(1.5)
        # Sharpening can sometimes amplify artifacts, keeping it subtle
        processed = processed.filter(ImageFilter.SHARPEN)
        
        # Windows OCR works better with some contrast
        results = await self.ocr_service.extract_text(processed)
        t1 = time.time()
        
        if not results:
            return [], ""
            
        texts_to_translate = []
        metadata = []
        
        # Capture all raw text before filtering for debugging
        raw_parts = [t for _, t, _ in results]
        raw_text = f"[{len(results)} raw items] {' | '.join(raw_parts)}"[:500] 

        # Prepare batch
        for bbox_points, text, conf in results:
            if conf < 0.35: continue 
            
            if not ValidationService.is_valid_text(text, self.source):
                continue
            
            pts = np.array(bbox_points)
            left = int(np.min(pts[:, 0]))
            top = int(np.min(pts[:, 1]))
            right = int(np.max(pts[:, 0]))
            bottom = int(np.max(pts[:, 1]))
            
            w, h = image.size
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

        if not metadata:
            return [], raw_text

        # Merge spatially close blocks
        merged_metadata = self._merge_text_blocks(metadata)
        
        texts_to_translate = [m["original"] for m in merged_metadata]

        # Batch Translate via Google
        translated_texts = await self.translator_service.translate(texts_to_translate)
        
        # Recombine
        final_results = []
        if isinstance(translated_texts, str): translated_texts = [translated_texts]
        
        for i, meta in enumerate(merged_metadata):
            meta["translated"] = translated_texts[i]
            final_results.append(meta)
            
        t2 = time.time()
        print(f"OCR: {t1-t0:.2f}s | Translate: {t2-t1:.2f}s | Items: {len(final_results)}", flush=True)
        
        return final_results, raw_text

    def _merge_text_blocks(self, elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Merges text boxes that are physically close together into single blocks.
        Helps translate full sentences instead of fragments.
        """
        if not elements: return []
        
        # Sort by vertical position first
        elements.sort(key=lambda x: x["bbox"][1])
        
        merged = []
        visited = [False] * len(elements)
        
        x_threshold = 100 # pixels horizontally
        y_threshold = 40  # pixels vertically (line height)
        
        for i in range(len(elements)):
            if visited[i]: continue
            
            # Start a new group
            group = [elements[i]]
            visited[i] = True
            
            # Look for others to add to this group
            changed = True
            while changed:
                changed = False
                for j in range(len(elements)):
                    if visited[j]: continue
                    
                    # Check if elements[j] is close to ANY element currently in group
                    is_close = False
                    for existing in group:
                        b1 = existing["bbox"]
                        b2 = elements[j]["bbox"]
                        
                        # Vertical distance between centers or edges
                        v_dist = abs(b1[1] - b2[1]) 
                        # Horizontal distance between right edge of one and left of other
                        h_dist = min(abs(b1[2] - b2[0]), abs(b2[2] - b1[0]))
                        
                        # If same line (ish) and close horizontally
                        if v_dist < y_threshold and h_dist < x_threshold:
                            is_close = True
                            break
                        
                        # Or if they overlap vertically (multiline block)
                        y_overlap = min(b1[3], b2[3]) - max(b1[1], b2[1])
                        if y_overlap > 0 and h_dist < x_threshold:
                            is_close = True
                            break
                            
                    if is_close:
                        group.append(elements[j])
                        visited[j] = True
                        changed = True
            
            # Merge the group
            group.sort(key=lambda x: (x["bbox"][1], x["bbox"][0])) # Sort top-to-bottom, left-to-right
            
            all_text = " ".join(item["original"] for item in group)
            
            # New bounding box
            min_l = min(item["bbox"][0] for item in group)
            min_t = min(item["bbox"][1] for item in group)
            max_r = max(item["bbox"][2] for item in group)
            max_b = max(item["bbox"][3] for item in group)
            
            merged.append({
                "bbox": [min_l, min_t, max_r, max_b],
                "original": all_text,
                "bg_color": group[0]["bg_color"] # Take from first item
            })
            
        return merged

    def stop(self) -> None:
        self._is_running = False
        if self._loop_task:
            self._loop_task.cancel()

