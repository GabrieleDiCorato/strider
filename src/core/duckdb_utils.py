"""Timezone-safe datetime helpers for DuckDB-backed relational stores.

DuckDB's Python client silently converts a timezone-aware `datetime` bound to
a `TIMESTAMP` column through the local session timezone before storing it as
naive — e.g. a UTC value gets shifted by the machine's local UTC offset and
loses its `tzinfo` entirely. `TIMESTAMPTZ` avoids this but requires the
optional `pytz` dependency and still depends on session timezone state, so
`SyncLedger` and `AccountStore` instead normalize explicitly at the boundary:
every stored value is naive UTC, and every value read back is given
`tzinfo=UTC` again on the way out.
"""

from __future__ import annotations

from datetime import datetime, timezone


def to_utc_naive(value: datetime) -> datetime:
    """Convert *value* to naive UTC for storage in a DuckDB TIMESTAMP column."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def from_utc_naive(value: datetime | None) -> datetime | None:
    """Reattach UTC tzinfo to a naive TIMESTAMP value read back from DuckDB."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc)
