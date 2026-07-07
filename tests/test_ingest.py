from datetime import datetime, timedelta, timezone

import pytest
from healthdatamodel.models import Record, Workout
from healthdatamodel.query import SLEEP_TYPE, ActivityMetric, SleepValue

from garmin import ingest
from garmin.ingest import (
    map_activity,
    map_body_comp,
    map_daily,
    map_epoch,
    map_pulse_ox,
    map_sleep,
    sync_user,
)

DAY_START = datetime(2026, 7, 5, 5, 0, tzinfo=timezone.utc)

DAILY = {
    "summaryId": "x-daily-1",
    "calendarDate": "2026-07-05",
    "startTimeInSeconds": int(DAY_START.timestamp()),
    "startTimeOffsetInSeconds": -18000,
    "activityType": "WALKING",
    "durationInSeconds": 86400,
    "steps": 7823,
    "distanceInMeters": 5163.5,
    "activeKilocalories": 447,
    "bmrKilocalories": 1731,
    "floorsClimbed": 8,
    "restingHeartRateInBeatsPerMinute": 64,
    "moderateIntensityDurationInSeconds": 600,
    "vigorousIntensityDurationInSeconds": 300,
    "timeOffsetHeartRateSamples": {"15": 75, "30": 79},
}

SLEEP = {
    "summaryId": "x-sleep-1",
    "calendarDate": "2026-07-05",
    "startTimeInSeconds": int(DAY_START.timestamp()),
    "durationInSeconds": 26580,
    "sleepLevelsMap": {
        "deep": [
            {
                "startTimeInSeconds": int(DAY_START.timestamp()),
                "endTimeInSeconds": int(DAY_START.timestamp()) + 7860,
            }
        ],
        "light": [
            {
                "startTimeInSeconds": int(DAY_START.timestamp()) + 7860,
                "endTimeInSeconds": int(DAY_START.timestamp()) + 20160,
            }
        ],
        "rem": [
            {
                "startTimeInSeconds": int(DAY_START.timestamp()) + 20160,
                "endTimeInSeconds": int(DAY_START.timestamp()) + 25380,
            }
        ],
        "awake": [
            {
                "startTimeInSeconds": int(DAY_START.timestamp()) + 25380,
                "endTimeInSeconds": int(DAY_START.timestamp()) + 26580,
            }
        ],
    },
    "validation": "ENHANCED_FINAL",
}

BODY_COMP = {
    "summaryId": "x-body-1",
    "measurementTimeInSeconds": int(DAY_START.timestamp()),
    "weightInGrams": 75450,
    "bodyFatInPercent": 17.1,
    "bodyMassIndex": 23.2,
}

PULSE_OX = {
    "summaryId": "x-spo2-1",
    "calendarDate": "2026-07-05",
    "startTimeInSeconds": int(DAY_START.timestamp()),
    "durationInSeconds": 86400,
    "timeOffsetSpo2Values": {"7140": 96, "10740": 98},
}

ACTIVITY = {
    "summaryId": "x-act-1",
    "activityId": 123456,
    "activityName": "Morning Run",
    "activityType": "RUNNING",
    "startTimeInSeconds": int(DAY_START.timestamp()),
    "startTimeOffsetInSeconds": -18000,
    "durationInSeconds": 1800,
    "averageHeartRateInBeatsPerMinute": 140,
    "distanceInMeters": 5000.0,
    "activeKilocalories": 350,
    "steps": 5200,
    "deviceName": "forerunner965",
}

EPOCH = {
    "summaryId": "x-epoch-1",
    "activityType": "WALKING",
    "startTimeInSeconds": int(DAY_START.timestamp()),
    "durationInSeconds": 900,
    "steps": 93,
    "distanceInMeters": 49.0,
    "activeKilocalories": 3,
}


