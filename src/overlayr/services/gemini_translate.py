from collections import OrderedDict
from typing import List, Union
import asyncio
import aiohttp
import json
import os

_MAX_CACHE_SIZE = 2000
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent"

class GeminiTranslateService:
    """
    Fast, context-aware translation using Gemini 2.5 Flash API via REST (aiohttp).
    Uses batch processing (JSON array) to translate multiple blocks in a single request.
    """
    
    def __init__(self, source: str = 'en', target: str = 'th'):
        self.source = source
        self.target = target
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._api_key = self._load_api_key()
        self._session = None

    def _load_api_key(self) -> str:
        env_path = r"d:\NotWork\NewWork\Overlayr\Translate.env"
        try:
            if os.path.exists(env_path):
                with open(env_path, "r", encoding="utf-8") as f:
                    key = f.read().strip()
                    if key:
                        return key
        except Exception as e:
            print(f"Failed to load API key from {env_path}: {e}")
        return ""

    def warmup(self):
        """Warmup stub."""
        pass

    async def load_model(self, src: str, tgt: str) -> None:
        """Update source/target languages."""
        if src != self.source or tgt != self.target:
            self._cache = OrderedDict()
        self.source = src
        self.target = tgt
        print(f"Gemini Translate: {src} -> {tgt}")
    
    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _translate_batch(self, texts: List[str]) -> List[str]:
        """Translate a batch of strings using Gemini."""
        if not self._api_key:
            print("Gemini API key is missing. Returning original texts.")
            return texts
            
        session = await self._get_session()
        
        prompt = f"Translate the following JSON array of strings from {self.source} to {self.target}. Maintain the context and tone. Output ONLY a valid JSON array of strings. Do not include any markdown formatting or explanations.\n\n{json.dumps(texts, ensure_ascii=False)}"
        
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "response_mime_type": "application/json"
            }
        }
        
        url = f"{API_URL}?key={self._api_key}"
        
        try:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    try:
                        text_response = data['candidates'][0]['content']['parts'][0]['text']
                        # Safely load the JSON array
                        translated_array = json.loads(text_response)
                        
                        # Validate length
                        if isinstance(translated_array, list) and len(translated_array) == len(texts):
                            return [str(t) for t in translated_array]
                        else:
                            print(f"Gemini output mismatch: Expected {len(texts)} items, got {len(translated_array) if isinstance(translated_array, list) else 'non-list'}.")
                    except (KeyError, json.JSONDecodeError, IndexError) as parse_err:
                        print(f"Gemini parsing error: {parse_err}. Raw output: {data}")
                else:
                    err_text = await response.text()
                    print(f"Gemini API Error {response.status}: {err_text}")
        except asyncio.TimeoutError:
            print("Gemini API Timeout.")
        except Exception as e:
            print(f"Gemini API Exception: {e}")
            
        # Fallback: return originals on any error
        return texts

    async def translate(self, text: Union[str, List[str]]) -> Union[str, List[str]]:
        """Translate text using Gemini with batch caching."""
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
        
        # Translate the remaining items in a single batch
        try:
            translated_batch = await self._translate_batch(texts_to_translate)
            
            # Update cache and results
            for idx, trans in zip(indices_to_translate, translated_batch):
                key = cache_prefix + texts[idx]
                self._cache[key] = trans
                # Evict oldest entries if cache exceeds limit
                if len(self._cache) > _MAX_CACHE_SIZE:
                    for _ in range(_MAX_CACHE_SIZE // 4):
                        self._cache.popitem(last=False)
                results[idx] = trans
            
            return results[0] if is_single else results
            
        except Exception as e:
            print(f"Gemini translate method error: {e}")
            for idx in indices_to_translate:
                if results[idx] is None:
                    results[idx] = texts[idx]
            return results[0] if is_single else results
