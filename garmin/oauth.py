"""Garmin OAuth 2.0 (PKCE) helpers.

Garmin's flow is plain OAuth2 + PKCE (S256) over two endpoints — no vendor SDK
needed, so this module speaks httpx directly. The public API is:

* :func:`build_authorization_url` — produce the consent URL for the web-callback flow.
* :func:`exchange_code` — server-side code → token exchange (PKCE verifier required).
* :func:`ingest_tokens` — persist tokens obtained externally (e.g. a mobile app that
  did the OAuth dance and POSTs the resulting token dict to your backend, mirroring
  the wellrider pattern).
* :func:`refresh_access_token` — refresh a stored connection's tokens (Garmin
  rotates the refresh token on every refresh).
* :func:`revoke` — delete the user registration at Garmin and mark the connection
  revoked. Garmin requires this call whenever the partner app offers its own
  "disconnect" mechanism.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx
from django.conf import settings

from .constants import (
    API_BASE_URL,
    OAUTH_AUTHORIZATION_URL,
    OAUTH_TOKEN_URL,
    WELLNESS_API_PATH,
)
from .models import ConnectionStatus, GarminConnection
from .schemas import GarminTokens, OAuthFlowState

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

log = logging.getLogger(__name__)


class OAuthError(Exception):
    """Base for OAuth-related errors raised by this module."""


class StateMismatchError(OAuthError):
    """Raised when the ``state`` returned from Garmin doesn't match what we stashed."""


class TokenExchangeError(OAuthError):
    """Raised when the token endpoint returns a non-2xx response."""

    def __init__(self, status_code: int, body: str):
        super().__init__(f"token endpoint returned HTTP {status_code}: {body}")
        self.status_code = status_code
        self.body = body


