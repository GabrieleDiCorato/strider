from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.analytics.queries import get_connection
from src.core.schemas import (
    EntityType,
    HrvStatus,
    SilverActivity,
    SilverDailySummary,
    SportType,
    TrainingStatus,
)
from src.processing.writer import ParquetWriter


def _activity(
    activity_id: str, start_time: datetime, *, distance: float, duration: float, effect: float
) -> SilverActivity:
    return SilverActivity(
        user_id="athlete-1",
        updated_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
        activity_id=activity_id,
        sport_type=SportType.RUNNING,
        start_time=start_time,
        distance_meters=distance,
        duration_seconds=duration,
        moving_time_seconds=duration,
        training_effect_aerobic=effect,
    )


def _daily(
    calendar_date: date, *, sleep_score: float, resting_hr: float, steps: int, hrv: HrvStatus, status: TrainingStatus
) -> SilverDailySummary:
    return SilverDailySummary(
        user_id="athlete-1",
        updated_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
        calendar_date=calendar_date,
        sleep_score=sleep_score,
        resting_hr=resting_hr,
        steps=steps,
        hrv_status=hrv,
        training_status=status,
    )


def test_silver_views_are_empty_but_typed_when_no_parquet_exists(tmp_path: Path) -> None:
    conn = get_connection(db_path=tmp_path / "gold.duckdb", silver_dir=tmp_path / "silver")

    rows = conn.execute("SELECT * FROM silver_activity").fetchall()
    columns = [description[0] for description in conn.description]

    assert rows == []
    assert "activity_id" in columns
    assert "user_id" in columns
    # Hive partition-only columns are not part of the logical contract.
    assert "year" not in columns and "month" not in columns


def test_recent_load_rolling_windows_respect_calendar_gaps_not_row_count(tmp_path: Path) -> None:
    silver_dir = tmp_path / "silver"
    writer = ParquetWriter(silver_dir)
    writer.write(
        [
            _activity(
                "a1", datetime(2026, 9, 1, tzinfo=timezone.utc), distance=5000, duration=1800, effect=2.0
            ),
            _activity(
                "a2", datetime(2026, 9, 2, tzinfo=timezone.utc), distance=8000, duration=2400, effect=3.0
            ),
            _activity(
                "a3", datetime(2026, 9, 8, tzinfo=timezone.utc), distance=10000, duration=3000, effect=3.5
            ),
        ],
        EntityType.ACTIVITY,
    )

    conn = get_connection(db_path=tmp_path / "gold.duckdb", silver_dir=silver_dir)
    rows = conn.execute(
        "SELECT activity_date, rolling_7d_distance_meters, rolling_28d_distance_meters "
        "FROM v_recent_load ORDER BY activity_date"
    ).fetchall()

    assert rows[0] == (date(2026, 9, 1), 5000.0, 5000.0)
    assert rows[1] == (date(2026, 9, 2), 13000.0, 13000.0)
    # Sep 1 is 7 calendar days before Sep 8 -> excluded from the 7-day window,
    # but included in the 28-day window. A row-count-based frame (only 3
    # distinct activity dates) would wrongly include it in both.
    assert rows[2] == (date(2026, 9, 8), 18000.0, 23000.0)


def test_weekly_readiness_aggregates_and_uses_mode_for_categoricals(tmp_path: Path) -> None:
    silver_dir = tmp_path / "silver"
    writer = ParquetWriter(silver_dir)
    writer.write(
        [
            _daily(
                date(2026, 9, 21),
                sleep_score=70,
                resting_hr=50,
                steps=8000,
                hrv=HrvStatus.BALANCED,
                status=TrainingStatus.PRODUCTIVE,
            ),
            _daily(
                date(2026, 9, 22),
                sleep_score=80,
                resting_hr=52,
                steps=10000,
                hrv=HrvStatus.BALANCED,
                status=TrainingStatus.PRODUCTIVE,
            ),
            _daily(
                date(2026, 9, 23),
                sleep_score=90,
                resting_hr=54,
                steps=12000,
                hrv=HrvStatus.LOW,
                status=TrainingStatus.MAINTAINING,
            ),
        ],
        EntityType.DAILY_SUMMARY,
    )

    conn = get_connection(db_path=tmp_path / "gold.duckdb", silver_dir=silver_dir)
    [row] = conn.execute(
        "SELECT week_start, days_with_data, avg_sleep_score, avg_resting_hr, total_steps, "
        "most_common_hrv_status, most_common_training_status FROM v_weekly_readiness"
    ).fetchall()

    week_start, days_with_data, avg_sleep_score, avg_resting_hr, total_steps, hrv_mode, status_mode = row
    assert week_start.weekday() == 0  # Monday
    assert days_with_data == 3
    assert avg_sleep_score == 80
    assert avg_resting_hr == 52
    assert total_steps == 30000
    assert hrv_mode == "balanced"
    assert status_mode == "productive"


def test_get_connection_is_idempotent_and_persists_view_definitions(tmp_path: Path) -> None:
    db_path = tmp_path / "gold.duckdb"
    silver_dir = tmp_path / "silver"
    get_connection(db_path=db_path, silver_dir=silver_dir).close()

    # Re-opening (e.g. a later process) must not fail even with zero files.
    conn = get_connection(db_path=db_path, silver_dir=silver_dir)
    assert conn.execute("SELECT * FROM v_weekly_readiness").fetchall() == []
