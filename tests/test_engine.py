import pytest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
from overlayr.core.engine import AsyncEngine

@pytest.fixture
def mock_ocr():
    with patch('src.overlayr.core.engine.WindowsOCREngine') as mock:
        yield mock

@pytest.fixture
def mock_translator():
    with patch('src.overlayr.core.engine.GoogleTranslateService') as mock:
        yield mock

@pytest.fixture
def mock_validation():
    with patch('src.overlayr.core.engine.ValidationService') as mock:
        mock.is_valid_text.return_value = True
        yield mock

@pytest.mark.asyncio
async def test_engine_process_flow(mock_ocr, mock_translator, mock_validation):
    # Setup mocks
    ocr_instance = mock_ocr.return_value
    ocr_instance.extract_text = AsyncMock(return_value=[
        ([[0, 0], [10, 0], [10, 10], [0, 10]], "Hello", 0.9)
    ])
    
    trans_instance = mock_translator.return_value
    trans_instance.translate = AsyncMock(return_value=["Hola"])

    engine = AsyncEngine()
    # Force reinject mocks because Singleton might hold old instances
    engine.ocr_service = ocr_instance
    engine.translator_service = trans_instance
    
    mock_img = MagicMock()
    mock_img.size = (100, 100)
    mock_img.crop.return_value = MagicMock()
    
    results, raw_text = await engine.process_image(mock_img)
    
    assert len(results) == 1
    assert results[0]["original"] == "Hello"
    assert results[0]["translated"] == "Hola"
    assert "Hello" in raw_text