def _code_challenge(code_verifier: str) -> str:
    """base64url(sha256(code_verifier)) without padding, per RFC 7636 S256."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorization_url(*, state: str | None = None) -> tuple[str, OAuthFlowState]:
    """Build the consent URL and the state to round-trip via the session.

    Garmin scopes are fixed per app (configured at app creation; users pick
    permissions on the consent screen), so unlike Google there is no ``scopes``
    argument here.
    """
    code_verifier = secrets.token_urlsafe(64)
    flow_state = OAuthFlowState(
        state=state or secrets.token_urlsafe(32),
        code_verifier=code_verifier,
    )
    params = {
        "response_type": "code",
        "client_id": settings.GARMIN_CLIENT_ID,
        "code_challenge": _code_challenge(code_verifier),
        "code_challenge_method": "S256",
        "redirect_uri": settings.GARMIN_REDIRECT_URI,
        "state": flow_state.state,
    }
    return f"{OAUTH_AUTHORIZATION_URL}?{urlencode(params)}", flow_state


def _token_request(data: dict[str, str]) -> GarminTokens:
    response = httpx.post(
        OAUTH_TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise TokenExchangeError(response.status_code, response.text)
    return GarminTokens.model_validate(response.json())


def exchange_code(
    *,
    code: str,
    code_verifier: str,
    expected_state: str | None = None,
    received_state: str | None = None,
) -> GarminTokens:
    """Exchange an authorization code for tokens.

    Pass ``expected_state`` and ``received_state`` to enforce CSRF protection at
    this layer; pass neither to skip (e.g. when the upstream view already
    validated).
    """
    if expected_state is not None and received_state != expected_state:
        raise StateMismatchError("OAuth state mismatch")
    return _token_request(
        {
            "grant_type": "authorization_code",
            "client_id": settings.GARMIN_CLIENT_ID,
            "client_secret": settings.GARMIN_CLIENT_SECRET,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": settings.GARMIN_REDIRECT_URI,
        }
    )


def ingest_tokens(
    *,
    customer: AbstractBaseUser,
    tokens: GarminTokens | dict[str, Any],
    garmin_user_id: str | None = None,
    permissions: list[str] | None = None,
    now: datetime | None = None,
) -> GarminConnection:
    """Persist tokens onto a :class:`GarminConnection` (create or update).

    This is the entry point for the "mobile app already did the OAuth dance and
    is shipping us the token dict" pattern. ``garmin_user_id`` is fetched via
    ``GET user/id`` if not provided; ``permissions`` via ``GET user/permissions``.
    Both are best-effort — a transient API failure stores an empty value rather
    than failing the connect.
    """
    parsed = (
        tokens
        if isinstance(tokens, GarminTokens)
        else GarminTokens.model_validate(tokens)
    )
    if garmin_user_id is None:
        try:
            garmin_user_id = _fetch_garmin_user_id(parsed.access_token)
        except (httpx.HTTPError, OAuthError) as exc:
            # The OAuth flow itself succeeded; the user id is only needed for
            # webhook routing. Store empty and let a later sync resolve it.
            log.warning("user/id fetch failed (%s) — storing empty garmin_user_id", exc)
            garmin_user_id = ""
    if permissions is None:
        try:
            permissions = _fetch_permissions(parsed.access_token)
        except (httpx.HTTPError, OAuthError) as exc:
            log.warning("user/permissions fetch failed (%s) — storing empty list", exc)
            permissions = []

    connection, _ = GarminConnection.objects.update_or_create(
        customer=customer,
        defaults={
            "garmin_user_id": garmin_user_id,
            "access_token": parsed.access_token,
            "refresh_token": parsed.refresh_token,
            "token_expires_at": parsed.expires_at(now=now),
            "refresh_token_expires_at": parsed.refresh_expires_at(now=now),
            "scopes": parsed.scopes,
            "permissions": permissions,
            "status": ConnectionStatus.ACTIVE,
        },
    )
    return connection


def refresh_access_token(connection: GarminConnection) -> GarminConnection:
    """Refresh the connection's tokens in place using its stored refresh token.

    Garmin returns a NEW refresh token with every refresh response; the old one
    should be considered spent, so both tokens persist together.
    """
    tokens = _token_request(
        {
            "grant_type": "refresh_token",
            "client_id": settings.GARMIN_CLIENT_ID,
            "client_secret": settings.GARMIN_CLIENT_SECRET,
            "refresh_token": connection.refresh_token,
        }
    )
    connection.access_token = tokens.access_token
    connection.refresh_token = tokens.refresh_token
    connection.token_expires_at = tokens.expires_at()
    connection.refresh_token_expires_at = tokens.refresh_expires_at()
    connection.save(
        update_fields=[
            "access_token",
            "refresh_token",
            "token_expires_at",
            "refresh_token_expires_at",
        ]
    )
    return connection


def revoke(connection: GarminConnection) -> None:
    """Delete the user registration at Garmin and mark the connection ``REVOKED``.

    Garmin requires ``DELETE user/registration`` whenever the partner offers a
    disconnect mechanism outside Garmin Connect's own consent-removal page.
    Best-effort: a non-2xx from Garmin still flips the local status — the
    user-facing intent (disconnect) shouldn't be blocked by a transient error.
    """
    try:
        httpx.delete(
            f"{API_BASE_URL}/{WELLNESS_API_PATH}/user/registration",
            headers={"Authorization": f"Bearer {connection.access_token}"},
            timeout=10.0,
        )
    except httpx.HTTPError:
        pass
    connection.status = ConnectionStatus.REVOKED
    connection.save(update_fields=["status"])


def _fetch_garmin_user_id(access_token: str) -> str:
    """Call ``GET user/id`` to resolve the stable API User ID for a token."""
    response = httpx.get(
        f"{API_BASE_URL}/{WELLNESS_API_PATH}/user/id",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10.0,
    )
    if response.status_code >= 400:
        # raise_for_status drops the response body; we want it visible.
        raise OAuthError(
            f"user/id returned HTTP {response.status_code}: {response.text}"
        )
    payload = response.json()
    user_id = payload.get("userId")
    if not user_id:
        raise OAuthError(f"user/id returned no user id: {payload!r}")
    return str(user_id)


def _fetch_permissions(access_token: str) -> list[str]:
    """Call ``GET user/permissions`` for the permissions this user granted."""
    response = httpx.get(
        f"{API_BASE_URL}/{WELLNESS_API_PATH}/user/permissions",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10.0,
    )
    if response.status_code >= 400:
        raise OAuthError(
            f"user/permissions returned HTTP {response.status_code}: {response.text}"
        )
    payload = response.json()
    # Documented shape is a bare JSON array; tolerate {"permissions": [...]} too.
    if isinstance(payload, dict):
        payload = payload.get("permissions", [])
    return [str(p) for p in payload]
