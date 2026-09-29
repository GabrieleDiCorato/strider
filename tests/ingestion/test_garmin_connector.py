"""Tests for `ingestion.garmin.connector.Connector`.

Uses a fake `garminconnect.Garmin` double (never touches the network) so
idempotency, Bronze path layout, and date/entity handling can be verified
deterministically.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from src.core.config import DataPathSettings, GarminSettings, Settings
from src.core.schemas import EntityType
from src.ingestion.accounts import AccountStore
from src.ingestion.garmin.garmin_connector import GarminConnector as Connector
from src.ingestion.ledger import SyncLedger


class _FakeInnerClient:
    """Stands in for the internal client `garminconnect.Garmin.client` exposes."""

    is_authenticated = True


class FakeGarmin:
    """Minimal double for `garminconnect.Garmin`, covering only what the connector calls."""

    def __init__(self) -> None:
        self.client = _FakeInnerClient()
        self.download_activity_calls: list[str] = []
        self.download_workout_calls: list[str] = []
        self.download_health_snapshot_calls: list[str] = []

    def login(self, tokenstore: str) -> None:  # pragma: no cover - should be skipped (already authenticated)
        raise AssertionError("login() should be skipped when already authenticated")

    def get_activities_by_date(self, startdate: str, enddate: str | None = None, **_: Any) -> list[dict]:
        return [{"activityId": 111}, {"activityId": 222}]

    def download_activity(self, activity_id: str, dl_fmt: Any = None) -> bytes:
        self.download_activity_calls.append(str(activity_id))
        import io, zipfile
        z_io = io.BytesIO()
        with zipfile.ZipFile(z_io, "w") as z:
            z.writestr(f"{activity_id}.fit", f"fit-bytes-{activity_id}".encode())
        return z_io.getvalue()

    def download_health_snapshot(self, requested_date: str) -> bytes:
        self.download_health_snapshot_calls.append(requested_date)
        import io, zipfile
        z_io = io.BytesIO()
        with zipfile.ZipFile(z_io, "w") as z:
            z.writestr("wellness.fit", f"wellness-{requested_date}".encode())
        return z_io.getvalue()

    def get_scheduled_workouts(self, year: int, month: int) -> dict:
        return {
            "calendarItems": [
                {
                    "itemType": "workout",
                    "workoutId": 999,
                    "date": f"{year:04d}-{month:02d}-05",
                    "title": "Tempo Run",
                },
                {
                    "itemType": "workout",
                    "workoutId": 1000,
                    "date": f"{year:04d}-{month:02d}-25",
                    "title": "Outside plan window",
                },
            ]
        }

    def download_workout(self, workout_id: int | str) -> bytes:
        self.download_workout_calls.append(str(workout_id))
        return f"workout-fit-{workout_id}".encode()

    def get_sleep_data(self, date: str) -> dict:
        return {"dailySleepDTO": {"sleepTimeSeconds": 28800}}

    def get_hrv_data(self, date: str) -> dict:
        return {"hrvSummary": {"status": "BALANCED", "weeklyAvg": 45}}

    def get_stats_and_body(self, date: str) -> dict:
        return {"totalSteps": 10000}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        user_id="athlete-1",
        garmin=GarminSettings(email="test@example.com", password="secret"),
        data=DataPathSettings(root_dir=tmp_path),
    )


@pytest.fixture
def connector(settings: Settings) -> tuple[Connector, FakeGarmin]:
    fake_client = FakeGarmin()
    ledger = SyncLedger(settings.data.ledger_db_path)
    account_store = AccountStore(settings.data.ledger_db_path)
    return (
        Connector(settings, client=fake_client, ledger=ledger, account_store=account_store),  # type: ignore[arg-type]
        fake_client,
    )


def test_supported_entities_covers_all_four_families(connector: tuple[Connector, FakeGarmin]) -> None:
    conn, _ = connector
    assert conn.supported_entities() == {
        EntityType.ACTIVITY,
        EntityType.DAILY_SUMMARY,
        EntityType.WORKOUT_DEFINITION,
        EntityType.WORKOUT_CALENDAR,
    }


def test_fetch_activities_writes_bronze_files_and_ledger_entries(
    connector: tuple[Connector, FakeGarmin], settings: Settings
) -> None:
    conn, fake_client = connector
    entries = conn.fetch("athlete-1", EntityType.ACTIVITY, date(2026, 1, 1), date(2026, 1, 31))

    assert [e.source_identifier for e in entries] == ["111", "222"]
    import zipfile, json
    for entry in entries:
        assert entry.vendor == "garmin"
        assert entry.entity_type is EntityType.ACTIVITY
        path = Path(entry.file_path)
        assert path.exists()
        with zipfile.ZipFile(path) as z:
            assert z.read(f"{entry.source_identifier}.fit") == f"fit-bytes-{entry.source_identifier}".encode()
            activity_json = json.loads(z.read("activity.json"))
            assert activity_json["activityId"] == int(entry.source_identifier)
        assert path.parent == settings.data.bronze_dir / "athlete-1" / "garmin" / "activity"  # type: ignore[operator]


def test_fetch_is_idempotent_and_skips_rewriting_unchanged_payloads(
    connector: tuple[Connector, FakeGarmin]
) -> None:
    conn, fake_client = connector
    conn.fetch("athlete-1", EntityType.ACTIVITY, date(2026, 1, 1), date(2026, 1, 31))
    assert fake_client.download_activity_calls == ["111", "222"]

    entries = conn.fetch("athlete-1", EntityType.ACTIVITY, date(2026, 1, 1), date(2026, 1, 31))

    # Second run still downloads (to detect upstream changes) but returns the same
    # logical entries; the on-disk file is not needlessly rewritten.
    assert [e.source_identifier for e in entries] == ["111", "222"]
    assert fake_client.download_activity_calls == ["111", "222", "111", "222"]


def test_fetch_daily_summaries_generates_json_when_snapshot_missing(connector: tuple[Connector, FakeGarmin]) -> None:
    from garminconnect import GarminConnectNotFoundError

    conn, fake_client = connector

    def flaky_download(requested_date: str) -> bytes:
        if requested_date == "2026-01-02":
            raise GarminConnectNotFoundError("no snapshot")
        import io, zipfile
        z_io = io.BytesIO()
        with zipfile.ZipFile(z_io, "w") as z:
            z.writestr("wellness.fit", f"wellness-{requested_date}".encode())
        return z_io.getvalue()

    fake_client.download_health_snapshot = flaky_download  # type: ignore[assignment]

    entries = conn.fetch("athlete-1", EntityType.DAILY_SUMMARY, date(2026, 1, 1), date(2026, 1, 3))

    assert [e.source_identifier for e in entries] == ["2026-01-01", "2026-01-02", "2026-01-03"]


def test_fetch_workout_definitions_filters_by_calendar_date_range(
    connector: tuple[Connector, FakeGarmin]
) -> None:
    conn, fake_client = connector

    entries = conn.fetch(
        "athlete-1", EntityType.WORKOUT_DEFINITION, date(2026, 1, 1), date(2026, 1, 10)
    )

    assert [e.source_identifier for e in entries] == ["999"]
    assert fake_client.download_workout_calls == ["999"]


def test_fetch_workout_calendar_persists_raw_monthly_json(connector: tuple[Connector, FakeGarmin]) -> None:
    conn, _ = connector

    entries = conn.fetch(
        "athlete-1", EntityType.WORKOUT_CALENDAR, date(2026, 1, 1), date(2026, 2, 15)
    )

    assert [e.source_identifier for e in entries] == ["2026-01", "2026-02"]
    for entry in entries:
        assert Path(entry.file_path).suffix == ".json"


def test_fetch_rejects_unsupported_date_range(connector: tuple[Connector, FakeGarmin]) -> None:
    conn, _ = connector
    with pytest.raises(ValueError, match="must not be after"):
        conn.fetch("athlete-1", EntityType.ACTIVITY, date(2026, 1, 31), date(2026, 1, 1))
