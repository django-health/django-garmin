import base64
import hashlib
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from httpx import Response

from garmin import oauth
from garmin.constants import API_BASE_URL, OAUTH_AUTHORIZATION_URL, OAUTH_TOKEN_URL
from garmin.models import ConnectionStatus, GarminConnection

TOKEN_RESPONSE = {
    "access_token": "new-access-token",
    "expires_in": 86400,
    "token_type": "bearer",
    "refresh_token": "new-refresh-token",
    "scope": "CONNECT_READ CONNECT_WRITE",
    "jti": "f9eb2316-9b9d-495a-8732-e16c4b5bcafd",
    "refresh_token_expires_in": 7775998,
}


class TestBuildAuthorizationUrl:
    def test_url_and_params(self):
        url, flow_state = oauth.build_authorization_url()
        parsed = urlparse(url)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        assert url.startswith(OAUTH_AUTHORIZATION_URL)
        assert params["response_type"] == "code"
        assert params["client_id"] == "test-client-id"
        assert params["redirect_uri"] == "http://testserver/garmin/callback/"
        assert params["code_challenge_method"] == "S256"
        assert params["state"] == flow_state.state

    def test_code_challenge_is_s256_of_verifier(self):
        url, flow_state = oauth.build_authorization_url()
        params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        expected = (
            base64.urlsafe_b64encode(
                hashlib.sha256(flow_state.code_verifier.encode()).digest()
            )
            .rstrip(b"=")
            .decode()
        )
        assert params["code_challenge"] == expected

    def test_explicit_state_is_used(self):
        url, flow_state = oauth.build_authorization_url(state="fixed-state")
        assert flow_state.state == "fixed-state"
        assert "state=fixed-state" in url

    def test_verifier_is_unique_per_call(self):
        _, first = oauth.build_authorization_url()
        _, second = oauth.build_authorization_url()
        assert first.code_verifier != second.code_verifier


class TestExchangeCode:
    @respx.mock
    def test_posts_grant_and_returns_tokens(self):
        route = respx.post(OAUTH_TOKEN_URL).mock(
            return_value=Response(200, json=TOKEN_RESPONSE)
        )
        tokens = oauth.exchange_code(code="auth-code", code_verifier="verifier-123")

        assert tokens.access_token == "new-access-token"
        assert tokens.refresh_token == "new-refresh-token"
        assert tokens.scopes == ["CONNECT_READ", "CONNECT_WRITE"]

        sent = dict(
            pair.split("=", 1)
            for pair in route.calls.last.request.content.decode().split("&")
        )
        assert sent["grant_type"] == "authorization_code"
        assert sent["code"] == "auth-code"
        assert sent["code_verifier"] == "verifier-123"
        assert sent["client_id"] == "test-client-id"

    def test_state_mismatch_raises(self):
        with pytest.raises(oauth.StateMismatchError):
            oauth.exchange_code(
                code="auth-code",
                code_verifier="verifier",
                expected_state="expected",
                received_state="tampered",
            )

    @respx.mock
    def test_non_2xx_raises_token_exchange_error(self):
        respx.post(OAUTH_TOKEN_URL).mock(
            return_value=Response(400, json={"error": "invalid_grant"})
        )
        with pytest.raises(oauth.TokenExchangeError) as excinfo:
            oauth.exchange_code(code="bad", code_verifier="verifier")
        assert excinfo.value.status_code == 400


@pytest.mark.django_db
class TestIngestTokens:
    @respx.mock
    def test_creates_connection_and_fetches_identity(self, customer):
        respx.get(f"{API_BASE_URL}/wellness-api/rest/user/id").mock(
            return_value=Response(200, json={"userId": "garmin-user-1"})
        )
        respx.get(f"{API_BASE_URL}/wellness-api/rest/user/permissions").mock(
            return_value=Response(200, json=["HEALTH_EXPORT", "ACTIVITY_EXPORT"])
        )
        now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)

        connection = oauth.ingest_tokens(
            customer=customer, tokens=TOKEN_RESPONSE, now=now
        )

        assert connection.garmin_user_id == "garmin-user-1"
        assert connection.access_token == "new-access-token"
        assert connection.refresh_token == "new-refresh-token"
        assert connection.token_expires_at == datetime(
            2026, 7, 7, 12, 0, tzinfo=timezone.utc
        )
        assert connection.refresh_token_expires_at is not None
        assert connection.permissions == ["HEALTH_EXPORT", "ACTIVITY_EXPORT"]
        assert connection.status == ConnectionStatus.ACTIVE

    @respx.mock
    def test_identity_failure_stores_empty_user_id(self, customer):
        respx.get(f"{API_BASE_URL}/wellness-api/rest/user/id").mock(
            return_value=Response(500, text="boom")
        )
        respx.get(f"{API_BASE_URL}/wellness-api/rest/user/permissions").mock(
            return_value=Response(200, json=["HEALTH_EXPORT"])
        )
        connection = oauth.ingest_tokens(customer=customer, tokens=TOKEN_RESPONSE)
        assert connection.garmin_user_id == ""

    def test_explicit_ids_skip_http(self, customer):
        connection = oauth.ingest_tokens(
            customer=customer,
            tokens=TOKEN_RESPONSE,
            garmin_user_id="explicit-id",
            permissions=["HEALTH_EXPORT"],
        )
        assert connection.garmin_user_id == "explicit-id"

    def test_reconnect_updates_existing_row(self, connection):
        updated = oauth.ingest_tokens(
            customer=connection.customer,
            tokens=TOKEN_RESPONSE,
            garmin_user_id="same-user",
            permissions=[],
        )
        assert updated.pk == connection.pk
        assert GarminConnection.objects.count() == 1
        assert updated.access_token == "new-access-token"


@pytest.mark.django_db
class TestRefreshAccessToken:
    @respx.mock
    def test_rotates_both_tokens(self, connection):
        route = respx.post(OAUTH_TOKEN_URL).mock(
            return_value=Response(200, json=TOKEN_RESPONSE)
        )
        oauth.refresh_access_token(connection)

        connection.refresh_from_db()
        assert connection.access_token == "new-access-token"
        # Garmin rotates the refresh token on every refresh.
        assert connection.refresh_token == "new-refresh-token"

        sent = dict(
            pair.split("=", 1)
            for pair in route.calls.last.request.content.decode().split("&")
        )
        assert sent["grant_type"] == "refresh_token"
        assert sent["refresh_token"] == "garmin-initial-refresh"


@pytest.mark.django_db
class TestRevoke:
    @respx.mock
    def test_deletes_registration_and_marks_revoked(self, connection):
        route = respx.delete(
            f"{API_BASE_URL}/wellness-api/rest/user/registration"
        ).mock(return_value=Response(204))
        oauth.revoke(connection)

        assert route.called
        auth = route.calls.last.request.headers["Authorization"]
        assert auth == "Bearer garmin-initial-access"
        connection.refresh_from_db()
        assert connection.status == ConnectionStatus.REVOKED

    @respx.mock
    def test_garmin_error_still_revokes_locally(self, connection):
        respx.delete(f"{API_BASE_URL}/wellness-api/rest/user/registration").mock(
            return_value=Response(500)
        )
        oauth.revoke(connection)
        connection.refresh_from_db()
        assert connection.status == ConnectionStatus.REVOKED
