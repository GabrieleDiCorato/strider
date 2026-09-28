from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Literal, Any
import re

from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator, ValidationInfo

from src.agent.tools import (
    ToolContext, get_readiness_snapshot, get_training_load, get_recent_activities,
    get_journal_context, get_memory_context, get_period_summary, get_wellness_averages,
    get_planned_workouts, get_activity_summary, get_activity_deep_dive
)
from src.agent.insights_log import Metric
import src.agent.prompts as prompts

class NarrativeBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    headline: str = Field(max_length=120)
    summary: str = Field(max_length=700)
    observations: list[str] = Field(default_factory=list, max_length=5)
    caveats: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("observations")
    @classmethod
    def check_observations_length(cls, v: list[str]) -> list[str]:
        for obs in v:
            if len(obs) > 200:
                raise ValueError("Observation exceeds 200 characters")
        return v


@dataclass(frozen=True)
class ActionContext:
    payload: BaseModel
    metrics: list[Metric]
    journal_date: date


class NoDataError(Exception):
    pass


@dataclass(frozen=True)
class ActionSpec:
    action_id: str
    version: int
    title: str
    params_model: type[BaseModel]
    narrative_model: type[BaseModel]
    prompt: str
    build_context: Callable[[ToolContext, BaseModel, date], ActionContext]
    writes_journal: bool = False


# ---------------------------------------------------------------------------
# Readiness Check
# ---------------------------------------------------------------------------

class ReadinessParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    day: date | None = None

    @field_validator("day")
    @classmethod
    def check_not_future(cls, v: date | None, info: ValidationInfo) -> date | None:
        if v is not None and info.context:
            today = info.context.get("today")
            if today and v > today:
                raise ValueError("Cannot check readiness for future dates")
        return v

class ReadinessPayload(BaseModel):
    readiness: Any
    load: Any
    recent_activities: list[Any]
    journal: list[Any]
    memory: list[Any]

class ReadinessNarrative(NarrativeBase):
    readiness_level: Literal["good", "moderate", "low", "unknown"]
    recommendation: str = Field(max_length=300)

def build_readiness_context(ctx: ToolContext, params: ReadinessParams, today: date) -> ActionContext:
    day = params.day or today
    
    readiness = get_readiness_snapshot(ctx, day)
    load = get_training_load(ctx, day)
    acts = get_recent_activities(ctx, day - timedelta(days=2), day, limit=10)
    
    recent_acts_7d = get_recent_activities(ctx, day - timedelta(days=7), day, limit=1)
    if not readiness.has_data and not recent_acts_7d:
        raise NoDataError("No wellness or training data found for readiness check.")
    
    journal = get_journal_context(ctx, day - timedelta(days=6), day)
    memory = get_memory_context(ctx)
    
    metrics = []
    if readiness.sleep_score is not None: metrics.append(Metric(label="Sleep Score", value=str(readiness.sleep_score)))
    if readiness.resting_hr is not None: metrics.append(Metric(label="Resting HR", value=str(readiness.resting_hr), unit="bpm"))
    if readiness.hrv_status is not None: metrics.append(Metric(label="HRV Status", value=readiness.hrv_status.value))
    if load.acwr is not None: metrics.append(Metric(label="ACWR", value=f"{load.acwr} ({load.acwr_band})"))
    
    payload = ReadinessPayload(
        readiness=readiness, load=load, recent_activities=acts, journal=journal, memory=memory
    )
    return ActionContext(payload=payload, metrics=metrics, journal_date=day)

# ---------------------------------------------------------------------------
# Weekly Digest
# ---------------------------------------------------------------------------

class WeeklyDigestParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    week_start: date | None = None

    @field_validator("week_start")
    @classmethod
    def check_monday(cls, v: date | None) -> date | None:
        if v is not None and v.weekday() != 0:
            raise ValueError("week_start must be a Monday")
        return v

class WeeklyDigestPayload(BaseModel):
    period: Any
    wellness: Any
    load: Any
    planned: list[Any]
    recent_activities: list[Any]
    journal: list[Any]
    memory: list[Any]

