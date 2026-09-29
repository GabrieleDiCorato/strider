"""Pydantic data contracts for the medallion pipeline (Bronze manifest -> Silver).

Per architecture guidelines §4:
- Bronze schemas describe *what was fetched* (manifest/metadata), never payload contents.
- Silver schemas are the vendor-agnostic contract: strict typed columns for universal
  metrics, plus a `vendor_specific` JSON escape hatch for proprietary fields.
- Every Silver record is explicitly `user_id`-scoped and carries a clear, entity-specific
  primary key (never a generic, ambiguous identifier).

Silver is vendor-blind: no `vendor` field on Silver records.  Vendor provenance is
tracked exclusively in the Bronze ledger (`BronzeLedgerEntry.vendor`).

Time-series data (1 Hz HR, GPS, stress epochs) is NOT materialized in Silver.  Silver
stores pre-aggregated summaries (one row per entity instance).  Raw time-series stays
in Bronze FIT files and is read on-demand by Gold-layer computations.

`src/core/dataframes.py` derives the Pandera/Polars DataFrame contracts from these same
models, so there is exactly one place that defines each entity's shape.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, Field, ConfigDict, StringConstraints, model_validator


# ---------------------------------------------------------------------------
# Domain enums — strict typing for universal concepts
# ---------------------------------------------------------------------------

class EntityType(str, Enum):
    """Bronze/Silver entity families driven by the pipeline."""

    ACTIVITY = "activity"
    DAILY_SUMMARY = "daily_summary"
    WORKOUT_DEFINITION = "workout_definition"
    WORKOUT_CALENDAR = "workout_calendar"


class SportType(str, Enum):
    """Canonical sport types supported by the coaching domain.

    Vendor-specific sub-types (e.g. ``treadmill_running``, ``indoor_cycling``)
    are captured in the free-form ``sport_sub_type`` field on the Silver model,
    NOT by extending this enum — that would couple the universal schema to
    every vendor's idiosyncratic type taxonomy.
    """

    RUNNING = "running"
    TRAIL_RUNNING = "trail_running"
    CYCLING = "cycling"
    WALKING = "walking"
    HIKING = "hiking"
    SWIMMING = "swimming"
    OTHER = "other"


class StepType(str, Enum):
    """Workout step types within a structured workout definition."""

    WARMUP = "warmup"
    COOLDOWN = "cooldown"
    INTERVAL = "interval"
    RECOVERY = "recovery"
    REST = "rest"
    REPEAT = "repeat"
    OTHER = "other"


class DurationType(str, Enum):
    """How a workout step's duration / end-condition is defined."""

    TIME = "time"
    DISTANCE = "distance"
    OPEN = "open"  # "press lap" / manual advance
    CALORIES = "calories"
    OTHER = "other"


class TargetType(str, Enum):
    """Target metric for a workout step's intensity prescription."""

    HEART_RATE = "heart_rate"
    PACE = "pace"
    SPEED = "speed"
    POWER = "power"
    CADENCE = "cadence"
    OPEN = "open"  # no specific target
    OTHER = "other"


class TrainingStatus(str, Enum):
    """Training status labels.

    These originate as a Garmin-proprietary concept but are promoted to a
    first-class enum because they directly inform coaching decisions.
    """

    PRODUCTIVE = "productive"
    MAINTAINING = "maintaining"
    RECOVERY = "recovery"
    UNPRODUCTIVE = "unproductive"
    DETRAINING = "detraining"
    PEAKING = "peaking"
    OVERREACHING = "overreaching"
    NO_STATUS = "no_status"


class HrvStatus(str, Enum):
    """HRV readiness status."""

    BALANCED = "balanced"
    UNBALANCED = "unbalanced"
    LOW = "low"
    POOR = "poor"
    NO_STATUS = "no_status"

basic_config = ConfigDict(
    extra="forbid",
    frozen=True
    )

