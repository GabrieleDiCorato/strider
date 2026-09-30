"""Abstract SourceConnector protocol for vendor-specific ingestion adapters.

A SourceConnector is responsible for authenticating with a vendor API,
discovering entities, and downloading raw payloads to the Bronze layer.

The protocol exposes a single ``fetch()`` method parameterised by
``EntityType`` rather than one method per entity family.  This means
adding a new entity type (e.g. body composition, race predictions)
requires adding an ``EntityType`` enum member and a handler inside the
concrete connector — NOT changing the protocol interface.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from src.core.schemas import BronzeLedgerEntry, EntityType


class SourceConnector(Protocol):
    """Protocol for vendor-specific source connectors.

    Implementations must:

    1. ``authenticate()`` — establish a valid session with the vendor API.
    2. ``supported_entities()`` — declare which entity families this
       connector can fetch (so the pipeline orchestrator only requests
       what's available).
    3. ``fetch()`` — download raw payloads for a given entity type and
       date range, persist them to Bronze, and return ledger entries
       describing what was written.
    """

    @property
    def vendor_name(self) -> str:
        """Canonical vendor identifier (e.g. ``'garmin'``)."""
        ...

    def authenticate(self) -> None:
        """Authenticate with the vendor API.

        Implementations should be idempotent — safe to call repeatedly
        without re-authenticating if the session is still valid.
        """
        ...

    def supported_entities(self) -> frozenset[EntityType]:
        """Return the set of entity types this connector can fetch."""
        ...

    def fetch(
        self,
        user_id: str,
        entity_type: EntityType,
        start: date,
        end: date,
    ) -> list[BronzeLedgerEntry]:
        """Fetch raw payloads for *entity_type* over ``[start, end]``.

        Downloads data from the vendor API, persists immutable Bronze
        files to disk, and returns one ``BronzeLedgerEntry`` per file
        written (for the sync ledger).

        :raises ValueError: If *entity_type* is not in
            ``supported_entities()``.
        """
        ...

from pydantic import BaseModel
from datetime import datetime

class RealtimeMetrics(BaseModel):
    heart_rate: int | None = None
    timestamp: datetime

class RealTimeConnector(Protocol):
    """Protocol for fetching real-time data from a source."""
    
    def get_realtime_metrics(self, user_id: str) -> RealtimeMetrics:
        """Poll the source for the most recent available real-time metrics."""
        ...
