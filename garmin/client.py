"""HTTP client for the Garmin Health (wellness) REST API.

Sync-only wrapper around ``httpx.Client``, built from a
:class:`~garmin.models.GarminConnection`.

Responsibilities:

* Authorization: inject ``Bearer <access_token>``.
* Token freshness: refresh proactively when the stored expiry is within the
  connection's leeway, and retry once on a 401 to absorb clock skew or external
  token invalidation.
* Resilience: retry 3× with exponential backoff on 429 and 5xx; honor
  ``Retry-After``.
* Windowing: the pull endpoints cap the upload-time window at 24 hours;
  :meth:`iter_summaries` chunks an arbitrary [start, end] range transparently.

Garmin's pull endpoints filter by **upload** time (when the device synced),
not by the summary's own start time. Recent windows therefore capture recently
synced data regardless of when it was recorded; for deep history use
:meth:`request_backfill`, whose results arrive asynchronously via push/ping
notifications.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

from . import oauth
from .constants import API_BASE_URL, MAX_PULL_WINDOW_SECONDS, WELLNESS_API_PATH

if TYPE_CHECKING:
    from .models import GarminConnection


DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0


def _epoch(dt: datetime) -> int:
    """Serialize a datetime as the unix-seconds integer Garmin's query params expect."""
    return int(dt.astimezone(timezone.utc).timestamp())


class GarminAPIError(Exception):
    """Non-retryable error returned by the Garmin Health API."""

    def __init__(self, status_code: int, message: str, payload: Any = None):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.payload = payload


class GarminClient:
    """Thin REST client. Use as a context manager so the underlying httpx
    session is closed deterministically.
    """

    def __init__(
        self,
        connection: GarminConnection,
        *,
        base_url: str = API_BASE_URL,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        backoff_seconds: float = BASE_BACKOFF_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.connection = connection
        self._base = f"{base_url.rstrip('/')}/{WELLNESS_API_PATH}"
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._sleep = sleep
        self._http = httpx.Client(timeout=timeout)

    # context manager ------------------------------------------------------

    def __enter__(self) -> GarminClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # core request loop ----------------------------------------------------

    def _ensure_fresh_token(self) -> None:
        if self.connection.is_token_expired():
            oauth.refresh_access_token(self.connection)

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.connection.access_token}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        self._ensure_fresh_token()
        url = f"{self._base}/{path.lstrip('/')}"
        retried_after_401 = False
        attempt = 0

        while True:
            response = self._http.request(
                method,
                url,
                params=params,
                headers=self._auth_headers(),
            )

            if response.status_code == 401 and not retried_after_401:
                # Either clock skew or the token was invalidated externally —
                # force a refresh and retry once.
                oauth.refresh_access_token(self.connection)
                retried_after_401 = True
                continue

            if response.status_code in RETRYABLE_STATUS and attempt < self._max_retries:
                self._sleep(self._compute_backoff(response, attempt))
                attempt += 1
                continue

            if response.status_code >= 400:
                payload = _safe_json(response)
                message = _extract_error_message(payload, response.text)
                raise GarminAPIError(response.status_code, message, payload)

            if not response.content:
                return None
            return response.json()

    def _compute_backoff(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return self._backoff * (2**attempt)

    # resource methods -----------------------------------------------------

    def get_user_id(self) -> str:
        """``GET user/id`` → the stable API User ID for this token."""
        payload = self._request("GET", "user/id") or {}
        return str(payload.get("userId", ""))

    def get_permissions(self) -> list[str]:
        """``GET user/permissions`` → permissions the user granted at consent."""
        payload = self._request("GET", "user/permissions")
        if isinstance(payload, dict):
            payload = payload.get("permissions", [])
        return [str(p) for p in payload or []]

    def delete_registration(self) -> None:
        """``DELETE user/registration`` — remove the user's consent at Garmin."""
        self._request("DELETE", "user/registration")

    def list_summaries(
        self,
        summary_type: str,
        *,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        """One pull-endpoint call: ``GET /{summary_type}`` over a ≤24h upload window.

        Use :meth:`iter_summaries` to walk a longer range.
        """
        if _epoch(end) - _epoch(start) > MAX_PULL_WINDOW_SECONDS:
            raise ValueError(
                "Garmin pull endpoints cap the upload window at 24 hours; "
                "use iter_summaries() to chunk longer ranges."
            )
        payload = self._request(
            "GET",
            summary_type,
            params={
                "uploadStartTimeInSeconds": _epoch(start),
                "uploadEndTimeInSeconds": _epoch(end),
            },
        )
        return list(payload or [])

    def iter_summaries(
        self,
        summary_type: str,
        *,
        start: datetime,
        end: datetime,
    ) -> Iterator[dict[str, Any]]:
        """Iterate every summary uploaded in [start, end], chunking into 24h windows."""
        window_start = _epoch(start)
        final_end = _epoch(end)
        while window_start < final_end:
            window_end = min(window_start + MAX_PULL_WINDOW_SECONDS, final_end)
            payload = self._request(
                "GET",
                summary_type,
                params={
                    "uploadStartTimeInSeconds": window_start,
                    "uploadEndTimeInSeconds": window_end,
                },
            )
            yield from payload or []
            window_start = window_end

    def fetch_url(self, url: str) -> list[dict[str, Any]]:
        """GET an absolute wellness-api URL — the ``callbackURL`` from a ping
        notification — with auth, refresh, and retry handling.

        Only URLs under the client's base are allowed, so a forged ping can't
        redirect the user's bearer token to an attacker host.
        """
        if not url.startswith(f"{self._base}/"):
            raise ValueError(f"refusing to fetch non-wellness-api URL: {url!r}")
        payload = self._request("GET", url[len(self._base) + 1 :])
        return list(payload or [])

    def request_backfill(
        self,
        summary_type: str,
        *,
        start: datetime,
        end: datetime,
    ) -> None:
        """``GET backfill/{summary_type}`` — ask Garmin to (re)send historic data.

        Returns 202 with no body; the data itself arrives asynchronously via
        push/ping notifications to your configured webhook endpoints. Windows
        are capped at 90 days per request by Garmin.
        """
        self._request(
            "GET",
            f"backfill/{summary_type}",
            params={
                "summaryStartTimeInSeconds": _epoch(start),
                "summaryEndTimeInSeconds": _epoch(end),
            },
        )


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _extract_error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        for key in ("errorMessage", "message", "error"):
            if key in payload:
                return str(payload[key])
    return fallback or "(no body)"
