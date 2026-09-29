import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import duckdb
import polars as pl

from src.core.schemas import SportType, HrvStatus, TrainingStatus, MemoryStatus
from src.agent.models import (
    ActivitySummary, ActivityDeepDive, LapBrief, SportBreakdown, PeriodSummary,
    TrainingLoadSummary, ReadinessSnapshot, WellnessAverages, PlannedWorkoutBrief,
    JournalBrief, MemoryBrief
)
from src.agent.journal import JournalStore
from src.agent.memory import MemoryStore
from src.ingestion.ledger import SyncLedger
from src.analytics import science


@dataclass(frozen=True)
class ToolContext:
    user_id: str
    gold: duckdb.DuckDBPyConnection
    journal: JournalStore
    memory: MemoryStore
    ledger: SyncLedger


def _compute_pace(sport: str, time_sec: float | None, dist_m: float | None) -> float | None:
    if not time_sec or not dist_m or dist_m <= 0:
        return None
    if sport not in (SportType.RUNNING.value, SportType.TRAIL_RUNNING.value, SportType.WALKING.value, SportType.HIKING.value):
        return None
    return round((time_sec / 60) / (dist_m / 1000), 1)


def get_activity_summary(ctx: ToolContext, activity_id: str) -> ActivitySummary | None:
    row = ctx.gold.execute("""
        SELECT
            activity_id, CAST(start_time AS DATE), sport_type,
            distance_meters, duration_seconds, moving_time_seconds,
            avg_heart_rate, max_heart_rate, hr_zone_seconds,
            training_effect_aerobic, training_effect_anaerobic,
            total_ascent_meters, lap_count, lap_summaries,
            workout_id, is_planned_workout, activity_name
        FROM silver_activity
        WHERE user_id = ? AND activity_id = ?
    """, [ctx.user_id, activity_id]).fetchone()
    if not row:
        return None

    sport = row[2]
    dist_km = round(row[3] / 1000, 2) if row[3] else None
    dur_min = round(row[4] / 60, 2) if row[4] else None
    
    time_for_pace = row[5] if row[5] else row[4]
    pace = _compute_pace(sport, time_for_pace, row[3])
    
    hr_zones = json.loads(row[8]) if row[8] else None
    hr_zone_minutes = [round(s / 60, 2) for s in hr_zones] if hr_zones else None
    
    laps_json = json.loads(row[13]) if row[13] else []
    laps = []
    for lap in laps_json[:20]:
        lap_dist = round(lap["distance_meters"] / 1000, 2) if lap.get("distance_meters") else None
        lap_dur = round(lap["duration_seconds"] / 60, 2) if lap.get("duration_seconds") else 0.0
        lap_pace = _compute_pace(sport, lap.get("duration_seconds"), lap.get("distance_meters"))
        lap_hr = round(lap["avg_heart_rate"], 1) if lap.get("avg_heart_rate") else None
        laps.append(LapBrief(
            lap_number=lap["lap_number"],
            distance_km=lap_dist,
            duration_min=lap_dur,
            avg_pace_min_per_km=lap_pace,
            avg_hr=lap_hr
        ))

    workout_id = row[14]
    activity_name = row[16]
    title = activity_name
    if workout_id:
        wd = ctx.gold.execute("SELECT workout_title FROM silver_workout_definition WHERE user_id = ? AND workout_id = ?", [ctx.user_id, workout_id]).fetchone()
        if wd and wd[0]:
            title = wd[0]

    return ActivitySummary(
        activity_id=row[0],
        date=row[1],
        sport_type=SportType(sport),
        title=title,
        distance_km=dist_km,
        duration_min=dur_min,
        avg_pace_min_per_km=pace,
        avg_hr=round(row[6], 1) if row[6] else None,
        max_hr=round(row[7], 1) if row[7] else None,
        hr_zone_minutes=hr_zone_minutes,
        training_effect_aerobic=round(row[9], 1) if row[9] else None,
        training_effect_anaerobic=round(row[10], 1) if row[10] else None,
        total_ascent_m=round(row[11], 2) if row[11] else None,
        lap_count=row[12],
        laps=laps,
        workout_id=workout_id,
        was_planned=row[15] if row[15] is not None else False
    )

