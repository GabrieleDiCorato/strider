"""Validated Silver persistence to user-scoped Hive-partitioned Parquet."""

from __future__ import annotations

import json
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

import polars as pl
from pydantic import BaseModel

from src.core.config import get_settings
from src.core.dataframes import (
    SilverActivitySchema,
    SilverDailySummarySchema,
    SilverWorkoutCalendarSchema,
    SilverWorkoutDefinitionSchema,
)
from src.core.schemas import (
    EntityType,
    SilverActivity,
    SilverDailySummary,
    SilverWorkoutCalendar,
    SilverWorkoutDefinition,
)

_ENTITY_CONFIG: dict[EntityType, tuple[type[BaseModel], type[Any], tuple[str, ...], str]] = {
    EntityType.ACTIVITY: (
        SilverActivity,
        SilverActivitySchema,
        ("user_id", "activity_id"),
        "start_time",
    ),
    EntityType.DAILY_SUMMARY: (
        SilverDailySummary,
        SilverDailySummarySchema,
        ("user_id", "calendar_date"),
        "calendar_date",
    ),
    EntityType.WORKOUT_DEFINITION: (
        SilverWorkoutDefinition,
        SilverWorkoutDefinitionSchema,
        ("user_id", "workout_id"),
        "updated_at",
    ),
    EntityType.WORKOUT_CALENDAR: (
        SilverWorkoutCalendar,
        SilverWorkoutCalendarSchema,
        ("user_id", "calendar_date"),
        "calendar_date",
    ),
}


class ParquetWriter:
    """Validate Silver records and upsert them into partitioned Parquet files."""

    def __init__(self, silver_dir: str | Path | None = None) -> None:
        if silver_dir is None:
            silver_dir = get_settings().data.silver_dir
        self._silver_dir = Path(silver_dir)

    def write(
        self,
        records: list[BaseModel],
        entity_type: EntityType,
        *,
        replace_partitions: set[tuple[str, int, int]] | None = None,
    ) -> None:
        model_type, dataframe_schema, primary_key, partition_field = _ENTITY_CONFIG[
            entity_type
        ]
        invalid = [record for record in records if not isinstance(record, model_type)]
        if invalid:
            raise TypeError(
                f"{entity_type.value} writer expects {model_type.__name__} records."
            )

        incoming_by_partition: dict[tuple[str, int, int], dict[tuple[Any, ...], dict[str, Any]]] = {}
        for record in records:
            row = _record_to_row(record)
            partition = _record_partition(row, partition_field)
            key = tuple(row[field] for field in primary_key)
            incoming_by_partition.setdefault(partition, {})[key] = row

        replacements = replace_partitions or set()
        if not incoming_by_partition and not replacements:
            return

        entity_dir = self._silver_dir / entity_type.value
        existing_files = _partition_files(entity_dir)
        incoming_keys = {
            key
            for partition_rows in incoming_by_partition.values()
            for key in partition_rows
        }
        affected_partitions = set(incoming_by_partition) | replacements

        for partition, files in existing_files.items():
            for file_path in files:
                stored = pl.read_parquet(file_path, columns=[
                    column for column in primary_key if column != "user_id"
                ])
                stored = stored.with_columns(pl.lit(partition[0]).alias("user_id"))
                if any(
                    tuple(row[field] for field in primary_key) in incoming_keys
                    for row in stored.iter_rows(named=True)
                ):
                    affected_partitions.add(partition)
                    break

        for partition in affected_partitions:
            old_files = existing_files.get(partition, [])
            rows: list[dict[str, Any]] = []
            if partition not in replacements:
                for file_path in old_files:
                    stored = pl.read_parquet(file_path).with_columns(
                        pl.lit(partition[0]).alias("user_id")
                    )
                    rows.extend(
                        row
                        for row in stored.iter_rows(named=True)
                        if tuple(row[field] for field in primary_key) not in incoming_keys
                    )

            rows.extend(incoming_by_partition.get(partition, {}).values())
            if not rows:
                _remove_partition_files(old_files, entity_dir)
                continue

            frame = pl.DataFrame(rows, infer_schema_length=None, strict=False)
            validated = dataframe_schema.validate(frame)
            destination_dir = _partition_dir(self._silver_dir, entity_type, partition)
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / "part.parquet"
            temporary = destination_dir / f".part-{uuid4().hex}.parquet"
            try:
                validated.drop("user_id").write_parquet(temporary)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)

            for old_file in old_files:
                if old_file != destination:
                    old_file.unlink(missing_ok=True)


def _record_to_row(record: BaseModel) -> dict[str, Any]:
    row = record.model_dump(mode="python")
    for name, value in row.items():
        if isinstance(value, Enum):
            row[name] = value.value
        elif isinstance(value, (dict, list, tuple)):
            row[name] = json.dumps(
                value,
                default=_json_default,
                sort_keys=True,
                separators=(",", ":"),
            )
    return row


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"Cannot serialize {type(value).__name__} to JSON.")


def _record_partition(
    row: dict[str, Any], partition_field: str
) -> tuple[str, int, int]:
    value = row[partition_field]
    if isinstance(value, datetime):
        partition_date = value.date()
    elif isinstance(value, date):
        partition_date = value
    else:
        raise TypeError(f"{partition_field} must be a date or datetime value.")
    return row["user_id"], partition_date.year, partition_date.month


def _partition_dir(
    silver_dir: Path, entity_type: EntityType, partition: tuple[str, int, int]
) -> Path:
    # `user_id` is schema-validated (`core.schemas.UserId`) to a filesystem/URL-safe
    # charset, so it's embedded literally here — no percent-encoding round-trip to
    # keep in sync with DuckDB's raw `hive_partitioning` segment parsing.
    user_id, year, month = partition
    return (
        silver_dir
        / entity_type.value
        / f"user_id={user_id}"
        / f"year={year:04d}"
        / f"month={month:02d}"
    )


def _partition_files(
    entity_dir: Path,
) -> dict[tuple[str, int, int], list[Path]]:
    result: dict[tuple[str, int, int], list[Path]] = {}
    if not entity_dir.exists():
        return result
    for file_path in entity_dir.rglob("*.parquet"):
        values = {
            segment.split("=", 1)[0]: segment.split("=", 1)[1]
            for segment in file_path.parts
            if "=" in segment
        }
        if not {"user_id", "year", "month"}.issubset(values):
            continue
        partition = (
            values["user_id"],
            int(values["year"]),
            int(values["month"]),
        )
        result.setdefault(partition, []).append(file_path)
    return result


def _remove_partition_files(files: list[Path], entity_dir: Path) -> None:
    for file_path in files:
        file_path.unlink(missing_ok=True)
        directory = file_path.parent
        while directory != entity_dir:
            try:
                directory.rmdir()
            except OSError:
                break
            directory = directory.parent