"""Map Garmin Health API summaries onto django-healthdatamodel records.

Mappers cover the beachhead summary types:

  dailies     → list[:class:`RecordInput`] (steps, active/BMR calories, distance,
                floors, resting HR, intensity minutes, per-sample heart rate)
  sleeps      → list[:class:`RecordInput`] (one per sleep-stage interval)
  bodyComps   → list[:class:`RecordInput`] (weight, body fat, BMI)
  pulseox     → list[:class:`RecordInput`] (one SpO2 sample per offset)
  activities  → :class:`WorkoutInput`

Plus ``epochs`` (15-minute activity granularity), which is intentionally NOT in
``DEFAULT_SUMMARY_TYPES``: epochs carry the same steps/calories/distance the
dailies aggregate, so ingesting both double-counts at query time. Pass
``summary_types=[SUMMARY_EPOCHS, ...]`` explicitly to trade daily aggregates
for fine granularity.

Unlike the Google Health integration there is no BMR estimation step here —
Garmin reports the day's BMR directly (``bmrKilocalories`` on dailies), so
``ActivityMetric.BASAL_CALORIES`` comes straight from the payload.

Timestamps: Garmin summaries carry ``startTimeInSeconds`` (unix UTC) plus a
``startTimeOffsetInSeconds`` local-zone offset. Records store absolute UTC
instants, so only the unix value is used.

The high-level orchestrator is :func:`sync_user`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from healthdatamodel.constants import DataSource
from healthdatamodel.ingest import ingest_records, ingest_workouts
from healthdatamodel.query import SLEEP_TYPE, ActivityMetric, SleepValue
from healthdatamodel.schemas import MetadataEntry, RecordInput, WorkoutInput

from .client import GarminClient
from .constants import (
    SOURCE_NAME,
    SUMMARY_ACTIVITIES,
    SUMMARY_BODY_COMPS,
    SUMMARY_DAILIES,
    SUMMARY_EPOCHS,
    SUMMARY_PULSE_OX,
    SUMMARY_SLEEPS,
)

if TYPE_CHECKING:
    from .models import GarminConnection

# Apple HealthKit identifiers not exported as enum members in healthdatamodel.
HK_HEART_RATE = "HKQuantityTypeIdentifierHeartRate"
HK_RESTING_HEART_RATE = "HKQuantityTypeIdentifierRestingHeartRate"
HK_BODY_MASS = "HKQuantityTypeIdentifierBodyMass"
HK_BODY_FAT_PERCENTAGE = "HKQuantityTypeIdentifierBodyFatPercentage"
HK_BODY_MASS_INDEX = "HKQuantityTypeIdentifierBodyMassIndex"
HK_DISTANCE_WALKING_RUNNING = "HKQuantityTypeIdentifierDistanceWalkingRunning"
HK_FLIGHTS_CLIMBED = "HKQuantityTypeIdentifierFlightsClimbed"
HK_EXERCISE_TIME = "HKQuantityTypeIdentifierAppleExerciseTime"
HK_OXYGEN_SATURATION = "HKQuantityTypeIdentifierOxygenSaturation"

# Garmin sleepLevelsMap keys → healthdatamodel SleepValue. Garmin only emits
# these four stage labels; sessions from devices without stage support come
# through with an empty/missing map and fall back to one ASLEEP_UNSPECIFIED
# record spanning the session.
_SLEEP_LEVEL_MAP: dict[str, str] = {
    "deep": SleepValue.ASLEEP_DEEP,
    "light": SleepValue.ASLEEP_CORE,
    "rem": SleepValue.ASLEEP_REM,
    "awake": SleepValue.AWAKE,
}


@dataclass
class SyncResult:
    """Per-type counts returned by :func:`sync_user`."""

    counts: dict[str, int] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _utc(seconds: Any) -> datetime:
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc)


def _summary_bounds(summary: dict[str, Any]) -> tuple[datetime, datetime]:
    start = _utc(summary["startTimeInSeconds"])
    return start, start + timedelta(seconds=int(summary.get("durationInSeconds") or 0))


def _record(
    summary: dict[str, Any],
    *,
    suffix: str,
    start: datetime,
    end: datetime,
    type: str,
    value: Any,
    unit: str | None,
) -> RecordInput:
    """Build a RecordInput keyed off the summary's ``summaryId``.

    One Garmin summary fans out into several Records (steps, calories, …), so
    the per-metric ``suffix`` keeps recordIds unique. Garmin re-sends a summary
    with the same summaryId when it's updated (e.g. more data synced for the
    same day), and the stable recordId lets the ingest layer upsert instead of
    duplicating.
    """
    summary_id = summary.get("summaryId")
    return RecordInput(
        recordId=f"{summary_id}-{suffix}" if summary_id else None,
        startDate=start,
        endDate=end,
        creationDate=start,
        sourceName=SOURCE_NAME,
        type=type,
        value=str(value),
        unit=unit,
    )


# Mappers ---------------------------------------------------------------------


def map_daily(summary: dict[str, Any]) -> list[RecordInput]:
    """Fan a daily summary out into one Record per available metric."""
    start, end = _summary_bounds(summary)
    records: list[RecordInput] = []

    def add(suffix: str, type: str, unit: str, value: Any) -> None:
        if value is None:
            return
        records.append(
            _record(
                summary,
                suffix=suffix,
                start=start,
                end=end,
                type=type,
                value=value,
                unit=unit,
            )
        )

    add("steps", str(ActivityMetric.STEPS), "count", summary.get("steps"))
    add(
        "active-kcal",
        str(ActivityMetric.ACTIVE_CALORIES),
        "kcal",
        summary.get("activeKilocalories"),
    )
    add(
        "bmr-kcal",
        str(ActivityMetric.BASAL_CALORIES),
        "kcal",
        summary.get("bmrKilocalories"),
    )
    add(
        "distance",
        HK_DISTANCE_WALKING_RUNNING,
        "m",
        summary.get("distanceInMeters"),
    )
    add(
        "floors",
        HK_FLIGHTS_CLIMBED,
        "count",
        summary.get("floorsClimbed"),
    )
    add(
        "resting-hr",
        HK_RESTING_HEART_RATE,
        "count/min",
        summary.get("restingHeartRateInBeatsPerMinute"),
    )

    # Garmin "intensity minutes" arrive as separate moderate/vigorous second
    # counts. Store their plain sum as exercise time; Garmin's own doubled
    # weighting for vigorous minutes is a display convention, not a duration.
    moderate = summary.get("moderateIntensityDurationInSeconds")
    vigorous = summary.get("vigorousIntensityDurationInSeconds")
    if moderate is not None or vigorous is not None:
        total_minutes = (int(moderate or 0) + int(vigorous or 0)) / 60.0
        records.append(
            _record(
                summary,
                suffix="intensity-min",
                start=start,
                end=end,
                type=HK_EXERCISE_TIME,
                value=total_minutes,
                unit="min",
            )
        )

    # Intraday heart rate: offsets (seconds from summary start) → bpm.
    for offset, bpm in (summary.get("timeOffsetHeartRateSamples") or {}).items():
        instant = start + timedelta(seconds=int(offset))
        records.append(
            _record(
                summary,
                suffix=f"hr-{offset}",
                start=instant,
                end=instant,
                type=HK_HEART_RATE,
                value=bpm,
                unit="count/min",
            )
        )

    return records


def map_epoch(summary: dict[str, Any]) -> list[RecordInput]:
    """Map one 15-minute epoch. NOT ingested by default — see module docstring."""
    start, end = _summary_bounds(summary)
    records: list[RecordInput] = []
    if summary.get("steps") is not None:
        records.append(
            _record(
                summary,
                suffix="steps",
                start=start,
                end=end,
                type=str(ActivityMetric.STEPS),
                value=summary["steps"],
                unit="count",
            )
        )
    if summary.get("activeKilocalories") is not None:
        records.append(
            _record(
                summary,
                suffix="active-kcal",
                start=start,
                end=end,
                type=str(ActivityMetric.ACTIVE_CALORIES),
                value=summary["activeKilocalories"],
                unit="kcal",
            )
        )
    if summary.get("distanceInMeters") is not None:
        records.append(
            _record(
                summary,
                suffix="distance",
                start=start,
                end=end,
                type=HK_DISTANCE_WALKING_RUNNING,
                value=summary["distanceInMeters"],
                unit="m",
            )
        )
    return records


def map_sleep(summary: dict[str, Any]) -> list[RecordInput]:
    """Decompose a sleep summary into one Record per stage interval.

    ``sleepLevelsMap`` holds ``{level: [{startTimeInSeconds, endTimeInSeconds}]}``.
    Sessions without stage data fall back to a single ASLEEP_UNSPECIFIED record
    over the whole session.
    """
    start, end = _summary_bounds(summary)
    records: list[RecordInput] = []
    levels = summary.get("sleepLevelsMap") or {}
    for level, intervals in levels.items():
        mapped = _SLEEP_LEVEL_MAP.get(str(level).lower())
        if mapped is None:
            continue
        for i, interval in enumerate(intervals or []):
            records.append(
                _record(
                    summary,
                    suffix=f"{level}-{i}",
                    start=_utc(interval["startTimeInSeconds"]),
                    end=_utc(interval["endTimeInSeconds"]),
                    type=SLEEP_TYPE,
                    value=mapped,
                    unit=None,
                )
            )
    if not records:
        records.append(
            _record(
                summary,
                suffix="session",
                start=start,
                end=end,
                type=SLEEP_TYPE,
                value=SleepValue.ASLEEP_UNSPECIFIED,
                unit=None,
            )
        )
    return records


def map_body_comp(summary: dict[str, Any]) -> list[RecordInput]:
    """Point-in-time body composition: weight (grams → kg), body fat %, BMI."""
    instant = _utc(summary["measurementTimeInSeconds"])
    records: list[RecordInput] = []
    if summary.get("weightInGrams") is not None:
        records.append(
            _record(
                summary,
                suffix="weight",
                start=instant,
                end=instant,
                type=HK_BODY_MASS,
                value=float(summary["weightInGrams"]) / 1000.0,
                unit="kg",
            )
        )
    if summary.get("bodyFatInPercent") is not None:
        records.append(
            _record(
                summary,
                suffix="body-fat",
                start=instant,
                end=instant,
                type=HK_BODY_FAT_PERCENTAGE,
                value=summary["bodyFatInPercent"],
                unit="%",
            )
        )
    if summary.get("bodyMassIndex") is not None:
        records.append(
            _record(
                summary,
                suffix="bmi",
                start=instant,
                end=instant,
                type=HK_BODY_MASS_INDEX,
                value=summary["bodyMassIndex"],
                unit="count",
            )
        )
    return records


def map_pulse_ox(summary: dict[str, Any]) -> list[RecordInput]:
    """SpO2 samples: offsets (seconds from summary start) → percentage."""
    start = _utc(summary["startTimeInSeconds"])
    records: list[RecordInput] = []
    for offset, percentage in (summary.get("timeOffsetSpo2Values") or {}).items():
        instant = start + timedelta(seconds=int(offset))
        records.append(
            _record(
                summary,
                suffix=f"spo2-{offset}",
                start=instant,
                end=instant,
                type=HK_OXYGEN_SATURATION,
                value=percentage,
                unit="%",
            )
        )
    return records


def map_activity(summary: dict[str, Any]) -> WorkoutInput:
    start, end = _summary_bounds(summary)
    duration_seconds = float(
        summary.get("durationInSeconds") or (end - start).total_seconds()
    )
    distance_m = summary.get("distanceInMeters")

    extra_metadata: list[MetadataEntry] = []
    if summary.get("steps") is not None:
        extra_metadata.append(MetadataEntry(key="steps", value=str(summary["steps"])))
    if summary.get("averageHeartRateInBeatsPerMinute") is not None:
        extra_metadata.append(
            MetadataEntry(
                key="average_heart_rate_bpm",
                value=str(summary["averageHeartRateInBeatsPerMinute"]),
            )
        )
    if summary.get("activityName"):
        extra_metadata.append(
            MetadataEntry(key="activity_name", value=str(summary["activityName"]))
        )

    return WorkoutInput(
        recordId=str(summary.get("summaryId")) if summary.get("summaryId") else None,
        startDate=start,
        endDate=end,
        creationDate=start,
        sourceName=SOURCE_NAME,
        device=summary.get("deviceName"),
        durationUnit="s",
        duration=duration_seconds,
        workoutActivityType=str(summary.get("activityType", "UNKNOWN")),
        caloriesBurned=float(summary["activeKilocalories"])
        if summary.get("activeKilocalories") is not None
        else None,
        caloriesUnit="kcal" if summary.get("activeKilocalories") is not None else None,
        distance=float(distance_m) / 1000.0 if distance_m is not None else None,
        distanceUnit="km" if distance_m is not None else None,
        metadataEntry=extra_metadata or None,
    )


# Orchestrator ----------------------------------------------------------------


RECORD_MAPPERS: dict[str, Callable[[dict[str, Any]], list[RecordInput]]] = {
    SUMMARY_DAILIES: map_daily,
    SUMMARY_EPOCHS: map_epoch,
    SUMMARY_SLEEPS: map_sleep,
    SUMMARY_BODY_COMPS: map_body_comp,
    SUMMARY_PULSE_OX: map_pulse_ox,
}

DEFAULT_SUMMARY_TYPES: tuple[str, ...] = (
    SUMMARY_DAILIES,
    SUMMARY_SLEEPS,
    SUMMARY_BODY_COMPS,
    SUMMARY_PULSE_OX,
    SUMMARY_ACTIVITIES,
)


def ingest_summaries(
    connection: GarminConnection,
    summary_type: str,
    summaries: list[dict[str, Any]],
) -> int:
    """Persist already-fetched summaries (e.g. from a push notification).

    Returns the number of records/workouts written. Unknown summary types are
    skipped with a zero count so webhook processing stays forward-compatible
    with summary types this version doesn't map yet.
    """
    if summary_type == SUMMARY_ACTIVITIES:
        workouts = [map_activity(s) for s in summaries]
        ingest_workouts(connection.customer, workouts, source=DataSource.GARMIN)
        return len(workouts)

    mapper = RECORD_MAPPERS.get(summary_type)
    if mapper is None:
        return 0
    records: list[RecordInput] = []
    for summary in summaries:
        records.extend(mapper(summary))
    ingest_records(connection.customer, records, source=DataSource.GARMIN)
    return len(records)


def sync_user(
    connection: GarminConnection,
    *,
    start: datetime,
    end: datetime,
    summary_types: list[str] | None = None,
    client: GarminClient | None = None,
) -> SyncResult:
    """Fetch + ingest all configured summary types for ``connection`` over [start, end].

    The window filters by **upload** time (Garmin's pull-endpoint semantics),
    chunked into the 24-hour windows the API requires. For history older than
    what the user's devices uploaded recently, use
    :meth:`GarminClient.request_backfill` — its results arrive via webhooks,
    not this call.

    Pass a pre-built ``client`` to override the default (useful in tests).
    """
    result = SyncResult()
    owns_client = client is None
    if client is None:
        client = GarminClient(connection)

    try:
        for summary_type in summary_types or DEFAULT_SUMMARY_TYPES:
            summaries = list(client.iter_summaries(summary_type, start=start, end=end))
            result.counts[summary_type] = ingest_summaries(
                connection, summary_type, summaries
            )
    finally:
        if owns_client:
            client.close()

    connection.last_sync_at = datetime.now(timezone.utc)
    connection.save(update_fields=["last_sync_at"])
    result.finished_at = datetime.now(timezone.utc)
    return result
