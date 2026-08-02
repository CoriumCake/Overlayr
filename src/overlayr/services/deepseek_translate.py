from collections import OrderedDict
from typing import List, Union
import asyncio
import aiohttp
import json
import os

_MAX_CACHE_SIZE = 2000
API_URL = "https://api.deepseek.com/chat/completions"
MODEL_NAME = "deepseek-chat" # Default model for DeepSeek (V3/V4 typically map to this)

class DeepSeekTranslateService:
    """
    Fast, context-aware translation using DeepSeek API via REST (aiohttp).
    Uses batch processing to translate multiple blocks in a single request.
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
        print(f"DeepSeek Translate: {src} -> {tgt}")
    
    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _translate_batch(self, texts: List[str]) -> List[str]:
        """Translate a batch of strings using DeepSeek."""
        if not self._api_key:
            print("DeepSeek API key is missing. Returning original texts.")
            return texts
            
        session = await self._get_session()
        
        # We wrap the output in a JSON object for strict JSON formatting
        system_prompt = (
            f"You are a highly accurate professional translator. Translate the given text from {self.source} to {self.target}. "
            "Maintain the context, tone, and formatting. "
            "You MUST return your response as a strict JSON object with a single key 'translations' whose value is a JSON array of strings corresponding to the input array."
        )
        
        user_prompt = f"Please translate this array: {json.dumps(texts, ensure_ascii=False)}"
        
        payload = {
            "model": MODEL_NAME,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.3
        }
        
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json"
        }
        
        try:
            async with session.post(API_URL, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 200:
                    data = await response.json()
                    try:
                        text_response = data['choices'][0]['message']['content']
                        # Safely load the JSON object
                        response_json = json.loads(text_response)
                        translated_array = response_json.get('translations', [])
                        
                        # Validate length
                        if isinstance(translated_array, list) and len(translated_array) == len(texts):
                            return [str(t) for t in translated_array]
                        else:
                            print(f"DeepSeek output mismatch: Expected {len(texts)} items, got {len(translated_array)}.")
                    except (KeyError, json.JSONDecodeError, IndexError) as parse_err:
                        print(f"DeepSeek parsing error: {parse_err}. Raw output: {text_response}")
                else:
                    err_text = await response.text()
                    print(f"DeepSeek API Error {response.status}: {err_text}")
        except asyncio.TimeoutError:
            print("DeepSeek API Timeout.")
        except Exception as e:
            print(f"DeepSeek API Exception: {e}")
            
        # Fallback: return originals on any error
        return texts

    async def translate(self, text: Union[str, List[str]]) -> Union[str, List[str]]:
        """Translate text using DeepSeek with batch caching."""
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
            print(f"DeepSeek translate method error: {e}")
            for idx in indices_to_translate:
                if results[idx] is None:
                    results[idx] = texts[idx]
            return results[0] if is_single else results
