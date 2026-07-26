import asyncio
import os
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np

_DET_MODEL = "PP-OCRv6_det_small.onnx"
_REC_SMALL_MODEL = "PP-OCRv6_rec_small.onnx"
_REC_MEDIUM_MODEL = "PP-OCRv6_rec_medium.onnx"
_CLS_MODEL = "ch_ppocr_mobile_v2.0_cls_mobile.onnx"


def _balanced_inference_threads(cpu_count: Optional[int] = None) -> int:
    """Leave enough CPU capacity for Windows and the capture/render threads."""
    available = cpu_count if cpu_count is not None else os.cpu_count()
    available = max(1, int(available or 1))
    return max(1, min(4, available // 2))


def _default_model_dir() -> Path:
    """Resolve bundled models in both source and PyInstaller executions."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        frozen_models = Path(frozen_root) / "models"
        if frozen_models.exists():
            return frozen_models

    project_models = Path(__file__).resolve().parents[3] / "models"
    if project_models.exists():
        return project_models

    return Path.cwd() / "models"


def _rapidocr_model_options(
    model_dir: Path,
    *,
    use_angle_classifier: bool,
):
    """Use a fast detector and the most accurate bundled v6 recognizer."""
    model_dir = Path(model_dir).resolve()
    det_path = model_dir / _DET_MODEL
    rec_medium_path = model_dir / _REC_MEDIUM_MODEL
    rec_small_path = model_dir / _REC_SMALL_MODEL

    missing = []
    if not det_path.is_file():
        missing.append(det_path)
    if rec_medium_path.is_file():
        rec_path = rec_medium_path
        rec_tier = "medium"
    elif rec_small_path.is_file():
        rec_path = rec_small_path
        rec_tier = "small"
    else:
        missing.extend((rec_medium_path, rec_small_path))

    cls_path = model_dir / _CLS_MODEL
    if use_angle_classifier and not cls_path.is_file():
        missing.append(cls_path)

    if missing:
        missing_names = ", ".join(path.name for path in missing)
        raise FileNotFoundError(
            f"RapidOCR model files are missing from {model_dir}: {missing_names}"
        )

    options = {
        "Global.model_root_dir": str(model_dir),
        "Global.use_cls": bool(use_angle_classifier),
        "Global.log_level": "warning",
        # Keep full-screen work bounded; this measured more accurately on the
        # compact FTB pixel font than sending the full 2560px frame unchanged.
        "Global.max_side_len": 2000,
        "Det.model_path": str(det_path),
        "Rec.model_path": str(rec_path),
    }
    if use_angle_classifier:
        options["Cls.model_path"] = str(cls_path)

    profile = f"PP-OCRv6-small-det/{rec_tier}-rec"
    return options, profile


def _rapidocr_runtime_options(
    available_providers,
    *,
    use_angle_classifier: bool,
    use_directml: bool = False,
    cpu_count: Optional[int] = None,
):
    """Build RapidOCR v3 runtime parameters with a safe CPU fallback."""
    del use_angle_classifier  # Runtime acceleration applies to all enabled stages.
    available = set(available_providers)
    options = {
        "EngineConfig.onnxruntime.intra_op_num_threads": (
            _balanced_inference_threads(cpu_count)
        ),
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    }

    if "CUDAExecutionProvider" in available:
        options["EngineConfig.onnxruntime.use_cuda"] = True
        return options, "CUDAExecutionProvider"

    if use_directml and "DmlExecutionProvider" in available:
        options["EngineConfig.onnxruntime.use_dml"] = True
        return options, "DmlExecutionProvider"

    return options, "CPUExecutionProvider"


class RapidOCREngine:
    """
    RapidOCR engine wrapper. Follows the same interface as WindowsOCREngine.
    """

    def __init__(
        self,
        *,
        use_angle_classifier: bool = False,
        use_directml: bool = True,
    ):
        print("RapidOCREngine: Init start...", flush=True)
        try:
            import onnxruntime as ort
            from rapidocr import RapidOCR

            runtime_options, provider = _rapidocr_runtime_options(
                ort.get_available_providers(),
                use_angle_classifier=use_angle_classifier,
                use_directml=use_directml,
            )
            model_options, model_profile = _rapidocr_model_options(
                _default_model_dir(),
                use_angle_classifier=use_angle_classifier,
            )
            engine_options = {**model_options, **runtime_options}
            try:
                self.engine = RapidOCR(params=engine_options)
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
                    use_directml=False,
                )
                engine_options = {**model_options, **runtime_options}
                self.engine = RapidOCR(params=engine_options)
            self.execution_provider = provider
            self.model_profile = model_profile
            self.accepts_bgr_numpy = True
            self._is_available = True
            print(
                "RapidOCREngine: Successfully initialized "
                f"{model_profile} with {provider}.",
                flush=True,
            )
        except ImportError:
            print("RapidOCREngine: rapidocr is not installed.", flush=True)
            self.engine = None
            self.execution_provider = "unavailable"
            self.model_profile = "unavailable"
            self.accepts_bgr_numpy = True
            self._is_available = False
        except Exception as error:
            print(f"RapidOCREngine initialization error: {error}", flush=True)
            self.engine = None
            self.execution_provider = "unavailable"
            self.model_profile = "unavailable"
            self.accepts_bgr_numpy = True
            self._is_available = False

        self._language = "en"
        self._status_callback: Optional[Callable] = None

    def set_callback(self, callback: Callable):
        self._status_callback = callback

    def set_language(self, lang_code: str):
        # PP-OCRv6 medium/small share one 50-language recognition model.
        self._language = lang_code
        print(f"RapidOCREngine: Language set to {lang_code}", flush=True)

    def warmup(self):
        if not self._is_available:
            return
        print("RapidOCREngine: Warming up...", flush=True)
        try:
            dummy = np.zeros((320, 640, 3), dtype=np.uint8)
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
            ocr_result = self.engine(img_cv)
        except Exception as e:
            print(f"RapidOCREngine extraction error: {e}", flush=True)
            return []

        boxes = getattr(ocr_result, "boxes", None)
        texts = getattr(ocr_result, "txts", None)
        scores = getattr(ocr_result, "scores", None)
        if boxes is None or texts is None or scores is None:
            return []

        return [
            (np.asarray(box).tolist(), str(text), float(score))
            for box, text, score in zip(boxes, texts, scores)
        ]
