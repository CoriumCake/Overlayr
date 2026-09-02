"""Tests for GoogleTranslateService - caching, batching, placeholders, glossary."""
import pytest
from unittest.mock import patch

from overlayr.services.google_translate import (GoogleTranslateService, _batches,
                                                _mask, _unmask)


@pytest.fixture
def service():
    return GoogleTranslateService(source="en", target="th")


class TestCaching:
    @pytest.mark.asyncio
    async def test_cached_results_not_retranslated(self, service):
        """After translating text once, subsequent calls should use cache."""
        with patch.object(service, '_translate_sync', return_value="สวัสดี") as mock:
            result1 = await service.translate("Hello")
            result2 = await service.translate("Hello")

            assert result1 == "สวัสดี"
            assert result2 == "สวัสดี"
            assert mock.call_count == 1  # Only called once, second was cached

    @pytest.mark.asyncio
    async def test_cache_cleared_on_language_change(self, service):
        """Changing languages should clear the cache."""
        with patch.object(service, '_translate_sync', return_value="สวัสดี"):
            await service.translate("Hello")
            assert len(service._cache) == 1

            await service.load_model("en", "ja")
            assert len(service._cache) == 0

    @pytest.mark.asyncio
    async def test_cache_bounded(self, service):
        """Cache should not grow beyond _MAX_CACHE_SIZE."""
        from overlayr.services.google_translate import _MAX_CACHE_SIZE

        with patch.object(service, '_translate_group_sync',
                          side_effect=lambda texts: [f"t_{t}" for t in texts]):
            texts = [f"text {i}" for i in range(_MAX_CACHE_SIZE + 100)]
            await service.translate(texts)
            assert len(service._cache) <= _MAX_CACHE_SIZE


class TestCacheReuse:
    """Reusing an earlier translation is the cheapest work there is.

    A stream returns to the same text constantly - a dialogue box that only
    partly changed, a menu the player reopens, a page scrolled back to - and
    every reuse is a request that never has to be made.
    """

    @pytest.mark.asyncio
    async def test_a_repeated_frame_costs_nothing(self, service):
        frame = ["Place the order in the kitchen", "Continue.", "Player"]
        with patch.object(service, '_request',
                          side_effect=lambda text, **kw:
                          "\n".join(f"TH:{line}" for line in text.split("\n"))) as mock:
            first = await service.translate(frame)
            mock.reset_mock()
            second = await service.translate(frame)

            assert mock.call_count == 0
            assert second == first

    @pytest.mark.asyncio
    async def test_only_the_new_lines_of_a_frame_are_sent(self, service):
        with patch.object(service, '_request',
                          side_effect=lambda text, **kw:
                          "\n".join(f"TH:{line}" for line in text.split("\n"))) as mock:
            await service.translate(["Continue.", "Player"])
            mock.reset_mock()
            await service.translate(["Continue.", "Something new", "Player"])

            sent = "\n".join(call.args[0] for call in mock.call_args_list)
            assert "Something new" in sent
            assert "Continue." not in sent

    @pytest.mark.asyncio
    @pytest.mark.parametrize("variant", ["Player ", "player", "Player."])
    async def test_reuse_requires_an_exact_match(self, service, variant):
        # Anything else would risk showing the translation of a sentence that
        # is not the one on screen.
        with patch.object(service, '_request', return_value="TH") as mock:
            await service.translate(["Player"])
            mock.reset_mock()
            await service.translate([variant])

            assert mock.call_count == 1

    @pytest.mark.asyncio
    async def test_a_line_seen_every_frame_is_never_evicted(self, service):
        # LRU order matters here: the line that keeps coming back is exactly the
        # one that must survive, however much other text scrolls past it.
        from overlayr.services.google_translate import _MAX_CACHE_SIZE

        recurring = "This line is on screen the whole time"
        with patch.object(service, '_request',
                          side_effect=lambda text, **kw:
                          "\n".join(f"TH:{line}" for line in text.split("\n"))) as mock:
            await service.translate([recurring])
            for start in range(0, _MAX_CACHE_SIZE + 200, 200):
                await service.translate([f"filler {n}" for n in range(start, start + 200)])
                await service.translate([recurring])

            assert len(service._cache) <= _MAX_CACHE_SIZE
            mock.reset_mock()
            await service.translate([recurring])
            assert mock.call_count == 0

    @pytest.mark.asyncio
    async def test_setting_a_glossary_invalidates_what_was_cached(self, service):
        # Otherwise a pinned term would keep coming back with its old wording.
        with patch.object(service, '_request', return_value="TH"):
            await service.translate(["Use the Elixir"])
        service.set_glossary({"Elixir": "ยาอมตะ"})

        assert len(service._cache) == 0


class TestTranslation:
    @pytest.mark.asyncio
    async def test_empty_string_returns_empty(self, service):
        assert await service.translate("") == ""

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty_list(self, service):
        assert await service.translate([]) == []

    @pytest.mark.asyncio
    async def test_same_language_bypass(self, service):
        """If source == target, return input directly."""
        service.source = "en"
        service.target = "en"
        assert await service.translate("Hello") == "Hello"

    @pytest.mark.asyncio
    async def test_single_string_returns_string(self, service):
        with patch.object(service, '_translate_sync', return_value="สวัสดี"):
            assert isinstance(await service.translate("Hello"), str)

    @pytest.mark.asyncio
    async def test_list_returns_list(self, service):
        with patch.object(service, '_request', return_value="สวัสดี\nโลก"):
            result = await service.translate(["Hello", "World"])
            assert result == ["สวัสดี", "โลก"]


