"""Polars-based Gold-layer science metrics: EWMA ACWR, Grade Adjusted Pace, cardiac drift.

Per architecture guidelines §1/§6, this module is the Polars half of the Gold layer's
"Strict Separation of Concerns" split: `analytics.queries` (DuckDB) handles SQL
aggregation and calendar-window rolling sums over Silver Parquet; this module handles
the genuinely complex time-series math — EWMA decay (ACWR), per-sample physiological
modeling (grade-adjusted pace, cardiac drift) — that doesn't reduce cleanly to SQL.

Grade Adjusted Pace and cardiac drift both read raw Bronze FIT payloads on-demand
(§6 Gold Layer: "Gold computations may also read directly from Bronze FIT files ...
where materializing the raw time-series in Silver would be wasteful"), since Silver
only stores pre-aggregated per-activity summaries, not 1Hz record-level series.

Vendor-blindness (§5): this module parses generic FIT 'record' messages via
`fitdecode` directly rather than importing `processing.extractors.garmin_adapter`
(vendor-specific), even though FIT happens to be the only format currently ingested.

Every public function returns a small, pre-aggregated Pydantic result — never a raw
per-sample DataFrame — per §8's context-window protection principle: whatever calls
these (eventually an agent tool) must never receive 1Hz time-series.

Training-load and physiological metrics here are estimates built on named, published
methods, not diagnoses:
- ACWR uses the EWMA formulation (Williams et al. 2016), the documented improvement
  over naive rolling-average ACWR — which is what `analytics.queries.v_recent_load`
  exposes as raw acute/chronic ingredients for simpler SQL consumers.
- Grade Adjusted Pace uses the widely-cited Minetti et al. (2002) polynomial
  approximation of the energy cost of locomotion on gradients.
- Cardiac drift (aerobic decoupling) follows the standard first-half/second-half
  HR:speed ratio comparison.
"""

from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import fitdecode
import polars as pl
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# ACWR (Acute:Chronic Workload Ratio) — EWMA, per Williams et al. 2016
# ---------------------------------------------------------------------------


class AcwrResult(BaseModel):
    """One user/day's EWMA acute:chronic workload ratio.

    `acwr` is `None` (never 0/inf) when chronic load is exactly zero — no
    meaningful ratio exists yet, a genuine "unknown" rather than a risk score.
    """

    user_id: str
    activity_date: date
    daily_load: float
    ewma_acute: float
    ewma_chronic: float
    acwr: float | None
    insufficient_history: bool = Field(
        description="True until at least `chronic_days` of calendar history exist for this user."
    )


def daily_training_load(activities: pl.DataFrame) -> pl.DataFrame:
    """A simple, documented session-load proxy from Silver activity rows.

    `activities` must carry `user_id`, `start_time`, and ideally
    `moving_time_seconds`/`duration_seconds` plus `training_effect_aerobic` — the
    same columns the `silver_activity` Gold view exposes. Per-session load is
    `duration_minutes * training_effect_aerobic`, summed per calendar day.

    This is a proxy, not a validated TRIMP/session-RPE score: Garmin's aerobic
    training effect (0.0-5.0) substitutes for the subjective RPE most session-load
    formulas use, since Silver doesn't capture perceived exertion. Missing aerobic
    TE falls back to an intensity factor of 1.0 (duration alone) rather than
    silently dropping the session. Treat the output as a relative, within-athlete
    trend indicator, not an absolute training-stress unit.
    """
    if "user_id" not in activities.columns or "start_time" not in activities.columns:
        raise ValueError("`activities` must have 'user_id' and 'start_time' columns.")

    duration_expr = (
        pl.coalesce(pl.col("moving_time_seconds"), pl.col("duration_seconds"))
        if {"moving_time_seconds", "duration_seconds"}.issubset(activities.columns)
        else pl.col(
            "moving_time_seconds" if "moving_time_seconds" in activities.columns else "duration_seconds"
        )
    )
    intensity_expr = (
        pl.coalesce(pl.col("training_effect_aerobic"), pl.lit(1.0))
        if "training_effect_aerobic" in activities.columns
        else pl.lit(1.0)
    )

    return (
        activities.with_columns(pl.col("start_time").cast(pl.Date).alias("activity_date"))
        .with_columns(((duration_expr.fill_null(0.0) / 60.0) * intensity_expr).alias("_session_load"))
        .group_by("user_id", "activity_date")
        .agg(pl.col("_session_load").sum().alias("daily_load"))
    )


