from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.agent.memory import MemoryStore
from src.core.schemas import EntrySource, MemoryCategory, MemoryStatus

T1 = datetime(2026, 9, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 9, 2, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory.db")


def _set(store: MemoryStore, **overrides):
    values = dict(
        user_id="athlete-1",
        key="run_time",
        value="Prefers morning runs",
        category=MemoryCategory.PREFERENCE,
        source=EntrySource.USER,
        now=T1,
    )
    values.update(overrides)
    return store.set_fact(**values)


def test_user_fact_is_active_and_round_trips(store: MemoryStore) -> None:
    fact = _set(store)

    assert fact.status is MemoryStatus.ACTIVE
    assert store.get_fact("athlete-1", "run_time") == fact
    assert store.list_facts("athlete-1") == [fact]
    assert store.get_fact("athlete-1", "missing") is None


def test_set_fact_is_an_idempotent_upsert_that_keeps_created_at(store: MemoryStore) -> None:
    _set(store)
    updated = _set(store, value="Prefers evening runs", now=T2)

    assert updated.created_at == T1
    assert updated.updated_at == T2
    assert [f.value for f in store.list_facts("athlete-1")] == ["Prefers evening runs"]


def test_ai_facts_are_proposed_and_hidden_until_confirmed(store: MemoryStore) -> None:
    _set(store, key="goal", value="Wants a sub-4 marathon", category=MemoryCategory.GOAL, source=EntrySource.AI)

    assert store.list_facts("athlete-1") == []
    [pending] = store.list_facts("athlete-1", status=MemoryStatus.PROPOSED)
    assert pending.source is EntrySource.AI

    assert store.confirm_fact("athlete-1", "goal", now=T2) is True
    assert [f.key for f in store.list_facts("athlete-1")] == ["goal"]
    assert store.confirm_fact("athlete-1", "goal") is False
    assert store.confirm_fact("athlete-1", "missing") is False


def test_ai_cannot_overwrite_user_fact(store: MemoryStore) -> None:
    _set(store)

    with pytest.raises(ValueError, match="cannot overwrite"):
        _set(store, value="Prefers night runs", source=EntrySource.AI)

    assert store.get_fact("athlete-1", "run_time").value == "Prefers morning runs"


def test_ai_edit_of_confirmed_fact_requires_reconfirmation(store: MemoryStore) -> None:
    _set(store, source=EntrySource.AI)
    store.confirm_fact("athlete-1", "run_time")

    _set(store, value="Prefers lunchtime runs", source=EntrySource.AI, now=T2)

    assert store.list_facts("athlete-1") == []


def test_user_write_takes_over_ai_fact(store: MemoryStore) -> None:
    _set(store, source=EntrySource.AI)
    fact = _set(store, value="Prefers morning runs, always")

    assert fact.source is EntrySource.USER
    assert fact.status is MemoryStatus.ACTIVE


def test_delete_and_user_isolation(store: MemoryStore) -> None:
    _set(store)
    _set(store, user_id="athlete-2", value="Other")

    assert store.delete_fact("athlete-1", "run_time") is True
    assert store.delete_fact("athlete-1", "run_time") is False
    assert store.list_facts("athlete-1") == []
    assert [f.value for f in store.list_facts("athlete-2")] == ["Other"]


@pytest.mark.parametrize(
    "overrides",
    [{"key": "Bad Key"}, {"key": "1st"}, {"value": " "}, {"value": "x" * 1001}, {"user_id": "a/b"}],
)
def test_invalid_facts_are_rejected(store: MemoryStore, overrides: dict) -> None:
    with pytest.raises(ValidationError):
        _set(store, **overrides)