# A `user_id` is embedded literally (never percent-encoded) into Bronze/Silver
# file paths and Hive partition segments (`user_id=...`), and DuckDB's
# `hive_partitioning` reader reconstructs it from that raw path segment. For
# that round-trip to be lossless and filesystem/URL safe, every `user_id`
# everywhere in the system is constrained to this charset at construction
# time — this is the single source of truth, not a check duplicated at each
# storage boundary.
UserId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"),
]


# ---------------------------------------------------------------------------
# Identity (Strider users <-> linked vendor accounts)
# ---------------------------------------------------------------------------

class User(BaseModel):
    model_config = basic_config
    """A Strider user: the root identity every data layer partitions on.

    Vendor account links (e.g. ``GarminAccountLink``) reference ``user_id``
    as a foreign key; this model itself never stores vendor credentials.
    """

    user_id: UserId = Field(
        ..., description="Primary key. Strider's internal user identifier."
    )
    display_name: str | None = Field(
        default=None, description="Human-readable name for UI/logging."
    )
    created_at: datetime = Field(
        ..., description="When this user was first registered."
    )


class GarminAccountLink(BaseModel):
    """Links a Strider ``user_id`` to the Garmin account used to fetch data.

    Does NOT store the Garmin password: secrets stay exclusively in
    ``core.config.GarminSettings``, sourced from ``.env``.
    """

    user_id: UserId = Field(..., description="FK to User.user_id.")
    email: str = Field(
        ..., description="Garmin Connect login email for this account."
    )
    tokenstore_path: str = Field(
        ...,
        description="Path to this user's cached Garmin OAuth token store.",
    )
    linked_at: datetime = Field(
        ..., description="When this Garmin account was linked to the user."
    )
    last_authenticated_at: datetime | None = Field(
        default=None,
        description="Last time a login succeeded for this account.",
    )


# ---------------------------------------------------------------------------
# Bronze Layer (Ledger)
# ---------------------------------------------------------------------------

class BronzeLedgerEntry(BaseModel):
    """Schema for the Bronze sync ledger.

    Describes *what was fetched* for idempotency and audit — not the payload
    contents.  ``entity_type`` is now an ``EntityType`` enum rather than a
    free-form string.
    """

    user_id: UserId = Field(
        ..., description="FK to User.user_id (not a vendor account id)."
    )
    vendor: str = Field(..., description="Vendor name, e.g. 'garmin'.")
    entity_type: EntityType = Field(
        ..., description="Which entity family this payload belongs to."
    )
    source_identifier: str = Field(
        ..., description="Vendor's identifier for the fetched entity."
    )
    content_hash: str = Field(
        ...,
        description="Hash of the raw payload, used to detect upstream mutations.",
    )
    fetch_timestamp: datetime = Field(
        ..., description="When this payload was fetched from the vendor."
    )
    file_path: str = Field(
        ..., description="Path to the immutable raw payload on disk."
    )


# ---------------------------------------------------------------------------
# Silver Layer — Base
# ---------------------------------------------------------------------------

class SilverRecordBase(BaseModel):
    """Base contract shared by all Silver-layer entities.

    Silver is vendor-blind — there is no ``vendor`` field.  Vendor provenance
    lives in the Bronze ledger.  The ``vendor_specific`` dict is an escape
    hatch for proprietary metrics that don't yet have universal typed columns;
    downstream code should never need to parse it for core coaching logic.

    Each concrete Silver entity declares its own explicitly-named primary key
    (e.g. ``activity_id``, ``calendar_date``).
    """

    user_id: UserId = Field(
        ..., description="FK to User.user_id (not a vendor account id)."
    )
    updated_at: datetime = Field(
        ...,
        description="Last time this record was refreshed from the vendor.",
    )
    vendor_specific: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Escape hatch for proprietary metrics not yet promoted to "
            "typed columns."
        ),
    )


# ---------------------------------------------------------------------------
# Silver Layer — Activity
# ---------------------------------------------------------------------------

