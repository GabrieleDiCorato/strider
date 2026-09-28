from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from src.core.schemas import (
    BronzeLedgerEntry,
    EntityType,
    SilverActivity,
    SilverDailySummary,
    SilverWorkoutCalendar,
    SportType,
)
from src.processing.pipeline import run_pipeline
from src.processing.writer import ParquetWriter


def _daily(date_value: date, *, steps: int, user_id: str = "athlete-1") -> SilverDailySummary:
    return SilverDailySummary(
        user_id=user_id,
        updated_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
        calendar_date=date_value,
        steps=steps,
        vendor_specific={"source": "garmin", "values": [1, 2]},
    )


def _activity(start_time: datetime, *, user_id: str = "athlete-1") -> SilverActivity:
    return SilverActivity(
        user_id=user_id,
        updated_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
        activity_id="activity-1",
        sport_type=SportType.RUNNING,
        start_time=start_time,
        distance_meters=5000,
    )


def test_writer_validates_serializes_and_upserts_by_primary_key(tmp_path: Path) -> None:
    writer = ParquetWriter(tmp_path)
    writer.write([_daily(date(2026, 9, 27), steps=100)], EntityType.DAILY_SUMMARY)
    writer.write(
        [
            _daily(date(2026, 9, 27), steps=200),
            _daily(date(2026, 9, 28), steps=300),
        ],
        EntityType.DAILY_SUMMARY,
    )

    partition_file = (
        tmp_path
        / "daily_summary"
        / "user_id=athlete-1"
        / "year=2026"
        / "month=09"
        / "part.parquet"
    )
    stored = pl.read_parquet(partition_file).sort("calendar_date")
    assert stored["steps"].to_list() == [200, 300]
    assert "user_id" not in stored.columns
    assert stored["vendor_specific"][0] == '{"source":"garmin","values":[1,2]}'

    with pytest.raises(TypeError, match="expects SilverDailySummary"):
        writer.write([_activity(datetime(2026, 9, 27, tzinfo=timezone.utc))], EntityType.DAILY_SUMMARY)


def test_writer_moves_changed_record_between_partitions(tmp_path: Path) -> None:
    writer = ParquetWriter(tmp_path)
    writer.write([_activity(datetime(2026, 8, 31, tzinfo=timezone.utc))], EntityType.ACTIVITY)
    writer.write([_activity(datetime(2026, 9, 1, tzinfo=timezone.utc))], EntityType.ACTIVITY)

    old_partition = tmp_path / "activity" / "user_id=athlete-1" / "year=2026" / "month=08"
    new_file = (
        tmp_path
        / "activity"
        / "user_id=athlete-1"
        / "year=2026"
        / "month=09"
        / "part.parquet"
    )
    assert not old_partition.exists()
    assert pl.read_parquet(new_file).height == 1


def test_writer_replaces_calendar_month_including_removed_rows(tmp_path: Path) -> None:
    writer = ParquetWriter(tmp_path)
    first = SilverWorkoutCalendar(
        user_id="athlete-1",
        updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        calendar_date=date(2026, 9, 10),
        workout_id="workout-1",
    )
    removed = first.model_copy(update={"calendar_date": date(2026, 9, 11), "workout_id": "workout-2"})
    writer.write([first, removed], EntityType.WORKOUT_CALENDAR)
    writer.write(
        [first],
        EntityType.WORKOUT_CALENDAR,
        replace_partitions={("athlete-1", 2026, 9)},
    )

    stored = pl.read_parquet(
        tmp_path
        / "workout_calendar"
        / "user_id=athlete-1"
        / "year=2026"
        / "month=09"
        / "part.parquet"
    )
    assert stored["calendar_date"].to_list() == [date(2026, 9, 10)]


class _Connector:
    vendor_name = "test"

    def __init__(self, entries: list[BronzeLedgerEntry]) -> None:
        self.entries = entries

    def supported_entities(self) -> frozenset[EntityType]:
        return frozenset({EntityType.WORKOUT_CALENDAR})

    def fetch(
        self, user_id: str, entity_type: EntityType, start: date, end: date
    ) -> list[BronzeLedgerEntry]:
        return self.entries


class _CalendarExtractor:
    entity_type = EntityType.WORKOUT_CALENDAR

    def extract(self, entries: list[BronzeLedgerEntry]) -> list[SilverWorkoutCalendar]:
        return []


class _RecordingWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[list[object], EntityType, set[tuple[str, int, int]] | None]] = []

    def write(
        self,
        records: list[object],
        entity_type: EntityType,
        *,
        replace_partitions: set[tuple[str, int, int]] | None = None,
    ) -> None:
        self.calls.append((records, entity_type, replace_partitions))


def test_pipeline_replaces_empty_calendar_month(tmp_path: Path) -> None:
    entry = BronzeLedgerEntry(
        user_id="athlete-1",
        vendor="test",
        entity_type=EntityType.WORKOUT_CALENDAR,
        source_identifier="2026-09",
        content_hash="hash",
        fetch_timestamp=datetime(2026, 9, 28, tzinfo=timezone.utc),
        file_path=str(tmp_path / "calendar.json"),
    )
    writer = _RecordingWriter()

    result = run_pipeline(
        _Connector([entry]),
        {EntityType.WORKOUT_CALENDAR: _CalendarExtractor()},
        writer,
        "athlete-1",
        date(2026, 9, 1),
        date(2026, 9, 30),
    )

    assert result == {EntityType.WORKOUT_CALENDAR: 0}
    assert writer.calls == [([], EntityType.WORKOUT_CALENDAR, {("athlete-1", 2026, 9)})]