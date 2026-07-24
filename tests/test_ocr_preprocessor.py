from overlayr.services import ocr_preprocessor


def test_ort_provider_selection_uses_only_installed_accelerators(monkeypatch):
    class FakeORT:
        @staticmethod
        def get_available_providers():
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]

    monkeypatch.setattr(ocr_preprocessor, "HAS_ORT", True)
    monkeypatch.setattr(ocr_preprocessor, "ort", FakeORT)

    assert ocr_preprocessor._preferred_ort_providers() == [
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]
