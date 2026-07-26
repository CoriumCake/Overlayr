"""Tests for GoogleTranslateService — caching, parallel translation, error handling."""

from unittest.mock import patch

import pytest

from overlayr.services.google_translate import GoogleTranslateService


@pytest.fixture
def service():
    return GoogleTranslateService(source="en", target="th")


class TestCaching:
    @pytest.mark.asyncio
    async def test_cached_results_not_retranslated(self, service):
        """After translating text once, subsequent calls should use cache."""
        with patch.object(service, "_translate_single", return_value="สวัสดี") as mock:
            result1 = await service.translate("Hello")
            result2 = await service.translate("Hello")

            assert result1 == "สวัสดี"
            assert result2 == "สวัสดี"
            assert mock.call_count == 1  # Only called once, second was cached

    @pytest.mark.asyncio
    async def test_cache_cleared_on_language_change(self, service):
        """Changing languages should clear the cache."""
        with patch.object(service, "_translate_single", return_value="สวัสดี"):
            await service.translate("Hello")
            assert len(service._cache) == 1

            await service.load_model("en", "ja")
            assert len(service._cache) == 0

    @pytest.mark.asyncio
    async def test_cache_bounded(self, service):
        """Cache should not grow beyond _MAX_CACHE_SIZE."""
        from overlayr.services.google_translate import _MAX_CACHE_SIZE

        with patch.object(service, "_translate_single", side_effect=lambda t: f"translated_{t}"):
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
        with patch.object(service, "_translate_single", return_value="สวัสดี"):
            result = await service.translate("Hello")
            assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_list_returns_list(self, service):
        with patch.object(
            service,
            "_translate_batch_single_request",
            return_value=["สวัสดี", "โลก"],
        ) as mock:
            result = await service.translate(["Hello", "World"])
            assert isinstance(result, list)
            assert len(result) == 2
            assert mock.call_count == 1


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_translate_error_returns_none_without_caching_source(self, service):
        """A failed request must not make the English source look translated."""
        service.max_attempts = 1
        with patch.object(service, "_translate_single", side_effect=Exception("API Error")):
            result = await service.translate("Hello")
            assert result is None
            assert len(service._cache) == 0

    @pytest.mark.asyncio
    async def test_transient_error_is_retried_then_cached(self, service):
        service.retry_backoff_seconds = 0
        with patch.object(
            service,
            "_translate_single",
            side_effect=[ConnectionError("temporary"), "สวัสดี"],
        ) as mock:
            result = await service.translate("Hello")

        assert result == "สวัสดี"
        assert mock.call_count == 2
        assert len(service._cache) == 1

    @pytest.mark.asyncio
    async def test_partial_batch_failure_preserves_successful_items(self, service):
        service.max_attempts = 1
        service.batch_translation = False
        with patch.object(
            service,
            "_translate_single",
            side_effect=["สวัสดี", ConnectionError("temporary")],
        ):
            result = await service.translate(["Hello", "World"])

        assert result == ["สวัสดี", None]
        assert len(service._cache) == 1


class TestDomainGlossary:
    @pytest.mark.asyncio
    async def test_ftb_rewards_ocr_error_is_corrected_without_network(self, service):
        with patch.object(service, "_translate_single") as mock:
            result = await service.translate(["Tasks", "Fiends"])

        assert result == ["ภารกิจ", "รางวัล"]
        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_minecraft_labels_use_glossary_without_network(self, service):
        with patch.object(service, "_translate_single") as mock:
            result = await service.translate(["Chest", "Minecraft"])

        assert result == ["หีบ", "Minecraft"]
        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_generic_chest_translation_does_not_reuse_game_cache(self, service):
        await service.translate(["Chest", "Minecraft"])

        with patch.object(service, "_translate_single", return_value="หน้าอก"):
            result = await service.translate(
                "Chest",
                context_texts=["Chest pain", "Medical notes"],
            )

        assert result == "หน้าอก"

    @pytest.mark.asyncio
    async def test_ftb_sentence_canonicalizes_watering_can_term(self, service):
        source = (
            "Fill a wooden watering can with water and use it to water "
            "your plants."
        )
        machine_translation = "เติมน้ำลงในกระป๋องไม้แล้วใช้รดน้ำต้นไม้"
        with patch.object(
            service,
            "_translate_single",
            return_value=machine_translation,
        ):
            result = await service.translate(
                source,
                context_texts=["Tasks", "Rewards", source],
            )

        assert result == "เติมน้ำลงในบัวรดน้ำไม้แล้วใช้รดน้ำต้นไม้"
