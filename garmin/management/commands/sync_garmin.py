"""``python manage.py sync_garmin`` — fetch + ingest Garmin Health data.

Defaults: sync the **last 24h of uploads** for **every active connection** in
the database. Garmin's pull endpoints filter by upload time (when the device
synced), so this catches whatever users synced recently; for older history use
``--backfill``, whose results arrive asynchronously via webhooks.

Common invocations::

    python manage.py sync_garmin
    python manage.py sync_garmin --user alice
    python manage.py sync_garmin --days 7
    python manage.py sync_garmin --start 2026-07-01 --end 2026-07-02
    python manage.py sync_garmin --summary-type dailies --summary-type sleeps
    python manage.py sync_garmin --user alice --days 90 --backfill

One failed user does not stop the rest; per-user errors are logged and the
final exit status is non-zero if any sync failed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from ...client import GarminClient
from ...ingest import DEFAULT_SUMMARY_TYPES, sync_user
from ...models import ConnectionStatus, GarminConnection

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Sync Garmin Health data into healthdatamodel for one or all active connections."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--user",
            dest="username",
            default=None,
            help="Sync only this user (by USERNAME_FIELD on AUTH_USER_MODEL).",
        )
        parser.add_argument(
            "--user-id",
            dest="user_id",
            type=int,
            default=None,
            help="Sync only this user (by primary key).",
        )

        window = parser.add_mutually_exclusive_group()
        window.add_argument(
            "--hours", type=int, default=None, help="Window size in hours."
        )
        window.add_argument(
            "--days", type=int, default=None, help="Window size in days."
        )

        parser.add_argument(
            "--start",
            type=str,
            default=None,
            help="ISO-8601 window start (UTC). Use with --end.",
        )
        parser.add_argument(
            "--end",
            type=str,
            default=None,
            help="ISO-8601 window end (UTC). Use with --start.",
        )

        parser.add_argument(
            "--summary-type",
            dest="summary_types",
            action="append",
            default=None,
            help="Restrict to this summary type. Repeatable. Default: all configured types.",
        )
        parser.add_argument(
            "--backfill",
            action="store_true",
            default=False,
            help=(
                "Instead of pulling, ask Garmin to (re)send historic data for "
                "the window. Results arrive via webhooks, not this command."
            ),
        )

    def handle(self, *args, **options) -> None:
        start, end = self._resolve_window(options)
        connections = self._resolve_connections(options)

        if not connections:
            self.stdout.write(self.style.WARNING("No matching active connections."))
            return

        summary_types = options["summary_types"]
        mode = "Backfilling" if options["backfill"] else "Syncing"
        self.stdout.write(
            f"{mode} {len(connections)} connection(s) over {start.isoformat()} → {end.isoformat()}"
        )

        failures = 0
        for connection in connections:
            label = f"customer={connection.customer_id} ({connection.garmin_user_id})"
            try:
                if options["backfill"]:
                    self._backfill(connection, start, end, summary_types)
                    self.stdout.write(f"  ✓ {label}: backfill requested")
                    continue
                result = sync_user(
                    connection,
                    start=start,
                    end=end,
                    summary_types=summary_types,
                )
            except Exception:  # noqa: BLE001 — log + continue is the contract
                log.exception("sync failed for %s", label)
                failures += 1
                self.stderr.write(self.style.ERROR(f"  ✗ {label}: failed (see logs)"))
                continue
            self.stdout.write(
                f"  ✓ {label}: {result.total} record(s) — "
                + ", ".join(f"{k}={v}" for k, v in result.counts.items())
            )

        if failures:
            raise CommandError(f"{failures} connection(s) failed; see logs.")

    # ------------------------------------------------------------------

    def _backfill(
        self,
        connection: GarminConnection,
        start: datetime,
        end: datetime,
        summary_types: list[str] | None,
    ) -> None:
        with GarminClient(connection) as client:
            for summary_type in summary_types or DEFAULT_SUMMARY_TYPES:
                client.request_backfill(summary_type, start=start, end=end)

    def _resolve_window(self, options: dict) -> tuple[datetime, datetime]:
        if (options["start"] is None) ^ (options["end"] is None):
            raise CommandError("--start and --end must be used together.")

        if options["start"] is not None:
            start = parse_datetime(options["start"])
            end = parse_datetime(options["end"])
            if start is None or end is None:
                raise CommandError("--start/--end must be ISO-8601 datetimes.")
            return _as_utc(start), _as_utc(end)

        if options["hours"] is not None:
            delta = timedelta(hours=options["hours"])
        elif options["days"] is not None:
            delta = timedelta(days=options["days"])
        else:
            delta = timedelta(hours=24)

        end = datetime.now(timezone.utc)
        return end - delta, end

    def _resolve_connections(self, options: dict) -> list[GarminConnection]:
        qs = GarminConnection.objects.filter(status=ConnectionStatus.ACTIVE)

        if options["user_id"] is not None and options["username"] is not None:
            raise CommandError("Pass --user OR --user-id, not both.")

        if options["user_id"] is not None:
            qs = qs.filter(customer_id=options["user_id"])
        elif options["username"] is not None:
            User = get_user_model()
            field = User.USERNAME_FIELD
            try:
                customer = User.objects.get(**{field: options["username"]})
            except User.DoesNotExist as exc:
                raise CommandError(
                    f"No user with {field}={options['username']!r}."
                ) from exc
            qs = qs.filter(customer=customer)

        return list(qs.select_related("customer"))


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