def compute_acwr(
    daily_load: pl.DataFrame,
    *,
    load_column: str = "daily_load",
    date_column: str = "activity_date",
    acute_days: int = 7,
    chronic_days: int = 28,
) -> list[AcwrResult]:
    """EWMA-based ACWR per user, from a (possibly sparse) per-day load table.

    Calendar days between each user's first and last recorded day are densified
    with zero load (a genuine rest day) before computing the EWMA — a sparse date
    axis would otherwise understate how much decay should occur across a gap, the
    same correctness concern `analytics.queries.v_recent_load` handles with RANGE
    framing, addressed here via dense resampling instead of a SQL window frame.
    """
    if daily_load.is_empty():
        return []
    required = {"user_id", date_column, load_column}
    missing = required - set(daily_load.columns)
    if missing:
        raise ValueError(f"`daily_load` is missing required column(s): {sorted(missing)}")

    bounds = daily_load.group_by("user_id").agg(
        pl.col(date_column).min().alias("_min_date"),
        pl.col(date_column).max().alias("_max_date"),
    )
    dense = bounds.select(
        "user_id",
        pl.date_ranges(pl.col("_min_date"), pl.col("_max_date"), interval="1d").alias(date_column),
    ).explode(date_column, empty_as_null=True)

    merged = (
        dense.join(
            daily_load.select("user_id", date_column, load_column),
            on=["user_id", date_column],
            how="left",
        )
        .with_columns(pl.col(load_column).fill_null(0.0))
        .sort(["user_id", date_column])
        .with_columns(
            pl.col(load_column).ewm_mean(span=acute_days).over("user_id").alias("ewma_acute"),
            pl.col(load_column).ewm_mean(span=chronic_days).over("user_id").alias("ewma_chronic"),
            pl.col(load_column).cum_count().over("user_id").alias("_day_number"),
        )
        .with_columns(
            pl.when(pl.col("ewma_chronic") > 0)
            .then(pl.col("ewma_acute") / pl.col("ewma_chronic"))
            .otherwise(None)
            .alias("acwr"),
            (pl.col("_day_number") < chronic_days).alias("insufficient_history"),
        )
    )

    return [
        AcwrResult(
            user_id=row["user_id"],
            activity_date=row[date_column],
            daily_load=row[load_column],
            ewma_acute=row["ewma_acute"],
            ewma_chronic=row["ewma_chronic"],
            acwr=row["acwr"],
            insufficient_history=row["insufficient_history"],
        )
        for row in merged.iter_rows(named=True)
    ]


# ---------------------------------------------------------------------------
# On-demand Bronze FIT reader
# ---------------------------------------------------------------------------


def _read_fit_records(file_path: str | Path) -> pl.DataFrame:
    """Read FIT 'record' messages (the ~1Hz time-series) from a Bronze payload.

    Handles both raw `.fit` files and zip-wrapped payloads (Garmin's activity
    download format). This is generic FIT parsing via `fitdecode`, not
    vendor-specific mapping logic — see the module docstring's vendor-blindness note.
    """
    path = Path(file_path)
    rows: list[dict[str, Any]] = []

    def consume(stream: Any) -> None:
        with fitdecode.FitReader(stream) as reader:
            for message in reader:
                if isinstance(message, fitdecode.FitDataMessage) and message.name == "record":
                    rows.append({field.name: field.value for field in message.fields})

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.lower().endswith(".fit"):
                    with archive.open(name) as stream:
                        consume(stream)
    else:
        with path.open("rb") as stream:
            consume(stream)

    if not rows:
        raise ValueError(f"No FIT 'record' messages found in {path}.")
    return pl.DataFrame(rows, infer_schema_length=None, strict=False)


