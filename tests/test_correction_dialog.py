from unittest.mock import Mock

from overlayr.ui.components.correction_dialog import feedback_items_from_results
from overlayr.ui.main_window import MainWindow


def test_feedback_items_excludes_missing_and_duplicate_results():
    results = [
        {"original": "Chest", "translated": "กล่อง"},
        {"original": "Chest", "translated": "กล่อง"},
        {"original": "Rewards", "translated": None},
        {"original": "", "translated": "ว่าง"},
    ]

    assert feedback_items_from_results(results) == [
        ("Chest", "กล่อง"),
    ]


def test_feedback_items_preserves_source_order():
    results = [
        {"original": "Tasks", "translated": "ภารกิจ"},
        {"original": "Rewards", "translated": "รางวัล"},
    ]

    assert feedback_items_from_results(results) == [
        ("Tasks", "ภารกิจ"),
        ("Rewards", "รางวัล"),
    ]


def test_saving_feedback_updates_profile_and_invalidates_pipeline():
    window = object.__new__(MainWindow)
    window.game_profile_service = Mock()
    window.async_engine = Mock()
    window.lbl_status = Mock()
    window.product_analytics = Mock()
    window._reset_pipeline_for_area_change = Mock()
    active_profile = Mock()
    active_profile.id = "javaw"
    window.game_profile_service.active_profile = active_profile

    window._save_translation_feedback("Chest", "หีบ")

    window.game_profile_service.add_translation_override.assert_called_once_with(
        "Chest",
        "หีบ",
    )
    window.async_engine.clear_translation_cache.assert_called_once_with()
    window._reset_pipeline_for_area_change.assert_called_once_with()
    window.product_analytics.record.assert_called_once_with(
        "translation_corrected",
        profile_id="javaw",
    )