class LapSummary(BaseModel):
    """Pre-aggregated per-lap metrics, extracted from FIT lap messages.

    Stored as a JSON-serialized list on ``SilverActivity.lap_summaries``.
    """

    lap_number: int = Field(
        ..., description="1-based lap index within the activity."
    )
    duration_seconds: float = Field(
        ..., description="Lap elapsed time in seconds."
    )
    distance_meters: float | None = Field(
        default=None, description="Lap distance in meters."
    )
    avg_heart_rate: float | None = Field(
        default=None, description="Lap average HR in bpm."
    )
    max_heart_rate: float | None = Field(
        default=None, description="Lap max HR in bpm."
    )
    avg_cadence: float | None = Field(
        default=None,
        description="Lap average cadence (spm for running, rpm for cycling).",
    )
    avg_speed_mps: float | None = Field(
        default=None, description="Lap average speed in m/s."
    )
    avg_power: float | None = Field(
        default=None, description="Lap average power in watts."
    )
    total_ascent_meters: float | None = Field(
        default=None, description="Lap total ascent in meters."
    )
    total_descent_meters: float | None = Field(
        default=None, description="Lap total descent in meters."
    )


class SilverActivity(SilverRecordBase):
    """Standardized activity record — one row per recorded activity.

    Contains scalar summary metrics computed from FIT session/lap messages
    plus pre-aggregated time-series summaries (HR zone distribution, per-lap
    breakdowns).

    Raw time-series data (1 Hz HR, GPS, cadence) is NOT materialized here —
    it stays in Bronze FIT files and is read on-demand by Gold-layer
    computations (e.g. cardiac drift, pace decoupling).
    """

    # ── Identity & type ────────────────────────────────────
    activity_id: str = Field(
        ..., description="Primary key. Vendor's unique activity identifier."
    )
    activity_name: str | None = Field(
        default=None, description="User-friendly name of the activity."
    )
    sport_type: SportType = Field(
        ..., description="Canonical sport type (enum)."
    )
    sport_sub_type: str | None = Field(
        default=None,
        description=(
            "Vendor-specific sub-type "
            "(e.g. 'treadmill_running', 'indoor_cycling')."
        ),
    )

    # ── Timing ─────────────────────────────────────────────
    start_time: datetime = Field(
        ..., description="Activity start timestamp (local)."
    )
    end_time: datetime | None = Field(
        default=None, description="Activity end timestamp (local)."
    )
    duration_seconds: float | None = Field(
        default=None,
        description="Total elapsed time in seconds (including pauses).",
    )
    moving_time_seconds: float | None = Field(
        default=None,
        description=(
            "Moving/active time in seconds (excluding auto-pause). "
            "Fundamental for accurate pace computation."
        ),
    )

    # ── Distance & elevation ───────────────────────────────
    distance_meters: float | None = Field(
        default=None, description="Total distance in meters."
    )
    total_ascent_meters: float | None = Field(
        default=None, description="Total ascent (climb) in meters."
    )
    total_descent_meters: float | None = Field(
        default=None, description="Total descent in meters."
    )

    # ── Heart rate ─────────────────────────────────────────
    avg_heart_rate: float | None = Field(
        default=None, description="Average HR in bpm."
    )
    max_heart_rate: float | None = Field(
        default=None, description="Peak HR in bpm."
    )
    hr_zone_seconds: list[float] | None = Field(
        default=None,
        description=(
            "Time spent in each HR zone (seconds), ordered zone 1 → zone N. "
            "Pre-aggregated from FIT time_in_hr_zone fields."
        ),
    )

    # ── Speed & pace ───────────────────────────────────────
    avg_speed_mps: float | None = Field(
        default=None, description="Average speed in m/s."
    )
    max_speed_mps: float | None = Field(
        default=None, description="Peak speed in m/s."
    )

    # ── Cadence ────────────────────────────────────────────
    avg_cadence: float | None = Field(
        default=None,
        description="Average cadence — steps/min for running, rpm for cycling.",
    )
    max_cadence: float | None = Field(
        default=None, description="Peak cadence."
    )

    # ── Power ──────────────────────────────────────────────
    avg_power: float | None = Field(
        default=None, description="Average power in watts."
    )
    max_power: float | None = Field(
        default=None, description="Peak power in watts."
    )
    normalized_power: float | None = Field(
        default=None,
        description=(
            "Normalized power (watts). Represents the physiological cost "
            "of variable-intensity effort."
        ),
    )

    # ── Training stimulus ──────────────────────────────────
    training_effect_aerobic: float | None = Field(
        default=None, description="Aerobic training effect (0.0–5.0 scale)."
    )
    training_effect_anaerobic: float | None = Field(
        default=None,
        description="Anaerobic training effect (0.0–5.0 scale).",
    )
    calories: float | None = Field(
        default=None, description="Estimated calories burned."
    )
    avg_temperature_celsius: float | None = Field(
        default=None,
        description="Average ambient temperature during activity (°C).",
    )

    # ── Lap structure (pre-aggregated) ─────────────────────
    lap_count: int | None = Field(
        default=None, description="Number of laps/splits."
    )
    lap_summaries: list[LapSummary] | None = Field(
        default=None,
        description="Per-lap summary metrics. JSON-serialized in Parquet.",
    )

    # ── Workout linkage ────────────────────────────────────
    workout_id: str | None = Field(
        default=None,
        description=(
            "FK to SilverWorkoutDefinition.workout_id, if this activity "
            "followed a planned workout."
        ),
    )
    is_planned_workout: bool = Field(
        default=False,
        description="Whether this activity was executed against a planned workout.",
    )