# ---------------------------------------------------------------------------
# Grade Adjusted Pace — Minetti et al. 2002 energy-cost-of-locomotion polynomial
# ---------------------------------------------------------------------------

# Descending powers of grade i (i^5 .. i^0), widely cited approximation of
# Minetti et al.'s energy cost of locomotion on gradients (J/kg/m).
_MINETTI_COEFFICIENTS = (155.4, -30.4, -43.3, 46.3, 19.5, 3.6)


def _minetti_cost(grade: pl.Expr) -> pl.Expr:
    c5, c4, c3, c2, c1, c0 = _MINETTI_COEFFICIENTS
    return c5 * grade**5 + c4 * grade**4 + c3 * grade**3 + c2 * grade**2 + c1 * grade + c0


class GradeAdjustedPaceResult(BaseModel):
    """Whole-activity grade-adjusted pace, derived from raw Bronze FIT records."""

    sample_count: int
    total_distance_meters: float
    total_ascent_meters: float
    total_descent_meters: float
    actual_pace_sec_per_km: float
    grade_adjusted_pace_sec_per_km: float


def compute_grade_adjusted_pace_from_records(
    records: pl.DataFrame,
    *,
    max_grade: float = 0.30,
    smoothing_window: int = 10,
) -> GradeAdjustedPaceResult:
    """Compute grade-adjusted pace from a FIT 'record'-message DataFrame.

    Segment speed is derived from consecutive `distance`/`timestamp` deltas rather
    than trusting a recorded `speed` field, so only `timestamp`, `distance`, and
    `altitude` are required. `altitude` is smoothed with a rolling mean
    (`smoothing_window` samples, assumed ~1Hz recording) before differencing, since
    raw per-sample grade from barometric/GPS altitude is too noisy to use directly.
    Grade is clipped to `+/- max_grade` to bound that residual noise, not because
    Minetti's model is invalid beyond it.
    """
    required = {"timestamp", "distance", "altitude"}
    missing = required - set(records.columns)
    if missing:
        raise ValueError(f"FIT records are missing required column(s) for GAP: {sorted(missing)}")

    df = (
        records.select("timestamp", "distance", "altitude")
        .drop_nulls()
        .sort("timestamp")
        .with_columns(
            pl.col("altitude").rolling_mean(window_size=smoothing_window, min_samples=1).alias("_smoothed_altitude")
        )
        .with_columns(
            pl.col("distance").diff().alias("_distance_delta"),
            pl.col("_smoothed_altitude").diff().alias("_altitude_delta"),
            pl.col("timestamp").diff().dt.total_seconds().alias("_time_delta_seconds"),
        )
        .filter((pl.col("_distance_delta") > 0) & (pl.col("_time_delta_seconds") > 0))
        .with_columns(
            (pl.col("_altitude_delta") / pl.col("_distance_delta")).clip(-max_grade, max_grade).alias("_grade"),
            (pl.col("_distance_delta") / pl.col("_time_delta_seconds")).alias("_segment_speed_mps"),
        )
        .with_columns((_minetti_cost(pl.col("_grade")) / _minetti_cost(pl.lit(0.0))).alias("_cost_ratio"))
        .with_columns((pl.col("_segment_speed_mps") * pl.col("_cost_ratio")).alias("_adjusted_speed_mps"))
    )

    if df.is_empty():
        raise ValueError("No valid moving samples found to compute grade-adjusted pace.")

    total_distance = float(df["_distance_delta"].sum())
    total_time_actual = float(df["_time_delta_seconds"].sum())
    total_time_equivalent = float((df["_distance_delta"] / df["_adjusted_speed_mps"]).sum())
    ascent = float(df.filter(pl.col("_altitude_delta") > 0)["_altitude_delta"].sum() or 0.0)
    descent = float(-(df.filter(pl.col("_altitude_delta") < 0)["_altitude_delta"].sum() or 0.0))

    return GradeAdjustedPaceResult(
        sample_count=df.height,
        total_distance_meters=total_distance,
        total_ascent_meters=ascent,
        total_descent_meters=descent,
        actual_pace_sec_per_km=total_time_actual / total_distance * 1000,
        grade_adjusted_pace_sec_per_km=total_time_equivalent / total_distance * 1000,
    )


