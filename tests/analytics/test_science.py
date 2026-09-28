from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from src.analytics.science import (
    compute_acwr,
    compute_cardiac_drift,
    compute_cardiac_drift_from_records,
    compute_grade_adjusted_pace,
    compute_grade_adjusted_pace_from_records,
    daily_training_load,
)

FIXTURE = Path(__file__).parent.parent / "resources" / "garmin" / "activity_user.zip"
T0 = datetime(2026, 9, 1, 7, tzinfo=timezone.utc)


def _records(
    seconds: int,
    *,
    speed: float = 3.0,
    altitude_per_second: float = 0.0,
    hr_first: float = 140,
    hr_second: float = 140,
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "timestamp": [T0 + timedelta(seconds=i) for i in range(seconds)],
            "distance": [speed * i for i in range(seconds)],
            "altitude": [100.0 + altitude_per_second * i for i in range(seconds)],
            "heart_rate": [hr_first if i < seconds / 2 else hr_second for i in range(seconds)],
        }
    )


def test_acwr_is_one_for_constant_load() -> None:
    load = pl.DataFrame(
        {
            "user_id": ["athlete-1"] * 40,
            "activity_date": [date(2026, 8, 1) + timedelta(days=i) for i in range(40)],
            "daily_load": [50.0] * 40,
        }
    )

    results = compute_acwr(load)

    assert results[-1].acwr == pytest.approx(1.0)
    assert results[0].insufficient_history is True
    assert results[27].insufficient_history is False


def test_acwr_densifies_rest_days_and_rises_after_load_spike() -> None:
    load = pl.DataFrame(
        {
            "user_id": ["athlete-1", "athlete-1"],
            "activity_date": [date(2026, 8, 1), date(2026, 8, 30)],
            "daily_load": [100.0, 100.0],
        }
    )

    results = compute_acwr(load)

    assert len(results) == 30
    assert results[10].daily_load == 0.0
    # Acute load decays faster than chronic, so a fresh session lifts the ratio above 1.
    assert results[-1].acwr is not None and results[-1].acwr > 1.0


def test_acwr_is_isolated_per_user_and_handles_empty_input() -> None:
    assert compute_acwr(pl.DataFrame({"user_id": [], "activity_date": [], "daily_load": []})) == []

    load = pl.DataFrame(
        {
            "user_id": ["a", "a", "b"],
            "activity_date": [date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 20)],
            "daily_load": [10.0, 10.0, 99.0],
        }
    )
    results = compute_acwr(load)

    assert {r.user_id for r in results} == {"a", "b"}
    assert [r.daily_load for r in results if r.user_id == "b"] == [99.0]


def test_acwr_rejects_missing_columns() -> None:
    with pytest.raises(ValueError, match="missing required"):
        compute_acwr(pl.DataFrame({"user_id": ["a"], "daily_load": [1.0]}))


def test_daily_training_load_weights_duration_by_training_effect() -> None:
    activities = pl.DataFrame(
        {
            "user_id": ["a", "a", "a"],
            "start_time": [
                datetime(2026, 9, 1, 7),
                datetime(2026, 9, 1, 18),
                datetime(2026, 9, 2, 7),
            ],
            "moving_time_seconds": [3600.0, None, 1800.0],
            "duration_seconds": [3700.0, 1200.0, 1800.0],
            "training_effect_aerobic": [3.0, None, 2.0],
        }
    )

    result = daily_training_load(activities).sort("activity_date")

    # Day 1: 60 min * 3.0 + 20 min * 1.0 (missing TE falls back to 1.0); day 2: 30 min * 2.0.
    assert result["daily_load"].to_list() == pytest.approx([200.0, 60.0])


def test_gap_equals_actual_pace_on_flat_ground() -> None:
    result = compute_grade_adjusted_pace_from_records(_records(120))

    assert result.actual_pace_sec_per_km == pytest.approx(1000 / 3.0)
    assert result.grade_adjusted_pace_sec_per_km == pytest.approx(result.actual_pace_sec_per_km)


def test_gap_is_faster_than_actual_pace_uphill() -> None:
    result = compute_grade_adjusted_pace_from_records(_records(120, altitude_per_second=0.3))

    assert result.grade_adjusted_pace_sec_per_km < result.actual_pace_sec_per_km
    assert result.total_ascent_meters > 0
    assert result.total_descent_meters == 0


def test_gap_and_drift_reject_missing_columns() -> None:
    with pytest.raises(ValueError, match="altitude"):
        compute_grade_adjusted_pace_from_records(_records(10).drop("altitude"))
    with pytest.raises(ValueError, match="heart_rate"):
        compute_cardiac_drift_from_records(_records(10).drop("heart_rate"))


def test_cardiac_drift_is_zero_when_hr_and_speed_are_steady() -> None:
    result = compute_cardiac_drift_from_records(_records(100))

    assert result.decoupling_percent == pytest.approx(0.0)


def test_cardiac_drift_is_positive_when_hr_rises_at_constant_speed() -> None:
    result = compute_cardiac_drift_from_records(_records(100, hr_first=140, hr_second=150))

    assert result.decoupling_percent == pytest.approx(150 / 140 * 100 - 100)


def test_cardiac_drift_ignores_zero_hr_dropouts() -> None:
    records = _records(100, hr_first=140, hr_second=140).with_columns(
        pl.when(pl.col("distance") % 7 == 0).then(0).otherwise(pl.col("heart_rate")).alias("heart_rate")
    )

    result = compute_cardiac_drift_from_records(records)

    assert result.first_half_avg_heart_rate == pytest.approx(140)


def test_cardiac_drift_is_unknown_when_stationary() -> None:
    result = compute_cardiac_drift_from_records(_records(100, speed=0.0))

    assert result.decoupling_percent is None


def test_bronze_fit_readers_return_summaries_from_real_zip() -> None:
    drift = compute_cardiac_drift(FIXTURE)
    gap = compute_grade_adjusted_pace(FIXTURE)

    assert drift.sample_count > 0
    assert gap.total_distance_meters == pytest.approx(3600.0, rel=0.01)
    assert gap.sample_count > 0


def test_bronze_fit_reader_rejects_payload_without_records(tmp_path: Path) -> None:
    empty = tmp_path / "empty.fit"
    empty.write_bytes(b"")

    with pytest.raises(Exception):
        compute_cardiac_drift(empty)
