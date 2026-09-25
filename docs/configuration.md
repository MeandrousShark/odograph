# Configuration reference

Follow the [README setup steps](../README.md#quick-start) for a new install,
including your timezone and administrator setup. Use this reference when you
want to enable an optional feature or change a default.

Recreate the `app` service after changing operator settings in `.env`.
Personal preferences are saved under **Settings** and take effect without a restart. A plain restart does not
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
| `POSTGRES_PASSWORD` | generated, required | URI-safe privileged setup and backup password used to initialize the Compose database and construct `DATABASE_URL`. Changing it does not change the role password in an existing database volume. |
| `DATABASE_URL` | set by `compose.yaml` | Privileged PostgreSQL URL used for migrations, managed-role setup, backup, and recovery. Normal requests and workers use generated restricted connections. Set it directly only outside the canonical Compose stack. |
| `DISPLAY_TZ` | `UTC` in the app | Initial IANA timezone suggestion for first-account setup, and the one-time timezone import for an upgraded account. After setup, change the account timezone in Settings. |
| `FORWARDED_ALLOW_IPS` | empty in the app, `*` in generated config | Immediate proxy IPs or CIDRs uvicorn may trust for forwarded client and scheme headers. The generated wildcard is safe only with the shipped loopback-bound port. See [Reverse proxy and TLS](reverse-proxy.md#trusting-forwarded-headers). |

Compose fixes the database name and role to `mileage`. `COMPOSE_PROJECT_NAME`
is a standard Compose variable that namespaces containers, networks, and named
volumes. It is useful for isolated restore drills, but it is not an Odograph
application setting.

## Account ownership and database roles

The supported baseline is PostgreSQL 16 with PostGIS. Startup now runs the
ownership migrations and validates the live `ownership-activated-v1` security
contract before opening restricted pools. The application remains a
single-account installation: the singleton guard stays in place, new-account
registration stays closed after setup, and invitations are not available.
PostgreSQL row-level security is enabled and forced on every account-owned
table. The runtime role reads and changes only the rows of the account bound
to its transaction, and none without one. Personal queries also filter their
authenticated owner explicitly. Control and reference tables, such as
accounts and the reference mileage rates, have no row-level security.

Startup uses `DATABASE_URL` briefly for migrations and managed setup. Its role
must be a superuser or have `BYPASSRLS`: forced row-level security also
applies to the owner of the tables, so startup refuses to migrate as any other
role rather than let a data migration silently skip rows. Setup creates
`odograph_control` for identity work and `odograph_runtime` for account work,
then closes the privileged setup connection. `odograph_migrate` owns the
application objects; `odograph_bootstrap` owns narrow account/admission
functions. Both owner roles are non-login roles. Runtime and control cannot
own application tables, bypass row security, create schema objects, truncate
protected tables, or assume an owner role.

Restricted login credentials are generated and stored in protected database
state, included in full backups. Operators do not maintain extra passwords or
connection URLs. Restarts reuse this state and validate its database identity,
roles, grants, ownership, functions, policies, and row-level security flags.
Unsafe grants, missing state, or the wrong security contract stop startup
without falling back to privileged request handling. Keep full backups and encrypted instance
configuration protected: the database archive includes managed credentials.

The canonical Compose database permits this setup. An external PostgreSQL
service must permit migrations and management of these fixed roles; a
restricted connection URL alone cannot initialize the application. Reserve
`odograph_control`, `odograph_runtime`, `odograph_migrate`, and
`odograph_bootstrap` for one Odograph installation per PostgreSQL cluster.
Use separate clusters for additional installations or restore drills; separate
databases in the same cluster share these role names and passwords. Setup and
restore refuse to change roles with dependencies, database grants, or
database-specific settings in another database. Run a
backup and the supported [upgrade procedure](upgrading.md) before upgrading an
existing installation. Ambiguous legacy ownership fails closed and requires
repair before migration.

First-account creation atomically installs the account and its fixed defaults.
An empty instance admits no personal data or tracking writes before setup.
Existing one-account data keeps its IDs and ownership, while legacy effective
preferences and ingest credentials are imported once.

Use the [backup and fresh-target restore commands](backups.md) to preserve
managed-role metadata and restore the required cluster roles, ownership, and
grants. A database archive alone does not contain cluster-wide role definitions.

## Personal preferences

**Settings > Time zone and notifications** owns the timezone, notification destinations,
notification choices, and local delivery hours. Mileage rates and default
vehicle behavior are also account-owned settings. Display, report boundaries,
manual trip and odometer input, and reminder schedules use the saved account
timezone.

On upgrade, the existing account receives the effective values of `DISPLAY_TZ`,
`NTFY_TOPIC`, the personal `EMAIL_*` settings, reminder hours/switches, and
valid `MILEAGE_RATE_<YEAR>` overrides exactly once. Subsequent `.env` edits do
not overwrite them. On a fresh account, choose the timezone during setup and
enable desired notifications in Settings; destinations and notification
choices begin empty/off. Service hosts, transport credentials, sender address,
detector parameters, and worker intervals remain operator configuration.

## Authentication and ingest

| Variable | Default | Purpose |
|---|---:|---|
| `INGEST_USERNAME` | `owntracks` | One-time legacy shared-login import on upgrade; new devices use usernames issued in Settings > Tracking. |
| `INGEST_PASSWORD` | generated baseline | One-time legacy shared-secret import on upgrade. Editing it later does not rotate or restore the saved credential. New devices use passwords issued in Tracking. |
| `SESSION_SECRET` | generated, required | Signs browser session cookies. Replacing it signs out every browser. |
| `INITIAL_ADMIN_SIGNUP` | `0` when absent | `1` permits creation of the first administrator only while no account exists. Generated fresh-install config sets it to `1`; the durable account row closes signup permanently. |
| `LOGIN_AUTH_MAX_FAILURES` | `10` | Failed local credential checks allowed per client within the login window. |
| `LOGIN_AUTH_WINDOW_S` | `900` | Local login, signup, password, and account-credential limiter window in seconds. |
| `INGEST_AUTH_MAX_FAILURES` | `10` | Failed OwnTracks authentication attempts allowed per client within the ingest window. Blocked clients receive `503` with `Retry-After` before credential verification or body reads, even when retrying with correct credentials. |
| `INGEST_AUTH_WINDOW_S` | `900` | Ingest authentication limiter window in seconds. |
| `INGEST_MAX_BODY_BYTES` | `65536` | Maximum OwnTracks request body. Oversized authenticated bodies are logged and dropped with a successful empty response so a phone does not retry-loop poison input. |

Each application process also admits at most two concurrent ingest credential
verifications. When both slots are busy, additional requests receive `503`
with `Retry-After: 1` before verification or body reads. Cancelled requests keep
their slot until verification finishes; a device can retry afterward.

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
| `DEV_NO_AUTH` | `0` | Development only. `1` binds a real synthetic account; it refuses an existing non-synthetic account. Never enable it on a deployed instance. |

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
| `MILEAGE_RATE_<YEAR>` | database rate | Legacy one-time upgrade input for the existing account. A valid positive value replaces that year's midyear split during import; later edits have no effect. Manage saved rates in Settings. |

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

Set `NTFY_URL` for the shared transport, then save your topic and reminder
choices in Settings. `NTFY_TOPIC` is only a legacy upgrade input.

| Variable | Default | Purpose |
|---|---:|---|
| `NTFY_URL` | unset | ntfy server base URL. |
| `NTFY_TOPIC` | unset | Legacy one-time destination import; manage the saved topic in Settings. |
| `NTFY_TOKEN` | unset | Optional bearer token. |
| `NTFY_USERNAME` | unset | Optional Basic-auth username. |
| `NTFY_PASSWORD` | unset | Optional Basic-auth password. Username/password take precedence over a token when both are set. |
| `APP_URL` | unset | Public HTTPS base URL placed in reminder and email confirmation links. It does not configure the reverse proxy. |

### Email

Set `SMTP_HOST` and `EMAIL_FROM` for the shared transport, and set `APP_URL` to
the public HTTPS base URL. These three values are required to send current
login-email verification and change challenges. Challenges expire after 30
minutes. Email challenges are sent to the address being verified or changed;
they do not use `EMAIL_TO`. Save digest recipients and delivery choices in
Settings. The personal values below are legacy one-time upgrade inputs, not
live overrides.

| Variable | Default | Purpose |
|---|---:|---|
| `SMTP_HOST` | unset | SMTP server hostname. |
| `SMTP_PORT` | `587` | SMTP server port. |
| `SMTP_USERNAME` | unset | Optional SMTP username. |
| `SMTP_PASSWORD` | unset | Optional SMTP password. |
| `SMTP_SECURITY` | `starttls` | `starttls`, `ssl`, or `none`. Use `none` only for a trusted local relay. |
| `SMTP_TLS_INSECURE` | `0` | `1` disables certificate verification. Use only for a localhost bridge with a self-signed certificate. |
| `EMAIL_FROM` | unset | Message sender address. |
| `EMAIL_TO` | unset | Legacy one-time import of the email digest recipient; manage the saved recipient in Settings. |
| `EMAIL_WEEKLY_NUDGE` | `0` | Sends the weekly unclassified-trip reminder by email. |
| `EMAIL_MONTHLY_SUMMARY` | `1` | Sends monthly mileage summaries when email is enabled. |
| `EMAIL_FILING_REMINDER` | `1` | Sends the annual filing reminder when email is enabled. |
| `EMAIL_ODOMETER_REMINDER` | `0` | Sends quarterly odometer reminders by email. |

## Worker scheduling

Personal hours use the saved account timezone. The reminder hour/switch
variables below are legacy one-time upgrade inputs; edit them in Settings
after migration. Interval and debounce values remain live operator settings
and use seconds.

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