# ---------------------------------------------------------------------------
# Silver Layer — Daily Summary  (replaces the former SilverWellness)
# ---------------------------------------------------------------------------

class SilverDailySummary(SilverRecordBase):
    """Complete daily physiological snapshot — one row per user per day.

    Covers the full readiness picture a coaching agent needs: sleep
    architecture, recovery signals (HRV, resting HR, body battery), stress,
    respiratory and SpO2 metrics, and training status.

    Fields are logically grouped by physiological concern but stored in a
    single wide table.  Parquet's columnar format means unused columns cost
    nothing at query time, and the coaching agent's most common query ("how
    is the athlete today?") gets everything in one scan without joins.

    Raw time-series data (HR epochs, stress curves, body battery values) is
    NOT materialized here — it stays in Bronze FIT files.
    """

    calendar_date: date = Field(
        ..., description="Primary key (per user). The calendar day."
    )

    # ── Sleep ──────────────────────────────────────────────
    sleep_score: float | None = Field(
        default=None, description="Vendor-computed overall sleep score."
    )
    sleep_start_time: datetime | None = Field(
        default=None, description="Sleep onset timestamp."
    )
    sleep_end_time: datetime | None = Field(
        default=None, description="Sleep end (wake) timestamp."
    )
    sleep_duration_seconds: int | None = Field(
        default=None, description="Total time in bed (seconds)."
    )
    deep_sleep_seconds: int | None = Field(
        default=None, description="Time in deep/N3 sleep (seconds)."
    )
    light_sleep_seconds: int | None = Field(
        default=None, description="Time in light/N1+N2 sleep (seconds)."
    )
    rem_sleep_seconds: int | None = Field(
        default=None, description="Time in REM sleep (seconds)."
    )
    awake_seconds: int | None = Field(
        default=None,
        description="Time awake during sleep window (seconds).",
    )
    avg_overnight_hr: float | None = Field(
        default=None, description="Average HR during sleep (bpm)."
    )
    avg_overnight_hrv: float | None = Field(
        default=None,
        description=(
            "Average HRV during sleep (ms), if available from "
            "sleep HRV monitoring."
        ),
    )

    # ── Readiness & recovery ───────────────────────────────
    resting_hr: float | None = Field(
        default=None, description="Resting heart rate (bpm)."
    )
    resting_hr_7day_avg: float | None = Field(
        default=None,
        description=(
            "7-day rolling average resting HR (bpm). "
            "Trend is more informative than the daily value."
        ),
    )
    hrv_status: HrvStatus | None = Field(
        default=None, description="HRV readiness status (enum)."
    )
    hrv_weekly_avg: float | None = Field(
        default=None, description="7-day average HRV (ms)."
    )
    body_battery_high: int | None = Field(
        default=None,
        description="Peak body battery value for the day (0–100).",
    )
    body_battery_low: int | None = Field(
        default=None,
        description="Lowest body battery value for the day (0–100).",
    )
    body_battery_charged: int | None = Field(
        default=None,
        description="Amount of body battery charged during the day.",
    )
    body_battery_drained: int | None = Field(
        default=None,
        description="Amount of body battery drained during the day.",
    )
    body_battery_current: int | None = Field(
        default=None,
        description="Most recent body battery value.",
    )
    avg_stress_level: float | None = Field(
        default=None, description="Average stress level (0–100 scale)."
    )
    max_stress_level: float | None = Field(
        default=None, description="Peak stress level for the day."
    )
    training_status: TrainingStatus | None = Field(
        default=None, description="Training status assessment (enum)."
    )
    training_load_balance: float | None = Field(
        default=None,
        description=(
            "Ratio of acute to chronic training load. "
            "Values >1 indicate increasing load; <1 recovery."
        ),
    )
    vo2max_running: float | None = Field(
        default=None,
        description="Estimated running VO2max (mL/kg/min).",
    )

    # ── Body metrics ───────────────────────────────────────
    avg_spo2: float | None = Field(
        default=None,
        description="Average blood oxygen saturation (%).",
    )
    lowest_spo2: float | None = Field(
        default=None, description="Lowest SpO2 reading (%)."
    )
    avg_respiration_waking: float | None = Field(
        default=None,
        description="Average waking respiration rate (breaths/min).",
    )
    avg_respiration_sleep: float | None = Field(
        default=None,
        description="Average sleeping respiration rate (breaths/min).",
    )
    steps: int | None = Field(
        default=None, description="Total step count for the day."
    )
    calories_active: float | None = Field(
        default=None, description="Active calories burned."
    )
    calories_total: float | None = Field(
        default=None, description="Total calories (active + BMR)."
    )


