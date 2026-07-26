import json

import pytest

from overlayr.services.game_profiles import GameProfileService


@pytest.fixture
def profile_store(tmp_path):
    return tmp_path / "game_profiles.json"


@pytest.fixture
def minecraft_source():
    return {
        "type": "window",
        "process": "javaw.exe",
        "friendly_name": "Minecraft",
        "original_title": "Minecraft 1.21.1",
    }


def test_activate_source_creates_and_persists_game_profile(
    profile_store,
    minecraft_source,
):
    service = GameProfileService(profile_store)

    profile = service.activate_source(
        minecraft_source,
        source_language="en",
        target_language="th",
    )

    assert profile is not None
    assert profile.id == "javaw"
    assert profile.name == "Minecraft"
    assert profile.source_language == "en"
    assert profile.target_language == "th"
    assert json.loads(profile_store.read_text(encoding="utf-8"))["profiles"]


def test_activate_source_reuses_profile_for_changing_window_title(
    profile_store,
    minecraft_source,
):
    service = GameProfileService(profile_store)
    first = service.activate_source(
        minecraft_source,
        source_language="en",
        target_language="th",
    )
    second_source = {
        **minecraft_source,
        "original_title": "Minecraft 1.21.1 - Singleplayer",
    }

    second = service.activate_source(
        second_source,
        source_language="en",
        target_language="th",
    )

    assert first is second
    assert second.usage_count == 2


def test_screen_source_does_not_create_game_profile(profile_store):
    service = GameProfileService(profile_store)

    profile = service.activate_source(
        {"type": "screen", "id": 0, "name": "Display 1"},
        source_language="en",
        target_language="th",
    )

    assert profile is None
    assert service.active_profile is None


def test_translation_override_is_scoped_to_game_and_language_pair(
    profile_store,
    minecraft_source,
):
    service = GameProfileService(profile_store)
    service.activate_source(
        minecraft_source,
        source_language="en",
        target_language="th",
    )
    service.add_translation_override("Wooden Watering Can", "บัวรดน้ำไม้")

    assert (
        service.lookup_translation_override(
            "  wooden   watering can ",
            source_language="en",
            target_language="th",
        )
        == "บัวรดน้ำไม้"
    )
    assert (
        service.lookup_translation_override(
            "Wooden Watering Can",
            source_language="en",
            target_language="ja",
        )
        is None
    )

    reloaded = GameProfileService(profile_store)
    reloaded.activate_source(
        minecraft_source,
        source_language="en",
        target_language="th",
    )
    assert (
        reloaded.lookup_translation_override(
            "Wooden Watering Can",
            source_language="en",
            target_language="th",
        )
        == "บัวรดน้ำไม้"
    )


def test_ocr_corrections_and_roi_are_persisted(
    profile_store,
    minecraft_source,
):
    service = GameProfileService(profile_store)
    service.activate_source(
        minecraft_source,
        source_language="en",
        target_language="th",
    )
    service.add_ocr_correction("Fiends", "Rewards")
    service.save_active_roi({"left": 10, "top": 20, "width": 300, "height": 90})

    assert service.apply_ocr_corrections(["Tasks", "fiends"]) == [
        "Tasks",
        "Rewards",
    ]

    reloaded = GameProfileService(profile_store)
    profile = reloaded.activate_source(
        minecraft_source,
        source_language="en",
        target_language="th",
    )
    assert profile.roi == {"left": 10, "top": 20, "width": 300, "height": 90}


def test_profile_cache_namespace_changes_after_correction(
    profile_store,
    minecraft_source,
):
    service = GameProfileService(profile_store)
    service.activate_source(
        minecraft_source,
        source_language="en",
        target_language="th",
    )
    before = service.cache_namespace

    service.add_translation_override("Chest", "หีบ")

    assert service.cache_namespace != before


def test_corrupt_store_is_quarantined_and_recreated(
    profile_store,
    minecraft_source,
):
    profile_store.write_text("{broken", encoding="utf-8")

    service = GameProfileService(profile_store)
    service.activate_source(
        minecraft_source,
        source_language="en",
        target_language="th",
    )

    assert profile_store.exists()
    assert list(profile_store.parent.glob("game_profiles.corrupt-*.json"))

