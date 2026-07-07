from datetime import datetime, timezone
from io import StringIO

import pytest
import respx
from django.core.management import CommandError, call_command
from httpx import Response

from garmin.constants import API_BASE_URL
from garmin.models import ConnectionStatus, GarminConnection

WELLNESS = f"{API_BASE_URL}/wellness-api/rest"

DAY_START = datetime(2026, 7, 5, 5, 0, tzinfo=timezone.utc)

SUMMARY_ROUTES = ("dailies", "sleeps", "bodyComps", "pulseox", "activities")


def _mock_empty_pull():
    for summary_type in SUMMARY_ROUTES:
        respx.get(f"{WELLNESS}/{summary_type}").mock(
            return_value=Response(200, json=[])
        )


@pytest.mark.django_db
class TestSyncGarminCommand:
    @respx.mock
    def test_syncs_all_active_connections_by_default(self, connection):
        _mock_empty_pull()
        out = StringIO()
        call_command("sync_garmin", stdout=out)
        assert "Syncing 1 connection(s)" in out.getvalue()
        assert "✓" in out.getvalue()
        connection.refresh_from_db()
        assert connection.last_sync_at is not None

    @respx.mock
    def test_ingests_fetched_summaries(self, connection):
        _mock_empty_pull()
        respx.get(f"{WELLNESS}/dailies").mock(
            return_value=Response(
                200,
                json=[
                    {
                        "summaryId": "x-daily-1",
                        "startTimeInSeconds": int(DAY_START.timestamp()),
                        "durationInSeconds": 86400,
                        "steps": 1234,
                    }
                ],
            )
        )
        out = StringIO()
        call_command("sync_garmin", "--summary-type", "dailies", stdout=out)
        assert "dailies=1" in out.getvalue()

    def test_skips_inactive_connections(self, connection):
        connection.status = ConnectionStatus.REVOKED
        connection.save(update_fields=["status"])
        out = StringIO()
        call_command("sync_garmin", stdout=out)
        assert "No matching active connections" in out.getvalue()

    def test_unknown_username_errors(self, db):
        with pytest.raises(CommandError, match="No user with"):
            call_command("sync_garmin", "--user", "nobody")

    def test_user_and_user_id_mutually_exclusive(self, connection):
        with pytest.raises(CommandError, match="not both"):
            call_command(
                "sync_garmin",
                "--user",
                connection.customer.username,
                "--user-id",
                str(connection.customer_id),
            )

    def test_start_requires_end(self, db):
        with pytest.raises(CommandError, match="together"):
            call_command("sync_garmin", "--start", "2026-07-01T00:00:00")

    def test_bad_iso_dates_error(self, db):
        with pytest.raises(CommandError, match="ISO-8601"):
            call_command("sync_garmin", "--start", "not-a-date", "--end", "also-not")

    @respx.mock
    def test_filters_by_username(self, connection, django_user_model):
        other = django_user_model.objects.create_user(username="other")
        GarminConnection.objects.create(
            customer=other,
            garmin_user_id="other-user",
            access_token="a",
            refresh_token="r",
            token_expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
        )
        _mock_empty_pull()
        out = StringIO()
        call_command("sync_garmin", "--user", connection.customer.username, stdout=out)
        assert "Syncing 1 connection(s)" in out.getvalue()

    @respx.mock
    def test_failure_is_reported_and_exits_nonzero(self, connection):
        for summary_type in SUMMARY_ROUTES:
            respx.get(f"{WELLNESS}/{summary_type}").mock(
                return_value=Response(400, json={"errorMessage": "nope"})
            )
        with pytest.raises(CommandError, match="1 connection"):
            call_command("sync_garmin", stderr=StringIO())

    @respx.mock
    def test_backfill_requests_async_resend(self, connection):
        routes = {
            summary_type: respx.get(f"{WELLNESS}/backfill/{summary_type}").mock(
                return_value=Response(202)
            )
            for summary_type in SUMMARY_ROUTES
        }
        out = StringIO()
        call_command("sync_garmin", "--backfill", "--days", "90", stdout=out)
        assert "backfill requested" in out.getvalue()
        assert all(route.called for route in routes.values())