class TestMapDaily:
    def test_fans_out_per_metric(self):
        records = map_daily(DAILY)
        by_type = {r.type: r for r in records if not r.type.endswith("HeartRate")}

        assert by_type[str(ActivityMetric.STEPS)].value == "7823"
        assert by_type[str(ActivityMetric.ACTIVE_CALORIES)].value == "447"
        assert by_type[str(ActivityMetric.BASAL_CALORIES)].value == "1731"
        assert by_type[ingest.HK_DISTANCE_WALKING_RUNNING].value == "5163.5"
        assert by_type[ingest.HK_FLIGHTS_CLIMBED].value == "8"
        assert by_type[ingest.HK_EXERCISE_TIME].value == "15.0"  # (600+300)/60

    def test_interval_bounds_span_duration(self):
        steps = next(r for r in map_daily(DAILY) if r.type == str(ActivityMetric.STEPS))
        assert steps.startDate == DAY_START
        assert steps.endDate == DAY_START + timedelta(days=1)

    def test_heart_rate_samples_offset_from_start(self):
        hr = [r for r in map_daily(DAILY) if r.type == ingest.HK_HEART_RATE]
        assert len(hr) == 2
        first = min(hr, key=lambda r: r.startDate)
        assert first.startDate == DAY_START + timedelta(seconds=15)
        assert first.value == "75"
        assert first.startDate == first.endDate

    def test_resting_heart_rate(self):
        rhr = next(
            r for r in map_daily(DAILY) if r.type == ingest.HK_RESTING_HEART_RATE
        )
        assert rhr.value == "64"

    def test_record_ids_stable_and_unique(self):
        ids = [r.recordId for r in map_daily(DAILY)]
        assert len(ids) == len(set(ids))
        assert all(i.startswith("x-daily-1-") for i in ids)

    def test_missing_metrics_skipped(self):
        records = map_daily(
            {
                "summaryId": "x",
                "startTimeInSeconds": int(DAY_START.timestamp()),
                "durationInSeconds": 86400,
                "steps": 100,
            }
        )
        assert len(records) == 1
        assert records[0].type == str(ActivityMetric.STEPS)


class TestMapSleep:
    def test_one_record_per_stage_interval(self):
        records = map_sleep(SLEEP)
        assert len(records) == 4
        values = {r.value for r in records}
        assert values == {
            SleepValue.ASLEEP_DEEP,
            SleepValue.ASLEEP_CORE,
            SleepValue.ASLEEP_REM,
            SleepValue.AWAKE,
        }
        assert all(r.type == SLEEP_TYPE for r in records)

    def test_stage_bounds(self):
        deep = next(r for r in map_sleep(SLEEP) if r.value == SleepValue.ASLEEP_DEEP)
        assert deep.startDate == DAY_START
        assert deep.endDate == DAY_START + timedelta(seconds=7860)

    def test_no_levels_falls_back_to_unspecified_session(self):
        records = map_sleep(
            {
                "summaryId": "x",
                "startTimeInSeconds": int(DAY_START.timestamp()),
                "durationInSeconds": 3600,
            }
        )
        assert len(records) == 1
        assert records[0].value == SleepValue.ASLEEP_UNSPECIFIED
        assert records[0].endDate - records[0].startDate == timedelta(hours=1)

    def test_unknown_level_skipped(self):
        records = map_sleep(
            {
                "summaryId": "x",
                "startTimeInSeconds": int(DAY_START.timestamp()),
                "durationInSeconds": 3600,
                "sleepLevelsMap": {
                    "mystery": [
                        {
                            "startTimeInSeconds": int(DAY_START.timestamp()),
                            "endTimeInSeconds": int(DAY_START.timestamp()) + 60,
                        }
                    ]
                },
            }
        )
        # Unknown stage contributes nothing → session fallback kicks in.
        assert len(records) == 1
        assert records[0].value == SleepValue.ASLEEP_UNSPECIFIED


class TestMapBodyComp:
    def test_weight_grams_to_kg(self):
        weight = next(
            r for r in map_body_comp(BODY_COMP) if r.type == ingest.HK_BODY_MASS
        )
        assert weight.value == "75.45"
        assert weight.unit == "kg"
        assert weight.startDate == weight.endDate == DAY_START

    def test_body_fat_and_bmi(self):
        by_type = {r.type: r for r in map_body_comp(BODY_COMP)}
        assert by_type[ingest.HK_BODY_FAT_PERCENTAGE].value == "17.1"
        assert by_type[ingest.HK_BODY_MASS_INDEX].value == "23.2"


