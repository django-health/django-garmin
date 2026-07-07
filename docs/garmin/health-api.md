# Garmin Health API (summary)

Summarized from the partner-gated Garmin Health API documentation. The Health
API delivers all-day wellness data as JSON "summaries"; the Activity API
(same wellness-api base) covers discrete workouts.

Base: `https://apis.garmin.com/wellness-api/rest`

## Delivery models

1. **Push** — Garmin POSTs full summaries to your configured endpoints when a
   user's device syncs. Payload: a JSON object keyed by summary type, each
   entry a summary dict plus `userId` / `userAccessToken`.
2. **Ping** — Garmin POSTs the same shape, but each entry carries a
   `callbackURL` instead of inline data; you GET that URL (with the matching
   user's bearer token) to fetch the summaries.
3. **Pull** — on-demand GETs (below). Windows filter by **upload time** (when
   the device synced to Garmin Connect), not the summary's own timestamps,
   and are capped at **24 hours** per request.

Webhook endpoints are configured per summary type in the developer portal.
There is no verification handshake and no shared-secret header; Garmin expects
a fast HTTP 200 and disables endpoints that repeatedly fail or time out.

## Pull endpoints

```
GET /{summaryType}?uploadStartTimeInSeconds=<epoch>&uploadEndTimeInSeconds=<epoch>
```

Summary types: `dailies`, `epochs`, `sleeps`, `bodyComps`, `stressDetails`,
`userMetrics`, `pulseox`, `respiration`, `hrv`, `activities`,
`activityDetails`, `manuallyUpdatedActivities`.

## Backfill

```
GET /backfill/{summaryType}?summaryStartTimeInSeconds=<epoch>&summaryEndTimeInSeconds=<epoch>
```

Returns `202 Accepted` with no body; Garmin re-generates the historic data
(windows capped at 90 days per request) and delivers it **asynchronously
through your push/ping endpoints**. This is the only way to reach data older
than what users recently uploaded.

## Summary shapes (fields this package maps)

Times are unix seconds UTC (`startTimeInSeconds`) with a separate local-zone
`startTimeOffsetInSeconds`. `summaryId` is stable per summary and re-sent when
a summary is updated (e.g. more data synced for the same day) — ingest keys
record IDs off it so updates upsert instead of duplicating.

### dailies

`steps`, `distanceInMeters`, `activeKilocalories`, `bmrKilocalories`,
`floorsClimbed`, `restingHeartRateInBeatsPerMinute`,
`min/average/maxHeartRateInBeatsPerMinute`,
`timeOffsetHeartRateSamples` (offset-seconds → bpm map),
`moderateIntensityDurationInSeconds`, `vigorousIntensityDurationInSeconds`,
`averageStressLevel`, ...

### epochs

15-minute activity slices: `steps`, `distanceInMeters`, `activeKilocalories`,
`met`, `intensity`, `activeTimeInSeconds`. Same metrics the dailies aggregate —
ingesting both double-counts.

### sleeps

`durationInSeconds`, per-stage duration fields, and `sleepLevelsMap`:
`{"deep"|"light"|"rem"|"awake": [{"startTimeInSeconds", "endTimeInSeconds"}]}`.
`validation` indicates data quality (e.g. `ENHANCED_FINAL`).

### bodyComps

`measurementTimeInSeconds`, `weightInGrams`, `bodyFatInPercent`,
`bodyMassIndex`, `muscleMassInGrams`, `boneMassInGrams`, `bodyWaterInPercent`.

### pulseox

`timeOffsetSpo2Values` (offset-seconds → percentage map), `onDemand`.

### activities

`activityId`, `activityName`, `activityType`, `durationInSeconds`,
`distanceInMeters`, `activeKilocalories`,
`averageHeartRateInBeatsPerMinute`, `steps`, `deviceName`,
`averageSpeedInMetersPerSecond`, ...
