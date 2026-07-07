# Notes for Claude (and human contributors)

This project is a reusable Django library — small, focused, with a real
shipping cadence to PyPI. Slice-sized changes; commit messages with the
"why"; tests for every behavior; live calibration over docs guessing.

It is a sibling of `django-health/django-google-health` and deliberately
mirrors its layout, pyproject/CI/pre-commit setup, and OAuth/client/ingest
module boundaries. When in doubt about a pattern, check what that repo does.

## Garmin API ground truth

Garmin's Health API docs are partner-gated; the summaries in `docs/garmin/`
are the local ground truth (sourced from the public OAuth2 PKCE spec PDF and
the partner documentation). The payload shapes in `garmin/ingest.py` and the
test fixtures have NOT yet been calibrated against a live partner account —
when live credentials are available, verify against real payloads and promote
findings into code comments + tests, not just commit messages.

Live-calibration entry point: run the demo (`README.md` → "Try it on your own
data"), then pull the access token from `db.sqlite3` and hit
`https://apis.garmin.com/wellness-api/rest/...` directly with `httpx` before
guessing at code fixes.

Things Garmin does differently from Google Health (don't "fix" them):

- No per-request scopes; API sections are app-level, users pick permissions.
- Refresh tokens ROTATE on every refresh — both tokens persist together.
- Pull windows filter by UPLOAD time, max 24h; history goes through
  `backfill/*` which answers 202 and delivers via webhooks.
- Webhooks have no verification handshake and no auth header; the receiver
  returns 200 fast and routing happens by `userId`.
- BMR comes straight from `dailies.bmrKilocalories` — no Mifflin-St Jeor
  estimation step like django-google-health has.

## Upstream contributions to django-healthdatamodel

The sister repo `django-health/django-healthdatamodel` is where the storage
layer lives. When a change there unblocks this project, you have end-to-end
release autonomy as long as CI is green:

1. Open the PR.
2. Wait for CI green.
3. Bump version in `pyproject.toml` (minor for additive, patch for fix).
4. Squash-merge.
5. Tag `v<X>` on `main`, push the tag — triggers PyPI publish via OIDC.
6. Bump the floor in this repo's `pyproject.toml`, swap any local stopgaps
   for the upstream API, commit + push.

Both repos publish to PyPI via trusted publishing — no manual upload.

## Out of scope for this project

- **Token encryption at rest.** Production deployment uses Postgres with
  encryption-at-rest at the storage layer. Don't add Fernet /
  django-cryptography fields to `GarminConnection`.
- **Signup view in the demo.** `createsuperuser` is the way. The demo exists
  so the maintainer can test against their own Garmin account; it isn't a
  hosted SaaS shell.
- **OAuth 1.0a support.** Retired by Garmin at the end of 2026; the legacy
  wellrider Garmin code that used it was never completed and is not a
  compatibility target.
