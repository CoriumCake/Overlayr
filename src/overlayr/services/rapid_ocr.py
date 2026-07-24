import asyncio
from typing import Callable, List, Optional, Tuple

import numpy as np


def _rapidocr_runtime_options(
    available_providers,
    *,
    use_angle_classifier: bool,
):
    """Choose the fastest installed ONNX provider with a safe CPU fallback."""
    available = set(available_providers)
    options = {"use_cls": bool(use_angle_classifier)}

    if "CUDAExecutionProvider" in available:
        options.update(
            {
                "det_use_cuda": True,
                "rec_use_cuda": True,
            }
        )
        if use_angle_classifier:
            options["cls_use_cuda"] = True
        return options, "CUDAExecutionProvider"

    if "DmlExecutionProvider" in available:
        options.update(
            {
                "det_use_dml": True,
                "rec_use_dml": True,
            }
        )
        if use_angle_classifier:
            options["cls_use_dml"] = True
        return options, "DmlExecutionProvider"

    return options, "CPUExecutionProvider"


class RapidOCREngine:
    """
    RapidOCR engine wrapper. Follows the same interface as WindowsOCREngine.
    """

    def __init__(self, *, use_angle_classifier: bool = False):
        print("RapidOCREngine: Init start...", flush=True)
        try:
            import onnxruntime as ort
            from rapidocr_onnxruntime import RapidOCR

            runtime_options, provider = _rapidocr_runtime_options(
                ort.get_available_providers(),
                use_angle_classifier=use_angle_classifier,
            )
            try:
                self.engine = RapidOCR(**runtime_options)
            except Exception as acceleration_error:
                if provider == "CPUExecutionProvider":
                    raise
                print(
                    f"RapidOCREngine: {provider} initialization failed "
                    f"({acceleration_error}); falling back to CPU.",
                    flush=True,
                )
                runtime_options, provider = _rapidocr_runtime_options(
                    ["CPUExecutionProvider"],
                    use_angle_classifier=use_angle_classifier,
                )
                self.engine = RapidOCR(**runtime_options)
            self.execution_provider = provider
            self.accepts_bgr_numpy = True
            self._is_available = True
            print(
                f"RapidOCREngine: Successfully initialized with {provider}.",
                flush=True,
            )
        except ImportError:
            print("RapidOCREngine: rapidocr_onnxruntime not installed.", flush=True)
            self.engine = None
            self.execution_provider = "unavailable"
            self.accepts_bgr_numpy = True
            self._is_available = False
        except Exception as error:
            print(f"RapidOCREngine initialization error: {error}", flush=True)
            self.engine = None
            self.execution_provider = "unavailable"
            self.accepts_bgr_numpy = True
            self._is_available = False

        self._language = "en"
        self._status_callback: Optional[Callable] = None

    def set_callback(self, callback: Callable):
        self._status_callback = callback

    def set_language(self, lang_code: str):
        # RapidOCR generally uses a unified model for multiple languages,
        # but we track language state here as expected by AsyncEngine.
        self._language = lang_code
        print(f"RapidOCREngine: Language set to {lang_code}", flush=True)

    def warmup(self):
        if not self._is_available:
            return
        print("RapidOCREngine: Warming up...", flush=True)
        try:
            # Run a small dummy image
            dummy = np.zeros((100, 100, 3), dtype=np.uint8)
            self.engine(dummy)
            print("RapidOCREngine: Warmup complete.", flush=True)
        except Exception as e:
            print(f"RapidOCREngine Warmup Error: {e}", flush=True)

    async def extract_text(self, image) -> List[Tuple[List[List[int]], str, float]]:
        """
        Executes OCR on the image.
        Returns a list of (bbox, text, confidence) where bbox is [[x,y], [x,y], [x,y], [x,y]].
        """
        if not self._is_available:
            print("RapidOCREngine is not available.", flush=True)
            return []

        if isinstance(image, np.ndarray):
            img_cv = image
        else:
            # Convert PIL to CV2 BGR when called outside AsyncEngine.
            img_cv = np.array(image.convert("RGB"))
            img_cv = img_cv[:, :, ::-1].copy()

        # Run OCR in a background thread to prevent blocking async loop
        results = await asyncio.to_thread(self._run_ocr_sync, img_cv)
        return results

    def _run_ocr_sync(self, img_cv: np.ndarray) -> List[Tuple[List[List[int]], str, float]]:
        try:
            ocr_result, elapse = self.engine(img_cv)
        except Exception as e:
            print(f"RapidOCREngine extraction error: {e}", flush=True)
            return []

        formatted_results = []
        if ocr_result:
            for item in ocr_result:
                # item format: [box, text, confidence]
                box = item[0]
                text = item[1]
                conf = item[2]
                formatted_results.append((box, text, float(conf)))

        return formatted_results
