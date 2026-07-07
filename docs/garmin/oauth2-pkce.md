# Garmin Connect Developer Program — OAuth 2.0 PKCE (summary)

Summarized from Garmin's "OAuth2.0 PKCE Specification" PDF (developerportal.garmin.com);
the official docs are partner-gated. OAuth 1.0a is retired 2026-12-31 — new
integrations must use this flow.

## Step 1 — Authorization request

Generate a `code_verifier` (43–128 chars of `A-Za-z0-9-._~`), then
`code_challenge = base64url(sha256(code_verifier))` without padding.

```
GET https://connect.garmin.com/oauth2Confirm
    ?response_type=code            (required)
    &client_id=<consumer key>      (required)
    &code_challenge=<S256 hash>    (required)
    &code_challenge_method=S256    (required)
    &redirect_uri=<registered URI> (optional, but exchange must match)
    &state=<random string>         (optional, CSRF protection)
```

CORS pre-flight (OPTIONS) is not supported. After consent the user lands on
`<redirect_uri>?code=<code>&state=<state>`.

## Step 2 — Token exchange

```
POST https://diauth.garmin.com/di-oauth2-service/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code
client_id=<consumer key>
client_secret=<consumer secret>
code=<authorization code>
code_verifier=<the verifier from step 1>
redirect_uri=<must match step 1 if it was sent>
```

Response:

```json
{
  "access_token": "...",
  "expires_in": 86400,
  "token_type": "bearer",
  "refresh_token": "...",
  "scope": "PARTNER_WRITE PARTNER_READ CONNECT_READ CONNECT_WRITE",
  "jti": "...",
  "refresh_token_expires_in": 7775998
}
```

Notes from the spec:

- `scope` is a fixed, app-level value — API sections are chosen at app
  creation and users pick data permissions on the consent screen. There is no
  per-request scope parameter.
- Access tokens last 24h (`expires_in`); refresh tokens ~90 days
  (`refresh_token_expires_in`).
- Garmin recommends refreshing **at least 600 seconds before** expiry
  (`GarminConnection.is_token_expired` defaults to that leeway).

## Refresh

Same token URL with `grant_type=refresh_token`, `client_id`, `client_secret`,
`refresh_token`. **A new refresh token is returned on every refresh** — persist
both tokens together and treat the old refresh token as spent.

## Calling the API

`Authorization: Bearer <access_token>` against
`https://apis.garmin.com/wellness-api/rest/...`.

## User endpoints

- `GET /wellness-api/rest/user/id` → `{"userId": "..."}` — the stable "API
  User ID": survives disconnect/reconnect and is shared across the partner's
  programs. Push/ping notifications carry it, so store it for routing.
- `GET /wellness-api/rest/user/permissions` → JSON array like
  `["ACTIVITY_EXPORT", "HEALTH_EXPORT", ...]` — what this user actually
  granted. Post-consent changes arrive via the User Permission webhook.
- `DELETE /wellness-api/rest/user/registration` — **must** be called when the
  partner app offers its own disconnect/delete-account mechanism (this is what
  `garmin.oauth.revoke` does).
