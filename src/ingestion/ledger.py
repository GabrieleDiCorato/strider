from pathlib import Path
from typing import List

import duckdb

from src.core.config import get_settings
from src.core.schemas import BronzeLedgerEntry


class SyncLedger:
    """
    Manages the idempotency and tracking of ingested files via DuckDB.
    """

    def __init__(self, db_path: str | Path | None = None):
        """:param db_path: Defaults to `settings.data.ledger_db_path` (single source of truth for paths)."""
        self.db_path = str(db_path) if db_path is not None else str(get_settings().data.ledger_db_path)
        self._init_db()
        
    def _init_db(self):
        """Initialize the DuckDB connection and ensure the ledger table exists."""
        with duckdb.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_ledger (
                    user_id VARCHAR,
                    vendor VARCHAR,
                    entity_type VARCHAR,
                    source_identifier VARCHAR,
                    content_hash VARCHAR,
                    fetch_timestamp TIMESTAMP,
                    file_path VARCHAR,
                    PRIMARY KEY (user_id, vendor, entity_type, source_identifier)
                )
            """)
            
    def is_already_ingested(self, user_id: str, vendor: str, entity_type: str, source_identifier: str, content_hash: str) -> bool:
        """
        Check if an entity with the exact same content hash has already been ingested.
        """
        with duckdb.connect(self.db_path) as conn:
            result = conn.execute("""
                SELECT content_hash FROM sync_ledger
                WHERE user_id = ? AND vendor = ? AND entity_type = ? AND source_identifier = ?
            """, [user_id, vendor, entity_type, source_identifier]).fetchone()
            
            if result and result[0] == content_hash:
                return True
        return False
        
    def record_ingestion(self, entry: BronzeLedgerEntry) -> None:
        """
        Record a successful fetch in the ledger (upsert).
        """
        with duckdb.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO sync_ledger (user_id, vendor, entity_type, source_identifier, content_hash, fetch_timestamp, file_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id, vendor, entity_type, source_identifier) DO UPDATE SET
                    content_hash = EXCLUDED.content_hash,
                    fetch_timestamp = EXCLUDED.fetch_timestamp,
                    file_path = EXCLUDED.file_path
            """, [
                entry.user_id,
                entry.vendor,
                entry.entity_type,
                entry.source_identifier,
                entry.content_hash,
                entry.fetch_timestamp,
                entry.file_path
            ])
            
    def get_entries(self, user_id: str, vendor: str, entity_type: str) -> List[BronzeLedgerEntry]:
        """
        Retrieve all ledger entries for a given user, vendor, and entity type.
        """
        with duckdb.connect(self.db_path) as conn:
            results = conn.execute("""
                SELECT user_id, vendor, entity_type, source_identifier, content_hash, fetch_timestamp, file_path
                FROM sync_ledger
                WHERE user_id = ? AND vendor = ? AND entity_type = ?
            """, [user_id, vendor, entity_type]).fetchall()
            
            entries = []
            for row in results:
                entries.append(BronzeLedgerEntry(
                    user_id=row[0],
                    vendor=row[1],
                    entity_type=row[2],
                    source_identifier=row[3],
                    content_hash=row[4],
                    fetch_timestamp=row[5],
                    file_path=row[6]
                ))
            return entries
