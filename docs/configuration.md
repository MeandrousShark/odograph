# Configuration reference

Follow the [README setup steps](../README.md#quick-start) for a new install,
including your timezone and administrator setup. Use this reference when you
want to enable an optional feature or change a default.

Recreate the `app` service after changing `.env`. A plain restart does not
reload container environment variables:

```sh
docker compose up -d app
# or: podman-compose up -d app
```

The generator refuses to replace an existing `.env`, writes it with mode 600,
and never prints generated values. Boolean switches use `1` for enabled and `0`
for disabled.

## Baseline and database

| Variable | Default | Purpose |
|---|---:|---|
| `POSTGRES_PASSWORD` | generated, required | URI-safe password used to initialize the Compose database and construct `DATABASE_URL`. Changing it does not change the role password in an existing database volume. |
| `DATABASE_URL` | set by `compose.yaml` | PostgreSQL connection URL required by the application. Set it directly only outside the canonical Compose stack. |
| `DISPLAY_TZ` | `UTC` in the app | IANA timezone used for display, report boundaries, reminder schedules, and manual trip input. The generated baseline asks the operator to choose it explicitly. |
| `FORWARDED_ALLOW_IPS` | empty in the app, `*` in generated config | Immediate proxy IPs or CIDRs uvicorn may trust for forwarded client and scheme headers. The generated wildcard is safe only with the shipped loopback-bound port. See [Reverse proxy and TLS](reverse-proxy.md#trusting-forwarded-headers). |

Compose fixes the database name and role to `mileage`. `COMPOSE_PROJECT_NAME`
is a standard Compose variable that namespaces containers, networks, and named
volumes. It is useful for isolated restore drills, but it is not an Odograph
application setting.

## Authentication and ingest

| Variable | Default | Purpose |
|---|---:|---|
| `INGEST_USERNAME` | `owntracks` | HTTP Basic username accepted by `/ingest`. |
| `INGEST_PASSWORD` | generated, required | HTTP Basic password shared by OwnTracks devices. |
| `SESSION_SECRET` | generated, required | Signs browser session cookies. Replacing it signs out every browser. |
| `INITIAL_ADMIN_SIGNUP` | `0` when absent | `1` permits creation of the first administrator only while no account exists. Generated fresh-install config sets it to `1`; the durable account row closes signup permanently. |
| `LOGIN_AUTH_MAX_FAILURES` | `10` | Failed local credential checks allowed per client within the login window. |
| `LOGIN_AUTH_WINDOW_S` | `900` | Local login, signup, password, and account-credential limiter window in seconds. |
| `INGEST_AUTH_MAX_FAILURES` | `10` | Failed OwnTracks authentication attempts allowed per client within the ingest window. Correct credentials are never throttled. |
| `INGEST_AUTH_WINDOW_S` | `900` | Ingest authentication limiter window in seconds. |
| `INGEST_MAX_BODY_BYTES` | `65536` | Maximum OwnTracks request body. Oversized authenticated bodies are logged and dropped with a successful empty response so a phone does not retry-loop poison input. |

OIDC is optional. Set all three required provider values together, register
`https://your-domain/auth/callback`, then link the identity from Account
Security while signed in locally.

| Variable | Default | Purpose |
|---|---:|---|
| `OIDC_ISSUER` | unset | Exact provider issuer URL. |
| `OIDC_CLIENT_ID` | unset | Provider client ID. |
| `OIDC_CLIENT_SECRET` | unset | Provider client secret. |
| `ALLOWED_EMAIL` | unset | Compatibility gate for the one-time claim of an upgraded OIDC-only installation. It does not authorize or link normal OIDC login. |
| `ADMIN_TOKEN` | obsolete and ignored | Accepted in an old `.env` for upgrade compatibility. It enables no route and its value is never reported. Remove it when convenient. |
| `DEV_NO_AUTH` | `0` | Development only. `1` disables UI authentication. Never enable it on a deployed instance. |

## Detector behavior

These values affect future detector runs. Changing detector parameters can
change how new or reprocessed points are grouped. It does not erase raw source
points.

| Variable | Default | Purpose |
|---|---:|---|
| `MAX_ACCURACY_M` | `100` | Drops fixes whose reported accuracy is worse than this many meters. |
| `MAX_SPEED_MS` | `60` | Drops points that imply an implausible speed from the last kept point. |
| `STAY_RADIUS_M` | `150` | Maximum radius for a stationary stay cluster. |
| `STAY_MIN_DURATION_S` | `300` | Minimum duration for stationary and on-foot stays. |
| `WALK_MAX_SPEED_MS` | `2.0` | Maximum sustained speed treated as an on-foot stay. |
| `MIN_TRIP_DISTANCE_M` | `300` | Rejects shorter movement between stays as jitter rather than a trip. |
| `GAP_FLAG_THRESHOLD_S` | `600` | Marks a trip when consecutive kept points are farther apart than this many seconds. |
| `FULL_REPROCESS_WARN_POINTS` | `500000` | Logs a warning before a full-device reprocess at or above this point count. `0` disables the warning. |

## UI, reports, and imports

| Variable | Default | Purpose |
|---|---:|---|
| `TRIPS_PAGE_SIZE` | `25` | Trip rows loaded per month before the Load more control appears. Values below 1 become 1. |
| `MISSING_TRIP_GAP_M` | `1000` | Spatial gap that flags a possible missing trip between detected trips. `0` disables the feature. |
| `MAP_TILE_URL` | OpenStreetMap tile URL | Tile template used by live trip maps. Its origin also feeds the image Content Security Policy. |
| `MAP_TILE_ATTRIBUTION` | OpenStreetMap attribution | HTML attribution rendered with live map tiles. Keep the selected provider's required attribution. |
| `HSTS_MAX_AGE` | `0` | Adds an HSTS header on requests already seen as HTTPS. `0` disables it. Prefer setting HSTS at the reverse proxy. |
| `PORTABLE_IMPORT_MAX_BYTES` | `52428800` | Maximum uploaded portable JSON document size in bytes. |
| `ACCOUNT_AVATAR_MAX_BYTES` | `512000` | Configurable maximum uploaded account avatar size in bytes. Images are also capped at 16,777,216 total decoded pixels and 8192 pixels per side. |
| `MILEAGE_RATE_<YEAR>` | database rate | Positive dollars-per-mile override for one year, for example `MILEAGE_RATE_2026=0.725`. It replaces any midyear split for that year. Invalid values are ignored with a warning. |

## Retention

| Variable | Default | Purpose |
|---|---:|---|
| `RAW_MESSAGE_RETENTION_DAYS` | `365` | Deletes old rows only from the raw ingest payload table. Values at or below 0 disable pruning. Points, trips, and other derived data are unaffected. Backup retention is independent. |

See [Backups and disaster recovery](backups.md#retention) before shortening
retention for privacy reasons. Older backups can still contain rows already
pruned from the live database.

## External services

All services in this section are disabled when their enabling values are
unset. Read [Privacy and external services](privacy.md) before sending location
or account-related data to a hosted provider.

### Road snapping and manual trip routing

| Variable | Default | Purpose |
|---|---:|---|
| `OSRM_URL` | unset | OSRM base URL. Enables both GPS-trace road snapping and routed manual trip entry. In the optional Compose profile use `http://osrm:5000`. |
| `OSRM_DATASET` | unset | Basename of the prepared dataset served by the Compose `osrm` profile. It is checked only when that profile starts. |
| `OSRM_MIN_CONFIDENCE` | `0.5` | Minimum OSRM match confidence accepted for a snapped route. |
| `OSRM_MAX_COORDS` | `250` | Maximum coordinates sent in one OSRM match request. |

Prepare a regional dataset before setting these values. See
[Self-hosted OSRM road-snapping](osrm.md).

### Reverse geocoding

| Variable | Default | Purpose |
|---|---:|---|
| `GEOCODE_PROVIDER` | unset | `geoapify` or `nominatim`. For compatibility, an API key with no provider selects Geoapify. |
| `GEOCODE_API_KEY` | unset | Geoapify API key. |
| `GEOCODE_NOMINATIM_URL` | unset | Base URL of an operator-controlled Nominatim instance. The public OpenStreetMap Nominatim service is not a supported bulk backend. |
| `GEOCODE_OMIT_COUNTRY` | `United States of America` | Exact trailing country label removed from returned addresses. Set an empty value to retain it. |
| `GEOCODE_MIN_INTERVAL_S` | `1.0` | Minimum interval between provider requests. |

### ntfy

Set both `NTFY_URL` and `NTFY_TOPIC` to enable ntfy reminders.

| Variable | Default | Purpose |
|---|---:|---|
| `NTFY_URL` | unset | ntfy server base URL. |
| `NTFY_TOPIC` | unset | Destination topic. |
| `NTFY_TOKEN` | unset | Optional bearer token. |
| `NTFY_USERNAME` | unset | Optional Basic-auth username. |
| `NTFY_PASSWORD` | unset | Optional Basic-auth password. Username/password take precedence over a token when both are set. |
| `APP_URL` | unset | Public HTTPS base URL placed in reminder links. It does not configure the reverse proxy. |

### Email

Set `SMTP_HOST`, `EMAIL_FROM`, and `EMAIL_TO` together to enable the email
worker.

| Variable | Default | Purpose |
|---|---:|---|
| `SMTP_HOST` | unset | SMTP server hostname. |
| `SMTP_PORT` | `587` | SMTP server port. |
| `SMTP_USERNAME` | unset | Optional SMTP username. |
| `SMTP_PASSWORD` | unset | Optional SMTP password. |
| `SMTP_SECURITY` | `starttls` | `starttls`, `ssl`, or `none`. Use `none` only for a trusted local relay. |
| `SMTP_TLS_INSECURE` | `0` | `1` disables certificate verification. Use only for a localhost bridge with a self-signed certificate. |
| `EMAIL_FROM` | unset | Message sender address. |
| `EMAIL_TO` | unset | Message recipient address. |
| `EMAIL_WEEKLY_NUDGE` | `0` | Sends the weekly unclassified-trip reminder by email. |
| `EMAIL_MONTHLY_SUMMARY` | `1` | Sends monthly mileage summaries when email is enabled. |
| `EMAIL_FILING_REMINDER` | `1` | Sends the annual filing reminder when email is enabled. |
| `EMAIL_ODOMETER_REMINDER` | `0` | Sends quarterly odometer reminders by email. |

## Worker scheduling

Hours use `DISPLAY_TZ`. Interval and debounce values are seconds.

| Variable | Default | Purpose |
|---|---:|---|
| `DETECT_DEBOUNCE_S` | `60` | Quiet period after the newest point before detection runs. |
| `DETECT_SWEEP_S` | `900` | Periodic detector catch-up interval. |
| `SNAP_DEBOUNCE_S` | `15` | Quiet period before pending routes are sent to OSRM. |
| `SNAP_SWEEP_S` | `300` | Periodic road-snapping catch-up interval. |
| `GEOCODE_DEBOUNCE_S` | `15` | Quiet period before pending endpoints are geocoded. |
| `GEOCODE_SWEEP_S` | `300` | Periodic geocoding catch-up interval. |
| `NUDGE_WEEKLY_HOUR` | `18` | Sunday hour for the weekly unclassified-trip reminder. |
| `ODOMETER_REMINDER` | enabled when ntfy is enabled | Controls the quarterly ntfy odometer reminder independently. |
| `ODOMETER_REMINDER_HOUR` | `9` | Local hour on the first day of a calendar quarter. |
| `EMAIL_DIGEST_HOUR` | `9` | Local hour for monthly, annual filing, and enabled email reminder delivery. |
| `EMAIL_FILING_REMINDER_MMDD` | `01-15` | Month and day for the annual filing reminder. |

## Release-managed identity

Published images set `APP_VERSION` and `APP_GIT_REVISION` at build time. They
appear only on authenticated diagnostics surfaces. Operators should not
override them: their purpose is to identify the exact image and source commit
that are running. Source builds honestly default to `dev` and `unknown`.
