from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.schemas import BronzeLedgerEntry, EntityType
from src.ingestion.ledger import SyncLedger


def _entry(fetch_timestamp: datetime) -> BronzeLedgerEntry:
    return BronzeLedgerEntry(
        user_id="athlete-1",
        vendor="garmin",
        entity_type=EntityType.ACTIVITY,
        source_identifier="activity-1",
        content_hash="hash-1",
        fetch_timestamp=fetch_timestamp,
        file_path="/tmp/activity-1.zip",
    )


def test_fetch_timestamp_round_trips_as_utc(tmp_path: Path) -> None:
    ledger = SyncLedger(tmp_path / "ledger.duckdb")
    fetch_timestamp = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    ledger.record_ingestion(_entry(fetch_timestamp))

    [entry] = ledger.get_entries("athlete-1", "garmin", EntityType.ACTIVITY.value)
    assert entry.fetch_timestamp == fetch_timestamp
    assert entry.fetch_timestamp.tzinfo is timezone.utc


def test_fetch_timestamp_round_trips_through_non_utc_input(tmp_path: Path) -> None:
    ledger = SyncLedger(tmp_path / "ledger.duckdb")
    plus_two = timezone(timedelta(hours=2))
    fetch_timestamp = datetime(2026, 9, 28, 14, 0, tzinfo=plus_two)
    ledger.record_ingestion(_entry(fetch_timestamp))

    [entry] = ledger.get_entries("athlete-1", "garmin", EntityType.ACTIVITY.value)
    assert entry.fetch_timestamp == fetch_timestamp
    assert ledger.is_already_ingested("athlete-1", "garmin", EntityType.ACTIVITY.value, "activity-1", "hash-1")
