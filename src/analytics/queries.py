"""DuckDB Gold-layer views over the Silver Parquet layer.

Per architecture guidelines, the Gold layer is "materialized dynamically
via DuckDB views over Silver Parquet" — this module never copies Silver rows
anywhere. The persisted `.duckdb` catalog file (``settings.data.gold_db_path``)
stores only ``CREATE VIEW`` SQL text; every query re-evaluates the underlying
``read_parquet(..., hive_partitioning=true)`` glob at execution time, so a
freshly-run Silver pipeline is visible on the very next query with no rebuild
step.

Two view layers are defined:

1. **Silver passthrough views** (``silver_activity``, ``silver_daily_summary``,
   ``silver_workout_definition``, ``silver_workout_calendar``) — one per
   `EntityType`, reading directly over that entity's Parquet directory. If an
   entity has no Parquet files yet (fresh install, nothing synced), the view
   still exists with the full column contract (reflected from the
   corresponding Silver Pydantic model) and simply yields zero rows, so
   downstream Gold views never need to special-case "table doesn't exist".
2. **Gold aggregate views** (``v_recent_load``, ``v_weekly_readiness``) —
   SQL aggregations for the AI agent, built on top of the passthrough views.
   These are deliberately limited to aggregations DuckDB's SQL engine
   expresses directly (grouping, rolling calendar-window sums, mode/avg).
   The actual ACWR ratio / EWMA-decayed load and other "complex rolling
   window" sports-science math (per the architecture roadmap) belongs in
   `analytics.science` (Polars), which can read `v_recent_load` as its raw
   acute/chronic ingredients rather than recomputing daily aggregates itself.

No view filters by ``user_id`` — per architecture guidelines §4, Gold
computations must never aggregate *across* users, but the views themselves
stay multi-tenant-ready (every aggregate is grouped/partitioned by
``user_id``); callers scope to the active user with a ``WHERE user_id = ?``.

``user_id`` is embedded literally (never percent-encoded) into the
`user_id=...` Hive partition segment, and is safe to round-trip via
`hive_partitioning`'s raw path parsing because `core.schemas.UserId`
constrains every `user_id` to a filesystem/URL-safe charset at construction
time — there is no separate encode/decode step to keep in sync.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
from pydantic import BaseModel

from src.core.config import get_settings
from src.core.dataframes import _is_json_serialized, _unwrap_optional
from src.core.schemas import (
    EntityType,
    SilverActivity,
    SilverDailySummary,
    SilverWorkoutCalendar,
    SilverWorkoutDefinition,
)

_SILVER_MODELS: dict[EntityType, type[BaseModel]] = {
    EntityType.ACTIVITY: SilverActivity,
    EntityType.DAILY_SUMMARY: SilverDailySummary,
    EntityType.WORKOUT_DEFINITION: SilverWorkoutDefinition,
    EntityType.WORKOUT_CALENDAR: SilverWorkoutCalendar,
}

_SILVER_VIEW_NAMES: dict[EntityType, str] = {
    EntityType.ACTIVITY: "silver_activity",
    EntityType.DAILY_SUMMARY: "silver_daily_summary",
    EntityType.WORKOUT_DEFINITION: "silver_workout_definition",
    EntityType.WORKOUT_CALENDAR: "silver_workout_calendar",
}

# Mirrors `core.dataframes._TYPE_MAP` but targets DuckDB SQL types rather than
# Pandera/Polars `Series[...]` annotations.
_DUCKDB_TYPE_MAP: dict[type, str] = {
    str: "VARCHAR",
    int: "BIGINT",
    float: "DOUBLE",
    bool: "BOOLEAN",
    datetime: "TIMESTAMP",
    date: "DATE",
}


def _duckdb_type(annotation: Any) -> str:
    annotation, _ = _unwrap_optional(annotation)
    if _is_json_serialized(annotation):
        return "VARCHAR"
    if annotation in _DUCKDB_TYPE_MAP:
        return _DUCKDB_TYPE_MAP[annotation]
    if isinstance(annotation, type) and issubclass(annotation, str):
        return "VARCHAR"
    raise TypeError(f"No DuckDB type mapping for {annotation!r}.")


def _empty_view_sql(model: type[BaseModel]) -> str:
    """A zero-row `SELECT` with the model's full, correctly-typed column set.

    Used as a bootstrap fallback when an entity's Parquet directory has no
    files yet, so the view still exists with the right schema instead of
    failing `read_parquet`'s glob resolution.
    """
    columns = ", ".join(
        f"CAST(NULL AS {_duckdb_type(field.annotation)}) AS {name}"
        for name, field in model.model_fields.items()
    )
    return f"SELECT {columns} WHERE FALSE"


def _silver_view_sql(silver_dir: Path, entity_type: EntityType) -> str:
    model = _SILVER_MODELS[entity_type]
    entity_dir = silver_dir / entity_type.value
    if not any(entity_dir.glob("**/*.parquet")):
        return _empty_view_sql(model)

    glob = (entity_dir / "**" / "*.parquet").as_posix().replace("'", "''")
    # `year`/`month` are Hive partition segments used only for physical
    # layout; the entity's own date/timestamp fields already carry that
    # information, so they're excluded from the logical Gold-facing view.
    return (
        f"SELECT * EXCLUDE (year, month) FROM read_parquet("
        f"'{glob}', hive_partitioning = true, union_by_name = true)"
    )


def create_silver_views(
    conn: duckdb.DuckDBPyConnection, silver_dir: str | Path | None = None
) -> None:
    """(Re)create one passthrough view per `EntityType` over its Parquet directory.

    Idempotent (`CREATE OR REPLACE VIEW`) — safe to call on every connection.
    """
    resolved_dir = Path(str(silver_dir) if silver_dir is not None else str(get_settings().data.silver_dir))
    for entity_type in EntityType:
        view_name = _SILVER_VIEW_NAMES[entity_type]
        conn.execute(f"CREATE OR REPLACE VIEW {view_name} AS {_silver_view_sql(resolved_dir, entity_type)}")


# ---------------------------------------------------------------------------
# Gold aggregate views
# ---------------------------------------------------------------------------

# Daily training volume per user, plus calendar-window (not row-count) rolling
# 7-day/28-day sums. RANGE framing (rather than ROWS) is required because rest
# days simply have no row in `daily` — a ROWS-based frame would silently
# include more or fewer than N calendar days whenever training days are sparse.
_RECENT_LOAD_SQL = """
WITH daily AS (
    SELECT
        user_id,
        CAST(start_time AS DATE) AS activity_date,
        SUM(distance_meters) AS daily_distance_meters,
        SUM(COALESCE(moving_time_seconds, duration_seconds)) AS daily_duration_seconds,
        SUM(training_effect_aerobic) AS daily_training_effect_aerobic,
        COUNT(*) AS daily_session_count
    FROM silver_activity
    GROUP BY user_id, CAST(start_time AS DATE)
)
SELECT
    user_id,
    activity_date,
    daily_distance_meters,
    daily_duration_seconds,
    daily_training_effect_aerobic,
    daily_session_count,
    SUM(daily_distance_meters) OVER acute AS rolling_7d_distance_meters,
    SUM(daily_duration_seconds) OVER acute AS rolling_7d_duration_seconds,
    SUM(daily_session_count) OVER acute AS rolling_7d_session_count,
    SUM(daily_distance_meters) OVER chronic AS rolling_28d_distance_meters,
    SUM(daily_duration_seconds) OVER chronic AS rolling_28d_duration_seconds,
    SUM(daily_session_count) OVER chronic AS rolling_28d_session_count
