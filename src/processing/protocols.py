from typing import Protocol, TypeVar

import pandera.polars as pa
from pandera.typing.polars import DataFrame

from src.core.schemas import BronzeLedgerEntry

SchemaT = TypeVar("SchemaT", bound=pa.DataFrameModel)


class FormatParser(Protocol):
    """
    Format-specific (not vendor-specific) parser: raw Bronze payloads -> structured records.

    Per architecture guidelines §5, one parser implementation is reused across every
    vendor that shares a payload format (e.g. a single FIT parser for all FIT-producing
    wearables). At this stage records are intentionally plain `dict`s keyed by the
    *format's own* field names (e.g. FIT message field names), not yet Silver-shaped
    Pydantic models: a format-level parser has no knowledge of Silver schemas, entity
    types, or vendor-to-Silver field aliasing — that mapping is entity- and
    vendor-specific and belongs to the `SchemaValidator` stage below (typically via a
    vendor-specific intermediate Pydantic model's `Field(alias=...)` declarations before
    constructing the vendor-agnostic Silver record). Typing this output as a specific
    Pydantic model here would either be a lie (the fields don't match yet) or would leak
    entity-specific knowledge into a component required to stay format-only. `dict` is
    the honest, minimal representation of "decoded, but not yet validated".
    """
    def parse(self, entries: list[BronzeLedgerEntry]) -> list[dict]:
        """Parse raw payloads pointed to by ledger entries into format-native records."""
        ...


class SchemaValidator(Protocol[SchemaT]):
    """
    Validates format-native records against one specific Silver Pydantic/Pandera schema,
    producing a Pandera-typed Polars DataFrame.

    Parametrized by `SchemaT` (a `pandera.polars.DataFrameModel`, e.g. `SilverActivitySchema`)
    so a concrete implementation — e.g. `GarminActivityValidator(SchemaValidator[SilverActivitySchema])` —
    is statically bound to one Silver entity, rather than returning a bare `pl.DataFrame`
    with no compile-time link to the schema it's supposed to satisfy.
    """
    def validate(self, records: list[dict]) -> DataFrame[SchemaT]:
        """Validate records (via the Silver Pydantic model's aliases) into a schema-typed DataFrame."""
        ...


class Writer(Protocol[SchemaT]):
    """
    Writes a schema-typed, validated DataFrame to Hive-partitioned Parquet under Silver.
    """
    def write(self, df: DataFrame[SchemaT], entity_type: str) -> None:
        """Write DataFrame to Silver layer partitioned storage."""
        ...

