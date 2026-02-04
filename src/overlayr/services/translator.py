from deep_translator import GoogleTranslator
import asyncio
from functools import lru_cache
from typing import Optional
from concurrent.futures import ThreadPoolExecutor
from ..utils.singleton import SingletonMeta

class TranslatorService(metaclass=SingletonMeta):
    def __init__(self, source: str = 'ja', target: str = 'th'):
        print("Initializing TranslatorService...")
        self.source = source
        self.target = target
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._translator = None # Lazy load
        print("TranslatorService initialized (Lazy).")

    def _get_translator(self):
        if self._translator is None:
            print("Creating GoogleTranslator instance...", flush=True)
            self._translator = GoogleTranslator(source=self.source, target=self.target)
        return self._translator
        
    def warmup(self):
        """Force initialization of translator."""
        self._get_translator()

    def update_settings(self, source: str, target: str) -> None:
        self.source = source
        self.target = target
        self._translator = None # Reset for lazy reload
        self.translate_sync.cache_clear() # Clear cache on language change

    @lru_cache(maxsize=1000)
    def translate_sync(self, text: str) -> str:
        """
        Synchronous translation with LRU cache.
        """
        if not text or not text.strip():
            return ""
        try:
            return self._get_translator().translate(text)
        except Exception as e:
            print(f"Translation Error: {e}")
            return f"[Error: {e}]"

    async def translate(self, text: str) -> str:
        """
        Asynchronous translation using thread pool to avoid blocking event loop.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, 
            self.translate_sync, 
            text
        )
