from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.core.schemas import BronzeLedgerEntry, EntityType, SportType, StepType
from src.processing.extractors import garmin_adapter
from src.processing.extractors.garmin_adapter import (
    ActivityExtractor,
    CalendarExtractor,
    DailySummaryExtractor,
    WorkoutExtractor,
)


def _entry(tmp_path: Path, entity_type: EntityType, source_identifier: str) -> BronzeLedgerEntry:
    payload_path = tmp_path / f"{source_identifier}.payload"
    payload_path.write_bytes(b"fixture")
    return BronzeLedgerEntry(
        user_id="athlete-1",
        vendor="garmin",
        entity_type=entity_type,
        source_identifier=source_identifier,
        content_hash="test-hash",
        fetch_timestamp=datetime(2026, 9, 28, tzinfo=timezone.utc),
        file_path=str(payload_path),
    )


def test_activity_extractor_maps_session_and_laps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entry = _entry(tmp_path, EntityType.ACTIVITY, "activity-42")
    monkeypatch.setattr(
        garmin_adapter,
        "_read_fit_payload",
        lambda _: {
            "session": [
                {
                    "start_time": datetime(2026, 9, 27, 7, tzinfo=timezone.utc),
                    "timestamp": datetime(2026, 9, 27, 8, tzinfo=timezone.utc),
                    "sport": "running",
                    "sub_sport": "trail",
                    "total_elapsed_time": 3600,
                    "total_timer_time": 3500,
                    "total_distance": 10000,
                    "avg_heart_rate": 150,
                    "max_heart_rate": 180,
                    "avg_temperature": -2,
                }
            ],
            "time_in_zone": [
                {
                    "reference_mesg": "session",
                    "time_in_hr_zone": [300, 600],
                }
            ],
            "lap": [
                {
                    "total_elapsed_time": 600,
                    "total_distance": 1600,
                    "avg_heart_rate": 145,
                    "avg_speed": 2.6,
                }
            ],
        },
    )

    [record] = ActivityExtractor().extract([entry])

    assert record.user_id == "athlete-1"
    assert record.activity_id == "activity-42"
    assert record.sport_type is SportType.TRAIL_RUNNING
    assert record.duration_seconds == 3600
    assert record.moving_time_seconds == 3500
    assert record.hr_zone_seconds == [300, 600]
    assert record.avg_temperature_celsius == -2
    assert record.lap_summaries[0].avg_heart_rate == 145


def test_activity_extractor_rejects_fit_without_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entry = _entry(tmp_path, EntityType.ACTIVITY, "activity-42")
    monkeypatch.setattr(garmin_adapter, "_read_fit_payload", lambda _: {})

    with pytest.raises(ValueError, match="no session message"):
        ActivityExtractor().extract([entry])


def test_daily_extractor_aggregates_monitoring_and_maps_sleep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entry = _entry(tmp_path, EntityType.DAILY_SUMMARY, "2026-09-27")
    monkeypatch.setattr(
        garmin_adapter,
        "_read_fit_payload",
        lambda _: {
            "sleep_assessment": [{"overall_sleep_score": 88}],
            "sleep_level": [
                {
                    "timestamp": datetime(2026, 9, 26, 22, 0, tzinfo=timezone.utc),
                    "sleep_level": "light",
                },
                {
                    "timestamp": datetime(2026, 9, 26, 22, 5, tzinfo=timezone.utc),
                    "sleep_level": "deep",
                },
                {
                    "timestamp": datetime(2026, 9, 26, 22, 10, tzinfo=timezone.utc),
                    "sleep_level": "rem",
                },
                {
                    "timestamp": datetime(2026, 9, 26, 22, 15, tzinfo=timezone.utc),
                    "sleep_level": "awake",
                },
                {
                    "timestamp": datetime(2026, 9, 26, 22, 20, tzinfo=timezone.utc),
                    "sleep_level": "deep",
                },
                {
                    "timestamp": datetime(2026, 9, 26, 22, 25, tzinfo=timezone.utc),
                    "sleep_level": "awake",
                },
                {
                    "timestamp": datetime(2026, 9, 26, 22, 30, tzinfo=timezone.utc),
                    "sleep_level": "awake",
                },
            ],
            "monitoring": [
                {"steps": 1000, "active_calories": 50},
                {"steps": 2200, "active_calories": 125, "calories": 800},
            ],
            "stress_level": [
                {"stress_level_time": datetime(2026, 9, 27, 7, tzinfo=timezone.utc), "stress_level_value": 20},
                {"stress_level_time": datetime(2026, 9, 27, 8, tzinfo=timezone.utc), "stress_level_value": 40},
                {"stress_level_time": datetime(2026, 9, 27, 9, tzinfo=timezone.utc), "stress_level_value": -2},
            ],
            "spo2_data": [
                {"timestamp": datetime(2026, 9, 27, 7, tzinfo=timezone.utc), "reading_spo2": 97},
                {"timestamp": datetime(2026, 9, 27, 8, tzinfo=timezone.utc), "reading_spo2": 95},
            ],
            "respiration_rate": [
                {"timestamp": datetime(2026, 9, 26, 22, 2, tzinfo=timezone.utc), "respiration_rate": 14},
                {"timestamp": datetime(2026, 9, 26, 22, 7, tzinfo=timezone.utc), "respiration_rate": 16},
                {"timestamp": datetime(2026, 9, 26, 22, 16, tzinfo=timezone.utc), "respiration_rate": 18},
                {"timestamp": datetime(2026, 9, 26, 22, 17, tzinfo=timezone.utc), "respiration_rate": -1},
            ],
            "monitoring_hr_data": [
                {"current_day_resting_heart_rate": 56, "resting_heart_rate": 57}
            ],
        },
    )

    [record] = DailySummaryExtractor().extract([entry])

    assert record.calendar_date == date(2026, 9, 27)
    assert record.sleep_score == 88
    assert record.deep_sleep_seconds == 600
    assert record.sleep_duration_seconds == 1500
    assert record.awake_seconds == 300
    assert record.resting_hr == 56
    assert record.steps == 2200
    assert record.calories_active == 125
    assert record.calories_total == 800
    assert record.avg_stress_level == 30
    assert record.max_stress_level == 40
    assert record.avg_spo2 == 96
    assert record.lowest_spo2 == 95
    assert record.avg_respiration_sleep == 15
    assert record.avg_respiration_waking == 18


