from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

from src.core.schemas import SportType, JournalCategory, EntrySource, MemoryCategory, HrvStatus, TrainingStatus


class LapBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    lap_number: int
    distance_km: float | None
    duration_min: float
    avg_pace_min_per_km: float | None
    avg_hr: float | None


class ActivitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    activity_id: str
    date: date
    sport_type: SportType
    title: str | None
    distance_km: float | None
    duration_min: float | None
    avg_pace_min_per_km: float | None
    avg_hr: float | None
    max_hr: float | None
    hr_zone_minutes: list[float] | None
    training_effect_aerobic: float | None
    training_effect_anaerobic: float | None
    total_ascent_m: float | None
    lap_count: int | None
    laps: list[LapBrief]
    was_planned: bool
    workout_id: str | None


class ActivityDeepDive(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cardiac_drift_percent: float | None
    grade_adjusted_pace_min_per_km: float | None
    unavailable_reason: str | None


class SportBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sport_type: SportType
    sessions: int
    distance_km: float
    duration_min: float


class PeriodSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    start: date
    end: date
    sessions: int
    active_days: int
    distance_km: float
    duration_min: float
    total_ascent_m: float
    avg_hr: float | None
    by_sport: list[SportBreakdown]
    longest_activity_id: str | None


class TrainingLoadSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    as_of: date
    rolling_7d_distance_km: float
    rolling_28d_distance_km: float
    acwr: float | None
    acwr_band: Literal["low", "balanced", "elevated", "high", "unknown"]
    insufficient_history: bool


class ReadinessSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    day: date
    has_data: bool
    sleep_score: float | None
    sleep_hours: float | None
    resting_hr: float | None
    resting_hr_baseline_7d: float | None
    hrv_status: HrvStatus | None
    hrv_weekly_avg: float | None
    body_battery_high: int | None
    avg_stress_level: float | None
    training_status: TrainingStatus | None


class WellnessAverages(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    days_with_data: int
    avg_sleep_score: float | None
    avg_sleep_hours: float | None
    avg_resting_hr: float | None
    avg_stress_level: float | None


class PlannedWorkoutBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    date: date
    workout_id: str | None
    title: str | None
    sport_type: SportType | None
    completed: bool


class JournalBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    date: date
    source: EntrySource
    category: JournalCategory
    text: str
    rating: int | None


class MemoryBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    category: MemoryCategory
    key: str
    value: str