# ---------------------------------------------------------------------------
# Silver Layer — Workout Definitions & Calendar
# ---------------------------------------------------------------------------

class WorkoutStep(BaseModel):
    """A single step within a structured workout definition.

    Supports recursive repeat structure: a step with
    ``step_type == StepType.REPEAT`` has a ``repeat_count`` and nested
    ``children`` steps (e.g. "5× { 800 m @ threshold, 400 m jog }").
    Leaf steps (warmup, interval, recovery, cooldown) have ``children == []``.
    """

    step_order: int = Field(
        ...,
        description="1-based position of this step within its parent.",
    )
    step_type: StepType = Field(..., description="Step type (enum).")
    duration_type: DurationType | None = Field(
        default=None,
        description="How the step ends (time, distance, open).",
    )
    duration_value: float | None = Field(
        default=None,
        description=(
            "Duration magnitude. Seconds for TIME, meters for DISTANCE."
        ),
    )
    target_type: TargetType | None = Field(
        default=None,
        description="Target metric (HR, pace, power, open).",
    )
    target_low: float | None = Field(
        default=None, description="Lower bound of the target range."
    )
    target_high: float | None = Field(
        default=None, description="Upper bound of the target range."
    )
    repeat_count: int | None = Field(
        default=None,
        description=(
            "For REPEAT steps: how many times to execute the children. "
            "None for non-repeat steps."
        ),
    )
    children: list[WorkoutStep] = Field(
        default_factory=list,
        description="Nested steps within a repeat group. Empty for leaf steps.",
    )


class SilverWorkoutDefinition(SilverRecordBase):
    """Structured workout definition (steps/targets).

    Sourced from vendor workout FIT files.  Distinct from the calendar
    assignment (``SilverWorkoutCalendar``): the definition is device/plan-
    recorded structure; the calendar is JSON-only scheduling metadata.
    """

    workout_id: str = Field(
        ...,
        description="Primary key. Vendor's unique workout definition identifier.",
    )
    workout_title: str = Field(
        ..., description="Human-readable workout name."
    )
    sport_type: SportType | None = Field(
        default=None, description="Target sport type (enum)."
    )
    estimated_duration_seconds: float | None = Field(
        default=None,
        description="Estimated total workout duration in seconds.",
    )
    estimated_distance_meters: float | None = Field(
        default=None,
        description="Estimated total workout distance in meters.",
    )
    description: str | None = Field(
        default=None, description="Workout description / notes."
    )
    steps: list[WorkoutStep] = Field(
        default_factory=list,
        description="Ordered, possibly nested, structured steps.",
    )


