"""Coaching Journal: an append-only, per-user chronological timeline (architecture §7).

Stored in the agent state DuckDB file (`data.memory_db_path`). Entries are never
updated in place, so AI narrative cannot silently rewrite history. Stored text is
free-form and must be treated as data, not instructions, by any prompt builder.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

import duckdb

from src.core.config import get_settings
from src.core.duckdb_utils import from_utc_naive, to_utc_naive
from src.core.schemas import EntrySource, JournalCategory, JournalEntry

_COLUMNS = "entry_id, user_id, entry_date, created_at, source, category, text, rating, related_activity_id"


class JournalStore:
    """Manages the `journal_entries` table."""

    def __init__(self, db_path: str | Path | None = None):
        """:param db_path: Defaults to `settings.data.memory_db_path`."""
        self.db_path = str(db_path) if db_path is not None else str(get_settings().data.memory_db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with duckdb.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS journal_entries (
                    entry_id VARCHAR PRIMARY KEY,
                    user_id VARCHAR NOT NULL,
                    entry_date DATE NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    source VARCHAR NOT NULL,
                    category VARCHAR NOT NULL,
                    text VARCHAR NOT NULL,
                    rating INTEGER,
                    related_activity_id VARCHAR
                )
            """)

    def add_entry(
        self,
        *,
        user_id: str,
        entry_date: date,
        source: EntrySource,
        category: JournalCategory,
        text: str,
        rating: int | None = None,
        related_activity_id: str | None = None,
        created_at: datetime | None = None,
    ) -> JournalEntry:
        """Validate and append one entry; the id is assigned here, never by the caller."""
        entry = JournalEntry(
            entry_id=uuid4().hex,
            user_id=user_id,
            entry_date=entry_date,
            created_at=created_at or datetime.now(timezone.utc),
            source=source,
            category=category,
            text=text,
            rating=rating,
            related_activity_id=related_activity_id,
        )
        with duckdb.connect(self.db_path) as conn:
            conn.execute(
                f"INSERT INTO journal_entries ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    entry.entry_id,
                    entry.user_id,
                    entry.entry_date,
                    to_utc_naive(entry.created_at),
                    entry.source.value,
                    entry.category.value,
                    entry.text,
                    entry.rating,
                    entry.related_activity_id,
                ],
            )
        return entry

    def list_entries(
        self,
        user_id: str,
        *,
        start: date | None = None,
        end: date | None = None,
        categories: Sequence[JournalCategory] | None = None,
        source: EntrySource | None = None,
        limit: int = 100,
    ) -> list[JournalEntry]:
        """Newest-first entries for one user, filtered by `entry_date` range, category, and source."""
        if limit < 1:
            raise ValueError("limit must be at least 1.")
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if start is not None:
            clauses.append("entry_date >= ?")
            params.append(start)
        if end is not None:
            clauses.append("entry_date <= ?")
            params.append(end)
        if categories:
            clauses.append(f"category IN ({', '.join('?' for _ in categories)})")
            params.extend(category.value for category in categories)
        if source is not None:
            clauses.append("source = ?")
            params.append(source.value)
        params.append(limit)

        with duckdb.connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT {_COLUMNS} FROM journal_entries
                WHERE {' AND '.join(clauses)}
                ORDER BY entry_date DESC, created_at DESC, entry_id
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            JournalEntry(
                entry_id=row[0],
                user_id=row[1],
                entry_date=row[2],
                created_at=from_utc_naive(row[3]),
                source=row[4],
                category=row[5],
                text=row[6],
                rating=row[7],
                related_activity_id=row[8],
            )
            for row in rows
        ]
