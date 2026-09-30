"""Garmin FIT/JSON mappings into vendor-blind Silver records."""

from __future__ import annotations

import json
import zipfile
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import fitdecode

from src.core.schemas import (
    BronzeLedgerEntry,
    DurationType,
    EntityType,
    HrvStatus,
    LapSummary,
    SilverActivity,
    SilverDailySummary,
    SilverWorkoutCalendar,
    SilverWorkoutDefinition,
    SportType,
    StepType,
    TargetType,
    TrainingStatus,
    WorkoutStep,
)


FitMessages = dict[str, list[dict[str, Any]]]


def _message_values(message: fitdecode.FitDataMessage) -> dict[str, Any]:
    return {field.name: field.value for field in message.fields}


def _read_fit_stream(stream: Any) -> FitMessages:
    messages: FitMessages = defaultdict(list)
    with fitdecode.FitReader(stream) as reader:
        for message in reader:
            if isinstance(message, fitdecode.FitDataMessage):
                messages[message.name].append(_message_values(message))
    return dict(messages)


def _merge_messages(target: FitMessages, source: FitMessages) -> None:
    for name, records in source.items():
        target.setdefault(name, []).extend(records)


def _read_fit_payload(path: str | Path) -> FitMessages:
    """Read FIT messages from a raw FIT payload or a ZIP of FIT files."""
    source = Path(path)
    messages: FitMessages = {}
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            fit_names = [name for name in archive.namelist() if name.lower().endswith(".fit")]
            for name in fit_names:
                with archive.open(name) as stream:
                    _merge_messages(messages, _read_fit_stream(stream))
        return messages

    with source.open("rb") as stream:
        return _read_fit_stream(stream)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _int(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _first(record: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = record.get(name)
        if value is not None:
            return value
    return None


def _enum_value(value: Any) -> str:
    if hasattr(value, "name"):
        return str(value.name).lower()
    return str(value).lower()


def _sport_type(value: Any, sub_sport: Any = None) -> SportType:
    sport = _enum_value(value) if value is not None else ""
    subtype = _enum_value(sub_sport) if sub_sport is not None else ""
    if "trail" in subtype:
        return SportType.TRAIL_RUNNING
    if "run" in sport or sport == "1":
        return SportType.RUNNING
    if "cycl" in sport or sport == "2":
        return SportType.CYCLING
    if "walk" in sport or sport == "11":
        return SportType.WALKING
    if "hik" in sport or sport == "17":
        return SportType.HIKING
    if "swim" in sport or sport == "5":
        return SportType.SWIMMING
    return SportType.OTHER


def _sport_subtype(value: Any) -> str | None:
    if value is None:
        return None
    label = _enum_value(value)
    return None if label in {"", "none", "generic", "0"} else label


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _require_entries(entries: list[BronzeLedgerEntry], entity_type: EntityType) -> None:
    wrong = [entry.entity_type for entry in entries if entry.entity_type is not entity_type]
    if wrong:
        raise ValueError(f"Expected {entity_type.value} ledger entries, received {wrong!r}.")


class ActivityExtractor:
    """Map FIT session and lap summaries to one `SilverActivity` per Bronze entry."""

    @property
    def entity_type(self) -> EntityType:
        return EntityType.ACTIVITY

    def extract(self, entries: list[BronzeLedgerEntry]) -> list[SilverActivity]:
        _require_entries(entries, self.entity_type)
        return [self._extract_entry(entry) for entry in entries]

    def _extract_entry(self, entry: BronzeLedgerEntry) -> SilverActivity:
        messages = _read_fit_payload(entry.file_path)

        import zipfile, json
        activity_json = {}
        if zipfile.is_zipfile(entry.file_path):
            with zipfile.ZipFile(entry.file_path) as archive:
                if 'activity.json' in archive.namelist():
                    activity_json = json.loads(archive.read('activity.json').decode('utf-8'))

        sessions = messages.get("session", [])
        if not sessions:
            raise ValueError(f"Activity FIT payload has no session message: {entry.file_path}")
        session = sessions[-1]
        start_time = _as_datetime(_first(session, "start_time", "timestamp"))
        if start_time is None:
            raise ValueError(f"Activity FIT session has no valid start_time: {entry.file_path}")

        sport_value = _first(session, "sport", "sport_type")
        subtype_value = _first(session, "sub_sport", "sport_sub_type")
        laps = [
            LapSummary(
                lap_number=index,
                duration_seconds=_number(_first(lap, "total_elapsed_time", "total_timer_time")) or 0.0,
                distance_meters=_number(_first(lap, "total_distance", "distance")),
                avg_heart_rate=_number(lap.get("avg_heart_rate")),
                max_heart_rate=_number(_first(lap, "max_heart_rate")),
                avg_cadence=_number(_first(lap, "avg_cadence", "avg_running_cadence")),
                avg_speed_mps=_number(_first(lap, "avg_speed")),
                avg_power=_number(lap.get("avg_power")),
                total_ascent_meters=_number(_first(lap, "total_ascent")),
                total_descent_meters=_number(_first(lap, "total_descent")),
            )
            for index, lap in enumerate(messages.get("lap", []), start=1)
        ]
        zone_records = [
            record
            for record in messages.get("time_in_zone", [])
            if record.get("reference_mesg") == "session"
        ]
        zone_record = zone_records[-1] if zone_records else session
        zone_values = _first(zone_record, "time_in_hr_zone")
        hr_zone_seconds = (
            [duration for value in zone_values if (duration := _number(value)) is not None]
            if isinstance(zone_values, (list, tuple))
            else None
        )

        return SilverActivity(
            user_id=entry.user_id,
            updated_at=entry.fetch_timestamp,
            activity_id=entry.source_identifier,
            activity_name=activity_json.get('activityName'),
            sport_type=_sport_type(sport_value, subtype_value),
            sport_sub_type=_sport_subtype(subtype_value),
            start_time=start_time,
            end_time=_as_datetime(session.get("timestamp")),
            duration_seconds=_number(_first(session, "total_elapsed_time", "elapsed_time")),
            moving_time_seconds=_number(
                _first(session, "total_moving_time", "total_timer_time", "timer_time")
            ),
            distance_meters=_number(_first(session, "total_distance", "distance")),
            total_ascent_meters=_number(_first(session, "total_ascent")),
            total_descent_meters=_number(_first(session, "total_descent")),
            avg_heart_rate=_number(_first(session, "avg_heart_rate")),
            max_heart_rate=_number(_first(session, "max_heart_rate")),
            hr_zone_seconds=hr_zone_seconds or None,
            avg_speed_mps=_number(_first(session, "avg_speed")),
            max_speed_mps=_number(_first(session, "max_speed")),
            avg_cadence=_number(_first(session, "avg_cadence", "avg_running_cadence")),
            max_cadence=_number(_first(session, "max_cadence", "max_running_cadence")),
            avg_power=_number(_first(session, "avg_power")),
            max_power=_number(_first(session, "max_power")),
            normalized_power=_number(_first(session, "normalized_power")),
            training_effect_aerobic=_number(_first(session, "total_training_effect")),
            training_effect_anaerobic=_number(_first(session, "total_anaerobic_training_effect")),
            calories=_number(_first(session, "total_calories", "calories")),
            avg_temperature_celsius=_number(_first(session, "avg_temperature")),
            lap_count=len(laps),
            lap_summaries=laps or None,
            workout_id=None,
            is_planned_workout=False,
            vendor_specific={
                "garmin_sport": sport_value,
                "garmin_sub_sport": subtype_value,
            },
        )


class DailySummaryExtractor:
    """Aggregate recognized daily FIT fields without materializing raw epochs."""

    @property
    def entity_type(self) -> EntityType:
        return EntityType.DAILY_SUMMARY

    def extract(self, entries: list[BronzeLedgerEntry]) -> list[SilverDailySummary]:
        _require_entries(entries, self.entity_type)
        return [self._extract_entry(entry) for entry in entries]

    def _extract_entry(self, entry: BronzeLedgerEntry) -> SilverDailySummary:
        messages = _read_fit_payload(entry.file_path)
        try:
            calendar_date = date.fromisoformat(entry.source_identifier)
        except ValueError as exc:
            raise ValueError(
                f"Daily summary identifier must be an ISO date: {entry.source_identifier!r}"
            ) from exc

        # Read JSON payloads from the zip if present
        import zipfile, json
        stats_json = {}
        sleep_json = {}
        hrv_json = {}
        ts_json = {}
        if zipfile.is_zipfile(entry.file_path):
            with zipfile.ZipFile(entry.file_path) as archive:
                if 'stats.json' in archive.namelist():
                    stats_json = json.loads(archive.read('stats.json').decode('utf-8'))
                if 'sleep.json' in archive.namelist():
                    sleep_json = json.loads(archive.read('sleep.json').decode('utf-8'))
                if 'hrv.json' in archive.namelist():
                    hrv_json = json.loads(archive.read('hrv.json').decode('utf-8'))
                if 'training_status.json' in archive.namelist():
                    ts_json = json.loads(archive.read('training_status.json').decode('utf-8'))

        all_records = [record for records in messages.values() for record in records]
        records = [record for record in all_records if record]

        sleep_records = messages.get("sleep_level", [])
        monitoring_records = messages.get("monitoring", [])
        sleep = _summarize_sleep(sleep_records)
        
        # Override sleep with JSON if available
        ds = sleep_json.get('dailySleepDTO', {})
        if ds.get('sleepTimeSeconds'):
            sleep['start_time'] = datetime.fromtimestamp(ds['sleepStartTimestampGMT'] / 1000, tz=timezone.utc).replace(tzinfo=None) if ds.get('sleepStartTimestampGMT') else sleep['start_time']
            sleep['end_time'] = datetime.fromtimestamp(ds['sleepEndTimestampGMT'] / 1000, tz=timezone.utc).replace(tzinfo=None) if ds.get('sleepEndTimestampGMT') else sleep['end_time']
            sleep['duration_seconds'] = ds.get('sleepTimeSeconds', sleep.get('duration_seconds'))
            sleep['deep_seconds'] = ds.get('deepSleepSeconds', sleep.get('deep_seconds'))
            sleep['light_seconds'] = ds.get('lightSleepSeconds', sleep.get('light_seconds'))
            sleep['rem_seconds'] = ds.get('remSleepSeconds', sleep.get('rem_seconds'))
            sleep['awake_seconds'] = ds.get('awakeSleepSeconds', sleep.get('awake_seconds'))

        stress_records = _unique_records(messages.get("stress_level", []), "stress_level_time")
        stress_values = [
            value
            for record in stress_records
            if (value := _bounded_number(record.get("stress_level_value"), 0, 100)) is not None
        ]
        spo2_records = _unique_records(messages.get("spo2_data", []), "timestamp")
        spo2_values = [
            value
            for record in spo2_records
            if (value := _bounded_number(record.get("reading_spo2"), 0, 100)) is not None
        ]
        respiration_records = _unique_records(messages.get("respiration_rate", []), "timestamp")
        respiration_values = [
            (record["timestamp"], value)
            for record in respiration_records
            if isinstance(record.get("timestamp"), datetime)
            and (value := _bounded_number(record.get("respiration_rate"), 0, 100)) is not None
        ]
        stage_intervals = sleep.get("stage_intervals", [])

        monitoring_hr = _unique_records(messages.get("monitoring_hr_data", []), "timestamp")

        hrv_status_str = _first(next((record for record in messages.get("hrv_status_summary", []) if record.get("status") is not None), {}), "status")
        if not hrv_status_str and hrv_json.get('hrvSummary'):
            hrv_status_str = hrv_json['hrvSummary'].get('status')
        hrv_status = _mapped_enum(hrv_status_str, HrvStatus)
        
        training_status_str = _first(next((r for r in records if _first(r, "training_status") is not None), {}), "training_status")
        if not training_status_str and ts_json.get('mostRecentTrainingStatus'):
            training_status_str = ts_json.get('mostRecentTrainingStatus')
        training_status = _mapped_enum(training_status_str, TrainingStatus)
        
        vo2max_running = ts_json.get('mostRecentVO2Max')

        sleep_score = _record_number(messages.get("sleep_assessment", []), "overall_sleep_score")
        if not sleep_score and ds.get('sleepScores'):
            sleep_score = ds['sleepScores'].get('overall', {}).get('value')
            
        hrv_weekly_avg = _record_number(messages.get("hrv_status_summary", []), "weekly_average")
        if not hrv_weekly_avg and hrv_json.get('hrvSummary'):
            hrv_weekly_avg = hrv_json['hrvSummary'].get('weeklyAvg')

        respiration_sleep = [
            value for record_time, value in respiration_values
            if _is_sleep_timestamp(record_time, sleep.get("intervals", []))
        ]
        respiration_waking = [
            value for record_time, value in respiration_values
            if not _is_sleep_timestamp(record_time, sleep.get("intervals", []))
        ]

        return SilverDailySummary(
            user_id=entry.user_id,
            updated_at=entry.fetch_timestamp,
            calendar_date=calendar_date,
            sleep_score=sleep_score,
            sleep_start_time=sleep["start_time"],
            sleep_end_time=sleep["end_time"],
            sleep_duration_seconds=sleep["duration_seconds"],
            deep_sleep_seconds=sleep["deep_seconds"],
            light_sleep_seconds=sleep["light_seconds"],
            rem_sleep_seconds=sleep["rem_seconds"],
            awake_seconds=sleep["awake_seconds"],
            avg_overnight_hr=_average(
                record["heart_rate"]
                for record in monitoring_records
                if isinstance(record.get("timestamp"), datetime)
                and record.get("heart_rate") is not None
                and _is_sleep_timestamp(record["timestamp"], stage_intervals)
            ),
            avg_overnight_hrv=_record_number(
                messages.get("hrv_status_summary", []), "last_night_average"
            ),
            resting_hr=_record_number(
                monitoring_hr, "current_day_resting_heart_rate", "resting_heart_rate"
            ) or stats_json.get('restingHeartRate'),
            resting_hr_7day_avg=_record_number(records, "resting_hr_7day_avg"),
            hrv_status=hrv_status,
            hrv_weekly_avg=hrv_weekly_avg,
            body_battery_high=_extreme_int(messages.get("hsa_body_battery_data", []), "level", max) or stats_json.get('bodyBatteryHighestValue'),
            body_battery_low=_extreme_int(messages.get("hsa_body_battery_data", []), "level", min) or stats_json.get('bodyBatteryLowestValue'),
            body_battery_charged=stats_json.get('bodyBatteryChargedValue'),
            body_battery_drained=stats_json.get('bodyBatteryDrainedValue'),
            body_battery_current=stats_json.get('bodyBatteryMostRecentValue'),
            avg_stress_level=_average(stress_values) or stats_json.get('averageStressLevel'),
            max_stress_level=_extreme_int(stress_records, "stress_level_value", max) or stats_json.get('maxStressLevel'),
            avg_spo2=_average(spo2_values) or stats_json.get('averageSpo2'),
            lowest_spo2=_extreme_int(spo2_records, "reading_spo2", min) or stats_json.get('lowestSpo2'),
            avg_respiration_waking=_average(respiration_waking),
            avg_respiration_sleep=_average(respiration_sleep),
            steps=_extreme_int(records, "steps", max) or stats_json.get('totalSteps'),
            calories_active=_extreme_int(records, "active_calories", max) or stats_json.get('activeKilocalories'),
            calories_total=_extreme_int(records, "calories", max),
            training_status=training_status,
            vo2max_running=vo2max_running,
        )

class WorkoutExtractor:
    """Map FIT workout/workout_step messages to structured Silver definitions."""

    @property
    def entity_type(self) -> EntityType:
        return EntityType.WORKOUT_DEFINITION

    def extract(self, entries: list[BronzeLedgerEntry]) -> list[SilverWorkoutDefinition]:
        _require_entries(entries, self.entity_type)
        return [self._extract_entry(entry) for entry in entries]

    def _extract_entry(self, entry: BronzeLedgerEntry) -> SilverWorkoutDefinition:
        messages = _read_fit_payload(entry.file_path)
        workout = next(iter(messages.get("workout", [])), {})
        fit_steps = messages.get("workout_step", [])
        steps = [_workout_step(record, index) for index, record in enumerate(fit_steps, start=1)]
        title = _first(workout, "wkt_name", "workout_name", "name") or f"Workout {entry.source_identifier}"
        sport = _first(workout, "sport", "sport_type")

        has_repeat = any(step.step_type is StepType.REPEAT for step in steps)
        durations = [
            _number(step.duration_value) for step in steps if step.duration_type is DurationType.TIME
        ]
        distances = [
            _number(step.duration_value) for step in steps if step.duration_type is DurationType.DISTANCE
        ]
        return SilverWorkoutDefinition(
            user_id=entry.user_id,
            updated_at=entry.fetch_timestamp,
            workout_id=entry.source_identifier,
            workout_title=str(title),
            sport_type=_sport_type(sport) if sport is not None else None,
            estimated_duration_seconds=(
                sum(value for value in durations if value is not None) or None
            ) if not has_repeat else None,
            estimated_distance_meters=(
                sum(value for value in distances if value is not None) or None
            ) if not has_repeat else None,
            description=_first(workout, "wkt_description", "description", "notes"),
            steps=steps,
            vendor_specific={},
        )


class CalendarExtractor:
    """Map Garmin's monthly calendar JSON to one Silver row per workout item."""

    @property
    def entity_type(self) -> EntityType:
        return EntityType.WORKOUT_CALENDAR

    def extract(self, entries: list[BronzeLedgerEntry]) -> list[SilverWorkoutCalendar]:
        _require_entries(entries, self.entity_type)
        result: list[SilverWorkoutCalendar] = []
        for entry in entries:
            with Path(entry.file_path).open(encoding="utf-8") as stream:
                payload = json.load(stream)
            items = payload.get("calendarItems", [])
            for item in items:
                if item.get("itemType") != "workout":
                    continue
                item_date = _calendar_date(item.get("date"))
                if item_date is None:
                    continue
                sport = _first(item, "sportTypeKey", "sportType")
                result.append(
                    SilverWorkoutCalendar(
                        user_id=entry.user_id,
                        updated_at=entry.fetch_timestamp,
                        calendar_date=item_date,
                        workout_id=str(item["workoutId"]) if item.get("workoutId") is not None else None,
                        workout_title=item.get("title"),
                        sport_type=_sport_type(sport) if sport is not None else None,
                        status=item.get("status"),
                        training_plan_id=(
                            str(item["atpPlanId"]) if item.get("atpPlanId") is not None else None
                        ),
                        vendor_specific={},
                    )
                )
        return result


def _record_number(records: Iterable[dict[str, Any]], *keys: str) -> float | None:
    for record in records:
        value = _number(_first(record, *keys))
        if value is not None:
            return value
    return None


def _record_int(records: Iterable[dict[str, Any]], *keys: str) -> int | None:
    value = _record_number(records, *keys)
    return int(value) if value is not None else None


def _extreme_int(records: Iterable[dict[str, Any]], key: str, reducer: Any) -> int | None:
    values = [value for record in records if (value := _int(record.get(key))) is not None]
    return reducer(values) if values else None


def _bounded_number(value: Any, minimum: float, maximum: float) -> float | None:
    number = _number(value)
    return number if number is not None and minimum <= number <= maximum else None


def _average(values: Iterable[Any]) -> float | None:
    numbers = [number for value in values if (number := _number(value)) is not None]
    return sum(numbers) / len(numbers) if numbers else None


def _unique_records(
    records: list[dict[str, Any]], timestamp_key: str
) -> list[dict[str, Any]]:
    unique: dict[Any, dict[str, Any]] = {}
    untimed: list[dict[str, Any]] = []
    for record in records:
        timestamp = record.get(timestamp_key)
        if timestamp is None:
            untimed.append(record)
        else:
            unique[timestamp] = record
    return [*unique.values(), *untimed]


def _summarize_sleep(records: list[dict[str, Any]]) -> dict[str, Any]:
    events = {
        record["timestamp"]: str(record["sleep_level"]).lower()
        for record in records
        if isinstance(record.get("timestamp"), datetime) and record.get("sleep_level") is not None
    }
    ordered = sorted(events.items())
    intervals: list[tuple[datetime, datetime, str]] = []
    for (start, stage), (end, _) in zip(ordered, ordered[1:]):
        seconds = (end - start).total_seconds()
        if 0 < seconds <= 300:
            intervals.append((start, end, stage))

    asleep = [item for item in intervals if item[2] in {"light", "deep", "rem"}]
    if not asleep:
        return {
            "start_time": None,
            "end_time": None,
            "duration_seconds": None,
            "deep_seconds": None,
            "light_seconds": None,
            "rem_seconds": None,
            "awake_seconds": None,
            "intervals": [],
            "stage_intervals": [],
        }

    sleep_start = asleep[0][0]
    sleep_end = asleep[-1][1]
    in_bed = [item for item in intervals if item[0] >= sleep_start and item[1] <= sleep_end]
    stage_seconds = {
        stage: sum((end - start).total_seconds() for start, end, value in in_bed if value == stage)
        for stage in ("deep", "light", "rem", "awake")
    }
    return {
        "start_time": sleep_start,
        "end_time": sleep_end,
        "duration_seconds": int(sum(stage_seconds.values())) or None,
        "deep_seconds": int(stage_seconds["deep"]) or None,
        "light_seconds": int(stage_seconds["light"]) or None,
        "rem_seconds": int(stage_seconds["rem"]) or None,
        "awake_seconds": int(stage_seconds["awake"]) or None,
        "intervals": asleep,
        "stage_intervals": in_bed,
    }


def _is_sleep_timestamp(
    timestamp: datetime, intervals: list[tuple[datetime, datetime, str]]
) -> bool:
    return any(start <= timestamp < end for start, end, _ in intervals)


def _mapped_enum(value: Any, enum_type: type[HrvStatus] | type[TrainingStatus]) -> Any:
    if value is None:
        return None
    label = _enum_value(value).replace(" ", "_").replace("-", "_")
    if label.isdigit():
        return None
    try:
        return enum_type(label)
    except ValueError:
        return enum_type.NO_STATUS


def _calendar_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _duration_type(value: Any) -> DurationType | None:
    label = _enum_value(value) if value is not None else ""
    mapping = {
        "time": DurationType.TIME,
        "0": DurationType.TIME,
        "time_only": DurationType.TIME,
        "31": DurationType.TIME,
        "distance": DurationType.DISTANCE,
        "1": DurationType.DISTANCE,
        "calories": DurationType.CALORIES,
        "4": DurationType.CALORIES,
        "open": DurationType.OPEN,
        "5": DurationType.OPEN,
    }
    if not label:
        return None
    return mapping.get(label, DurationType.OTHER)


def _target_type(value: Any) -> TargetType | None:
    label = _enum_value(value) if value is not None else ""
    mapping = {
        "heart_rate": TargetType.HEART_RATE,
        "1": TargetType.HEART_RATE,
        "speed": TargetType.SPEED,
        "0": TargetType.SPEED,
        "open": TargetType.OPEN,
        "2": TargetType.OPEN,
        "cadence": TargetType.CADENCE,
        "3": TargetType.CADENCE,
        "power": TargetType.POWER,
        "4": TargetType.POWER,
        "pace": TargetType.PACE,
    }
    if not label:
        return None
    return mapping.get(label, TargetType.OTHER)


def _step_type(record: dict[str, Any]) -> StepType:
    duration = _enum_value(record.get("duration_type"))
    intensity = _enum_value(record.get("intensity"))
    name = str(_first(record, "wkt_step_name", "step_name") or "").lower()
    if "repeat" in duration or duration in {str(value) for value in range(6, 14)}:
        return StepType.REPEAT
    if "warm" in intensity or "warm" in name or intensity == "2":
        return StepType.WARMUP
    if "cool" in intensity or "cool" in name or intensity == "3":
        return StepType.COOLDOWN
    if "rest" in intensity or intensity == "1":
        return StepType.REST
    if "recover" in intensity or intensity == "4":
        return StepType.RECOVERY
    if "interval" in intensity or intensity == "5":
        return StepType.INTERVAL
    return StepType.OTHER


def _workout_step(record: dict[str, Any], order: int) -> WorkoutStep:
    step_type = _step_type(record)
    duration_type = _duration_type(record.get("duration_type"))
    target_low = _number(_first(record, "custom_target_value_low", "target_value"))
    target_high = _number(_first(record, "custom_target_value_high", "target_value"))
    repeat_count = _int(_first(record, "repeat_steps", "repeat_count"))
    return WorkoutStep(
        step_order=order,
        step_type=step_type,
        duration_type=duration_type,
        duration_value=None if step_type is StepType.REPEAT else _number(record.get("duration_value")),
        target_type=_target_type(record.get("target_type")),
        target_low=target_low,
        target_high=target_high,
        repeat_count=repeat_count,
        children=[],
    )
