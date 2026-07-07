import json
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from django.urls import reverse
from httpx import Response

from garmin.constants import API_BASE_URL, OAUTH_TOKEN_URL
from garmin.models import ConnectionStatus, GarminConnection
from garmin.signals import notification_received
from garmin.views import SESSION_KEY

WELLNESS = f"{API_BASE_URL}/wellness-api/rest"

TOKEN_RESPONSE = {
    "access_token": "cb-access",
    "expires_in": 86400,
    "refresh_token": "cb-refresh",
    "scope": "CONNECT_READ",
}


@pytest.mark.django_db
class TestConnect:
    def test_requires_login(self, client):
        response = client.get(reverse("garmin:connect"))
        assert response.status_code == 302
        assert "login" in response["Location"]

    def test_redirects_to_garmin_and_stashes_flow(self, client, customer):
        client.force_login(customer)
        response = client.get(reverse("garmin:connect"))

        assert response.status_code == 302
        location = urlparse(response["Location"])
        assert location.hostname == "connect.garmin.com"
        params = {k: v[0] for k, v in parse_qs(location.query).items()}

        stashed = client.session[SESSION_KEY]
        assert params["state"] == stashed["state"]
        assert stashed["code_verifier"]


@pytest.mark.django_db
class TestCallback:
    def _start_flow(self, client, customer):
        client.force_login(customer)
        response = client.get(reverse("garmin:connect"))
        params = {
            k: v[0] for k, v in parse_qs(urlparse(response["Location"]).query).items()
        }
        return params["state"]

    @respx.mock
    def test_happy_path_creates_connection(self, client, customer):
        state = self._start_flow(client, customer)
        respx.post(OAUTH_TOKEN_URL).mock(
            return_value=Response(200, json=TOKEN_RESPONSE)
        )
        respx.get(f"{WELLNESS}/user/id").mock(
            return_value=Response(200, json={"userId": "garmin-user-9"})
        )
        respx.get(f"{WELLNESS}/user/permissions").mock(
            return_value=Response(200, json=["HEALTH_EXPORT"])
        )

        response = client.get(
            reverse("garmin:callback"), {"code": "auth-code", "state": state}
        )

        assert response.status_code == 302
        connection = GarminConnection.objects.get(customer=customer)
        assert connection.garmin_user_id == "garmin-user-9"
        assert connection.access_token == "cb-access"

    def test_state_mismatch_rejected(self, client, customer):
        self._start_flow(client, customer)
        response = client.get(
            reverse("garmin:callback"), {"code": "auth-code", "state": "tampered"}
        )
        assert response.status_code == 400
        assert not GarminConnection.objects.exists()

    def test_error_param_rejected(self, client, customer):
        client.force_login(customer)
        response = client.get(reverse("garmin:callback"), {"error": "access_denied"})
        assert response.status_code == 400

    def test_missing_code_rejected(self, client, customer):
        client.force_login(customer)
        response = client.get(reverse("garmin:callback"))
        assert response.status_code == 400

    def test_no_flow_in_progress_rejected(self, client, customer):
        client.force_login(customer)
        response = client.get(
            reverse("garmin:callback"), {"code": "auth-code", "state": "s"}
        )
        assert response.status_code == 400


@pytest.mark.django_db
class TestDisconnect:
    @respx.mock
    def test_revokes_connection(self, client, connection):
        respx.delete(f"{WELLNESS}/user/registration").mock(return_value=Response(204))
        client.force_login(connection.customer)
        response = client.post(reverse("garmin:disconnect"))

        assert response.status_code == 302
        connection.refresh_from_db()
        assert connection.status == ConnectionStatus.REVOKED

    def test_no_connection_is_a_noop(self, client, customer):
        client.force_login(customer)
        response = client.post(reverse("garmin:disconnect"))
        assert response.status_code == 302

    def test_get_not_allowed(self, client, connection):
        client.force_login(connection.customer)
        response = client.get(reverse("garmin:disconnect"))
        assert response.status_code == 405


@pytest.mark.django_db
class TestNotificationReceiver:
    def test_emits_signal_and_returns_200(self, client):
        received = []

        def handler(sender, payload, **kwargs):
            received.append(payload)

        notification_received.connect(handler)
        try:
            payload = {"dailies": [{"userId": "u1", "callbackURL": "https://x"}]}
            response = client.post(
                reverse("garmin:notifications"),
                data=json.dumps(payload),
                content_type="application/json",
            )
        finally:
            notification_received.disconnect(handler)

        assert response.status_code == 200
        assert received == [payload]

    def test_invalid_json_rejected(self, client):
        response = client.post(
            reverse("garmin:notifications"),
            data="not-json{",
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_non_object_body_rejected(self, client):
        response = client.post(
            reverse("garmin:notifications"),
            data="[1, 2]",
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_get_not_allowed(self, client):
        response = client.get(reverse("garmin:notifications"))
        assert response.status_code == 405