def get_activity_deep_dive(ctx: ToolContext, activity_id: str) -> ActivityDeepDive:
    path = ctx.ledger.find_payload_path(ctx.user_id, "activity", activity_id)
    if not path:
        return ActivityDeepDive(cardiac_drift_percent=None, grade_adjusted_pace_min_per_km=None, unavailable_reason="bronze file missing")
    
    cd, gap, reason = None, None, None
    try:
        cd_res = science.compute_cardiac_drift(path)
        cd = round(cd_res.decoupling_percent, 1) if cd_res.decoupling_percent is not None else None
    except Exception as e:
        reason = str(e)

    try:
        gap_res = science.compute_grade_adjusted_pace(path)
        gap = round(gap_res.grade_adjusted_pace_sec_per_km / 60, 1) if gap_res.grade_adjusted_pace_sec_per_km is not None else None
    except Exception as e:
        if not reason: reason = str(e)

    return ActivityDeepDive(cardiac_drift_percent=cd, grade_adjusted_pace_min_per_km=gap, unavailable_reason=reason)

def get_period_summary(ctx: ToolContext, start: date, end: date) -> PeriodSummary:
    rows = ctx.gold.execute("""
        SELECT
            sport_type,
            COUNT(*),
            SUM(distance_meters),
            SUM(COALESCE(moving_time_seconds, duration_seconds)),
            SUM(total_ascent_meters),
            AVG(avg_heart_rate)
        FROM silver_activity
        WHERE user_id = ? AND CAST(start_time AS DATE) >= ? AND CAST(start_time AS DATE) <= ?
        GROUP BY sport_type
        ORDER BY SUM(COALESCE(moving_time_seconds, duration_seconds)) DESC
    """, [ctx.user_id, start, end]).fetchall()

    if not rows:
        return PeriodSummary(
            start=start, end=end, sessions=0, active_days=0,
            distance_km=0.0, duration_min=0.0, total_ascent_m=0.0,
            avg_hr=None, by_sport=[], longest_activity_id=None
        )

    by_sport = []
    tot_sess = 0
    tot_dist = 0.0
    tot_dur = 0.0
    tot_asc = 0.0
    
    for row in rows:
        sport = SportType(row[0])
        sess = row[1]
        dist = row[2] or 0.0
        dur = row[3] or 0.0
        asc = row[4] or 0.0
        
        tot_sess += sess
        tot_dist += dist
        tot_dur += dur
        tot_asc += asc
        
        by_sport.append(SportBreakdown(
            sport_type=sport,
            sessions=sess,
            distance_km=round(dist / 1000, 2),
            duration_min=round(dur / 60, 2)
        ))
        
    active_days = ctx.gold.execute("""
        SELECT COUNT(DISTINCT CAST(start_time AS DATE))
        FROM silver_activity
        WHERE user_id = ? AND CAST(start_time AS DATE) >= ? AND CAST(start_time AS DATE) <= ?
    """, [ctx.user_id, start, end]).fetchone()[0]

    avg_hr_row = ctx.gold.execute("""
        SELECT AVG(avg_heart_rate) FROM silver_activity
        WHERE user_id = ? AND CAST(start_time AS DATE) >= ? AND CAST(start_time AS DATE) <= ?
    """, [ctx.user_id, start, end]).fetchone()
    avg_hr = round(avg_hr_row[0], 1) if avg_hr_row and avg_hr_row[0] else None

    longest_row = ctx.gold.execute("""
        SELECT activity_id FROM silver_activity
        WHERE user_id = ? AND CAST(start_time AS DATE) >= ? AND CAST(start_time AS DATE) <= ?
        ORDER BY distance_meters DESC NULLS LAST LIMIT 1
    """, [ctx.user_id, start, end]).fetchone()
    longest_id = longest_row[0] if longest_row else None

    return PeriodSummary(
        start=start, end=end, sessions=tot_sess, active_days=active_days,
        distance_km=round(tot_dist / 1000, 2),
        duration_min=round(tot_dur / 60, 2),
        total_ascent_m=round(tot_asc, 2),
        avg_hr=avg_hr,
        by_sport=by_sport,
        longest_activity_id=longest_id
    )

