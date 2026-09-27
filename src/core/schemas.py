"""Pydantic data contracts for the medallion pipeline (Bronze manifest -> Silver).

Per architecture guidelines §4:
- Bronze schemas describe *what was fetched* (manifest/metadata), never payload contents.
- Silver schemas are the vendor-agnostic contract: strict typed columns for universal
  metrics, plus a `vendor_specific` JSON escape hatch for proprietary fields.
- Every Silver record is explicitly `user_id`-scoped and carries a clear, entity-specific
  primary key (never a generic, ambiguous identifier).

`src/core/dataframes.py` derives the Pandera/Polars DataFrame contracts from these same
models, so there is exactly one place that defines each entity's shape.

Identifier namespaces are NOT interchangeable: `user_id` is always Strider's own
internal identifier (see `core.config.Settings.user_id` and `User` below), never a
vendor's account identifier. `GarminAccountLink` shows how a vendor account is
referenced by that identifier without ever duplicating vendor secrets.
"""

from datetime import datetime, date
from typing import Any
from pydantic import BaseModel, Field


# -----------------------------------------------------------------------------
# Identity (Strider users <-> linked vendor accounts)
# -----------------------------------------------------------------------------
class User(BaseModel):
    """
    A Strider user: the root identity every data layer (ledger, Silver partitions, agent
    memory) partitions on. Vendor account links (e.g. `GarminAccountLink`) reference
    `user_id` as a foreign key; this model itself never stores vendor credentials.
    """

    user_id: str = Field(..., description="Primary key. Strider's internal user identifier.")
    display_name: str | None = Field(default=None, description="Human-readable name for UI/logging.")
    created_at: datetime = Field(..., description="When this user was first registered.")


class GarminAccountLink(BaseModel):
    """
    Links a Strider `user_id` to the Garmin account used to fetch their data.

    Deliberately does NOT store the Garmin password: secrets stay exclusively in
    `core.config.GarminSettings`, sourced from `.env` (or a future secrets manager),
    never duplicated into this table. Only the login email (low sensitivity, needed to
    identify *which* account) and OAuth session bookkeeping are persisted here, so a
    future multi-user deployment can look up "which Garmin account authenticates for
    this user" without requiring per-user `.env` files.
    """

    user_id: str = Field(..., description="FK to User.user_id.")
    email: str = Field(..., description="Garmin Connect login email for this account.")
    tokenstore_path: str = Field(
        ..., description="Path to this user's cached Garmin OAuth token store (garth session, not a password)."
    )
    linked_at: datetime = Field(..., description="When this Garmin account was linked to the user.")
    last_authenticated_at: datetime | None = Field(
        default=None, description="Last time a login (cached-token or credential) succeeded for this account."
    )


# -----------------------------------------------------------------------------
# Bronze Layer (Ledger)
# -----------------------------------------------------------------------------
class BronzeLedgerEntry(BaseModel):
    """
    Schema for the Bronze sync ledger.
    Describes what was fetched for idempotency and audit, not the payload contents.
    """

    user_id: str = Field(..., description="FK to User.user_id (not a vendor account id).")
    vendor: str = Field(..., description="Vendor name, e.g. 'garmin'.")
    entity_type: str = Field(
        ..., description="One of 'activity', 'wellness', 'workout_definition', 'workout_calendar'."
    )
    source_identifier: str = Field(..., description="Vendor's identifier for the fetched entity.")
    content_hash: str = Field(..., description="Hash of the raw payload, used to detect upstream mutations.")
    fetch_timestamp: datetime = Field(..., description="When this payload was fetched from the vendor.")
    file_path: str = Field(..., description="Path to the immutable raw payload on disk.")


# -----------------------------------------------------------------------------
# Silver Layer (Universal Vendor-Agnostic Models)
# -----------------------------------------------------------------------------
class SilverRecordBase(BaseModel):
    """
    Base contract shared by all Silver-layer entities.
    Enforces user_id partitioning and a flexible vendor_specific payload. Deliberately
    does NOT define a generic identifier field: each concrete entity below declares its
    own explicitly-named primary key (e.g. `activity_id`), per the architecture's
    requirement for "clear primary keys per entity".
    """

    user_id: str = Field(..., description="FK to User.user_id (not a vendor account id).")
    vendor: str = Field(..., description="Vendor name, e.g. 'garmin'.")
    updated_at: datetime = Field(..., description="Last time this record was refreshed from the vendor.")
    vendor_specific: dict[str, Any] = Field(
        default_factory=dict, description="Flexible JSON column for proprietary metrics."
    )