FROM daily
WINDOW
    acute AS (
        PARTITION BY user_id ORDER BY activity_date
        RANGE BETWEEN INTERVAL '6 days' PRECEDING AND CURRENT ROW
    ),
    chronic AS (
        PARTITION BY user_id ORDER BY activity_date
        RANGE BETWEEN INTERVAL '27 days' PRECEDING AND CURRENT ROW
    )
ORDER BY user_id, activity_date
"""

# Weekly (Mon-start, matching DuckDB's default ISO week) readiness rollup from
# daily wellness snapshots. Categorical fields (hrv_status, training_status)
# use `mode()` rather than `avg()`; `days_with_data` flags sparse weeks.
_WEEKLY_READINESS_SQL = """
SELECT
    user_id,
    date_trunc('week', calendar_date)::DATE AS week_start,
    COUNT(*) AS days_with_data,
    AVG(sleep_score) AS avg_sleep_score,
    AVG(sleep_duration_seconds) AS avg_sleep_duration_seconds,
    AVG(resting_hr) AS avg_resting_hr,
    AVG(hrv_weekly_avg) AS avg_hrv_weekly_avg,
    mode(hrv_status) AS most_common_hrv_status,
    AVG(avg_stress_level) AS avg_stress_level,
    AVG(body_battery_high) AS avg_body_battery_high,
    AVG(body_battery_low) AS avg_body_battery_low,
    mode(training_status) AS most_common_training_status,
    AVG(training_load_balance) AS avg_training_load_balance,
    AVG(vo2max_running) AS avg_vo2max_running,
    AVG(avg_spo2) AS avg_spo2,
    SUM(steps) AS total_steps,
    AVG(calories_total) AS avg_calories_total
FROM silver_daily_summary
GROUP BY user_id, date_trunc('week', calendar_date)
ORDER BY user_id, week_start
"""


def create_gold_views(conn: duckdb.DuckDBPyConnection) -> None:
    """(Re)create the Gold aggregate views. Requires the Silver views to exist."""
    conn.execute(f"CREATE OR REPLACE VIEW v_recent_load AS {_RECENT_LOAD_SQL}")
    conn.execute(f"CREATE OR REPLACE VIEW v_weekly_readiness AS {_WEEKLY_READINESS_SQL}")


def get_connection(
    db_path: str | Path | None = None, silver_dir: str | Path | None = None
) -> duckdb.DuckDBPyConnection:
    """Open the local Gold-layer DuckDB catalog, (re)creating all views.

    The catalog file stores only view definitions, never data, so there is no
    separate "refresh" step — call this again (or just keep reusing the
    connection) after a pipeline run to pick up newly written partitions.
    """
    settings = get_settings()
    resolved_db_path = str(db_path) if db_path is not None else str(settings.data.gold_db_path)
    resolved_silver_dir = str(silver_dir) if silver_dir is not None else str(settings.data.silver_dir)
    conn = duckdb.connect(resolved_db_path)
    create_silver_views(conn, Path(resolved_silver_dir))
    create_gold_views(conn)
    return conn
