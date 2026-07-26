"""Local, privacy-safe product analytics for PMF experiments.

Events intentionally exclude OCR text, translations, screenshots, and window
titles.  Nothing is uploaded; the newline-delimited log can be summarized or
exported later with explicit user consent.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


_SENSITIVE_KEY_PARTS = (
    "text",
    "translation",
    "screenshot",
    "image",
    "window_title",
)
_EVENT_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")


class ProductAnalytics:
    """Append product events and calculate a compact local funnel summary."""

    def __init__(
        self,
        events_path: Path | str,
        *,
        clock: Callable[[], datetime] = _utc_now,
        max_bytes: int = 5_000_000,
    ) -> None:
        self.events_path = Path(events_path)
        self._clock = clock
        self.max_bytes = max(1, int(max_bytes))
        self.session_id = uuid.uuid4().hex
        self.installation_id = self._load_or_create_installation_id()

    @classmethod
    def for_default_location(cls) -> "ProductAnalytics":
        from .game_profiles import _default_store_path

        root = _default_store_path().parent
        return cls(root / "product_events.jsonl")

    def record(self, event: str, **properties: Any) -> None:
        if not _EVENT_PATTERN.fullmatch(event):
            raise ValueError(f"Invalid analytics event name: {event!r}")
        safe_properties = {
            key: sanitized
            for key, value in sorted(properties.items())
            if not self._is_sensitive_key(key)
            if (sanitized := self._sanitize_value(value)) is not None
        }
        payload = {
            "event": event,
            "installation_id": self.installation_id,
            "properties": safe_properties,
            "session_id": self.session_id,
            "timestamp": self._clock()
            .astimezone(timezone.utc)
            .isoformat(),
        }
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_if_needed()
        with self.events_path.open("a", encoding="utf-8") as event_file:
            event_file.write(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
            event_file.write("\n")

    def summary(self) -> dict[str, int | bool | None]:
        active_days: set[str] = set()
        capture_sessions = 0
        translation_events = 0
        translated_items = 0
        corrections = 0
        first_event_at: Optional[datetime] = None
        first_translation_at: Optional[datetime] = None

        for event in self._iter_events():
            timestamp = self._parse_timestamp(event.get("timestamp"))
            if timestamp is not None:
                active_days.add(timestamp.date().isoformat())
                if first_event_at is None or timestamp < first_event_at:
                    first_event_at = timestamp

            event_name = event.get("event")
            properties = event.get("properties") or {}
            if event_name == "capture_started":
                capture_sessions += 1
            elif event_name == "translation_rendered":
                translation_events += 1
                translated_items += max(
                    0,
                    int(properties.get("item_count") or 0),
                )
                if timestamp is not None and (
                    first_translation_at is None
                    or timestamp < first_translation_at
                ):
                    first_translation_at = timestamp
            elif event_name == "translation_corrected":
                corrections += 1

        seconds_to_first_translation = None
        if first_event_at is not None and first_translation_at is not None:
            seconds_to_first_translation = max(
                0,
                int((first_translation_at - first_event_at).total_seconds()),
            )

        return {
            "active_days": len(active_days),
            "capture_sessions": capture_sessions,
            "translation_events": translation_events,
            "translated_items": translated_items,
            "corrections": corrections,
            "activated": translation_events > 0,
            "seconds_to_first_translation": seconds_to_first_translation,
        }

    @staticmethod
    def _is_sensitive_key(key: str) -> bool:
        normalized = key.casefold()
        return any(part in normalized for part in _SENSITIVE_KEY_PARTS)

    @staticmethod
    def _sanitize_value(value: Any) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return round(value, 4)
        if isinstance(value, str):
            return value[:128]
        return None

    def _load_or_create_installation_id(self) -> str:
        installation_path = self.events_path.with_name("installation_id")
        try:
            installation_id = installation_path.read_text(
                encoding="utf-8"
            ).strip()
            if installation_id:
                return installation_id
        except OSError:
            pass

        installation_id = uuid.uuid4().hex
        installation_path.parent.mkdir(parents=True, exist_ok=True)
        installation_path.write_text(installation_id, encoding="utf-8")
        return installation_id

    def _rotate_if_needed(self) -> None:
        if (
            not self.events_path.exists()
            or self.events_path.stat().st_size < self.max_bytes
        ):
            return
        rotated_path = self.events_path.with_name(
            f"{self.events_path.stem}.1{self.events_path.suffix}"
        )
        if rotated_path.exists():
            rotated_path.unlink()
        self.events_path.replace(rotated_path)

    def _iter_events(self) -> Iterator[dict[str, Any]]:
        rotated_path = self.events_path.with_name(
            f"{self.events_path.stem}.1{self.events_path.suffix}"
        )
        for path in (rotated_path, self.events_path):
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict):
                    yield value

    @staticmethod
    def _parse_timestamp(value: Any) -> Optional[datetime]:
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None


__all__ = ["ProductAnalytics"]
