"""Garmin Connect implementation of `ingestion.protocols.SourceConnector`.

Owns entity discovery and Bronze payload download for the four `EntityType`
families confirmed via `notebooks/garminconnect_test.ipynb`:

* ``ACTIVITY``            -> zipped original FIT, via ``download_activity(..., ORIGINAL)``.
* ``DAILY_SUMMARY``       -> zipped FIT wellness snapshot, via ``download_health_snapshot``.
* ``WORKOUT_DEFINITION``  -> raw (unzipped) FIT, via ``download_workout``.
* ``WORKOUT_CALENDAR``    -> raw JSON, via ``get_scheduled_workouts`` (month-granular).

Authentication is delegated to `ingestion.garmin.auth` (cooldown-guarded login);
this module only decides *what* to fetch and *where* to persist it in Bronze.

Idempotency: every downloaded payload is hashed and checked against the
`sync_ledger` (`ingestion.ledger.SyncLedger`) before being written to disk, per
architecture guidelines §1 ("every ingestion/processing step must consult and
update the sync_ledger"). Unchanged payloads are not rewritten, but a
`BronzeLedgerEntry` is still returned so callers always get a complete picture
of the requested range.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from garminconnect import (
    Garmin,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
)

from src.core.config import Settings
from src.core.schemas import BronzeLedgerEntry, EntityType
from src.ingestion.accounts import AccountStore
from src.ingestion.garmin.garmin_auth import build_client, login
from src.ingestion.ledger import SyncLedger

logger = logging.getLogger(__name__)

_VENDOR_NAME = "garmin"


def _date_range(start: date, end: date) -> Iterator[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _months_between(start: date, end: date) -> Iterator[tuple[int, int]]:
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


class GarminConnector:
    """`SourceConnector` for Garmin Connect, backed by the `garminconnect` library."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: Garmin | None = None,
        ledger: SyncLedger | None = None,
        account_store: AccountStore | None = None,
    ) -> None:
        self._settings = settings
        self._client = client if client is not None else build_client(settings)
        self._ledger = ledger if ledger is not None else SyncLedger(settings.data.ledger_db_path)
        self._account_store = (
            account_store if account_store is not None else AccountStore(settings.data.ledger_db_path)
        )
        self._bronze_dir = Path(settings.data.bronze_dir)  # type: ignore[arg-type]
        self._last_request_at = 0.0

    @property
    def vendor_name(self) -> str:
        return _VENDOR_NAME

    def supported_entities(self) -> frozenset[EntityType]:
        return frozenset(
            {
                EntityType.ACTIVITY,
                EntityType.DAILY_SUMMARY,
                EntityType.WORKOUT_DEFINITION,
                EntityType.WORKOUT_CALENDAR,
            }
        )

    def authenticate(self) -> None:
        """Idempotent login; safe to call before every `fetch()`."""
        login(self._client, self._settings)
        self._account_store.record_authentication(self._settings.user_id, datetime.now(timezone.utc))

    def fetch(
        self,
        user_id: str,
        entity_type: EntityType,
        start: date,
        end: date,
    ) -> list[BronzeLedgerEntry]:
        if start > end:
            raise ValueError(f"start ({start}) must not be after end ({end}).")
        if entity_type not in self.supported_entities():
            raise ValueError(
                f"{entity_type!r} is not supported by GarminConnector. "
                f"Supported: {sorted(e.value for e in self.supported_entities())}"
            )

        self.authenticate()

        if entity_type is EntityType.ACTIVITY:
            return self._fetch_activities(user_id, start, end)
        if entity_type is EntityType.DAILY_SUMMARY:
            return self._fetch_daily_summaries(user_id, start, end)
        if entity_type is EntityType.WORKOUT_DEFINITION:
            return self._fetch_workout_definitions(user_id, start, end)
        return self._fetch_workout_calendar(user_id, start, end)

    # ------------------------------------------------------------------
    # Entity handlers
    # ------------------------------------------------------------------

    def _fetch_activities(self, user_id: str, start: date, end: date) -> list[BronzeLedgerEntry]:
        raw_activities = self._call(
            self._client.get_activities_by_date, start.isoformat(), end.isoformat()
        )
        entries = []
        for activity in raw_activities:
            activity_id = str(activity["activityId"])
            payload = self._call(
                self._client.download_activity,
                activity_id,
                dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL,
            )
            entries.append(
                self._persist_bronze_payload(
                    user_id=user_id,
                    entity_type=EntityType.ACTIVITY,
                    source_identifier=activity_id,
                    payload=payload,
                    suffix=".zip",
                )
            )
        return entries

    def _fetch_daily_summaries(self, user_id: str, start: date, end: date) -> list[BronzeLedgerEntry]:
        entries = []
        for day in _date_range(start, end):
            try:
                payload = self._call(self._client.download_health_snapshot, day.isoformat())
            except (GarminConnectNotFoundError, GarminConnectConnectionError) as e:
                logger.info("No wellness snapshot available for %s: %s", day, e)
                continue
            entries.append(
                self._persist_bronze_payload(
                    user_id=user_id,
                    entity_type=EntityType.DAILY_SUMMARY,
                    source_identifier=day.isoformat(),
                    payload=payload,
                    suffix=".zip",
                )
            )
        return entries

    def _fetch_workout_calendar(self, user_id: str, start: date, end: date) -> list[BronzeLedgerEntry]:
        entries = []
        for year, month in _months_between(start, end):
            calendar = self._call(self._client.get_scheduled_workouts, year, month)
            source_identifier = f"{year:04d}-{month:02d}"
            entries.append(
                self._persist_bronze_payload(
                    user_id=user_id,
                    entity_type=EntityType.WORKOUT_CALENDAR,
                    source_identifier=source_identifier,
                    payload=_to_json_bytes(calendar),
                    suffix=".json",
                )
            )
        return entries

    def _fetch_workout_definitions(self, user_id: str, start: date, end: date) -> list[BronzeLedgerEntry]:
        workout_ids = self._discover_scheduled_workout_ids(start, end)
        entries = []
        for workout_id in workout_ids:
            payload = self._call(self._client.download_workout, workout_id)
            entries.append(
                self._persist_bronze_payload(
                    user_id=user_id,
                    entity_type=EntityType.WORKOUT_DEFINITION,
                    source_identifier=workout_id,
                    payload=payload,
                    suffix=".fit",
                )
            )
        return entries

    def _discover_scheduled_workout_ids(self, start: date, end: date) -> list[str]:
        """Workout ids scheduled within ``[start, end]``, discovered via the monthly calendar.

        Workout *definitions* aren't inherently date-ranged (a saved workout can be
        scheduled at any time), so we use the calendar as the date-scoping source of
        truth, matching the activity <-> workout linkage confirmed in the deep-dive
        notebook.
        """
        ids: dict[str, None] = {}  # ordered de-dup
        for year, month in _months_between(start, end):
            calendar = self._call(self._client.get_scheduled_workouts, year, month)
            for item in calendar.get("calendarItems", []):
                if item.get("itemType") != "workout" or item.get("workoutId") is None:
                    continue
                item_date = item.get("date")
                if item_date is None or not (start.isoformat() <= item_date <= end.isoformat()):
                    continue
                ids[str(item["workoutId"])] = None
        return list(ids)

    # ------------------------------------------------------------------
    # Shared plumbing
    # ------------------------------------------------------------------

    def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Invoke a `garminconnect` client method with client-side throttling applied."""
        min_interval = self._settings.rate_limit.min_request_interval_seconds
        if min_interval > 0:
            remaining = min_interval - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        try:
            return fn(*args, **kwargs)
        finally:
            self._last_request_at = time.monotonic()

    def _bronze_path(self, user_id: str, entity_type: EntityType, source_identifier: str, suffix: str) -> Path:
        safe_id = source_identifier.replace("/", "_")
        return self._bronze_dir / user_id / self.vendor_name / entity_type.value / f"{safe_id}{suffix}"

    def _persist_bronze_payload(
        self,
        *,
        user_id: str,
        entity_type: EntityType,
        source_identifier: str,
        payload: bytes,
        suffix: str,
    ) -> BronzeLedgerEntry:
        """Hash *payload*, write it to Bronze if new/changed, and upsert the sync ledger."""
        content_hash = hashlib.sha256(payload).hexdigest()
        file_path = self._bronze_path(user_id, entity_type, source_identifier, suffix)

        if not self._ledger.is_already_ingested(
            user_id, self.vendor_name, entity_type.value, source_identifier, content_hash
        ):
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(payload)

        entry = BronzeLedgerEntry(
            user_id=user_id,
            vendor=self.vendor_name,
            entity_type=entity_type,
            source_identifier=source_identifier,
            content_hash=content_hash,
            fetch_timestamp=datetime.now(timezone.utc),
            file_path=str(file_path),
        )
        self._ledger.record_ingestion(entry)
        return entry


def _to_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
