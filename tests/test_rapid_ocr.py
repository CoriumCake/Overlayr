import numpy as np
import pytest

from overlayr.services.rapid_ocr import RapidOCREngine, _rapidocr_runtime_options


def test_rapidocr_uses_cuda_when_available():
    options, provider = _rapidocr_runtime_options(
        ["CUDAExecutionProvider", "CPUExecutionProvider"],
        use_angle_classifier=False,
    )

    assert provider == "CUDAExecutionProvider"
    assert options == {
        "use_cls": False,
        "det_use_cuda": True,
        "rec_use_cuda": True,
    }


def test_rapidocr_uses_directml_when_cuda_is_unavailable():
    options, provider = _rapidocr_runtime_options(
        ["DmlExecutionProvider", "CPUExecutionProvider"],
        use_angle_classifier=False,
    )

    assert provider == "DmlExecutionProvider"
    assert options == {
        "use_cls": False,
        "det_use_dml": True,
        "rec_use_dml": True,
    }


def test_rapidocr_cpu_fallback_keeps_horizontal_fast_path():
    options, provider = _rapidocr_runtime_options(
        ["CPUExecutionProvider"],
        use_angle_classifier=False,
    )

    assert provider == "CPUExecutionProvider"
    assert options == {"use_cls": False}


def test_rapidocr_accelerates_classifier_only_when_enabled():
    options, provider = _rapidocr_runtime_options(
        ["DmlExecutionProvider", "CPUExecutionProvider"],
        use_angle_classifier=True,
    )

    assert provider == "DmlExecutionProvider"
    assert options["use_cls"] is True
    assert options["cls_use_dml"] is True


@pytest.mark.asyncio
async def test_rapidocr_reuses_preconverted_bgr_frame():
    engine = RapidOCREngine.__new__(RapidOCREngine)
    engine._is_available = True
    captured = {}

    def fake_ocr(image):
        captured["image"] = image
        return []

    engine._run_ocr_sync = fake_ocr
    frame = np.zeros((24, 32, 3), dtype=np.uint8)

    assert await engine.extract_text(frame) == []
    assert captured["image"] is frame
