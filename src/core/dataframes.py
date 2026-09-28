"""Pandera/Polars DataFrame contracts for the Silver layer.

Per architecture guidelines §4: "The same schema definition used for record-level
Pydantic validation should also determine the DataFrame column contract — a single
source of truth, not parallel definitions that can drift."  ``derive_schema()`` builds
each ``pandera.polars.DataFrameModel`` by reflecting over the corresponding Pydantic
model in ``core.schemas``, so column names/types/nullability can never diverge between
the two validation layers.

JSON-serialized fields (``dict``, ``list``, nested ``BaseModel``) are auto-detected
rather than maintained in a manual allowlist — adding a nested field to a Pydantic
model automatically maps it to a ``Series[str]`` column without touching this file.

``str``-based ``Enum`` types (``SportType``, ``TrainingStatus``, etc.) are
auto-detected via ``issubclass(annotation, str)`` and mapped to ``Series[str]``.
"""

from __future__ import annotations

import types
from datetime import date, datetime
from typing import Any, Union, get_args, get_origin

import pandera.polars as pa
from pandera.typing.polars import Series
from pydantic import BaseModel

from src.core.schemas import (
    SilverActivity,
    SilverDailySummary,
    SilverWorkoutCalendar,
    SilverWorkoutDefinition,
)

# Python/Pydantic scalar type -> Polars-backed pandera Series[...] annotation.
_TYPE_MAP: dict[type, Any] = {
    str: Series[str],
    int: Series[int],
    float: Series[float],
    bool: Series[bool],
    datetime: Series[datetime],
    date: Series[date],
}


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """Return ``(inner_type, is_optional)`` for ``Optional[X]`` / ``X | None``."""
    if get_origin(annotation) in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _is_json_serialized(annotation: Any) -> bool:
    """Detect types that must be JSON-serialized to a string Parquet column.

    Covers ``dict[...]``, ``list[...]``, and nested ``BaseModel`` subclasses.
    This replaces a manually maintained allowlist — adding a new nested field
    to a Pydantic model now works automatically.
    """
    origin = get_origin(annotation)
    if origin in (list, dict):
        return True
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return True
    return False


def derive_schema(
    model: type[BaseModel],
    *,
    primary_key: list[str] | None = None,
) -> type[pa.DataFrameModel]:
    """Build a ``pandera.polars.DataFrameModel`` from a Silver Pydantic model.

    Field type mapping rules (applied in order):

    1. **JSON-serialized** — ``dict``, ``list``, nested ``BaseModel``
       → ``Series[str]`` (the pipeline serializes these to JSON strings
       before writing Parquet).
    2. **Scalar type map** — ``str``, ``int``, ``float``, ``bool``,
       ``datetime``, ``date`` → their corresponding ``Series[T]``.
    3. **str-Enum fallback** — any ``type`` that is a subclass of ``str``
       (which includes all ``(str, Enum)`` domain enums like ``SportType``,
       ``TrainingStatus``, etc.) → ``Series[str]``.

    :param primary_key: Column(s) enforced unique together
        (e.g. ``["user_id", "activity_id"]``).
    """
    annotations: dict[str, Any] = {}
    namespace: dict[str, Any] = {"__annotations__": annotations}

    for field_name, field_info in model.model_fields.items():
        raw_annotation = field_info.annotation
        annotation, is_optional = _unwrap_optional(raw_annotation)

        # 1. Nested / collection types → JSON string column
        if _is_json_serialized(annotation):
            series_type = Series[str]

        # 2. Direct scalar match
        elif annotation in _TYPE_MAP:
            series_type = _TYPE_MAP[annotation]

        # 3. str-Enum fallback (SportType, TrainingStatus, HrvStatus, ...)
        elif isinstance(annotation, type) and issubclass(annotation, str):
            series_type = Series[str]

        else:
            raise TypeError(
                f"{model.__name__}.{field_name}: no Polars/pandera mapping "
                f"for {annotation!r}. Add an entry to `_TYPE_MAP`, or if "
                f"this is a nested/collection type ensure `_is_json_serialized` "
                f"covers it."
            )

        annotations[field_name] = series_type
        namespace[field_name] = pa.Field(coerce=True, nullable=is_optional)

    if primary_key:
        namespace["Config"] = type(
            "Config", (), {"unique": primary_key, "coerce": True}
        )

    return type(f"{model.__name__}Schema", (pa.DataFrameModel,), namespace)


# ---------------------------------------------------------------------------
# Concrete Silver DataFrame schemas
# ---------------------------------------------------------------------------

SilverActivitySchema = derive_schema(
    SilverActivity, primary_key=["user_id", "activity_id"]
)
SilverDailySummarySchema = derive_schema(
    SilverDailySummary, primary_key=["user_id", "calendar_date"]
)
SilverWorkoutDefinitionSchema = derive_schema(
    SilverWorkoutDefinition, primary_key=["user_id", "workout_id"]
)
SilverWorkoutCalendarSchema = derive_schema(
    SilverWorkoutCalendar, primary_key=["user_id", "calendar_date"]
)
