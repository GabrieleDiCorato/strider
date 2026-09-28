from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.agent.journal import JournalStore
from src.core.schemas import EntrySource, JournalCategory


@pytest.fixture
def store(tmp_path: Path) -> JournalStore:
    return JournalStore(tmp_path / "memory.db")


def _add(
    store: JournalStore,
    *,
    user_id: str = "athlete-1",
    entry_date: date = date(2026, 9, 27),
    source: EntrySource = EntrySource.USER,
    category: JournalCategory = JournalCategory.FEELING,
    text: str = "Slept poorly",
    rating: int | None = None,
    related_activity_id: str | None = None,
    created_at: datetime | None = None,
):
    return store.add_entry(
        user_id=user_id,
        entry_date=entry_date,
        source=source,
        category=category,
        text=text,
        rating=rating,
        related_activity_id=related_activity_id,
        created_at=created_at,
    )


def test_entry_round_trips_with_utc_timestamp_and_internal_id(store: JournalStore) -> None:
    created_at = datetime(2026, 9, 28, 8, tzinfo=timezone.utc)
    entry = _add(
        store,
        category=JournalCategory.INJURY,
        text="  Ankle hurts  ",
        rating=4,
        related_activity_id="activity-1",
        created_at=created_at,
    )

    [stored] = store.list_entries("athlete-1")

    assert stored == entry
    assert stored.text == "Ankle hurts"
    assert stored.created_at == created_at
    assert len(entry.entry_id) == 32


def test_entries_persist_across_store_instances(tmp_path: Path) -> None:
    _add(JournalStore(tmp_path / "memory.db"))

    assert len(JournalStore(tmp_path / "memory.db").list_entries("athlete-1")) == 1


def test_list_is_newest_first_and_filters_combine(store: JournalStore) -> None:
    _add(store, entry_date=date(2026, 9, 25), text="old")
    _add(store, entry_date=date(2026, 9, 27), category=JournalCategory.INJURY, text="injury")
    _add(
        store,
        entry_date=date(2026, 9, 28),
        source=EntrySource.AI,
        category=JournalCategory.OBSERVATION,
        text="week summary",
    )

    assert [e.text for e in store.list_entries("athlete-1")] == ["week summary", "injury", "old"]
    assert [e.text for e in store.list_entries("athlete-1", start=date(2026, 9, 26), end=date(2026, 9, 27))] == [
        "injury"
    ]
    assert [e.text for e in store.list_entries("athlete-1", categories=[JournalCategory.INJURY])] == ["injury"]
    assert [e.text for e in store.list_entries("athlete-1", source=EntrySource.AI)] == ["week summary"]
    assert len(store.list_entries("athlete-1", limit=2)) == 2


def test_users_are_isolated(store: JournalStore) -> None:
    _add(store, user_id="athlete-1")
    _add(store, user_id="athlete-2", text="other")

    assert [e.text for e in store.list_entries("athlete-2")] == ["other"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": EntrySource.AI, "category": JournalCategory.FEELING},
        {"source": EntrySource.USER, "category": JournalCategory.OBSERVATION},
        {"text": "   "},
        {"text": "x" * 4001},
        {"rating": 11},
        {"rating": 0},
        {"user_id": "../escape"},
    ],
)
def test_invalid_entries_are_rejected_and_not_stored(store: JournalStore, overrides: dict) -> None:
    with pytest.raises(ValidationError):
        _add(store, **overrides)

    assert store.list_entries("athlete-1") == []


def test_list_rejects_non_positive_limit(store: JournalStore) -> None:
    with pytest.raises(ValueError, match="limit"):
        store.list_entries("athlete-1", limit=0)
