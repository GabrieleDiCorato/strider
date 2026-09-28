"""Processing-layer protocols: EntityExtractor and Writer.

The ingestion-to-Silver pipeline is composed of three stages:

1. **SourceConnector** (``ingestion.source_connector``) — vendor-specific: auth,
   entity discovery, raw payload download → Bronze.
2. **EntityExtractor** (this module) — entity-specific, format-aware:
   reads Bronze payloads (FIT binary via ``fitdecode``, JSON via
   ``json.loads``) and produces typed Silver Pydantic models directly.
3. **Writer** (this module) — universal: validates and persists Silver
   records to Hive-partitioned Parquet.

This three-stage design collapses the former ``FormatParser`` +
``SchemaValidator`` two-step into a single ``EntityExtractor`` that is
fully typed from input to output.  The rationale:

- ``fitdecode`` already IS the format parser — it returns typed
  ``FitDataMessage`` objects.  Wrapping it in our own ``FormatParser``
  protocol added an abstraction layer over a library that already
  provides the abstraction.
- The intermediate ``list[dict]`` representation between the old
  ``FormatParser`` and ``SchemaValidator`` was a type-safety gap —
  going from typed FIT messages → untyped dicts → re-typed Silver
  models was a round-trip through the untyped world.
- The field mapping from FIT message fields to Silver schema columns is
  inherently entity-specific (and possibly vendor-specific), so it
  cannot be separated from the entity knowledge anyway.

Pipeline composition example::

    connector = GarminConnector(settings)
    extractors: dict[EntityType, EntityExtractor] = {
        EntityType.ACTIVITY: GarminActivityExtractor(),
        EntityType.DAILY_SUMMARY: GarminDailySummaryExtractor(),
        EntityType.WORKOUT_DEFINITION: GarminWorkoutExtractor(),
        EntityType.WORKOUT_CALENDAR: GarminCalendarExtractor(),
    }
    writer = ParquetWriter(settings)

    for entity_type in connector.supported_entities():
        entries = connector.fetch(user_id, entity_type, start, end)
        records = extractors[entity_type].extract(entries)
        writer.write(records, entity_type)
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

from src.core.schemas import BronzeLedgerEntry, EntityType

SilverT = TypeVar("SilverT", bound=BaseModel)


class EntityExtractor(Protocol[SilverT]):
    """Extracts Silver-layer records from raw Bronze payloads.

    Each implementation is bound to ONE Silver entity type (via ``SilverT``)
    and knows both the source format (FIT binary, JSON) and the field
    mapping to Silver columns.

    This is entity-specific AND format-aware — a
    ``GarminActivityExtractor`` knows that Garmin FIT session messages
    map to ``SilverActivity`` fields.  That's not a violation of
    separation of concerns — it's an honest acknowledgment that the
    mapping IS the domain logic.
    """

    @property
    def entity_type(self) -> EntityType:
        """The Silver entity type this extractor produces."""
        ...

    def extract(self, entries: list[BronzeLedgerEntry]) -> list[SilverT]:
        """Extract typed Silver records from Bronze ledger entries.

        Reads the raw payloads pointed to by *entries* (FIT files,
        JSON files), parses them using the appropriate format library
        (``fitdecode``, ``json``), maps fields to the Silver schema,
        and returns fully validated Pydantic model instances.

        :param entries: Bronze ledger entries whose ``file_path`` fields
            point to the raw payloads on disk.
        :returns: One Silver record per logical entity found in the
            payloads (e.g. one ``SilverActivity`` per FIT session
            message, one ``SilverDailySummary`` per day's wellness
            FIT files).
        """
        ...


class Writer(Protocol[SilverT]):
    """Validates and writes Silver records to Hive-partitioned Parquet.

    Receives typed Pydantic model instances from an ``EntityExtractor``,
    converts them to a Polars DataFrame, validates against the Pandera
    schema (from ``core.dataframes``), and writes to the appropriate
    Silver partition path.
    """

    def write(
        self,
        records: list[SilverT],
        entity_type: EntityType,
        *,
        replace_partitions: set[tuple[str, int, int]] | None = None,
    ) -> None:
        """Write validated Silver records to partitioned Parquet.

        :param records: Typed Silver Pydantic model instances.
        :param entity_type: Determines the Parquet partition path
            (``silver/{entity_type}/user_id=.../year=.../...``).
        :param replace_partitions: Optional complete partition scopes to
            replace, expressed as ``(user_id, year, month)``. Used when a
            source payload represents a complete partition, such as a monthly
            workout calendar that may no longer contain previously seen rows.
        """
        ...
