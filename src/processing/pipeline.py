"""Vendor-agnostic orchestration from Bronze entries to Silver Parquet."""

from __future__ import annotations

from datetime import date, datetime

from src.core.schemas import BronzeLedgerEntry, EntityType
from src.ingestion.source_connector import SourceConnector
from src.processing.extractor_interfaces import EntityExtractor, Writer


def run_pipeline(
    connector: SourceConnector,
    extractors: dict[EntityType, EntityExtractor],
    writer: Writer,
    user_id: str,
    start: date,
    end: date,
) -> dict[EntityType, int]:
    """Fetch supported entities, extract typed rows, and persist each result.

    The returned mapping contains the number of Silver records extracted per
    entity. Calendar source payloads are complete monthly snapshots, so their
    corresponding month partitions are replaced even when a workout vanished.
    """
    if start > end:
        raise ValueError(f"start ({start}) must not be after end ({end}).")

    supported = connector.supported_entities()
    missing = supported.difference(extractors)
    if missing:
        names = sorted(entity.value for entity in missing)
        raise ValueError(f"No extractor configured for supported entities: {names}.")

    counts: dict[EntityType, int] = {}
    for entity_type in sorted(supported, key=lambda entity: entity.value):
        extractor = extractors[entity_type]
        if extractor.entity_type is not entity_type:
            raise ValueError(
                f"Extractor for {entity_type.value} declares {extractor.entity_type.value}."
            )

        entries = connector.fetch(user_id, entity_type, start, end)
        _validate_entries(entries, user_id, entity_type)
        records = extractor.extract(entries)
        replace_partitions = (
            _calendar_partitions(entries)
            if entity_type is EntityType.WORKOUT_CALENDAR
            else None
        )
        if records or replace_partitions:
            writer.write(
                records,
                entity_type,
                replace_partitions=replace_partitions,
            )
        counts[entity_type] = len(records)

    return counts


def _validate_entries(
    entries: list[BronzeLedgerEntry], user_id: str, entity_type: EntityType
) -> None:
    invalid = [
        entry
        for entry in entries
        if entry.user_id != user_id or entry.entity_type is not entity_type
    ]
    if invalid:
        raise ValueError(
            f"Connector returned entries outside the requested user/entity scope: "
            f"{[(entry.user_id, entry.entity_type.value) for entry in invalid]!r}."
        )


def _calendar_partitions(
    entries: list[BronzeLedgerEntry],
) -> set[tuple[str, int, int]]:
    partitions = set()
    for entry in entries:
        try:
            parsed = datetime.strptime(entry.source_identifier, "%Y-%m")
        except ValueError as exc:
            raise ValueError(
                "Workout calendar source identifiers must use YYYY-MM format; "
                f"received {entry.source_identifier!r}."
            ) from exc
        partitions.add((entry.user_id, parsed.year, parsed.month))
    return partitions