"""Translation through Google Translate, batched and guarded.

Three things beyond a plain call to the API (CLAUDE.md section 8):

* **Batching.** Blocks are joined with newlines and sent as one request, then
  split back apart. ``deep_translator``'s own ``translate_batch`` is a serial
  loop over single calls, so it would send just as many requests as before and
  lose the parallelism; joining is what actually reduces them. Measured on six
  lines: one request at 1.3s against six at 3.4s.
* **A placeholder guard.** Numbers, format specifiers and HP/MP values are
  masked before the text is sent and put back afterwards, so a quantity cannot
  come back reworded. Marker tokens were checked against the live service and
  pass through untouched.
* **A glossary.** Item, skill and character names translate to whatever the
  user pinned them to, every time, instead of drifting between frames.

The cache lives here and nowhere else. There used to be a second one in
AsyncEngine in front of it, which meant this one was never consulted.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Union

import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator

_MAX_CACHE_SIZE = 2000

# Google rejects very long inputs; batches are kept well under the limit.
MAX_BATCH_CHARS = 4000

# The free endpoint refuses a request now and then for no lasting reason.
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 0.4

REQUEST_TIMEOUT_SECONDS = 10

# One session for the whole process. Each translation used to open a fresh TCP
# and TLS connection; keeping one alive cut the round trip from about 537ms to
# 152ms, which is most of the cost of a translation once the text is short.
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0"})


def _parse_html(response) -> Optional[str]:
    """Read a translation out of the mobile page Google serves."""
    soup = BeautifulSoup(response.text, "html.parser")
    element = (soup.find("div", {"class": "t0"})
               or soup.find("div", {"class": "result-container"}))
    return element.get_text() if element else None


def _parse_json(response) -> Optional[str]:
    """Read a translation out of the segment array the JSON endpoints return."""
    data = json.loads(response.text)
    if not isinstance(data, list) or not data or not isinstance(data[0], list):
        return None
    return "".join(segment[0] for segment in data[0]
                   if segment and isinstance(segment[0], str))


@dataclass(frozen=True)
class Endpoint:
    name: str
    url: str
    build_params: Callable[[str, str, str], dict]
    parse: Callable[[object], Optional[str]]


# Tried in order, fastest first. Every one of these was checked against the live
# service for two things: that it answers, and that it returns the newlines
# batching depends on. Timings measured on the same six-line payload.
#
#   clients5 single       194ms
#   clients5 t            425ms
#   translate.google.co.th/m  711ms
#   translate.google.com/m    912ms   <- the one deep-translator uses
#
# The list is a chain rather than a single choice because these endpoints rate
# limit independently: googleapis was returning 429 while the others were fine.
ENDPOINTS = (
    Endpoint("clients5-json", "https://clients5.google.com/translate_a/single",
             lambda text, src, tgt: {"client": "gtx", "sl": src, "tl": tgt,
                                     "dt": "t", "q": text},
             _parse_json),
    Endpoint("googleapis-json", "https://translate.googleapis.com/translate_a/single",
             lambda text, src, tgt: {"client": "gtx", "sl": src, "tl": tgt,
                                     "dt": "t", "q": text},
             _parse_json),
    Endpoint("google-m", "https://translate.google.com/m",
             lambda text, src, tgt: {"q": text, "sl": src, "tl": tgt},
             _parse_html),
    Endpoint("google-m-th", "https://translate.google.co.th/m",
             lambda text, src, tgt: {"q": text, "sl": src, "tl": tgt},
             _parse_html),
)

# Which endpoint answered last. Starting from it means a rate-limited endpoint
# is skipped on every later call instead of being tried again each time.
_preferred = 0
_preferred_lock = threading.Lock()


def _endpoint_order():
    """The chain, starting from whichever endpoint last worked."""
    with _preferred_lock:
        start = _preferred
    return [(start + offset) % len(ENDPOINTS) for offset in range(len(ENDPOINTS))]


def _remember(index: int) -> None:
    global _preferred
    with _preferred_lock:
        _preferred = index


def translate_via_chain(text: str, source: str, target: str) -> Optional[str]:
    """Translate through the endpoint chain. None means every one of them failed."""
    for index in _endpoint_order():
        endpoint = ENDPOINTS[index]
        try:
            response = _SESSION.get(endpoint.url,
                                    params=endpoint.build_params(text, source, target),
                                    timeout=REQUEST_TIMEOUT_SECONDS)
            if response.status_code != 200:
                print(f"Translate endpoint {endpoint.name}: HTTP "
                      f"{response.status_code}; trying the next one.", flush=True)
                continue

            translated = endpoint.parse(response)
            if translated:
                _remember(index)
                return translated
            print(f"Translate endpoint {endpoint.name}: nothing to read in the "
                  f"reply; trying the next one.", flush=True)
        except Exception as e:
            print(f"Translate endpoint {endpoint.name} failed "
                  f"({type(e).__name__}); trying the next one.", flush=True)

    return None

# Marker wrapped around masked content. Verified against the live service: it
# comes back unchanged, and OCR of real text never produces these brackets.
_MASK_OPEN = "⟦"
_MASK_CLOSE = "⟧"
_MASK_RE = re.compile(f"{_MASK_OPEN}(\\d+){_MASK_CLOSE}")

# What must survive translation exactly as it went in.
_PROTECTED = re.compile(
    r"""
    %\d*\$?[sdif]           # printf style: %s %d %1$s
    | \{[A-Za-z0-9_]*\}     # brace style: {0} {name}
    | \d+(?:[.,]\d+)*\s*/\s*\d+(?:[.,]\d+)*   # HP/MP values: 240/320
    | \d+(?:[.,]\d+)*%?     # plain numbers, percentages
    """,
    re.VERBOSE,
)


def _mask(text: str, glossary: Dict[str, str]):
    """Replace protected spans with markers. Returns the text and what they hold.

    Glossary terms are masked too: that is what makes their translation fixed
    rather than whatever the service felt like this time.
    """
    replacements: List[str] = []

    def take(value: str) -> str:
        replacements.append(value)
        return f"{_MASK_OPEN}{len(replacements) - 1}{_MASK_CLOSE}"

    # Numbers first, glossary second. The other way round, the digit inside a
    # glossary marker is itself read as a number and masked again, nesting one
    # marker inside another so neither survives the restore.
    masked = _PROTECTED.sub(lambda m: take(m.group(0)), text)

    for term, fixed in glossary.items():
        if not term:
            continue
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        masked = pattern.sub(lambda _m: take(fixed), masked)

    return masked, replacements


def _unmask(text: str, replacements: Sequence[str]) -> str:
    """Put the protected spans back. Markers the service dropped are ignored."""
    def restore(match):
        index = int(match.group(1))
        return replacements[index] if index < len(replacements) else match.group(0)

    return _MASK_RE.sub(restore, text)


def _is_only_markers(text: str) -> bool:
    """Whether nothing is left to translate once the markers are removed."""
    return not _MASK_RE.sub("", text).strip()


def _batches(texts: Sequence[str], limit: int = MAX_BATCH_CHARS) -> List[List[int]]:
    """Group indices into batches whose joined length stays under ``limit``."""
    groups: List[List[int]] = []
    current: List[int] = []
    length = 0

    for index, text in enumerate(texts):
        size = len(text) + 1
        if current and length + size > limit:
            groups.append(current)
            current, length = [], 0
        current.append(index)
        length += size

    if current:
        groups.append(current)
    return groups


class GoogleTranslateService:
    """Translation with one cache, batched requests, and protected placeholders."""

    def __init__(self, source: str = 'en', target: str = 'th'):
        self.source = source
        self.target = target
        self._cache: "OrderedDict[str, str]" = OrderedDict()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)
        self.glossary: Dict[str, str] = {}

    def warmup(self):
        """Warmup stub."""
        pass

    def set_glossary(self, glossary: Dict[str, str]) -> None:
        """Pin terms to fixed translations, and forget anything cached without them."""
        self.glossary = {str(k): str(v) for k, v in (glossary or {}).items() if k}
        self._cache.clear()
        if self.glossary:
            print(f"Glossary: {len(self.glossary)} fixed terms.", flush=True)

    async def load_model(self, src: str, tgt: str) -> None:
        """Update source/target languages."""
        if src != self.source or tgt != self.target:
            self._cache = OrderedDict()
        self.source = src
        self.target = tgt
        print(f"Google Translate: {src} -> {tgt}")

    # -- cache ------------------------------------------------------------

    def _cache_key(self, text: str) -> str:
        return f"{self.source}_{self.target}_{text}"

    def _cache_get(self, text: str):
        key = self._cache_key(text)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def _cache_put(self, text: str, translation: str) -> None:
        self._cache[self._cache_key(text)] = translation
        self._cache.move_to_end(self._cache_key(text))
        while len(self._cache) > _MAX_CACHE_SIZE:
            self._cache.popitem(last=False)

    # -- requests ---------------------------------------------------------

    def _request(self, text: str, attempts: int = RETRY_ATTEMPTS):
        """One translation, retried on failure. Returns None if it never worked.

        The free endpoint refuses a request now and then for no lasting reason -
        the same text was observed failing three times and succeeding on the
        fourth. Returning None rather than the original text is what lets the
        caller tell "the service was busy" apart from "this text needs no
        translation", and retry it a different way.
        """
        for attempt in range(attempts):
            translated = translate_via_chain(text, self.source, self.target)
            if translated:
                return translated
            if attempt + 1 < attempts:
                time.sleep(RETRY_DELAY_SECONDS)

        # Last resort: deep-translator, the library CLAUDE.md section 3 pins the
        # engine to. It reaches the same service over its own connection, so it
        # is worth one try when every endpoint in the chain has refused us.
        try:
            return GoogleTranslator(source=self.source, target=self.target).translate(text)
        except Exception as e:
            print(f"Google Translate failed after {attempts} attempt(s) across "
                  f"{len(ENDPOINTS)} endpoints and the library fallback "
                  f"({type(e).__name__}).", flush=True)
            return None

    def _translate_sync(self, text: str) -> str:
        """One text. Falls back to showing it untranslated rather than failing."""
        result = self._request(text)
        if result is None:
            # Never let the failure vanish -- silent pass-through reads as
            # "nothing to translate" rather than "the service was unreachable".
            print(f"Showing original text for: {text[:60]!r}", flush=True)
            return text
        return result

    def _translate_group_sync(self, texts: Sequence[str]) -> List[str]:
        """Translate several texts in one request, one per line.

        If the service does not return the same number of lines, the batch is
        redone one item at a time: a miscounted split would attach a translation
        to the wrong box on screen, which is worse than being slow.
        """
        if len(texts) == 1:
            return [self._translate_sync(texts[0])]

        joined = "\n".join(text.replace("\n", " ") for text in texts)
        # One attempt only: a refused batch is retried as single requests
        # anyway, and those succeed often enough that retrying the whole batch
        # first would just add a second of waiting before the same fallback.
        result = self._request(joined, attempts=1)

        if result is not None:
            parts = result.split("\n")
            if len(parts) == len(texts):
                return [part.strip() for part in parts]
            reason = "came back misaligned"
        else:
            reason = "was refused"

        # Either way the batch is unusable. Single requests often succeed when a
        # batch did not, and a partly translated frame beats an untranslated one.
        print(f"Google Translate: batch of {len(texts)} {reason}; "
              f"retrying one at a time.", flush=True)
        return [self._translate_sync(text) for text in texts]

    # -- entry point ------------------------------------------------------

    async def translate(self, text: Union[str, List[str]]) -> Union[str, List[str]]:
        """Translate one string or a list of them."""
        if not text:
            return "" if isinstance(text, str) else []

        is_single = isinstance(text, str)
        texts = [text] if is_single else list(text)

        if self.source == self.target:
            return text

        results: List[str] = [""] * len(texts)
        pending: List[int] = []
        masked_texts: Dict[int, str] = {}
        replacements: Dict[int, List[str]] = {}

        for index, item in enumerate(texts):
            cached = self._cache_get(item)
            if cached is not None:
                results[index] = cached
                continue

            masked, holders = _mask(item, self.glossary)
            replacements[index] = holders

            # Nothing but protected content: a bare HP value or item count has
            # no words to translate, so it is returned as it came in.
            if _is_only_markers(masked):
                results[index] = _unmask(masked, holders)
                self._cache_put(item, results[index])
                continue

            masked_texts[index] = masked
            pending.append(index)

        if pending:
            await self._translate_pending(texts, results, pending, masked_texts,
                                          replacements)

        return results[0] if is_single else results

    async def _translate_pending(self, texts, results, pending, masked_texts,
                                 replacements) -> None:
        ordered = [masked_texts[index] for index in pending]
        groups = _batches(ordered)

        loop = asyncio.get_running_loop()
        futures = [
            loop.run_in_executor(self._executor, self._translate_group_sync,
                                 [ordered[position] for position in group])
            for group in groups
        ]

        try:
            translated_groups = await asyncio.gather(*futures)
        except Exception as e:
            print(f"Google Translate error: {e}", flush=True)
            for index in pending:
                results[index] = texts[index]
            return

        for group, translations in zip(groups, translated_groups):
            for position, translation in zip(group, translations):
                index = pending[position]
                restored = _unmask(translation or texts[index],
                                   replacements.get(index, []))
                results[index] = restored
                self._cache_put(texts[index], restored)