class SilverActivity(SilverRecordBase):
    """
    Standardized Activity record (e.g., a run or a cycle), one row per recorded activity.
    """

    activity_id: str = Field(..., description="Primary key. Vendor's unique activity identifier.")
    start_time: datetime = Field(..., description="Activity start timestamp.")
    activity_type: str = Field(..., description="Universal activity type, e.g. 'running', 'cycling'.")
    duration_seconds: float | None = Field(default=None, description="Elapsed duration in seconds.")
    distance_meters: float | None = Field(default=None, description="Total distance covered in meters.")
    avg_heart_rate: float | None = Field(default=None, description="Average heart rate in bpm.")
    max_heart_rate: float | None = Field(default=None, description="Maximum heart rate in bpm.")
    avg_speed_mps: float | None = Field(default=None, description="Average speed in meters/second.")
    elevation_gain_meters: float | None = Field(default=None, description="Total ascent in meters.")
    calories: float | None = Field(default=None, description="Estimated calories burned.")
    workout_id: str | None = Field(
        default=None,
        description="FK to SilverWorkoutDefinition.workout_id, if this activity followed a planned workout.",
    )
    is_planned_workout: bool = Field(
        default=False, description="Whether this activity was executed against a planned workout."
    )


class SilverWellness(SilverRecordBase):
    """
    Daily wellness snapshot (sleep, HRV, readiness). One row per user per calendar day.
    """

    calendar_date: date = Field(..., description="Primary key (per user).")
    sleep_score: float | None = Field(default=None, description="Vendor-computed overnight sleep score.")
    resting_hr: float | None = Field(default=None, description="Resting heart rate in bpm.")
    hrv_status: str | None = Field(default=None, description="Vendor-computed HRV status label, e.g. 'BALANCED'.")
    avg_stress_level: float | None = Field(default=None, description="Average daily stress level.")
    steps: int | None = Field(default=None, description="Total steps for the day.")
    calories_total: float | None = Field(default=None, description="Total calories burned for the day.")


class WorkoutStep(BaseModel):
    """A single structured step within a workout definition (e.g. '5x800m @ Threshold')."""

    step_order: int = Field(..., description="1-based position of this step within the workout.")
    step_type: str = Field(..., description="e.g. 'warmup', 'interval', 'recovery', 'cooldown', 'rest', 'other'.")
    duration_type: str | None = Field(default=None, description="e.g. 'time', 'distance', 'open'.")
    duration_value: float | None = Field(default=None, description="Duration magnitude, in seconds or meters.")
    target_type: str | None = Field(default=None, description="e.g. 'heart_rate', 'pace', 'power', 'open'.")
    target_low: float | None = Field(default=None, description="Lower bound of the target range.")
    target_high: float | None = Field(default=None, description="Upper bound of the target range.")


class SilverWorkoutDefinition(SilverRecordBase):
    """
    Structured workout definition (steps/targets), sourced from vendor workout FIT files.
    Distinct from the calendar assignment (see `SilverWorkoutCalendar`) per architecture
    §6: the definition is device/plan-recorded structure; the calendar is JSON-only
    scheduling metadata that references a definition by `workout_id`.
    """

    workout_id: str = Field(..., description="Primary key. Vendor's unique workout definition identifier.")
    workout_title: str = Field(..., description="Human-readable workout name.")
    sport_type: str | None = Field(default=None, description="e.g. 'running', 'cycling'.")
    steps: list[WorkoutStep] = Field(default_factory=list, description="Ordered structured steps for this workout.")


class SilverWorkoutCalendar(SilverRecordBase):
    """
    Scheduled workout calendar entry: which workout (if any) is assigned on which date.
    JSON-only since calendar assignment isn't device-recorded.
    """

    calendar_date: date = Field(..., description="Primary key (per user).")
    workout_id: str | None = Field(
        default=None,
        description="FK to SilverWorkoutDefinition.workout_id; None for an ad-hoc/unstructured entry.",
    )
    workout_title: str | None = Field(default=None, description="Human-readable workout name.")
    sport_type: str | None = Field(default=None, description="e.g. 'running', 'cycling'.")
    status: str | None = Field(default=None, description="e.g. 'scheduled', 'completed', 'skipped'.")

