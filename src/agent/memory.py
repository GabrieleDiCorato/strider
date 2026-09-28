"""Static user memory: durable facts, preferences, and goals (architecture §8).

Facts are keyed per user, so writes are idempotent upserts. Because ACTIVE facts are
injected into the system prompt, AI-authored facts start as PROPOSED and are excluded
from `list_facts()` until the user confirms them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from src.core.config import get_settings
from src.core.duckdb_utils import from_utc_naive, to_utc_naive
from src.core.schemas import EntrySource, MemoryCategory, MemoryFact, MemoryStatus

_COLUMNS = "user_id, key, category, value, source, status, created_at, updated_at"


def _to_fact(row: Any) -> MemoryFact:
    return MemoryFact(
        user_id=row[0],
        key=row[1],
        category=row[2],
        value=row[3],
        source=row[4],
        status=row[5],
        created_at=from_utc_naive(row[6]),
        updated_at=from_utc_naive(row[7]),
    )


class MemoryStore:
    """Manages the `memory_facts` table."""

    def __init__(self, db_path: str | Path | None = None):
        """:param db_path: Defaults to `settings.data.memory_db_path`."""
        self.db_path = str(db_path) if db_path is not None else str(get_settings().data.memory_db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with duckdb.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_facts (
                    user_id VARCHAR NOT NULL,
                    key VARCHAR NOT NULL,
                    category VARCHAR NOT NULL,
                    value VARCHAR NOT NULL,
                    source VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (user_id, key)
                )
            """)

    def set_fact(
        self,
        *,
        user_id: str,
        key: str,
        value: str,
        category: MemoryCategory,
        source: EntrySource,
        now: datetime | None = None,
    ) -> MemoryFact:
        """Insert or update a fact.

        USER writes are ACTIVE immediately. AI writes are PROPOSED (an AI edit to a
        confirmed fact therefore needs reconfirmation) and may never overwrite a
        user-authored fact.
        """
        timestamp = now or datetime.now(timezone.utc)
        status = MemoryStatus.ACTIVE if source is EntrySource.USER else MemoryStatus.PROPOSED
        with duckdb.connect(self.db_path) as conn:
            existing_row = conn.execute(
                f"SELECT {_COLUMNS} FROM memory_facts WHERE user_id = ? AND key = ?", [user_id, key]
            ).fetchone()
            existing = _to_fact(existing_row) if existing_row else None
            if existing and existing.source is EntrySource.USER and source is EntrySource.AI:
                raise ValueError(f"AI cannot overwrite the user-authored memory fact '{key}'.")

            fact = MemoryFact(
                user_id=user_id,
                key=key,
                category=category,
                value=value,
                source=source,
                status=status,
                created_at=existing.created_at if existing else timestamp,
                updated_at=timestamp,
            )
            conn.execute(
                f"""
                INSERT INTO memory_facts ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id, key) DO UPDATE SET
                    category = EXCLUDED.category,
                    value = EXCLUDED.value,
                    source = EXCLUDED.source,
                    status = EXCLUDED.status,
                    updated_at = EXCLUDED.updated_at
                """,
                [
                    fact.user_id,
                    fact.key,
                    fact.category.value,
                    fact.value,
                    fact.source.value,
                    fact.status.value,
                    to_utc_naive(fact.created_at),
                    to_utc_naive(fact.updated_at),
                ],
            )
        return fact

    def get_fact(self, user_id: str, key: str) -> MemoryFact | None:
        with duckdb.connect(self.db_path) as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM memory_facts WHERE user_id = ? AND key = ?", [user_id, key]
            ).fetchone()
        return _to_fact(row) if row else None

    def list_facts(self, user_id: str, *, status: MemoryStatus = MemoryStatus.ACTIVE) -> list[MemoryFact]:
        """Facts in one status (default ACTIVE, i.e. safe to inject into prompts), ordered by key."""
        with duckdb.connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM memory_facts WHERE user_id = ? AND status = ? ORDER BY key",
                [user_id, status.value],
            ).fetchall()
        return [_to_fact(row) for row in rows]

    def confirm_fact(self, user_id: str, key: str, *, now: datetime | None = None) -> bool:
        """Promote a PROPOSED fact to ACTIVE. Returns False if there was nothing to confirm."""
        with duckdb.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT status FROM memory_facts WHERE user_id = ? AND key = ?", [user_id, key]
            ).fetchone()
            if row is None or row[0] != MemoryStatus.PROPOSED.value:
                return False
            conn.execute(
                "UPDATE memory_facts SET status = ?, updated_at = ? WHERE user_id = ? AND key = ?",
                [MemoryStatus.ACTIVE.value, to_utc_naive(now or datetime.now(timezone.utc)), user_id, key],
            )
        return True

    def delete_fact(self, user_id: str, key: str) -> bool:
        """Remove a fact (used for user rejection/removal). Returns whether it existed."""
        with duckdb.connect(self.db_path) as conn:
            existed = conn.execute(
                "SELECT 1 FROM memory_facts WHERE user_id = ? AND key = ?", [user_id, key]
            ).fetchone()
            conn.execute("DELETE FROM memory_facts WHERE user_id = ? AND key = ?", [user_id, key])
        return existed is not None