class SilverWorkoutCalendar(SilverRecordBase):
    """Scheduled workout calendar entry.

    Which workout is assigned on which date.  JSON-only since calendar
    assignment is not device-recorded.
    """

    calendar_date: date = Field(
        ..., description="Primary key (per user). Scheduled date."
    )
    workout_id: str | None = Field(
        default=None,
        description=(
            "FK to SilverWorkoutDefinition.workout_id; "
            "None for ad-hoc entries."
        ),
    )
    workout_title: str | None = Field(
        default=None, description="Human-readable workout name."
    )
    sport_type: SportType | None = Field(
        default=None, description="Target sport type (enum)."
    )
    status: str | None = Field(
        default=None,
        description="e.g. 'scheduled', 'completed', 'skipped'.",
    )
    training_plan_id: str | None = Field(
        default=None,
        description=(
            "Garmin ATP plan id, if this workout belongs to a formal "
            "training plan.  Lets the agent distinguish 'following a 10K "
            "plan' from 'coach-created workouts'."
        ),
    )


# ---------------------------------------------------------------------------
# Qualitative context — Coaching Journal & static memory (architecture §7-8)
# ---------------------------------------------------------------------------

class EntrySource(str, Enum):
    """Who authored a journal entry or memory fact."""

    USER = "user"
    AI = "ai"


class JournalCategory(str, Enum):
    """What kind of context a journal entry captures."""

    FEELING = "feeling"
    INJURY = "injury"
    EXERTION = "exertion"
    LIFESTYLE = "lifestyle"
    NOTE = "note"
    OBSERVATION = "observation"  # AI narrative only


class MemoryCategory(str, Enum):
    PREFERENCE = "preference"
    GOAL = "goal"
    CONSTRAINT = "constraint"
    PROFILE = "profile"
    COMMUNICATION = "communication"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    PROPOSED = "proposed"  # AI-authored, awaiting user confirmation


JournalText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
MemoryKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
MemoryValue = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class JournalEntry(BaseModel):
    """One immutable entry on a user's chronological coaching timeline.

    `text` is user- or model-authored free text: consumers must treat it as
    inert data, never as instructions.
    """

    model_config = basic_config

    entry_id: str = Field(..., description="Primary key, assigned internally by the store.")
    user_id: UserId
    entry_date: date = Field(..., description="The day this entry is about (may differ from created_at).")
    created_at: datetime = Field(..., description="When the entry was recorded.")
    source: EntrySource
    category: JournalCategory
    text: JournalText
    rating: int | None = Field(
        default=None,
        ge=1,
        le=10,
        description="Self-reported intensity: perceived exertion for exertion entries, severity for injuries.",
    )
    related_activity_id: str | None = Field(
        default=None, description="Optional link to SilverActivity.activity_id."
    )

    @model_validator(mode="after")
    def _source_matches_category(self) -> JournalEntry:
        is_observation = self.category is JournalCategory.OBSERVATION
        if self.source is EntrySource.AI and not is_observation:
            raise ValueError("AI-authored entries must use the 'observation' category.")
        if self.source is EntrySource.USER and is_observation:
            raise ValueError("The 'observation' category is reserved for AI-authored entries.")
        return self


class MemoryFact(BaseModel):
    """A durable user-level fact injected into the agent's system prompt once ACTIVE."""

    model_config = basic_config

    user_id: UserId
    key: MemoryKey
    category: MemoryCategory
    value: MemoryValue
    source: EntrySource
    status: MemoryStatus
    created_at: datetime
    updated_at: datetime
