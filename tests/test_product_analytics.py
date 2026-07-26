import json
from datetime import datetime, timezone

from overlayr.services.product_analytics import ProductAnalytics


def _fixed_clock():
    return datetime(2026, 7, 26, 12, 30, tzinfo=timezone.utc)


def test_record_writes_privacy_safe_event(tmp_path):
    events_path = tmp_path / "events.jsonl"
    analytics = ProductAnalytics(events_path, clock=_fixed_clock)

    analytics.record(
        "translation_rendered",
        item_count=3,
        duration_ms=42,
        source_text="secret quest text",
        screenshot="private.png",
    )

    event = json.loads(events_path.read_text(encoding="utf-8"))
    assert event["event"] == "translation_rendered"
    assert event["properties"] == {"duration_ms": 42, "item_count": 3}
    assert event["timestamp"] == "2026-07-26T12:30:00+00:00"


def test_summary_calculates_activation_and_usage_without_content(tmp_path):
    events_path = tmp_path / "events.jsonl"
    analytics = ProductAnalytics(events_path, clock=_fixed_clock)
    analytics.record("app_started")
    analytics.record("source_selected", source_type="window", profile_id="javaw")
    analytics.record("capture_started", profile_id="javaw")
    analytics.record("translation_rendered", item_count=2, profile_id="javaw")
    analytics.record("translation_rendered", item_count=4, profile_id="javaw")
    analytics.record("translation_corrected", profile_id="javaw")

    summary = analytics.summary()

    assert summary["active_days"] == 1
    assert summary["capture_sessions"] == 1
    assert summary["translation_events"] == 2
    assert summary["translated_items"] == 6
    assert summary["corrections"] == 1
    assert summary["activated"] is True


def test_invalid_lines_are_ignored_when_building_summary(tmp_path):
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("not-json\n", encoding="utf-8")
    analytics = ProductAnalytics(events_path, clock=_fixed_clock)

    analytics.record("app_started")

    assert analytics.summary()["active_days"] == 1


def test_event_file_rotates_before_exceeding_size_limit(tmp_path):
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("x" * 200, encoding="utf-8")
    analytics = ProductAnalytics(
        events_path,
        clock=_fixed_clock,
        max_bytes=100,
    )

    analytics.record("app_started")

    assert events_path.stat().st_size < 200
    assert (tmp_path / "events.1.jsonl").exists()