def test_workout_extractor_maps_steps_and_estimates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entry = _entry(tmp_path, EntityType.WORKOUT_DEFINITION, "workout-9")
    monkeypatch.setattr(
        garmin_adapter,
        "_read_fit_payload",
        lambda _: {
            "workout": [{"wkt_name": "Tempo", "sport": "running"}],
            "workout_step": [
                {
                    "duration_type": "time",
                    "duration_value": 600,
                    "intensity": "warmup",
                    "target_type": "heart_rate",
                    "custom_target_value_low": 130,
                    "custom_target_value_high": 145,
                },
                {"duration_type": "distance", "duration_value": 1000, "intensity": "interval"},
            ],
        },
    )

    [record] = WorkoutExtractor().extract([entry])

    assert record.workout_title == "Tempo"
    assert record.sport_type is SportType.RUNNING
    assert record.estimated_duration_seconds == 600
    assert record.estimated_distance_meters == 1000
    assert record.steps[0].step_type is StepType.WARMUP
    assert record.steps[0].target_low == 130


def test_workout_repeat_reference_is_not_a_duration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entry = _entry(tmp_path, EntityType.WORKOUT_DEFINITION, "workout-repeat")
    monkeypatch.setattr(
        garmin_adapter,
        "_read_fit_payload",
        lambda _: {
            "workout": [{"wkt_name": "Repeats", "sport": "running"}],
            "workout_step": [
                {"duration_type": "time", "duration_value": 300, "intensity": "active"},
                {"duration_type": "repeat_until_steps_cmplt", "duration_value": 0},
            ],
        },
    )

    [record] = WorkoutExtractor().extract([entry])

    assert record.steps[1].step_type is StepType.REPEAT
    assert record.steps[1].duration_value is None
    assert record.estimated_duration_seconds is None


def test_calendar_extractor_maps_workout_items_only(tmp_path: Path) -> None:
    entry = _entry(tmp_path, EntityType.WORKOUT_CALENDAR, "2026-09")
    Path(entry.file_path).write_text(
        json.dumps(
            {
                "calendarItems": [
                    {
                        "itemType": "workout",
                        "workoutId": 33,
                        "atpPlanId": 7,
                        "title": "Intervals",
                        "date": "2026-09-28",
                        "sportTypeKey": "running",
                        "status": "scheduled",
                    },
                    {"itemType": "activity", "date": "2026-09-28"},
                    {"itemType": "workout", "title": "Invalid date", "date": "n/a"},
                ]
            }
        ),
        encoding="utf-8",
    )

    [record] = CalendarExtractor().extract([entry])

    assert record.calendar_date == date(2026, 9, 28)
    assert record.workout_id == "33"
    assert record.training_plan_id == "7"
    assert record.sport_type is SportType.RUNNING
    assert record.status == "scheduled"


def test_extractor_rejects_entry_from_another_entity(tmp_path: Path) -> None:
    entry = _entry(tmp_path, EntityType.ACTIVITY, "activity-42")

    with pytest.raises(ValueError, match="Expected daily_summary"):
        DailySummaryExtractor().extract([entry])


def test_daily_extractor_does_not_infer_sleep_from_awake_only_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entry = _entry(tmp_path, EntityType.DAILY_SUMMARY, "2026-09-27")
    monkeypatch.setattr(
        garmin_adapter,
        "_read_fit_payload",
        lambda _: {
            "sleep_level": [
                {
                    "timestamp": datetime(2026, 9, 27, 7, tzinfo=timezone.utc),
                    "sleep_level": "awake",
                },
                {
                    "timestamp": datetime(2026, 9, 27, 7, 5, tzinfo=timezone.utc),
                    "sleep_level": "awake",
                },
            ],
            "respiration_rate": [
                {
                    "timestamp": datetime(2026, 9, 27, 7, 2, tzinfo=timezone.utc),
                    "respiration_rate": 15,
                }
            ],
        },
    )

    [record] = DailySummaryExtractor().extract([entry])

    assert record.sleep_duration_seconds is None
    assert record.avg_respiration_sleep is None
    assert record.avg_respiration_waking == 15