class TestMapPulseOx:
    def test_samples_offset_from_start(self):
        records = map_pulse_ox(PULSE_OX)
        assert len(records) == 2
        first = min(records, key=lambda r: r.startDate)
        assert first.startDate == DAY_START + timedelta(seconds=7140)
        assert first.value == "96"
        assert first.unit == "%"


class TestMapEpoch:
    def test_epoch_metrics(self):
        records = map_epoch(EPOCH)
        by_type = {r.type: r for r in records}
        assert by_type[str(ActivityMetric.STEPS)].value == "93"
        assert by_type[str(ActivityMetric.ACTIVE_CALORIES)].value == "3"
        steps = by_type[str(ActivityMetric.STEPS)]
        assert steps.endDate - steps.startDate == timedelta(minutes=15)


class TestMapActivity:
    def test_workout_fields(self):
        workout = map_activity(ACTIVITY)
        assert workout.recordId == "x-act-1"
        assert workout.workoutActivityType == "RUNNING"
        assert workout.duration == 1800.0
        assert workout.durationUnit == "s"
        assert workout.caloriesBurned == 350.0
        assert workout.distance == 5.0  # meters → km
        assert workout.device == "forerunner965"
        metadata = {m.key: m.value for m in workout.metadataEntry}
        assert metadata["steps"] == "5200"
        assert metadata["average_heart_rate_bpm"] == "140"
        assert metadata["activity_name"] == "Morning Run"


class FakeClient:
    """Stands in for GarminClient in sync_user tests."""

    def __init__(self, pages: dict[str, list[dict]]):
        self.pages = pages
        self.calls: list[str] = []

    def iter_summaries(self, summary_type, *, start, end):
        self.calls.append(summary_type)
        yield from self.pages.get(summary_type, [])


@pytest.mark.django_db
class TestSyncUser:
    def test_ingests_defaults_and_counts(self, connection):
        client = FakeClient(
            {
                "dailies": [DAILY],
                "sleeps": [SLEEP],
                "bodyComps": [BODY_COMP],
                "pulseox": [PULSE_OX],
                "activities": [ACTIVITY],
            }
        )
        result = sync_user(
            connection,
            start=DAY_START,
            end=DAY_START + timedelta(days=1),
            client=client,
        )

        assert client.calls == [
            "dailies",
            "sleeps",
            "bodyComps",
            "pulseox",
            "activities",
        ]
        assert result.counts["dailies"] == 9  # 6 metrics + intensity + 2 HR samples
        assert result.counts["sleeps"] == 4
        assert result.counts["bodyComps"] == 3
        assert result.counts["pulseox"] == 2
        assert result.counts["activities"] == 1
        assert result.total == 19

        assert Record.objects.filter(customer=connection.customer).count() == 18
        assert Workout.objects.filter(customer=connection.customer).count() == 1
        record = Record.objects.filter(type=str(ActivityMetric.STEPS)).get()
        assert record.source == "garmin"
        assert record.sourceName == "Garmin"

        connection.refresh_from_db()
        assert connection.last_sync_at is not None

    def test_explicit_summary_types(self, connection):
        client = FakeClient({"sleeps": [SLEEP]})
        result = sync_user(
            connection,
            start=DAY_START,
            end=DAY_START + timedelta(days=1),
            summary_types=["sleeps"],
            client=client,
        )
        assert client.calls == ["sleeps"]
        assert result.counts == {"sleeps": 4}

    def test_unknown_summary_type_counts_zero(self, connection):
        client = FakeClient({"stressDetails": [{"summaryId": "x"}]})
        result = sync_user(
            connection,
            start=DAY_START,
            end=DAY_START + timedelta(days=1),
            summary_types=["stressDetails"],
            client=client,
        )
        assert result.counts == {"stressDetails": 0}
