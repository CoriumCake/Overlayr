import asyncio
import concurrent.futures
import time
from collections import OrderedDict
from typing import List, Optional, Union

from deep_translator import GoogleTranslator

_MAX_CACHE_SIZE = 2000
_BATCH_SEPARATOR = "__OVERLAYR_SPLIT_7F3A__"
_MAX_BATCH_CHARS = 4500


class GoogleTranslateService:
    """
    Fast translation using Google Translate via deep-translator.
    Uses parallel processing for speed.
    """

    def __init__(self, source: str = "en", target: str = "th"):
        self.source = source
        self.target = target
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)
        self.max_attempts = 2
        self.retry_backoff_seconds = 0.15
        self.batch_translation = True

    def warmup(self):
        """Warmup stub."""
        pass

    async def load_model(self, src: str, tgt: str) -> None:
        """Update source/target languages."""
        if src != self.source or tgt != self.target:
            self._cache = OrderedDict()
        self.source = src
        self.target = tgt
        print(f"Google Translate: {src} -> {tgt}")

    def _translate_single(self, text: str) -> str:
        """Translate a single text item."""
        translator = GoogleTranslator(source=self.source, target=self.target)
        translated = translator.translate(text)
        if not isinstance(translated, str) or not translated.strip():
            raise ValueError("Google Translate returned an empty result")
        return translated

    def _translate_with_retry(self, text: str) -> Optional[str]:
        """Retry transient translation failures without returning source text."""
        last_error = None
        attempts = max(1, int(self.max_attempts))
        for attempt in range(attempts):
            try:
                return self._translate_single(text)
            except Exception as error:
                last_error = error
                if attempt + 1 < attempts and self.retry_backoff_seconds > 0:
                    time.sleep(self.retry_backoff_seconds * (2**attempt))

        print(
            f"Google Translate failed after {attempts} attempt(s): {last_error}",
            flush=True,
        )
        return None

    def _translate_batch_single_request(
        self,
        texts: List[str],
    ) -> List[str]:
        """Translate a frame's text blocks with one network round trip."""
        payload = f"\n{_BATCH_SEPARATOR}\n".join(texts)
        translated = self._translate_single(payload)
        parts = translated.split(_BATCH_SEPARATOR)
        if len(parts) != len(texts):
            raise ValueError("Google Translate did not preserve batch separators")
        return [part.strip() for part in parts]

    def _translate_batch_with_retry(
        self,
        texts: List[str],
    ) -> Optional[List[str]]:
        last_error = None
        attempts = max(1, int(self.max_attempts))
        for attempt in range(attempts):
            try:
                translated = self._translate_batch_single_request(texts)
                if any(not item for item in translated):
                    raise ValueError("Google Translate returned an empty batch item")
                return translated
            except Exception as error:
                last_error = error
                if attempt + 1 < attempts and self.retry_backoff_seconds > 0:
                    time.sleep(self.retry_backoff_seconds * (2**attempt))

        print(
            f"Google Translate batch failed after {attempts} attempt(s): {last_error}",
            flush=True,
        )
        return None

    async def translate(
        self,
        text: Union[str, List[str]],
    ) -> Union[Optional[str], List[Optional[str]]]:
        """Translate text using Google Translate with parallel requests."""
        if not text:
            return "" if isinstance(text, str) else []

        # Bypass if same language
        if self.source == self.target:
            return text

        is_single = isinstance(text, str)
        texts = [text] if is_single else text

        # Check cache
        cache_prefix = f"{self.source}_{self.target}_"
        results = [None] * len(texts)
        texts_to_translate = []
        indices_to_translate = []

        for i, t in enumerate(texts):
            key = cache_prefix + t
            if key in self._cache:
                results[i] = self._cache[key]
                self._cache.move_to_end(key)
            else:
                texts_to_translate.append(t)
                indices_to_translate.append(i)

        # If all cached, return early
        if not texts_to_translate:
            return results[0] if is_single else results

        # Translate in parallel using thread pool
        try:
            loop = asyncio.get_running_loop()

            batch_char_count = sum(len(item) for item in texts_to_translate) + (len(texts_to_translate) - 1) * (
                len(_BATCH_SEPARATOR) + 2
            )
            if self.batch_translation and len(texts_to_translate) > 1 and batch_char_count <= _MAX_BATCH_CHARS:
                translated = await loop.run_in_executor(
                    self._executor,
                    self._translate_batch_with_retry,
                    texts_to_translate,
                )
                if translated is None:
                    translated = [None] * len(texts_to_translate)
            else:
                # Large batches use bounded parallel requests so no request
                # exceeds Google Translate's input limit.
                futures = [
                    loop.run_in_executor(
                        self._executor,
                        self._translate_with_retry,
                        item,
                    )
                    for item in texts_to_translate
                ]
                translated = await asyncio.gather(*futures)

            # Update cache and results
            for idx, trans in zip(indices_to_translate, translated):
                key = cache_prefix + texts[idx]
                results[idx] = trans
                if trans is None:
                    continue
                self._cache[key] = trans
                self._cache.move_to_end(key)
                # Evict oldest entries if cache exceeds limit
                while len(self._cache) > _MAX_CACHE_SIZE:
                    self._cache.popitem(last=False)

            return results[0] if is_single else results

        except Exception as e:
            print(f"Google Translate error: {e}")
            # Preserve failures as missing values. Returning source text here
            # makes the overlay visibly oscillate between English and Thai.
            for idx in indices_to_translate:
                if results[idx] is None:
                    results[idx] = None
            return results[0] if is_single else results