class WeeklyDigestNarrative(NarrativeBase):
    highlights: list[str] = Field(max_length=3)
    focus_next_week: str = Field(max_length=300)
    journal_observation: str | None = Field(default=None, max_length=500)

def build_weekly_digest_context(ctx: ToolContext, params: WeeklyDigestParams, today: date) -> ActionContext:
    ws = params.week_start
    if ws is None:
        ws = today - timedelta(days=today.weekday() + 7) # last completed week's Monday
        
    we = ws + timedelta(days=6)
    period = get_period_summary(ctx, ws, we)
    wellness = get_wellness_averages(ctx, ws, we)
    load = get_training_load(ctx, we)
    planned = get_planned_workouts(ctx, ws, we)
    acts = get_recent_activities(ctx, ws, we, limit=10)
    journal = get_journal_context(ctx, ws, we)
    memory = get_memory_context(ctx)
    
    if period.sessions == 0 and wellness.days_with_data == 0:
        raise NoDataError("No data found for this week.")
        
    metrics = [
        Metric(label="Sessions", value=str(period.sessions)),
        Metric(label="Distance", value=str(period.distance_km), unit="km"),
        Metric(label="Duration", value=str(round(period.duration_min/60, 1)), unit="h")
    ]
    if planned:
        comp = sum(1 for p in planned if p.completed)
        metrics.append(Metric(label="Plan Adherence", value=f"{comp}/{len(planned)} completed"))
    if load.acwr is not None: metrics.append(Metric(label="ACWR", value=str(load.acwr)))
    if wellness.avg_sleep_score is not None: metrics.append(Metric(label="Avg Sleep Score", value=str(wellness.avg_sleep_score)))

    payload = WeeklyDigestPayload(
        period=period, wellness=wellness, load=load, planned=planned,
        recent_activities=acts, journal=journal, memory=memory
    )
    return ActionContext(payload=payload, metrics=metrics, journal_date=we)

# ---------------------------------------------------------------------------
# Explain Activity
# ---------------------------------------------------------------------------

class ExplainActivityParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    activity_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")

class ExplainActivityPayload(BaseModel):
    activity: Any
    deep_dive: Any
    load: Any
    journal: list[Any]
    memory: list[Any]

class ExplainActivityNarrative(NarrativeBase):
    effort_assessment: Literal["easy", "moderate", "hard", "very_hard", "unknown"]
    what_went_well: list[str] = Field(max_length=3)
    to_watch: list[str] = Field(max_length=3)

def build_explain_activity_context(ctx: ToolContext, params: ExplainActivityParams, today: date) -> ActionContext:
    act = get_activity_summary(ctx, params.activity_id)
    if not act:
        raise NoDataError("Activity not found")
        
    deep_dive = get_activity_deep_dive(ctx, params.activity_id)
    load = get_training_load(ctx, act.date)
    journal = get_journal_context(ctx, act.date - timedelta(days=1), act.date + timedelta(days=1))
    memory = get_memory_context(ctx)
    
    metrics = []
    if act.distance_km is not None: metrics.append(Metric(label="Distance", value=str(act.distance_km), unit="km"))
    if act.duration_min is not None: metrics.append(Metric(label="Duration", value=str(act.duration_min), unit="min"))
    if act.avg_pace_min_per_km is not None: metrics.append(Metric(label="Avg Pace", value=str(act.avg_pace_min_per_km), unit="min/km"))
    if act.avg_hr is not None: metrics.append(Metric(label="Avg HR", value=str(act.avg_hr), unit="bpm"))
    if act.training_effect_aerobic is not None: metrics.append(Metric(label="Aerobic TE", value=str(act.training_effect_aerobic)))
    if deep_dive.cardiac_drift_percent is not None: metrics.append(Metric(label="Cardiac Drift", value=str(deep_dive.cardiac_drift_percent), unit="%"))

    payload = ExplainActivityPayload(activity=act, deep_dive=deep_dive, load=load, journal=journal, memory=memory)
    return ActionContext(payload=payload, metrics=metrics, journal_date=today)

# ---------------------------------------------------------------------------
# Compare Periods
# ---------------------------------------------------------------------------

class ComparePeriodsParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    period_a_start: date
    period_a_end: date
    period_b_start: date
    period_b_end: date

    @model_validator(mode="after")
    def check_periods(self, info: ValidationInfo) -> 'ComparePeriodsParams':
        if self.period_a_end < self.period_a_start:
            raise ValueError("period_a_end must be >= period_a_start")
        if self.period_b_end < self.period_b_start:
            raise ValueError("period_b_end must be >= period_b_start")
        if (self.period_a_end - self.period_a_start).days > 92:
            raise ValueError("period_a span cannot exceed 92 days")
        if (self.period_b_end - self.period_b_start).days > 92:
            raise ValueError("period_b span cannot exceed 92 days")
        if info.context:
            today = info.context.get("today")
            if today and (self.period_a_end > today or self.period_b_end > today):
                raise ValueError("Cannot compare future dates")
        return self

class ComparePeriodsPayload(BaseModel):
    period_a: Any
    wellness_a: Any
    period_b: Any
    wellness_b: Any
    memory: list[Any]

class ComparePeriodsNarrative(NarrativeBase):
    trend: Literal["up", "down", "flat", "mixed"]
    differences: list[str] = Field(max_length=4)

def build_compare_periods_context(ctx: ToolContext, params: ComparePeriodsParams, today: date) -> ActionContext:
    pa = get_period_summary(ctx, params.period_a_start, params.period_a_end)
    wa = get_wellness_averages(ctx, params.period_a_start, params.period_a_end)
    pb = get_period_summary(ctx, params.period_b_start, params.period_b_end)
    wb = get_wellness_averages(ctx, params.period_b_start, params.period_b_end)
    memory = get_memory_context(ctx)
    
    if pa.sessions == 0 and pb.sessions == 0:
        raise NoDataError("Both periods have zero activities")
        
    metrics = [
        Metric(label="Sessions (A vs B)", value=f"{pa.sessions} vs {pb.sessions}"),
        Metric(label="Distance (A vs B)", value=f"{pa.distance_km} vs {pb.distance_km}", unit="km"),
        Metric(label="Avg HR (A vs B)", value=f"{pa.avg_hr} vs {pb.avg_hr}", unit="bpm"),
        Metric(label="Avg Sleep (A vs B)", value=f"{wa.avg_sleep_hours} vs {wb.avg_sleep_hours}", unit="h")
    ]
    
    payload = ComparePeriodsPayload(period_a=pa, wellness_a=wa, period_b=pb, wellness_b=wb, memory=memory)
    return ActionContext(payload=payload, metrics=metrics, journal_date=today)


ACTIONS: dict[str, ActionSpec] = {
    "readiness_check": ActionSpec(
        action_id="readiness_check",
        version=1,
        title="Morning Readiness Check",
        params_model=ReadinessParams,
        narrative_model=ReadinessNarrative,
        prompt=prompts.READINESS_CHECK_PROMPT,
        build_context=build_readiness_context,
        writes_journal=False
    ),
    "weekly_digest": ActionSpec(
        action_id="weekly_digest",
        version=1,
        title="Weekly Digest",
        params_model=WeeklyDigestParams,
        narrative_model=WeeklyDigestNarrative,
        prompt=prompts.WEEKLY_DIGEST_PROMPT,
        build_context=build_weekly_digest_context,
        writes_journal=True
    ),
    "explain_activity": ActionSpec(
        action_id="explain_activity",
        version=1,
        title="Explain Activity",
        params_model=ExplainActivityParams,
        narrative_model=ExplainActivityNarrative,
        prompt=prompts.EXPLAIN_ACTIVITY_PROMPT,
        build_context=build_explain_activity_context,
        writes_journal=False
    ),
    "compare_periods": ActionSpec(
        action_id="compare_periods",
        version=1,
        title="Compare Periods",
        params_model=ComparePeriodsParams,
        narrative_model=ComparePeriodsNarrative,
        prompt=prompts.COMPARE_PERIODS_PROMPT,
        build_context=build_compare_periods_context,
        writes_journal=False
    )
}
