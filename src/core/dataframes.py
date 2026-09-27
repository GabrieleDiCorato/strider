"""Pandera/Polars DataFrame contracts for the Silver layer.

Per architecture guidelines §4: "The same schema definition used for record-level
Pydantic validation should also determine the DataFrame column contract — a single
source of truth, not parallel definitions that can drift." `derive_schema()` builds
each `pandera.polars.DataFrameModel` by reflecting over the corresponding Pydantic
model in `core.schemas`, so column names/types/nullability can never diverge between
the two validation layers.
"""

import types
from datetime import date, datetime
from typing import Any, Union, get_args, get_origin

import pandera.polars as pa
from pandera.typing.polars import Series
from pydantic import BaseModel

from src.core.schemas import (
    SilverActivity,
    SilverWellness,
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

# Fields typed as dict/list/nested-model in Pydantic have no scalar column
# representation; the pipeline serializes them to a JSON string before writing Parquet.
_JSON_SERIALIZED_FIELDS = {"vendor_specific", "steps"}


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """Return `(inner_type, is_optional)` for `Optional[X]` / `X | None` annotations."""
    if get_origin(annotation) in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def derive_schema(model: type[BaseModel], *, primary_key: list[str] | None = None) -> type[pa.DataFrameModel]:
    """Build a `pandera.polars.DataFrameModel` from a Silver Pydantic model's fields.

    :param primary_key: Column(s) enforced unique together (e.g. `["user_id", "activity_id"]`).
    """
    annotations: dict[str, Any] = {}
    namespace: dict[str, Any] = {"__annotations__": annotations}

    for field_name, field_info in model.model_fields.items():
        annotation, is_optional = _unwrap_optional(field_info.annotation)

        if field_name in _JSON_SERIALIZED_FIELDS:
            series_type = Series[str]
        else:
            series_type = _TYPE_MAP.get(annotation)
            if series_type is None:
                raise TypeError(
                    f"{model.__name__}.{field_name}: no Polars/pandera mapping for "
                    f"{annotation!r}. Add one to `_TYPE_MAP` or `_JSON_SERIALIZED_FIELDS`."
                )

        annotations[field_name] = series_type
        namespace[field_name] = pa.Field(coerce=True, nullable=is_optional)

    if primary_key:
        namespace["Config"] = type("Config", (), {"unique": primary_key, "coerce": True})

    return type(f"{model.__name__}Schema", (pa.DataFrameModel,), namespace)


SilverActivitySchema = derive_schema(SilverActivity, primary_key=["user_id", "activity_id"])
SilverWellnessSchema = derive_schema(SilverWellness, primary_key=["user_id", "calendar_date"])
SilverWorkoutDefinitionSchema = derive_schema(SilverWorkoutDefinition, primary_key=["user_id", "workout_id"])
SilverWorkoutCalendarSchema = derive_schema(SilverWorkoutCalendar, primary_key=["user_id", "calendar_date"])
