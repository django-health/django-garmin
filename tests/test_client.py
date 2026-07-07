from datetime import datetime, timedelta, timezone

import pytest
import respx
from httpx import Response

from garmin.client import GarminAPIError, GarminClient
from garmin.constants import API_BASE_URL, OAUTH_TOKEN_URL

WELLNESS = f"{API_BASE_URL}/wellness-api/rest"

START = datetime(2026, 7, 5, 0, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=12)


@pytest.mark.django_db
class TestRequestLoop:
    @respx.mock
    def test_sends_bearer_header(self, connection):
        route = respx.get(f"{WELLNESS}/user/id").mock(
            return_value=Response(200, json={"userId": "u1"})
        )
        with GarminClient(connection) as client:
            assert client.get_user_id() == "u1"
        auth = route.calls.last.request.headers["Authorization"]
        assert auth == "Bearer garmin-initial-access"

    @respx.mock
    def test_proactive_refresh_when_expired(self, connection):
        connection.token_expires_at = datetime.now(timezone.utc)
        connection.save(update_fields=["token_expires_at"])
        respx.post(OAUTH_TOKEN_URL).mock(
            return_value=Response(
                200,
                json={
                    "access_token": "refreshed-access",
                    "expires_in": 86400,
                    "refresh_token": "refreshed-refresh",
                },
            )
        )
        route = respx.get(f"{WELLNESS}/user/id").mock(
            return_value=Response(200, json={"userId": "u1"})
        )
        with GarminClient(connection) as client:
            client.get_user_id()
        auth = route.calls.last.request.headers["Authorization"]
        assert auth == "Bearer refreshed-access"

    @respx.mock
    def test_retries_once_after_401(self, connection):
        respx.post(OAUTH_TOKEN_URL).mock(
            return_value=Response(
                200,
                json={
                    "access_token": "refreshed-access",
                    "expires_in": 86400,
                    "refresh_token": "refreshed-refresh",
                },
            )
        )
        route = respx.get(f"{WELLNESS}/user/id").mock(
            side_effect=[
                Response(401),
                Response(200, json={"userId": "u1"}),
            ]
        )
        with GarminClient(connection) as client:
            assert client.get_user_id() == "u1"
        assert route.call_count == 2

    @respx.mock
    def test_retries_on_429_honoring_retry_after(self, connection):
        sleeps: list[float] = []
        route = respx.get(f"{WELLNESS}/user/id").mock(
            side_effect=[
                Response(429, headers={"Retry-After": "7"}),
                Response(200, json={"userId": "u1"}),
            ]
        )
        with GarminClient(connection, sleep=sleeps.append) as client:
            assert client.get_user_id() == "u1"
        assert route.call_count == 2
        assert sleeps == [7.0]

    @respx.mock
    def test_gives_up_after_max_retries(self, connection):
        respx.get(f"{WELLNESS}/user/id").mock(return_value=Response(503))
        with GarminClient(connection, max_retries=2, sleep=lambda _s: None) as client:
            with pytest.raises(GarminAPIError) as excinfo:
                client.get_user_id()
        assert excinfo.value.status_code == 503

    @respx.mock
    def test_error_payload_message_surfaced(self, connection):
        respx.get(f"{WELLNESS}/user/id").mock(
            return_value=Response(400, json={"errorMessage": "bad window"})
        )
        with GarminClient(connection) as client:
            with pytest.raises(GarminAPIError, match="bad window"):
                client.get_user_id()


@pytest.mark.django_db
class TestSummaries:
    @respx.mock
    def test_list_summaries_sends_upload_window(self, connection):
        route = respx.get(f"{WELLNESS}/dailies").mock(
            return_value=Response(200, json=[{"summaryId": "s1"}])
        )
        with GarminClient(connection) as client:
            summaries = client.list_summaries("dailies", start=START, end=END)
        assert summaries == [{"summaryId": "s1"}]
        params = route.calls.last.request.url.params
        assert params["uploadStartTimeInSeconds"] == str(int(START.timestamp()))
        assert params["uploadEndTimeInSeconds"] == str(int(END.timestamp()))

    def test_list_summaries_rejects_window_over_24h(self, connection):
        with GarminClient(connection) as client:
            with pytest.raises(ValueError, match="24 hours"):
                client.list_summaries(
                    "dailies", start=START, end=START + timedelta(hours=25)
                )

    @respx.mock
    def test_iter_summaries_chunks_into_24h_windows(self, connection):
        route = respx.get(f"{WELLNESS}/dailies").mock(
            return_value=Response(200, json=[{"summaryId": "s"}])
        )
        with GarminClient(connection) as client:
            summaries = list(
                client.iter_summaries(
                    "dailies", start=START, end=START + timedelta(days=2, hours=12)
                )
            )
        # 60 hours → 24 + 24 + 12.
        assert route.call_count == 3
        assert len(summaries) == 3
        last_params = route.calls.last.request.url.params
        assert last_params["uploadEndTimeInSeconds"] == str(
            int((START + timedelta(days=2, hours=12)).timestamp())
        )

    @respx.mock
    def test_fetch_url_allows_wellness_api_urls(self, connection):
        respx.get(f"{WELLNESS}/dailies").mock(
            return_value=Response(200, json=[{"summaryId": "s1"}])
        )
        with GarminClient(connection) as client:
            summaries = client.fetch_url(
                f"{WELLNESS}/dailies?uploadStartTimeInSeconds=1&uploadEndTimeInSeconds=2"
            )
        assert summaries == [{"summaryId": "s1"}]

    def test_fetch_url_refuses_foreign_hosts(self, connection):
        with GarminClient(connection) as client:
            with pytest.raises(ValueError, match="refusing"):
                client.fetch_url("https://evil.example.com/wellness-api/rest/dailies")

    @respx.mock
    def test_request_backfill(self, connection):
        route = respx.get(f"{WELLNESS}/backfill/sleeps").mock(
            return_value=Response(202)
        )
        with GarminClient(connection) as client:
            client.request_backfill("sleeps", start=START, end=END)
        params = route.calls.last.request.url.params
        assert params["summaryStartTimeInSeconds"] == str(int(START.timestamp()))
        assert params["summaryEndTimeInSeconds"] == str(int(END.timestamp()))
