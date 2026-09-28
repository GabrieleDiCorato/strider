from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.schemas import GarminAccountLink, User
from src.ingestion.accounts import AccountStore


def test_upsert_user_and_link_garmin_account_round_trip(tmp_path: Path) -> None:
    store = AccountStore(tmp_path / "ledger.duckdb")
    user = User(
        user_id="athlete-1",
        display_name="Athlete One",
        created_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
    )
    store.upsert_user(user)

    assert store.get_user("athlete-1") == user
    assert store.get_user("missing") is None

    link = GarminAccountLink(
        user_id="athlete-1",
        email="athlete@example.com",
        tokenstore_path=str(tmp_path / "auth" / "athlete-1" / "garmin"),
        linked_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
    )
    store.link_garmin_account(link)
    assert store.get_garmin_account("athlete-1") == link

    authenticated_at = datetime(2026, 9, 29, tzinfo=timezone.utc)
    store.record_authentication("athlete-1", authenticated_at)
    stored_link = store.get_garmin_account("athlete-1")
    assert stored_link is not None
    assert stored_link.last_authenticated_at == authenticated_at


def test_datetimes_round_trip_through_non_utc_input_as_utc(tmp_path: Path) -> None:
    """A non-UTC tz-aware input must still compare equal to its UTC equivalent."""
    store = AccountStore(tmp_path / "ledger.duckdb")
    plus_two = timezone(timedelta(hours=2))
    created_at_plus_two = datetime(2026, 9, 28, 12, 0, tzinfo=plus_two)
    store.upsert_user(User(user_id="athlete-1", created_at=created_at_plus_two))

    stored = store.get_user("athlete-1")
    assert stored is not None
    assert stored.created_at == created_at_plus_two
    assert stored.created_at.tzinfo is timezone.utc


def test_upsert_user_is_idempotent_on_display_name(tmp_path: Path) -> None:
    store = AccountStore(tmp_path / "ledger.duckdb")
    created_at = datetime(2026, 9, 28, tzinfo=timezone.utc)
    store.upsert_user(User(user_id="athlete-1", display_name="First", created_at=created_at))
    store.upsert_user(User(user_id="athlete-1", display_name="Second", created_at=created_at))

    user = store.get_user("athlete-1")
    assert user is not None
    assert user.display_name == "Second"
