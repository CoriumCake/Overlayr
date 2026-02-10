from deep_translator import GoogleTranslator
from collections import OrderedDict
from typing import List, Union
import asyncio
import concurrent.futures

_MAX_CACHE_SIZE = 2000

class GoogleTranslateService:
    """
    Fast translation using Google Translate via deep-translator.
    Uses parallel processing for speed.
    """
    
    def __init__(self, source: str = 'en', target: str = 'th'):
        self.source = source
        self.target = target
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)
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
        try:
            translator = GoogleTranslator(source=self.source, target=self.target)
            return translator.translate(text)
        except Exception:
            return text
    
    async def translate(self, text: Union[str, List[str]]) -> Union[str, List[str]]:
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
            else:
                texts_to_translate.append(t)
                indices_to_translate.append(i)
        
        # If all cached, return early
        if not texts_to_translate:
            return results[0] if is_single else results
        
        # Translate in parallel using thread pool
        try:
            loop = asyncio.get_running_loop()
            
            # Submit all translations in parallel
            futures = [
                loop.run_in_executor(self._executor, self._translate_single, t)
                for t in texts_to_translate
            ]
            
            # Wait for all to complete
            translated = await asyncio.gather(*futures)
            
            # Update cache and results
            for idx, trans in zip(indices_to_translate, translated):
                key = cache_prefix + texts[idx]
                self._cache[key] = trans
                # Evict oldest entries if cache exceeds limit
                if len(self._cache) > _MAX_CACHE_SIZE:
                    # Remove oldest 25%
                    for _ in range(_MAX_CACHE_SIZE // 4):
                        self._cache.popitem(last=False)
                results[idx] = trans
            
            return results[0] if is_single else results
            
        except Exception as e:
            print(f"Google Translate error: {e}")
            # Return originals for failures
            for idx in indices_to_translate:
                if results[idx] is None:
                    results[idx] = texts[idx]
            return results[0] if is_single else results
