"""Local persistence for Strider users and their linked vendor accounts.

Backed by the same DuckDB file as the sync ledger (`data.ledger_db_path`): both are
small pieces of local relational state, and DuckDB is the project's single SQL storage
interface (architecture guidelines §1). Never persists vendor secrets — the Garmin
password lives exclusively in `core.config.GarminSettings`, sourced from `.env`.
"""

from datetime import datetime
from pathlib import Path

import duckdb

from src.core.config import get_settings
from src.core.duckdb_utils import from_utc_naive, to_utc_naive
from src.core.schemas import GarminAccountLink, User


class AccountStore:
    """Manages the `users` and `garmin_accounts` tables."""

    def __init__(self, db_path: str | Path | None = None):
        """:param db_path: Defaults to `settings.data.ledger_db_path` (single source of truth for paths)."""
        self.db_path = str(db_path) if db_path is not None else str(get_settings().data.ledger_db_path)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the DuckDB connection and ensure the identity tables exist."""
        with duckdb.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id VARCHAR PRIMARY KEY,
                    display_name VARCHAR,
                    created_at TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS garmin_accounts (
                    user_id VARCHAR PRIMARY KEY REFERENCES users(user_id),
                    email VARCHAR,
                    tokenstore_path VARCHAR,
                    linked_at TIMESTAMP,
                    last_authenticated_at TIMESTAMP
                )
            """)

    def upsert_user(self, user: User) -> None:
        """Insert or update a Strider user row."""
        with duckdb.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO users (user_id, display_name, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT (user_id) DO UPDATE SET display_name = EXCLUDED.display_name
            """, [user.user_id, user.display_name, to_utc_naive(user.created_at)])

    def get_user(self, user_id: str) -> User | None:
        """Look up a Strider user by id."""
        with duckdb.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT user_id, display_name, created_at FROM users WHERE user_id = ?", [user_id]
            ).fetchone()
        if row is None:
            return None
        return User(user_id=row[0], display_name=row[1], created_at=from_utc_naive(row[2]))

    def link_garmin_account(self, link: GarminAccountLink) -> None:
        """Insert or update the Garmin account linked to a user (requires the user to already exist)."""
        with duckdb.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO garmin_accounts (user_id, email, tokenstore_path, linked_at, last_authenticated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (user_id) DO UPDATE SET
                    email = EXCLUDED.email,
                    tokenstore_path = EXCLUDED.tokenstore_path,
                    last_authenticated_at = EXCLUDED.last_authenticated_at
            """, [
                link.user_id,
                link.email,
                link.tokenstore_path,
                to_utc_naive(link.linked_at),
                to_utc_naive(link.last_authenticated_at) if link.last_authenticated_at else None,
            ])

    def get_garmin_account(self, user_id: str) -> GarminAccountLink | None:
        """Look up the Garmin account linked to a user, if any."""
        with duckdb.connect(self.db_path) as conn:
            row = conn.execute("""
                SELECT user_id, email, tokenstore_path, linked_at, last_authenticated_at
                FROM garmin_accounts WHERE user_id = ?
            """, [user_id]).fetchone()
        if row is None:
            return None
        return GarminAccountLink(
            user_id=row[0],
            email=row[1],
            tokenstore_path=row[2],
            linked_at=from_utc_naive(row[3]),
            last_authenticated_at=from_utc_naive(row[4]),
        )

    def record_authentication(self, user_id: str, when: datetime) -> None:
        """Update `last_authenticated_at` after a successful login for this user's Garmin account."""
        with duckdb.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE garmin_accounts SET last_authenticated_at = ? WHERE user_id = ?", [to_utc_naive(when), user_id]
            )