def get_recent_activities(ctx: ToolContext, start: date | None = None, end: date | None = None, limit: int = 10, offset: int = 0) -> list[ActivitySummary]:
    query = "SELECT activity_id FROM silver_activity WHERE user_id = ?"
    params = [ctx.user_id]
    if start:
        query += " AND CAST(start_time AS DATE) >= ?"
        params.append(start)
    if end:
        query += " AND CAST(start_time AS DATE) <= ?"
        params.append(end)
    
    query += " ORDER BY start_time DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = ctx.gold.execute(query, params).fetchall()
    
    res = []
    for (aid,) in rows:
        act = get_activity_summary(ctx, aid)
        if act:
            # remove laps to save context
            act_dict = act.model_dump()
            act_dict["laps"] = []
            res.append(ActivitySummary(**act_dict))
    return res

def get_training_load(ctx: ToolContext, as_of: date) -> TrainingLoadSummary:
    row = ctx.gold.execute("""
        SELECT rolling_7d_distance_meters, rolling_28d_distance_meters
        FROM v_recent_load
        WHERE user_id = ? AND activity_date <= ?
        ORDER BY activity_date DESC LIMIT 1
    """, [ctx.user_id, as_of]).fetchone()
    
    d7 = round(row[0] / 1000, 2) if row and row[0] else 0.0
    d28 = round(row[1] / 1000, 2) if row and row[1] else 0.0

    start = as_of - timedelta(days=59)
    acts = ctx.gold.execute("""
        SELECT CAST(start_time AS DATE) as date, duration_seconds as duration_s, training_effect_aerobic as te
        FROM silver_activity
        WHERE user_id = ? AND CAST(start_time AS DATE) >= ? AND CAST(start_time AS DATE) <= ?
    """, [ctx.user_id, start, as_of]).fetchall()

    acwr = None
    band = "unknown"
    insuff = True
    if acts:
        df = pl.DataFrame(acts, schema=["date", "duration_s", "te"], orient="row")
        df = df.with_columns(
            pl.lit(ctx.user_id).alias("user_id"),
            pl.col("date").cast(pl.Datetime).alias("start_time"),
            pl.col("duration_s").fill_null(0.0).alias("duration_seconds"),
            pl.col("te").fill_null(0.0).alias("training_effect_aerobic")
        )
        dl = science.daily_training_load(df)
        acwr_list = science.compute_acwr(dl)
        acwr_list = [a for a in acwr_list if a.activity_date <= as_of]
        if acwr_list:
            last_acwr = acwr_list[-1]
            acwr = round(last_acwr.acwr, 2) if last_acwr.acwr is not None else None
            insuff = last_acwr.insufficient_history
            if acwr is None:
                band = "unknown"
            elif acwr < 0.8: band = "low"
            elif acwr <= 1.3: band = "balanced"
            elif acwr <= 1.5: band = "elevated"
            else: band = "high"

    return TrainingLoadSummary(
        as_of=as_of,
        rolling_7d_distance_km=d7,
        rolling_28d_distance_km=d28,
        acwr=acwr,
        acwr_band=band,
        insufficient_history=insuff
    )