class TestBatching:
    @pytest.mark.asyncio
    async def test_a_list_costs_one_request_not_one_each(self, service):
        # This is the whole point of batching: six blocks on screen should not
        # mean six round trips to the service.
        with patch.object(service, '_request',
                          return_value="\n".join(f"t{i}" for i in range(6))) as mock:
            await service.translate([f"line {i}" for i in range(6)])
            assert mock.call_count == 1

    @pytest.mark.asyncio
    async def test_a_misaligned_answer_falls_back_to_one_at_a_time(self, service):
        # Attaching a translation to the wrong box is worse than being slow, so
        # a reply with the wrong number of lines is not trusted.
        answers = ["only one line back", "a", "b", "c"]
        with patch.object(service, '_request', side_effect=answers) as mock:
            result = await service.translate(["x", "y", "z"])

            assert result == ["a", "b", "c"]
            assert mock.call_count == 4  # one batch, then three singles

    @pytest.mark.asyncio
    async def test_a_refused_batch_falls_back_to_one_at_a_time(self, service):
        # The free endpoint refuses requests intermittently. A refused batch
        # must not leave the whole frame untranslated when the individual
        # requests would have gone through.
        with patch.object(service, '_request',
                          side_effect=[None, "a", "b", "c"]) as mock:
            result = await service.translate(["x", "y", "z"])

            assert result == ["a", "b", "c"]
            assert mock.call_count == 4

    @pytest.mark.asyncio
    async def test_the_batch_attempt_is_not_retried(self, service):
        # Retrying the batch just delays the fallback that fixes it.
        with patch.object(service, '_request', return_value="a\nb") as mock:
            await service.translate(["x", "y"])
            assert mock.call_args.kwargs.get("attempts") == 1

    def test_batches_respect_the_length_limit(self):
        texts = ["a" * 30] * 10
        groups = _batches(texts, limit=100)
        assert all(sum(len(texts[i]) + 1 for i in g) <= 100 for g in groups)
        assert sorted(i for g in groups for i in g) == list(range(10))

    def test_everything_fits_in_one_batch_when_short(self):
        assert _batches(["a", "b", "c"], limit=1000) == [[0, 1, 2]]


class TestPlaceholderGuard:
    def test_numbers_are_masked(self):
        masked, holders = _mask("Alright fellas 100 burgers", {})
        assert "100" not in masked
        assert holders == ["100"]

    def test_hp_values_are_masked_whole(self):
        _, holders = _mask("HP 240/320", {})
        assert holders == ["240/320"]

    def test_format_specifiers_are_masked(self):
        _, holders = _mask("You gained %d gold and {0} items", {})
        assert holders == ["%d", "{0}"]

    def test_round_trip_restores_exactly(self):
        text = "Deal 25% more damage for 3.5 seconds"
        masked, holders = _mask(text, {})
        assert _unmask(masked, holders) == text

    def test_a_dropped_marker_does_not_crash_the_restore(self):
        assert _unmask("no markers here", ["100"]) == "no markers here"

    def test_an_unknown_marker_index_is_left_alone(self):
        assert _unmask("value ⟦7⟧", ["100"]) == "value ⟦7⟧"

    @pytest.mark.asyncio
    async def test_numbers_survive_a_translation_that_would_reword_them(self, service):
        # SymSpell once turned "100" into "a of"; a translator can do the same.
        # The number must come back exactly as it went in.
        with patch.object(service, '_translate_sync',
                          side_effect=lambda text: text.replace("burgers", "เบอร์เกอร์")):
            result = await service.translate("100 burgers")
            assert "100" in result

    @pytest.mark.asyncio
    async def test_text_that_is_only_a_number_is_never_sent(self, service):
        with patch.object(service, '_translate_sync') as mock:
            result = await service.translate("240/320")

            assert result == "240/320"
            assert mock.call_count == 0


class TestGlossary:
    def test_a_pinned_term_is_masked_with_its_translation(self):
        masked, holders = _mask("Use the Elixir now", {"Elixir": "ยาอมตะ"})
        assert "Elixir" not in masked
        assert holders == ["ยาอมตะ"]

    def test_matching_ignores_case(self):
        _, holders = _mask("use the elixir", {"Elixir": "ยาอมตะ"})
        assert holders == ["ยาอมตะ"]

    @pytest.mark.asyncio
    async def test_a_pinned_term_comes_out_the_same_every_time(self, service):
        service.set_glossary({"Elixir": "ยาอมตะ"})
        with patch.object(service, '_translate_sync', side_effect=lambda t: t):
            first = await service.translate("Elixir found")
            service._cache.clear()
            second = await service.translate("An Elixir")

            assert "ยาอมตะ" in first
            assert "ยาอมตะ" in second

    def test_setting_a_glossary_clears_the_cache(self, service):
        service._cache["en_th_x"] = "old"
        service.set_glossary({"Elixir": "ยาอมตะ"})
        assert len(service._cache) == 0


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_translate_error_returns_original(self, service):
        """On error, return original text instead of crashing."""
        with patch.object(service, '_translate_sync', side_effect=Exception("API Error")):
            assert await service.translate("Hello") == "Hello"

    @pytest.mark.asyncio
    async def test_one_failure_does_not_lose_the_other_blocks(self, service):
        with patch.object(service, '_translate_group_sync',
                          side_effect=Exception("API Error")):
            result = await service.translate(["Hello", "World"])
            assert result == ["Hello", "World"]
