from datetime import datetime, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model

from garmin.models import GarminConnection


@pytest.fixture
def customer(db):
    User = get_user_model()
    return User.objects.create_user(username="test-customer")


@pytest.fixture
def connection(customer):
    return GarminConnection.objects.create(
        customer=customer,
        garmin_user_id="d3315b1072421d0dd7c8f6b8e1de4df8",
        access_token="garmin-initial-access",
        refresh_token="garmin-initial-refresh",
        token_expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        refresh_token_expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        scopes=["CONNECT_READ", "CONNECT_WRITE"],
        permissions=["HEALTH_EXPORT", "ACTIVITY_EXPORT"],
    )
