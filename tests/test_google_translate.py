"""Tests for GoogleTranslateService — caching, parallel translation, error handling."""
import pytest
import asyncio
from unittest.mock import patch, MagicMock
from overlayr.services.google_translate import GoogleTranslateService


@pytest.fixture
def service():
    return GoogleTranslateService(source="en", target="th")


class TestCaching:
    @pytest.mark.asyncio
    async def test_cached_results_not_retranslated(self, service):
        """After translating text once, subsequent calls should use cache."""
        with patch.object(service, '_translate_single', return_value="สวัสดี") as mock:
            result1 = await service.translate("Hello")
            result2 = await service.translate("Hello")
            
            assert result1 == "สวัสดี"
            assert result2 == "สวัสดี"
            assert mock.call_count == 1  # Only called once, second was cached

    @pytest.mark.asyncio
    async def test_cache_cleared_on_language_change(self, service):
        """Changing languages should clear the cache."""
        with patch.object(service, '_translate_single', return_value="สวัสดี"):
            await service.translate("Hello")
            assert len(service._cache) == 1
            
            await service.load_model("en", "ja")
            assert len(service._cache) == 0

    @pytest.mark.asyncio
    async def test_cache_bounded(self, service):
        """Cache should not grow beyond _MAX_CACHE_SIZE."""
        from overlayr.services.google_translate import _MAX_CACHE_SIZE
        
        with patch.object(service, '_translate_single', side_effect=lambda t: f"translated_{t}"):
            texts = [f"text_{i}" for i in range(_MAX_CACHE_SIZE + 100)]
            await service.translate(texts)
            assert len(service._cache) <= _MAX_CACHE_SIZE


class TestTranslation:
    @pytest.mark.asyncio
    async def test_empty_string_returns_empty(self, service):
        result = await service.translate("")
        assert result == ""

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty_list(self, service):
        result = await service.translate([])
        assert result == []

    @pytest.mark.asyncio
    async def test_same_language_bypass(self, service):
        """If source == target, return input directly."""
        service.source = "en"
        service.target = "en"
        result = await service.translate("Hello")
        assert result == "Hello"

    @pytest.mark.asyncio
    async def test_single_string_returns_string(self, service):
        with patch.object(service, '_translate_single', return_value="สวัสดี"):
            result = await service.translate("Hello")
            assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_list_returns_list(self, service):
        with patch.object(service, '_translate_single', side_effect=["สวัสดี", "โลก"]):
            result = await service.translate(["Hello", "World"])
            assert isinstance(result, list)
            assert len(result) == 2


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_translate_error_returns_original(self, service):
        """On error, return original text instead of crashing."""
        with patch.object(service, '_translate_single', side_effect=Exception("API Error")):
            result = await service.translate("Hello")
            assert result == "Hello"
