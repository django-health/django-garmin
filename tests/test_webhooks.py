from datetime import datetime, timezone

import pytest
import respx
from healthdatamodel.models import Record, Workout
from httpx import Response

from garmin.constants import API_BASE_URL
from garmin.webhooks import is_ping_entry, process_notification

WELLNESS = f"{API_BASE_URL}/wellness-api/rest"

DAY_START = datetime(2026, 7, 5, 5, 0, tzinfo=timezone.utc)


def _daily(user_id: str) -> dict:
    return {
        "userId": user_id,
        "summaryId": "x-daily-1",
        "startTimeInSeconds": int(DAY_START.timestamp()),
        "durationInSeconds": 86400,
        "steps": 5000,
    }


class TestIsPingEntry:
    def test_ping(self):
        assert is_ping_entry({"userId": "u", "callbackURL": "https://x"}) is True

    def test_push(self):
        assert is_ping_entry(_daily("u")) is False


@pytest.mark.django_db
class TestProcessNotification:
    def test_push_payload_ingests_inline_summaries(self, connection):
        counts = process_notification({"dailies": [_daily(connection.garmin_user_id)]})
        assert counts == {"dailies": 1}
        record = Record.objects.get(customer=connection.customer)
        assert record.value == "5000"

        connection.refresh_from_db()
        assert connection.last_sync_at is not None

    @respx.mock
    def test_ping_payload_fetches_callback_url(self, connection):
        callback = (
            f"{WELLNESS}/activities?uploadStartTimeInSeconds=1&uploadEndTimeInSeconds=2"
        )
        respx.get(f"{WELLNESS}/activities").mock(
            return_value=Response(
                200,
                json=[
                    {
                        "summaryId": "x-act-9",
                        "activityType": "RUNNING",
                        "startTimeInSeconds": int(DAY_START.timestamp()),
                        "durationInSeconds": 600,
                    }
                ],
            )
        )
        counts = process_notification(
            {
                "activities": [
                    {"userId": connection.garmin_user_id, "callbackURL": callback}
                ]
            }
        )
        assert counts == {"activities": 1}
        assert Workout.objects.filter(customer=connection.customer).count() == 1

    def test_unknown_user_skipped(self, connection):
        counts = process_notification({"dailies": [_daily("not-a-known-user")]})
        assert counts == {"dailies": 0}
        assert not Record.objects.exists()

    def test_entry_without_user_id_skipped(self, connection, db):
        entry = _daily(connection.garmin_user_id)
        del entry["userId"]
        counts = process_notification({"dailies": [entry]})
        assert counts == {"dailies": 0}

    def test_unknown_summary_keys_ignored(self, db):
        counts = process_notification(
            {"stressDetails": [{"userId": "u"}], "deregistrations": []}
        )
        assert counts == {}

    def test_non_list_values_ignored(self, db):
        assert process_notification({"dailies": "nope"}) == {}
