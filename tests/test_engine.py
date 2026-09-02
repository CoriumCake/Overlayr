import pytest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
from PIL import Image
from overlayr.core.engine import AsyncEngine

@pytest.fixture
def mock_ocr():
    with patch('overlayr.core.engine.WindowsOCREngine') as mock:
        yield mock

@pytest.fixture
def mock_translator():
    with patch('overlayr.core.engine.GoogleTranslateService') as mock:
        yield mock

@pytest.fixture
def mock_validation():
    with patch('overlayr.core.engine.ValidationService') as mock:
        mock.is_valid_text.return_value = True
        yield mock

@pytest.mark.asyncio
async def test_engine_process_flow(mock_ocr, mock_translator, mock_validation):
    # Setup mocks
    ocr_instance = mock_ocr.return_value
    # The engine asks for the regions alongside the readings: the grouper needs
    # them to know what it may not merge across.
    ocr_instance.extract_text_with_regions = AsyncMock(return_value=(
        [([[0, 0], [10, 0], [10, 10], [0, 10]], "Hello", 0.9)],
        [],
    ))


    trans_instance = mock_translator.return_value
    trans_instance.translate = AsyncMock(return_value=["Hola"])

    engine = AsyncEngine()
    engine.ocr_service = ocr_instance
    engine.translator_service = trans_instance
    engine.source = 'en'  # Required by ValidationService.is_valid_text
    
    # Use a real PIL Image so np.array() produces a proper 3D array
    mock_img = Image.new("RGB", (100, 100), color=(255, 255, 255))
    
    results, raw_text = await engine.process_image(mock_img)
    
    assert len(results) >= 1
    assert any("hello" in r["original"].lower() for r in results)
    assert "hello" in raw_text.lower()


def _engine_reading(text, mock_ocr, mock_translator):
    """An engine whose OCR returns one line and whose translator is a spy."""
    ocr_instance = mock_ocr.return_value
    ocr_instance.extract_text_with_regions = AsyncMock(return_value=(
        [([[0, 0], [10, 0], [10, 10], [0, 10]], text, 0.9)],
        [],
    ))
    trans_instance = mock_translator.return_value
    trans_instance.translate = AsyncMock(return_value=["translated"])

    engine = AsyncEngine()
    engine.ocr_service = ocr_instance
    engine.translator_service = trans_instance
    engine.source = 'en'
    return engine, trans_instance


@pytest.mark.asyncio
async def test_a_stale_frame_is_never_translated(mock_ocr, mock_translator,
                                                 mock_validation):
    """Translation is the expensive half; a frame nobody will see skips it.

    The check sits after reading and grouping because that is the last moment
    before the cost is committed - once the request is out, it is paid for.
    """
    engine, translator = _engine_reading("Hello", mock_ocr, mock_translator)
    image = Image.new("RGB", (100, 100), color=(255, 255, 255))

    results, _ = await engine.process_image(image, skip_translation=False,
                                            should_continue=lambda: False)

    translator.translate.assert_not_called()
    assert results == []


@pytest.mark.asyncio
async def test_a_current_frame_is_still_translated(mock_ocr, mock_translator,
                                                   mock_validation):
    engine, translator = _engine_reading("Hello", mock_ocr, mock_translator)
    image = Image.new("RGB", (100, 100), color=(255, 255, 255))

    results, _ = await engine.process_image(image, skip_translation=False,
                                            should_continue=lambda: True)

    translator.translate.assert_awaited_once()
    assert len(results) == 1


@pytest.mark.asyncio
async def test_without_the_check_nothing_changes(mock_ocr, mock_translator,
                                                 mock_validation):
    """Callers that do not pass one - the harness among them - are unaffected."""
    engine, translator = _engine_reading("Hello", mock_ocr, mock_translator)
    image = Image.new("RGB", (100, 100), color=(255, 255, 255))

    results, _ = await engine.process_image(image, skip_translation=False)

    translator.translate.assert_awaited_once()
    assert len(results) == 1
