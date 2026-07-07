"""Pydantic models for Garmin OAuth request/response payloads."""

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict, field_validator


class OAuthFlowState(BaseModel):
    """Per-request state stashed in the Django session between ``connect`` and ``callback``.

    ``state`` defends against CSRF; ``code_verifier`` completes the PKCE handshake
    after the user returns from Garmin's consent screen.
    """

    state: str
    code_verifier: str


class GarminTokens(BaseModel):
    """Token-endpoint response from ``diauth.garmin.com``.

    Mirrors the JSON Garmin returns for both ``authorization_code`` and
    ``refresh_token`` grants — unlike Google, Garmin returns a (rotated)
    ``refresh_token`` on every grant, and both responses carry
    ``refresh_token_expires_in`` (~90 days).
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    access_token: str
    expires_in: int
    token_type: str = "bearer"
    scope: str = ""
    refresh_token: str
    refresh_token_expires_in: int | None = None
    jti: str | None = None

    @field_validator("scope", mode="before")
    @classmethod
    def _coerce_scope(cls, value: object) -> str:
        # Garmin returns a space-separated string (e.g. "CONNECT_READ
        # CONNECT_WRITE"); accept a pre-parsed list too.
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            return " ".join(str(v) for v in value)
        return str(value)

    @property
    def scopes(self) -> list[str]:
        return self.scope.split() if self.scope else []

    def expires_at(self, *, now: datetime | None = None) -> datetime:
        anchor = now or datetime.now(timezone.utc)
        return anchor + timedelta(seconds=self.expires_in)

    def refresh_expires_at(self, *, now: datetime | None = None) -> datetime | None:
        if self.refresh_token_expires_in is None:
            return None
        anchor = now or datetime.now(timezone.utc)
        return anchor + timedelta(seconds=self.refresh_token_expires_in)
