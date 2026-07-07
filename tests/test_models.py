from datetime import datetime, timedelta, timezone

import pytest

from garmin.models import ConnectionStatus, GarminConnection


@pytest.mark.django_db
class TestGarminConnection:
    def test_defaults(self, connection):
        assert connection.status == ConnectionStatus.ACTIVE
        assert connection.last_sync_at is None
        assert connection.connected_at is not None

    def test_one_connection_per_customer(self, connection, customer):
        assert customer.garmin_connection == connection

    def test_str(self, connection):
        text = str(connection)
        assert str(connection.customer_id) in text
        assert "active" in text

    def test_token_not_expired(self, connection):
        now = connection.token_expires_at - timedelta(hours=1)
        assert connection.is_token_expired(now=now) is False

    def test_token_expired_at_expiry(self, connection):
        assert connection.is_token_expired(now=connection.token_expires_at) is True

    def test_token_expired_within_default_leeway(self, connection):
        # Garmin recommends refreshing >= 600s before expiry.
        now = connection.token_expires_at - timedelta(seconds=599)
        assert connection.is_token_expired(now=now) is True
        now = connection.token_expires_at - timedelta(seconds=601)
        assert connection.is_token_expired(now=now) is False

    def test_custom_leeway(self, connection):
        now = connection.token_expires_at - timedelta(seconds=30)
        assert connection.is_token_expired(leeway_seconds=0, now=now) is False

    def test_refresh_expiry_nullable(self, customer):
        connection = GarminConnection.objects.create(
            customer=customer,
            garmin_user_id="abc",
            access_token="a",
            refresh_token="r",
            token_expires_at=datetime.now(timezone.utc),
        )
        assert connection.refresh_token_expires_at is None
        assert connection.scopes == []
        assert connection.permissions == []
