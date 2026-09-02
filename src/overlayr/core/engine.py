import asyncio
from typing import Optional, Tuple, Callable, List, Dict, Any
from PIL import Image
import numpy as np
from ..services.windows_ocr import WindowsOCREngine
from ..services.google_translate import GoogleTranslateService
from ..services.validation_service import ValidationService
from ..utils.singleton import SingletonMeta
from .block_colors import estimate_block_colors
from .grouper import get_grouper
from .scene import uses_spell_correction
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

        # Current language state
        self.source = "en"
        self.target = "th"

    def set_status_callback(self, callback):
        """Pass UI status callback to services."""
        if hasattr(self.ocr_service, 'set_callback'):
            self.ocr_service.set_callback(callback)

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
        """Update both translator and OCR languages. `source`/`target` are UI codes
        such as "en", "ja", "ko", "th", "zh-CN"; WindowsOCREngine maps them to BCP-47."""
        self.source = source
        self.target = target

        await self.translator_service.load_model(source, target)
        self.ocr_service.set_language(source)

    async def process_image(self, image: Image.Image, step_callback: Optional[Callable] = None, skip_translation: bool = True, merge_mode: str = "paragraph", scene=None, should_continue: Optional[Callable[[], bool]] = None) -> Tuple[List[Dict[str, Any]], str]:
        """
        Runs OCR and translates text using fast Windows+Google APIs.
        Supports step_callback(stage_name, data) for debugging flow.
        merge_mode: 'paragraph' (group lines) or 'line' (separate lines)
        scene: SceneType from the classifier; picks the OCR profile and the
               region finder. None falls back to reading the whole frame.
        should_continue: asked once, after reading and grouping but before
               translating, whether the answer is still wanted. Translation is
               the expensive half and it cannot be interrupted once started, so
               this is the last point where a frame the screen has moved past
               can be abandoned instead of paid for.
        """
        import time
        t0 = time.time()
        
        # --- OCR Execution ---
        
        # Execute OCR
        ocr_start = time.perf_counter()
        results, regions = await self.ocr_service.extract_text_with_regions(image, scene=scene)
        ocr_dur = (time.perf_counter() - ocr_start) * 1000
        print(f"[DEBUG] OCR Processing: {ocr_dur:.2f} ms", flush=True)
        t1 = time.time()
        
        # Post-Process Text
        spell_correct = uses_spell_correction(scene)
        final_results = []
        for bbox_points, text, conf in results:
            cleaned_text = self.post_processor.clean_text(text)
            final_text = (self.post_processor.correct_text(cleaned_text)
                          if spell_correct else cleaned_text)

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
            # Confidence is now the share of OCR reads that agreed on this text,
            # not a quality score. Low agreement means the techniques read the
            # same pixels differently, not that there is no text there, so the
            # best reading is kept and shown rather than dropped in silence.


            # Spell correction already ran on this text above. Running it a
            # second time compounded its mistakes: each pass rewrites what the
            # previous one produced, so a misread word drifts further from the
            # original instead of back towards it.

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
            
            # The overlay paints over this box, so it needs the paper colour
            # to hide the original words and the ink colour to keep the same
            # contrast direction the reader already had (CLAUDE.md 9.24).
            bg_color_hex, text_color_hex = "#010101", "#ffffff"
            try:
                region = image.crop((left, top, right, bottom))
                if region.width == 0 or region.height == 0: continue
                bg_color_hex, text_color_hex = estimate_block_colors(np.array(region))
            except Exception as e:
                print(f"[DEBUG] Block colour estimate failed for '{text}': "
                      f"{type(e).__name__}: {e}", flush=True)

            texts_to_translate.append(text)
            metadata.append({
                "bbox": [left, top, right, bottom],
                "original": text,
                "bg_color": bg_color_hex,
                "text_color": text_color_hex,
                # Height of the line OCR read, so the translation can be drawn
                # at the size of the words it replaces.
                "line_height": max(1, bottom - top),
            })

        # --- DEBUG SAVE: Processed Image + Grouped Boxes ---
        # Removed as WindowsOCREngine handles its own debug images if needed.

        if not metadata:
            return [], raw_text

        # OCR returns flat lines; the grouper turns them into the blocks that get
        # translated, using the regions as walls it may not merge across.
        merged_metadata = self._merge_text_blocks(metadata, mode=merge_mode,
                                                  regions=regions, scene=scene)
        # DEBUG CALLBACK: MERGE DONE
        if step_callback:
            await step_callback("MERGED", merged_metadata)
        
        texts_to_translate = [m["original"] for m in merged_metadata]

        # Caching lives in GoogleTranslateService and only there. A second cache
        # used to sit in front of it here, which meant the one inside the
        # service was never reached and the two could disagree about a string.
        if should_continue is not None and not should_continue():
            print("[DEBUG] Frame went stale before translation; skipping it.",
                  flush=True)
            return [], raw_text

        if skip_translation:
            translated_texts = list(texts_to_translate)
        else:
            translated_texts = await self.translator_service.translate(texts_to_translate)
            if isinstance(translated_texts, str):
                translated_texts = [translated_texts]

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

    def _merge_text_blocks(self, elements: List[Dict[str, Any]], mode: str = "paragraph",
                           regions=None, scene=None) -> List[Dict[str, Any]]:
        """Group OCR lines into the blocks that get translated.

        The rules live in core/grouper.py, one set per scene, because what
        counts as one block differs completely between a speech bubble, a
        paragraph in a column, and a game dialogue panel.
        """
        grouper = get_grouper(scene, regions=regions, source_lang=self.source,
                              merge_mode=mode)
        return grouper.group(elements)

    def stop(self) -> None:
        self._is_running = False
        if self._loop_task:
            self._loop_task.cancel()

