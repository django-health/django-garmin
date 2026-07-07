# django-garmin

[![CI](https://github.com/django-health/django-garmin/actions/workflows/ci.yml/badge.svg)](https://github.com/django-health/django-garmin/actions/workflows/ci.yml)

A reusable Django app for the [Garmin Health API](https://developer.garmin.com/gc-developer-program/health-api/) (Garmin Connect Developer Program). Handles Garmin's OAuth 2.0 + PKCE flow, fetches wellness summaries from `apis.garmin.com`, receives push/ping webhook notifications, and persists everything through [`django-healthdatamodel`](https://github.com/django-health/django-healthdatamodel) so the same storage and query layer serves Apple Health, Fitbit, Google Health, and Garmin side-by-side.

> Garmin's Health API is partner-gated: you need an approved [Garmin Connect Developer Program](https://developer.garmin.com/gc-developer-program/) app (an evaluation-tier app works) to obtain a consumer key/secret. OAuth 1.0a is retired at the end of 2026; this package speaks only the current OAuth 2.0 + PKCE flow.

## Install

```
pip install django-garmin
```

Add both this app and `django-healthdatamodel` to `INSTALLED_APPS`, then run migrations:

```python
INSTALLED_APPS = [
    ...
    "healthdatamodel",
    "garmin",
]
```

```
python manage.py migrate
```

The model uses `settings.AUTH_USER_MODEL` so it works with any custom user model.

## Configuration

```python
GARMIN_CLIENT_ID = "..."       # consumer key from the Garmin developer portal
GARMIN_CLIENT_SECRET = "..."   # consumer secret
GARMIN_REDIRECT_URI = "https://your-app.example.com/garmin/callback/"
```

Register the redirect URI in the Garmin developer portal — the token exchange fails on any mismatch. Garmin has no per-request scope parameter: the API sections your app can reach are fixed at app creation, and each user picks permissions (e.g. `HEALTH_EXPORT`, `ACTIVITY_EXPORT`) on the consent screen. The permissions a user actually granted are stored on their `GarminConnection.permissions`.

## URLs

```python
urlpatterns = [
    ...
    path("garmin/", include("garmin.urls")),
]
```

This mounts:

- `garmin/connect/` — start the OAuth 2.0 + PKCE flow (login required)
- `garmin/callback/` — the redirect URI target
- `garmin/disconnect/` — POST; calls `DELETE user/registration` at Garmin (required by their terms when you offer your own disconnect) and marks the connection revoked
- `garmin/notifications/` — webhook receiver for push/ping notifications

Mobile clients that run the OAuth dance themselves can POST the resulting token dict to a project-local endpoint that calls `garmin.oauth.ingest_tokens`.

## Getting data

Garmin delivers data two ways; this package supports both.

**Webhooks (push/ping) — Garmin's recommended architecture.** Configure your summary-type endpoints in the developer portal to point at `garmin/notifications/`, then connect a signal handler:

```python
from django.dispatch import receiver
from garmin.signals import notification_received
from garmin.webhooks import process_notification

@receiver(notification_received)
def on_notification(sender, payload, **kwargs):
    process_notification(payload)  # or hand off to celery / a queue
```

`process_notification` routes entries to users by Garmin's stable `userId`, fetches `callbackURL`s for ping-style notifications, and ingests everything into `healthdatamodel`. Garmin expects a fast 200 and disables endpoints that keep failing — hand off to a queue if your processing is heavy.

**Pull.** `garmin.ingest.sync_user(connection, start=..., end=...)` fetches and ingests the configured summary types. Note Garmin's pull semantics: the window filters by **upload** time (when the user's device synced), capped at 24 hours per request (the client chunks longer windows automatically). For deep history, request a **backfill** (`GarminClient.request_backfill` or `manage.py sync_garmin --backfill`); Garmin re-sends the data asynchronously through your webhook endpoints.

```
python manage.py sync_garmin                       # last 24h of uploads, all active connections
python manage.py sync_garmin --user alice --days 7
python manage.py sync_garmin --summary-type dailies --summary-type sleeps
python manage.py sync_garmin --user alice --days 90 --backfill
```

## What gets stored

This app does **not** define `Record` / `Workout` tables — those live in `django-healthdatamodel`. The `garmin.ingest` module maps Garmin summaries to `healthdatamodel` inputs and persists them with `source="garmin"`:

| Garmin summary | healthdatamodel records |
| --- | --- |
| `dailies` | steps, active + BMR calories, distance, floors, resting HR, intensity minutes, intraday heart-rate samples |
| `sleeps` | one sleep-stage record per `sleepLevelsMap` interval (deep/light/rem/awake) |
| `bodyComps` | weight, body-fat %, BMI |
| `pulseox` | SpO2 samples |
| `activities` | one `Workout` per activity |
| `epochs` | steps/calories/distance at 15-minute granularity — **opt-in**, double-counts against `dailies` |

Garmin reports BMR directly (`bmrKilocalories`), so unlike the Google Health integration there is no BMR-estimation step.

The only model defined here is `GarminConnection`: per-user OAuth tokens (Garmin rotates the refresh token on every refresh — both are rewritten together), granted permissions, connection status, and last sync timestamp.

## Try it on your own data

The repo includes a runnable demo Django project at `demo/`.

### 1. Get Garmin developer program access (one-time)

Apply at the [Garmin Connect Developer Program](https://developer.garmin.com/gc-developer-program/) — approval unlocks an evaluation environment. Create an app to get a consumer key/secret, and register the redirect URI `http://localhost:8000/garmin/callback/` (exact match, trailing slash included).

### 2. Run the demo

```
uv sync
uv run python manage.py migrate
uv run python manage.py createsuperuser
export GARMIN_CLIENT_ID=...
export GARMIN_CLIENT_SECRET=...
uv run python manage.py runserver
```

Open <http://localhost:8000/>, sign in with the superuser you just created, then:

- Click **Connect Garmin** → consent on Garmin Connect → land back on the homepage with a `GarminConnection` saved for your user.
- Sync your watch in the Garmin Connect mobile app (pull windows filter by upload time), then click **Sync now**.
- Browse the resulting rows at `/admin/healthdatamodel/record/` (and `.../workout/`).

Or drive sync from the terminal:

```
uv run python manage.py sync_garmin --user <your-username>
```

## Documentation

Garmin's Health API docs are partner-gated, so summaries live under `docs/garmin/`:

- `oauth2-pkce.md` — the OAuth 2.0 + PKCE flow (endpoints, parameters, token lifetimes)
- `health-api.md` — pull endpoints, summary payload shapes, push/ping webhooks, backfill

## Development

```
uv sync --group dev
uv run pytest tests/ -v
uv run pre-commit run --all-files
```
