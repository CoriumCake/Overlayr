from transformers import M2M100ForConditionalGeneration, M2M100Tokenizer
import torch
import gc
from collections import OrderedDict
from typing import List, Union
from functools import lru_cache
from ..utils.singleton import SingletonMeta

_MAX_CACHE_SIZE = 2000

class LocalTranslatorService(metaclass=SingletonMeta):
    def __init__(self, source: str = 'ja', target: str = 'en'):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = None
        self.tokenizer = None
        self.model_name = "facebook/m2m100_418M"
        self.source = source
        self.target = target
        self._is_loading = False
        self._load_error = False
        self._cache: OrderedDict[str, str] = OrderedDict()  # Bounded cache
        print(f"LocalTranslatorService initialized. Device: {self.device}")

    async def load_model(self, src: str, tgt: str):
        print(f"LocalTranslatorService.load_model called: {src} -> {tgt}", flush=True)
        self.source = src
        self.target = tgt
        
        # Clear cache when languages change
        self._cache = OrderedDict()
        
        if self.model is not None:
             return
             
        if self._is_loading:
            print("Model is currently loading... skipping request.", flush=True)
            return

        if self._load_error:
            print("Model previously failed to load. Skipping to save resources.", flush=True)
            return

        self._is_loading = True
        print(f"Loading M2M100 model: {self.model_name}...", flush=True)
        try:
            self.tokenizer = M2M100Tokenizer.from_pretrained(self.model_name, use_safetensors=True)
            self.model = M2M100ForConditionalGeneration.from_pretrained(self.model_name, use_safetensors=True).to(self.device)
            self.model.eval() 
            print(f"Model {self.model_name} loaded successfully on {self.device}.", flush=True)
            self._load_error = False
        except Exception as e:
            print(f"Failed to load model {self.model_name}: {e}")
            self.model = None
            self._load_error = True
        finally:
            self._is_loading = False

    async def translate(self, text: Union[str, List[str]]) -> Union[str, List[str]]:
        if not text: return "" if isinstance(text, str) else []
        
        # Bypass equal languages
        if self.source == self.target:
            return text

        if not self.model:
            await self.load_model(self.source, self.target)
            if not self.model: return text
        
        # Handle both single string and list inputs
        is_single = isinstance(text, str)
        texts = [text] if is_single else text
        
        # --- Caching Logic ---
        cache_key_prefix = f"{self.source}_{self.target}_"
        results = []
        texts_to_translate = []
        indices_to_translate = []
        
        for i, t in enumerate(texts):
            key = cache_key_prefix + t
            if key in self._cache:
                results.append((i, self._cache[key]))
            else:
                texts_to_translate.append(t)
                indices_to_translate.append(i)
                results.append((i, None)) # Placeholder
        
        # If everything was cached, return early
        if not texts_to_translate:
            final = [r[1] for r in sorted(results, key=lambda x: x[0])]
            return final[0] if is_single else final

        # --- Actual Translation ---
        try:
            import time
            t0 = time.time()
            print(f"Translating {len(texts_to_translate)} items...", flush=True)
            
            self.tokenizer.src_lang = self.source
            inputs = self.tokenizer(texts_to_translate, return_tensors="pt", padding=True, truncation=True).to(self.device)
            forced_bos_token_id = self.tokenizer.get_lang_id(self.target)
            
            t1 = time.time()
            print(f"Tokenized in {t1-t0:.2f}s", flush=True)
            
            with torch.no_grad():
                outputs = self.model.generate(
                    **inputs, 
                    forced_bos_token_id=forced_bos_token_id,
                    max_new_tokens=64,
                    num_beams=1,
                    do_sample=False, # Deterministic
                    repetition_penalty=1.5, # Prevent repetition loops
                    no_repeat_ngram_size=3, # No 3-gram repeats
                )
            
            t2 = time.time()
            print(f"Generated in {t2-t1:.2f}s", flush=True)
            
            decoded = self.tokenizer.batch_decode(outputs, skip_special_tokens=True)
            
            # Populate cache and results
            for idx, (orig_text, translated) in enumerate(zip(texts_to_translate, decoded)):
                key = cache_key_prefix + orig_text
                self._cache[key] = translated
                # Evict oldest entries if cache exceeds limit
                if len(self._cache) > _MAX_CACHE_SIZE:
                    for _ in range(_MAX_CACHE_SIZE // 4):
                        self._cache.popitem(last=False)
                
                # Find position in results
                result_idx = indices_to_translate[idx]
                for j, r in enumerate(results):
                    if r[0] == result_idx:
                        results[j] = (result_idx, translated)
                        break
            
            final = [r[1] for r in sorted(results, key=lambda x: x[0])]
            return final[0] if is_single else final
            
        except Exception as e:
            print(f"Translation Error: {e}")
            return text