def compute_grade_adjusted_pace(file_path: str | Path, **kwargs: Any) -> GradeAdjustedPaceResult:
    """Read a Bronze activity FIT/zip payload and compute its grade-adjusted pace."""
    return compute_grade_adjusted_pace_from_records(_read_fit_records(file_path), **kwargs)


# ---------------------------------------------------------------------------
# Cardiac drift (aerobic decoupling)
# ---------------------------------------------------------------------------


class CardiacDriftResult(BaseModel):
    """First-half vs second-half HR:speed comparison for one activity.

    `decoupling_percent` is the % change in HR-per-speed from the first half to
    the second; positive means heart rate drifted up relative to pace (commonly
    read as inadequate aerobic base or accumulating fatigue). `None` when either
    half has no valid moving samples — a genuine "can't tell," not a zero.
    """

    sample_count: int
    first_half_avg_heart_rate: float
    first_half_avg_speed_mps: float
    second_half_avg_heart_rate: float
    second_half_avg_speed_mps: float
    decoupling_percent: float | None


def compute_cardiac_drift_from_records(records: pl.DataFrame) -> CardiacDriftResult:
    """Compute aerobic decoupling from a FIT 'record'-message DataFrame.

    A recorded `heart_rate` of 0 is a sensor dropout, not a real reading, and is
    excluded before averaging.
    """
    required = {"timestamp", "heart_rate", "distance"}
    missing = required - set(records.columns)
    if missing:
        raise ValueError(f"FIT records are missing required column(s) for cardiac drift: {sorted(missing)}")

    df = (
        records.select("timestamp", "heart_rate", "distance")
        .drop_nulls()
        .filter(pl.col("heart_rate") > 0)
        .sort("timestamp")
        .with_columns(
            pl.col("distance").diff().fill_null(0.0).alias("_distance_delta"),
            pl.col("timestamp").diff().dt.total_seconds().fill_null(0.0).alias("_time_delta_seconds"),
            (pl.col("timestamp") - pl.col("timestamp").min()).dt.total_seconds().alias("_elapsed_seconds"),
        )
    )
    if df.height < 2:
        raise ValueError("Not enough valid heart-rate samples to compute cardiac drift.")

    # Kept in Polars: materializing tz-aware timestamps to Python needs the tzdata package on Windows.
    midpoint = float(df["_elapsed_seconds"].max()) / 2  # type: ignore[arg-type]
    first_half = df.filter(pl.col("_elapsed_seconds") <= midpoint)
    second_half = df.filter(pl.col("_elapsed_seconds") > midpoint)
    if first_half.is_empty() or second_half.is_empty():
        raise ValueError("Activity too short to split into two halves for cardiac drift.")

    def half_summary(half: pl.DataFrame) -> tuple[float, float]:
        avg_hr = float(half["heart_rate"].mean())  # type: ignore[arg-type]
        distance = float(half["_distance_delta"].sum())
        time_seconds = float(half["_time_delta_seconds"].sum())
        avg_speed = distance / time_seconds if time_seconds > 0 else 0.0
        return avg_hr, avg_speed

    first_hr, first_speed = half_summary(first_half)
    second_hr, second_speed = half_summary(second_half)

    decoupling_percent = None
    if first_speed > 0 and second_speed > 0:
        first_ratio = first_hr / first_speed
        second_ratio = second_hr / second_speed
        if first_ratio > 0:
            decoupling_percent = (second_ratio - first_ratio) / first_ratio * 100

    return CardiacDriftResult(
        sample_count=df.height,
        first_half_avg_heart_rate=first_hr,
        first_half_avg_speed_mps=first_speed,
        second_half_avg_heart_rate=second_hr,
        second_half_avg_speed_mps=second_speed,
        decoupling_percent=decoupling_percent,
    )


def compute_cardiac_drift(file_path: str | Path) -> CardiacDriftResult:
    """Read a Bronze activity FIT/zip payload and compute its cardiac drift."""
    return compute_cardiac_drift_from_records(_read_fit_records(file_path))
