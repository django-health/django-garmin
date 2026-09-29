"""Incoming push/ping notification processing.

Garmin's Health API delivers data through webhooks configured per summary type
in the developer portal. Two delivery styles share one payload shape — a JSON
object whose top-level keys are summary-type names mapped to entry lists:

* **Push**: each entry IS a full summary (plus ``userId`` /
  ``userAccessToken``). Ingest directly.
* **Ping**: each entry carries a ``callbackURL`` pointing back at the
  wellness API. Fetch it with the matching user's credentials, then ingest.

Garmin sends no verification handshake and no shared-secret header; endpoints
are expected to answer 200 quickly (they disable endpoints that keep failing).
Entries are routed to users via the stable ``userId``, so a connection whose
``garmin_user_id`` is unknown is skipped with a warning.

:func:`process_notification` is safe to call from a queue worker — the
receiver view only emits the :data:`garmin.signals.notification_received`
signal, and your handler decides whether to process inline or hand off.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .client import GarminClient
from .constants import SUMMARY_ACTIVITIES
from .ingest import RECORD_MAPPERS, ingest_summaries
from .models import GarminConnection

log = logging.getLogger(__name__)

# Payload keys we know how to ingest. Notifications for anything else (e.g.
# stressDetails before a mapper exists) are counted but skipped.
KNOWN_SUMMARY_KEYS = frozenset(RECORD_MAPPERS) | {SUMMARY_ACTIVITIES}


def is_ping_entry(entry: dict[str, Any]) -> bool:
    """Ping entries carry a callbackURL instead of inline summary data."""
    return "callbackURL" in entry


def process_notification(payload: dict[str, Any]) -> dict[str, int]:
    """Ingest every entry in a push/ping payload. Returns per-type record counts.

    Entries are grouped by ``userId`` per summary type so each user's client
    (and token refresh) is reused. Unknown users and summary types are logged
    and skipped, never raised — a webhook batch should not fail wholesale
    because one entry is unroutable.
    """
    counts: dict[str, int] = {}
    for summary_type, entries in payload.items():
        if summary_type not in KNOWN_SUMMARY_KEYS or not isinstance(entries, list):
            continue
        counts[summary_type] = _process_entries(summary_type, entries)
    return counts


def _process_entries(summary_type: str, entries: list[dict[str, Any]]) -> int:
    by_user: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        user_id = str(entry.get("userId") or "")
        if not user_id:
            log.warning("Skipping %s entry without userId: %r", summary_type, entry)
            continue
        by_user.setdefault(user_id, []).append(entry)

    count = 0
    for user_id, user_entries in by_user.items():
        try:
            connection = GarminConnection.objects.get(garmin_user_id=user_id)
        except GarminConnection.DoesNotExist:
            log.warning("Notification for unknown garmin userId=%s", user_id)
            continue

        pings = [e for e in user_entries if is_ping_entry(e)]
        pushes = [e for e in user_entries if not is_ping_entry(e)]

        summaries = list(pushes)
        if pings:
            with GarminClient(connection) as client:
                for ping in pings:
                    summaries.extend(client.fetch_url(ping["callbackURL"]))

        count += ingest_summaries(connection, summary_type, summaries)
        connection.last_sync_at = datetime.now(timezone.utc)
        connection.save(update_fields=["last_sync_at"])
    return count