def get_readiness_snapshot(ctx: ToolContext, day: date) -> ReadinessSnapshot:
    row = ctx.gold.execute("""
        SELECT
            sleep_score, sleep_duration_seconds, resting_hr,
            hrv_status, hrv_weekly_avg, body_battery_high,
            avg_stress_level, training_status, body_battery_charged, body_battery_drained, body_battery_current,
            vo2max_running
        FROM silver_daily_summary
        WHERE user_id = ? AND calendar_date = ?
    """, [ctx.user_id, day]).fetchone()
    
    if not row:
        return ReadinessSnapshot(
            day=day, has_data=False,
            sleep_score=None, sleep_hours=None, resting_hr=None,
            resting_hr_baseline_7d=None, hrv_status=None, hrv_weekly_avg=None,
            body_battery_high=None, body_battery_charged=None, body_battery_drained=None, body_battery_current=None, avg_stress_level=None, training_status=None, vo2max=None
        )
        
    baseline_row = ctx.gold.execute("""
        SELECT AVG(resting_hr) FROM silver_daily_summary
        WHERE user_id = ? AND calendar_date >= ? AND calendar_date < ?
    """, [ctx.user_id, day - timedelta(days=7), day]).fetchone()
    
    baseline = round(baseline_row[0], 1) if baseline_row and baseline_row[0] else None
    
    return ReadinessSnapshot(
        day=day,
        has_data=True,
        sleep_score=round(row[0], 1) if row[0] else None,
        sleep_hours=round(row[1]/3600, 1) if row[1] else None,
        resting_hr=round(row[2], 1) if row[2] else None,
        resting_hr_baseline_7d=baseline,
        hrv_status=HrvStatus(row[3]) if row[3] else None,
        hrv_weekly_avg=round(row[4], 1) if row[4] else None,
        body_battery_high=row[5],
        avg_stress_level=round(row[6], 1) if row[6] else None,
        training_status=TrainingStatus(row[7]) if row[7] else None,
        body_battery_charged=row[8],
        body_battery_drained=row[9],
        body_battery_current=row[10],
        vo2max=round(row[11], 1) if row[11] else None
    )


def get_wellness_averages(ctx: ToolContext, start: date, end: date) -> WellnessAverages:
    row = ctx.gold.execute("""
        SELECT
            COUNT(calendar_date),
            AVG(sleep_score),
            AVG(sleep_duration_seconds),
            AVG(resting_hr),
            AVG(avg_stress_level)
        FROM silver_daily_summary
        WHERE user_id = ? AND calendar_date >= ? AND calendar_date <= ?
    """, [ctx.user_id, start, end]).fetchone()
    
    if not row or row[0] == 0:
        return WellnessAverages(
            days_with_data=0, avg_sleep_score=None, avg_sleep_hours=None,
            avg_resting_hr=None, avg_stress_level=None
        )
        
    return WellnessAverages(
        days_with_data=row[0],
        avg_sleep_score=round(row[1], 1) if row[1] else None,
        avg_sleep_hours=round(row[2]/3600, 1) if row[2] else None,
        avg_resting_hr=round(row[3], 1) if row[3] else None,
        avg_stress_level=round(row[4], 1) if row[4] else None
    )


def get_planned_workouts(ctx: ToolContext, start: date, end: date) -> list[PlannedWorkoutBrief]:
    rows = ctx.gold.execute("""
        SELECT calendar_date, workout_id, workout_title, sport_type
        FROM silver_workout_calendar
        WHERE user_id = ? AND calendar_date >= ? AND calendar_date <= ?
        ORDER BY calendar_date ASC LIMIT 14
    """, [ctx.user_id, start, end]).fetchall()
    
    res = []
    for r in rows:
        d = r[0]
        wid = r[1]
        completed = False
        if wid:
            act = ctx.gold.execute("""
                SELECT 1 FROM silver_activity
                WHERE user_id = ? AND workout_id = ? AND CAST(start_time AS DATE) >= ? AND CAST(start_time AS DATE) <= ?
            """, [ctx.user_id, wid, d, d + timedelta(days=1)]).fetchone()
            if act:
                completed = True
                
        res.append(PlannedWorkoutBrief(
            date=d,
            workout_id=wid,
            title=r[2],
            sport_type=SportType(r[3]) if r[3] else None,
            completed=completed
        ))
    return res


def get_journal_context(ctx: ToolContext, start: date, end: date, limit: int = 20) -> list[JournalBrief]:
    entries = ctx.journal.list_entries(ctx.user_id, start_date=start, end_date=end, limit=limit)
    res = []
    for e in entries:
        t = e.text
        if len(t) > 500:
            t = t[:499] + "…"
        res.append(JournalBrief(
            date=e.entry_date,
            source=e.source,
            category=e.category,
            text=t,
            rating=e.rating
        ))
    return res


def get_memory_context(ctx: ToolContext) -> list[MemoryBrief]:
    facts = ctx.memory.list_facts(ctx.user_id)
    res = []
    for f in facts:
        if f.status == MemoryStatus.ACTIVE:
            res.append(MemoryBrief(category=f.category, key=f.key, value=f.value))
    return res[:30]

