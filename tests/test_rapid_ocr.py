from types import SimpleNamespace

import numpy as np
import pytest

from overlayr.services.rapid_ocr import (
    RapidOCREngine,
    _balanced_inference_threads,
    _rapidocr_model_options,
    _rapidocr_runtime_options,
)


def test_rapidocr_uses_cuda_when_available():
    options, provider = _rapidocr_runtime_options(
        ["CUDAExecutionProvider", "CPUExecutionProvider"],
        use_angle_classifier=False,
        cpu_count=16,
    )

    assert provider == "CUDAExecutionProvider"
    assert options == {
        "EngineConfig.onnxruntime.intra_op_num_threads": 4,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "EngineConfig.onnxruntime.use_cuda": True,
    }


def test_rapidocr_skips_directml_by_default():
    options, provider = _rapidocr_runtime_options(
        ["DmlExecutionProvider", "CPUExecutionProvider"],
        use_angle_classifier=False,
        cpu_count=16,
    )

    assert provider == "CPUExecutionProvider"
    assert options == {
        "EngineConfig.onnxruntime.intra_op_num_threads": 4,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    }


def test_rapidocr_uses_directml_when_explicitly_enabled():
    options, provider = _rapidocr_runtime_options(
        ["DmlExecutionProvider", "CPUExecutionProvider"],
        use_angle_classifier=False,
        use_directml=True,
        cpu_count=16,
    )

    assert provider == "DmlExecutionProvider"
    assert options == {
        "EngineConfig.onnxruntime.intra_op_num_threads": 4,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "EngineConfig.onnxruntime.use_dml": True,
    }


def test_rapidocr_cpu_fallback_keeps_horizontal_fast_path():
    options, provider = _rapidocr_runtime_options(
        ["CPUExecutionProvider"],
        use_angle_classifier=False,
        cpu_count=8,
    )

    assert provider == "CPUExecutionProvider"
    assert options == {
        "EngineConfig.onnxruntime.intra_op_num_threads": 4,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    }


def test_rapidocr_accelerates_classifier_only_when_enabled():
    options, provider = _rapidocr_runtime_options(
        ["DmlExecutionProvider", "CPUExecutionProvider"],
        use_angle_classifier=True,
        use_directml=True,
        cpu_count=4,
    )

    assert provider == "DmlExecutionProvider"
    assert options["EngineConfig.onnxruntime.use_dml"] is True
    assert options["EngineConfig.onnxruntime.intra_op_num_threads"] == 2


@pytest.mark.parametrize(
    ("cpu_count", "expected"),
    [(1, 1), (2, 1), (4, 2), (8, 4), (16, 4)],
)
def test_inference_threads_reserve_capacity_for_windows(cpu_count, expected):
    assert _balanced_inference_threads(cpu_count) == expected


def test_model_options_prefer_medium_recognizer(tmp_path):
    (tmp_path / "PP-OCRv6_det_small.onnx").touch()
    (tmp_path / "PP-OCRv6_rec_small.onnx").touch()
    (tmp_path / "PP-OCRv6_rec_medium.onnx").touch()
    (tmp_path / "ch_ppocr_mobile_v2.0_cls_mobile.onnx").touch()

    options, profile = _rapidocr_model_options(
        tmp_path,
        use_angle_classifier=True,
    )

    assert options["Det.model_path"].endswith("PP-OCRv6_det_small.onnx")
    assert options["Rec.model_path"].endswith("PP-OCRv6_rec_medium.onnx")
    assert options["Cls.model_path"].endswith(
        "ch_ppocr_mobile_v2.0_cls_mobile.onnx"
    )
    assert options["Global.use_cls"] is True
    assert profile == "PP-OCRv6-small-det/medium-rec"


def test_model_options_fall_back_to_small_recognizer(tmp_path):
    (tmp_path / "PP-OCRv6_det_small.onnx").touch()
    (tmp_path / "PP-OCRv6_rec_small.onnx").touch()

    options, profile = _rapidocr_model_options(
        tmp_path,
        use_angle_classifier=False,
    )

    assert options["Rec.model_path"].endswith("PP-OCRv6_rec_small.onnx")
    assert options["Global.use_cls"] is False
    assert "Cls.model_path" not in options
    assert profile == "PP-OCRv6-small-det/small-rec"


def test_model_options_report_missing_required_models(tmp_path):
    with pytest.raises(FileNotFoundError, match="PP-OCRv6_det_small.onnx"):
        _rapidocr_model_options(tmp_path, use_angle_classifier=False)


def test_rapidocr_formats_v3_output():
    engine = RapidOCREngine.__new__(RapidOCREngine)
    engine.engine = lambda _image: SimpleNamespace(
        boxes=np.array(
            [
                [[1, 2], [20, 2], [20, 10], [1, 10]],
                [[3, 12], [30, 12], [30, 20], [3, 20]],
            ]
        ),
        txts=("Rewards", "Pure daisy"),
        scores=(0.999, 0.987),
    )

    result = engine._run_ocr_sync(np.zeros((24, 32, 3), dtype=np.uint8))

    assert result == [
        ([[1, 2], [20, 2], [20, 10], [1, 10]], "Rewards", 0.999),
        ([[3, 12], [30, 12], [30, 20], [3, 20]], "Pure daisy", 0.987),
    ]


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
