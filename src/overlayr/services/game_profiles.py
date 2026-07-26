"""Persistent, game-specific translation context.

Profiles are keyed by the selected window's executable instead of its title
because game window titles frequently change while playing.  The store is
local-first so corrections work offline and can become the payload for a
future opt-in community profile service.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _default_store_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = (
        Path(local_app_data)
        if local_app_data
        else Path.home() / "AppData" / "Local"
    )
    return root / "Overlayr" / "game_profiles.json"


def _normalize_term(value: str) -> str:
    return " ".join(value.casefold().split())


def _language_key(value: str) -> str:
    return value.casefold().split("-", 1)[0]


def _pair_key(source_language: str, target_language: str) -> str:
    return f"{_language_key(source_language)}>{_language_key(target_language)}"


def _profile_id_for_process(process_name: str) -> str:
    stem = Path(process_name.strip()).stem.casefold()
    normalized = re.sub(r"[^a-z0-9._-]+", "-", stem).strip("-._")
    return normalized or "unknown-game"


@dataclass
class GameProfile:
    id: str
    name: str
    process_name: str
    source_language: str
    target_language: str
    translation_overrides: dict[str, dict[str, str]] = field(
        default_factory=dict
    )
    ocr_corrections: dict[str, dict[str, str]] = field(default_factory=dict)
    roi: Optional[dict[str, float]] = None
    usage_count: int = 0
    revision: int = 1
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GameProfile":
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            process_name=str(data.get("process_name") or f"{data['id']}.exe"),
            source_language=str(data.get("source_language") or "en"),
            target_language=str(data.get("target_language") or "th"),
            translation_overrides={
                str(pair): {
                    str(term): str(translation)
                    for term, translation in dict(overrides).items()
                }
                for pair, overrides in dict(
                    data.get("translation_overrides") or {}
                ).items()
            },
            ocr_corrections={
                str(language): {
                    str(term): str(correction)
                    for term, correction in dict(corrections).items()
                }
                for language, corrections in dict(
                    data.get("ocr_corrections") or {}
                ).items()
            },
            roi=(
                {
                    key: float(value)
                    for key, value in dict(data["roi"]).items()
                    if key in {"left", "top", "width", "height"}
                }
                if data.get("roi")
                else None
            ),
            usage_count=max(0, int(data.get("usage_count") or 0)),
            revision=max(1, int(data.get("revision") or 1)),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )


class GameProfileService:
    """Create, activate, and update local game profiles."""

    STORE_VERSION = 1

    def __init__(
        self,
        storage_path: Optional[Path | str] = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.storage_path = Path(storage_path or _default_store_path())
        self._clock = clock
        self._profiles: dict[str, GameProfile] = {}
        self._active_profile_id: Optional[str] = None
        self._load()

    @property
    def active_profile(self) -> Optional[GameProfile]:
        if self._active_profile_id is None:
            return None
        return self._profiles.get(self._active_profile_id)

    @property
    def cache_namespace(self) -> str:
        profile = self.active_profile
        if profile is None:
            return "generic:0"
        return f"{profile.id}:{profile.revision}"

    def activate_source(
        self,
        source: Mapping[str, Any],
        *,
        source_language: str,
        target_language: str,
    ) -> Optional[GameProfile]:
        if source.get("type") != "window" or not source.get("process"):
            self._active_profile_id = None
            return None

        process_name = str(source["process"])
        profile_id = _profile_id_for_process(process_name)
        profile = self._profiles.get(profile_id)
        now = self._timestamp()
        if profile is None:
            profile = GameProfile(
                id=profile_id,
                name=self._display_name(source, profile_id),
                process_name=process_name,
                source_language=source_language,
                target_language=target_language,
                created_at=now,
                updated_at=now,
            )
            self._profiles[profile_id] = profile
        else:
            profile.name = self._display_name(source, profile_id)
            profile.process_name = process_name
            if (
                profile.source_language != source_language
                or profile.target_language != target_language
            ):
                profile.source_language = source_language
                profile.target_language = target_language
                profile.revision += 1

        profile.usage_count += 1
        profile.updated_at = now
        self._active_profile_id = profile_id
        self._save()
        return profile

    def update_active_languages(
        self,
        source_language: str,
        target_language: str,
    ) -> None:
        profile = self.active_profile
        if profile is None:
            return
        if (
            profile.source_language == source_language
            and profile.target_language == target_language
        ):
            return
        profile.source_language = source_language
        profile.target_language = target_language
        self._touch(profile)

    def add_translation_override(
        self,
        source_text: str,
        translated_text: str,
    ) -> None:
        profile = self._require_active_profile()
        normalized = _normalize_term(source_text)
        translation = translated_text.strip()
        if not normalized or not translation:
            raise ValueError("Source and corrected translation are required")
        pair = _pair_key(
            profile.source_language,
            profile.target_language,
        )
        profile.translation_overrides.setdefault(pair, {})[
            normalized
        ] = translation
        self._touch(profile)

    def lookup_translation_override(
        self,
        text: str,
        *,
        source_language: str,
        target_language: str,
    ) -> Optional[str]:
        profile = self.active_profile
        if profile is None:
            return None
        pair = _pair_key(source_language, target_language)
        return profile.translation_overrides.get(pair, {}).get(
            _normalize_term(text)
        )

    def add_ocr_correction(
        self,
        recognized_text: str,
        corrected_text: str,
    ) -> None:
        profile = self._require_active_profile()
        normalized = _normalize_term(recognized_text)
        correction = corrected_text.strip()
        if not normalized or not correction:
            raise ValueError("Recognized and corrected OCR text are required")
        language = _language_key(profile.source_language)
        profile.ocr_corrections.setdefault(language, {})[
            normalized
        ] = correction
        self._touch(profile)

    def apply_ocr_corrections(self, texts: list[str]) -> list[str]:
        profile = self.active_profile
        if profile is None:
            return list(texts)
        language = _language_key(profile.source_language)
        corrections = profile.ocr_corrections.get(language, {})
        return [
            corrections.get(_normalize_term(text), text) if text else text
            for text in texts
        ]

    def save_active_roi(
        self,
        roi: Optional[Mapping[str, Any]],
    ) -> None:
        profile = self.active_profile
        if profile is None:
            return
        if roi is None:
            normalized_roi = None
        else:
            normalized_roi = {
                key: float(roi[key])
                for key in ("left", "top", "width", "height")
            }
            if (
                normalized_roi["width"] <= 0
                or normalized_roi["height"] <= 0
            ):
                raise ValueError("ROI width and height must be positive")
        if profile.roi == normalized_roi:
            return
        profile.roi = normalized_roi
        profile.updated_at = self._timestamp()
        self._save()

    def _require_active_profile(self) -> GameProfile:
        profile = self.active_profile
        if profile is None:
            raise RuntimeError(
                "Select an application before saving a game correction"
            )
        return profile

    def _touch(self, profile: GameProfile) -> None:
        profile.revision += 1
        profile.updated_at = self._timestamp()
        self._save()

    def _timestamp(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat()

    @staticmethod
    def _display_name(source: Mapping[str, Any], profile_id: str) -> str:
        friendly_name = str(source.get("friendly_name") or "").strip()
        if friendly_name and friendly_name.casefold() != profile_id:
            return friendly_name
        original_title = str(source.get("original_title") or "").strip()
        if original_title:
            return original_title
        return friendly_name or profile_id.replace("-", " ").title()

    def _load(self) -> None:
        if not self.storage_path.exists():
            return
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            raw_profiles = data.get("profiles") or {}
            self._profiles = {
                str(profile_id): GameProfile.from_dict(profile_data)
                for profile_id, profile_data in dict(raw_profiles).items()
            }
        except Exception as error:
            self._quarantine_corrupt_store()
            self._profiles = {}
            print(f"Game profile store was reset: {error}", flush=True)

    def _save(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.STORE_VERSION,
            "profiles": {
                profile_id: asdict(profile)
                for profile_id, profile in sorted(self._profiles.items())
            },
        }
        temporary_path = self.storage_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary_path, self.storage_path)

    def _quarantine_corrupt_store(self) -> None:
        if not self.storage_path.exists():
            return
        timestamp = self._clock().strftime("%Y%m%d-%H%M%S")
        corrupt_path = self.storage_path.with_name(
            f"{self.storage_path.stem}.corrupt-{timestamp}"
            f"{self.storage_path.suffix}"
        )
        os.replace(self.storage_path, corrupt_path)


__all__ = ["GameProfile", "GameProfileService"]
