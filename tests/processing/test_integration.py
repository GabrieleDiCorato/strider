from __future__ import annotations
import pytest

from datetime import datetime, timezone
from pathlib import Path

from src.core.schemas import BronzeLedgerEntry, EntityType, SportType
from src.processing.extractors.garmin_adapter import (
    ActivityExtractor,
    DailySummaryExtractor,
)


def _create_ledger_entry(file_path: Path, entity_type: EntityType, source_identifier: str) -> BronzeLedgerEntry:
    return BronzeLedgerEntry(
        user_id="athlete-1",
        vendor="garmin",
        entity_type=entity_type,
        source_identifier=source_identifier,
        content_hash="mock-hash",
        fetch_timestamp=datetime(2026, 9, 28, tzinfo=timezone.utc),
        file_path=str(file_path),
    )


def get_resource_files(prefix: str) -> list[Path]:
    resources_dir = Path(__file__).parent.parent / "resources" / "garmin"
    return list(resources_dir.glob(f"{prefix}_*.zip"))


@pytest.mark.parametrize("activity_zip", get_resource_files("activity"))
def test_real_fit_activity_extraction(activity_zip: Path) -> None:
    assert activity_zip.exists(), f"Missing test resource: {activity_zip}"
    
    activity_id = activity_zip.stem.replace("activity_", "")

    entry = _create_ledger_entry(
        activity_zip, EntityType.ACTIVITY, activity_id
    )

    extractor = ActivityExtractor()
    records = extractor.extract([entry])

    assert len(records) == 1
    record = records[0]

    assert record.user_id == "athlete-1"
    assert record.activity_id == activity_id
    # Basic data checks
    assert record.sport_type is not None
    assert record.duration_seconds is not None and record.duration_seconds > 0
    assert record.start_time is not None
    
    # Optional fields that usually exist in a real file
    if record.distance_meters is not None:
        assert record.distance_meters > 0
    if record.avg_heart_rate is not None:
        assert record.avg_heart_rate > 0
    
    assert record.lap_count is not None and record.lap_count > 0
    assert record.lap_summaries is not None
    assert len(record.lap_summaries) == record.lap_count


@pytest.mark.parametrize("wellness_zip", get_resource_files("wellness"))
def test_real_fit_daily_summary_extraction(wellness_zip: Path) -> None:
    assert wellness_zip.exists(), f"Missing test resource: {wellness_zip}"
    
    calendar_date = wellness_zip.stem.replace("wellness_", "")

    entry = _create_ledger_entry(
        wellness_zip, EntityType.DAILY_SUMMARY, calendar_date
    )

    extractor = DailySummaryExtractor()
    records = extractor.extract([entry])

    assert len(records) == 1
    record = records[0]

    assert record.user_id == "athlete-1"
    assert str(record.calendar_date) == calendar_date
    
    assert record.updated_at is not None
    
    # At least some of these typical metrics should be parsed successfully
    has_some_metric = (
        record.steps is not None
        or record.resting_hr is not None
        or record.sleep_duration_seconds is not None
        or record.avg_stress_level is not None
    )
    assert has_some_metric, "Failed to extract any meaningful metrics from the daily summary FIT file"

