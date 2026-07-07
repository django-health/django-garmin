from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.db import models


class ConnectionStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    DISCONNECTED = "disconnected", "Disconnected"
    REVOKED = "revoked", "Revoked"


class GarminConnection(models.Model):
    """Per-user OAuth state for the Garmin Health API.

    Health records persist through django-healthdatamodel; this model only
    holds the credentials needed to fetch them. Garmin rotates the refresh
    token on every refresh, so both tokens are rewritten together.
    """

    customer = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="garmin_connection",
    )
    # Garmin's "API User ID": stable across re-consents and programs, and the
    # key that push/ping notifications carry to identify the user.
    garmin_user_id = models.CharField(max_length=128, db_index=True)
    access_token = models.TextField()
    refresh_token = models.TextField()
    token_expires_at = models.DateTimeField()
    refresh_token_expires_at = models.DateTimeField(null=True, blank=True)
    scopes = models.JSONField(default=list)
    # Permissions the user actually granted at consent time
    # (GET user/permissions) — a subset of what the app is configured for.
    permissions = models.JSONField(default=list)
    status = models.CharField(
        max_length=32,
        choices=ConnectionStatus.choices,
        default=ConnectionStatus.ACTIVE,
    )
    connected_at = models.DateTimeField(auto_now_add=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Garmin connection"
        verbose_name_plural = "Garmin connections"

    def __str__(self) -> str:
        return f"GarminConnection(customer={self.customer_id}, status={self.status})"

    def is_token_expired(
        self, *, leeway_seconds: int = 600, now: datetime | None = None
    ) -> bool:
        """True if the access token is at or past ``token_expires_at - leeway``.

        Garmin's OAuth2 spec recommends refreshing at least 600 seconds before
        the reported expiry to absorb network delays, hence the default leeway.
        """
        anchor = now or datetime.now(timezone.utc)
        return anchor >= self.token_expires_at - timedelta(seconds=leeway_seconds)
