"""Service URLs, summary-type identifiers, and permissions for the Garmin Health API.

Garmin's Connect Developer Program docs are partner-gated; the OAuth2 PKCE
specification and endpoint shapes are summarized in ``docs/garmin/``.
"""

# User-facing consent page (GET). CORS pre-flight is not supported.
OAUTH_AUTHORIZATION_URL = "https://connect.garmin.com/oauth2Confirm"
# Token endpoint (POST, form-encoded) for both authorization_code and
# refresh_token grants.
OAUTH_TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"

API_BASE_URL = "https://apis.garmin.com"
WELLNESS_API_PATH = "wellness-api/rest"

# Health API summary types, as they appear in both REST paths
# (``/wellness-api/rest/{type}``) and push/ping notification payload keys.
SUMMARY_DAILIES = "dailies"
SUMMARY_EPOCHS = "epochs"
SUMMARY_SLEEPS = "sleeps"
SUMMARY_BODY_COMPS = "bodyComps"
SUMMARY_STRESS_DETAILS = "stressDetails"
SUMMARY_USER_METRICS = "userMetrics"
SUMMARY_PULSE_OX = "pulseox"
SUMMARY_RESPIRATION = "respiration"
SUMMARY_HRV = "hrv"
SUMMARY_ACTIVITIES = "activities"
SUMMARY_ACTIVITY_DETAILS = "activityDetails"
SUMMARY_MANUALLY_UPDATED_ACTIVITIES = "manuallyUpdatedActivities"

# The pull (non-backfill) endpoints reject upload-time windows longer than
# 24 hours; clients must chunk longer ranges.
MAX_PULL_WINDOW_SECONDS = 24 * 60 * 60
# Backfill requests are capped at 90 days per request.
MAX_BACKFILL_WINDOW_SECONDS = 90 * 24 * 60 * 60

# Permissions granted by the user at consent time, returned by
# ``GET /wellness-api/rest/user/permissions``. HEALTH_EXPORT covers the
# wellness summaries; ACTIVITY_EXPORT covers activities.
PERMISSION_ACTIVITY_EXPORT = "ACTIVITY_EXPORT"
PERMISSION_HEALTH_EXPORT = "HEALTH_EXPORT"
PERMISSION_WORKOUT_IMPORT = "WORKOUT_IMPORT"
PERMISSION_COURSE_IMPORT = "COURSE_IMPORT"
PERMISSION_MCT_EXPORT = "MCT_EXPORT"

# Human-readable, stored in Record.sourceName. The machine identifier is
# ``healthdatamodel.constants.DataSource.GARMIN`` (added in 0.6.0).
SOURCE_NAME = "Garmin"
