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
contract before opening restricted pools. Normal installations remain
single-account: the database singleton and admin-only account constraints stay
in place, and public signup closes after the first administrator is created.
Invitation redemption and passwordless OIDC provisioning are exercised only
in a controlled activated fixture. A separately reviewed activation migration
and release are required before those flows are available in normal installs.
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

First-account creation atomically installs the administrator and fixed
defaults. An empty instance admits no personal data or tracking writes before
setup. Existing account data keeps its IDs and ownership, while legacy
effective preferences and ingest credentials are imported once. The activated
fixture also checks that invited accounts receive separate defaults and
account-owned data.

Use the [backup and fresh-target restore commands](backups.md) to preserve
managed-role metadata and restore the required cluster roles, ownership, and
grants. A database archive alone does not contain cluster-wide role definitions.

### Logical storage accounting

Schema 40 records versioned logical usage for stored personal data, with
separate actual, reserved, raw-message and optional-enhancement byte counts.
Each retained point reserves capacity for detector output, including a
durable high-water mark for copied device labels. These counts describe
stored-value charges, not PostgreSQL disk usage. Durable geocode retry and
discovery rows count toward actual usage. Schema 42 enforces funded
account/raw/enhancement allowances using transaction-final net growth.

Startup verifies the counters against stored data once through the privileged
setup connection. This scan can increase startup time for large histories.
Counter drift stops startup; the supported fresh-target restore procedure
reconciles restored counters after validating the security contract. Normal
request connections cannot change counters directly or repair drift.

| Variable | Default | Purpose |
| --- | ---: | --- |
| `STORAGE_ACCOUNT_LIMIT_BYTES` | `2147483648` (2 GiB) | Default allowance for each account, including stored data and prepaid core processing. |
| `STORAGE_RAW_LIMIT_BYTES` | `268435456` (256 MiB) | Raw messages and their exact-replay receipts; a subset of the account allowance. |
| `STORAGE_ENHANCEMENT_LIMIT_BYTES` | `134217728` (128 MiB) | Optional snapped geometry and cached addresses; also a subset of the account allowance. |
| `STORAGE_INSTANCE_BUDGET_BYTES` | `12884901888` (12 GiB) | Total funded logical grant budget. This is not database free space. |
| `STORAGE_INSTANCE_RESERVE_BYTES` | `2147483648` (2 GiB) | Unallocated logical reserve, unavailable for ordinary account growth. |

All values must be positive integers. Raw and enhancement limits cannot exceed
the account limit; reserve must be smaller than budget. At validated restart,
the default grant applies to every extant account, including disabled accounts.
The sum of grants must fit budget minus reserve, or startup refuses to serve.
Account creation acquires a funded grant before any account state commits.
Disabled accounts retain their grant until actual purge. These controls do not
enable multi-account activation or provide a browser quota editor.

Lowering allowances never removes history. Positive net growth pauses at a
ceiling, while reads, exports, sign-in, recovery, cleanup and zero/negative net
changes remain available. Reserved detector output for accepted points remains
funded. Raise limits through a validated restart with enough grant budget, or
deliberately clean up unneeded data; eligible work resumes when capacity returns.
Settings shows actual/reserved usage, each subset, warnings from 80%, and
over-budget state. Operator diagnostics expose aggregate grants and counts,
without another account's history.

At account or enhancement capacity, new optional provider work pauses and
existing enhancements remain. Trips use raw routes/distances and coordinate
fallbacks as needed. Provider routes exceeding 100,000 vertices and cached
addresses exceeding 4,096 UTF-8 bytes are rejected, never clipped or stored as
false misses. Manual route additions must fit the actual account allowance.

After fresh tracking admission, an identical retained raw message succeeds
without adding raw data, points or receipt charges, including at a ceiling.
Recognition uses a digest search plus exact canonical PostgreSQL JSON text
equality in the durable device namespace, so changed fields or numeric
representations remain new messages. Raw retention cascades receipt deletion
and refunds both; replay never extends retention. After deletion, the same
request needs allowance again and may receive retryable HTTP 503 with
`Retry-After`. With retention disabled, raw data and receipts remain charged.

Credential rotation preserves a resolved device's replay namespace. Unresolved
legacy raw-only messages use their credential's stable public ID; credential
replacement or conversion to a device starts a new namespace. Upgrade backfills
known durable-device history only: old unresolved raw rows lack a recorded
credential, so their replay recognition starts with newly stored receipts.
Existing point deduplication remains unchanged, and revoked/disabled credentials
cannot bypass fresh admission through replay. OwnTracks iOS retry evidence is
version-specific; recovery does not depend on clients honoring `Retry-After`.

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

### Shared capacity

Run one application process with its background workers in that process.
`WEB_CONCURRENCY` and `UVICORN_WORKERS` must be unset or `1`. Independent
replicas or separately launched workers do not share these in-memory limits;
startup cannot detect every externally launched duplicate process.

The restricted runtime pool retains at most six connections and the control
pool at most five.
Reservations prevent an upload, report or background job from consuming every
connection. Expensive operations include complete reports/exports, large trip
selections and structural corrections. The full-week dashboard and its tag
refreshes have a separate navigation reservation. Their owners cover parsing,
rendering and response transmission. Queued operations hold bounded metadata, without
upload bodies, database connections or lifecycle leases. An account may have
only one active or pending request across navigation and foreground together,
and one in each other account lane. Waiting accounts use FIFO order; repeated requests from an active or queued account receive busy.

| Variable | Default | Purpose |
|---|---:|---|
| `CAPACITY_INGEST_SLOTS` | `2` | Runtime connections reserved for authenticated intake. |
| `CAPACITY_ROUTINE_SLOTS` | `1` | Runtime connections reserved for small reads, edits and account binding. |
| `CAPACITY_NAVIGATION_SLOTS` | `1` | Concurrent full-week dashboard or tag refresh, including rendering and send. |
| `CAPACITY_FOREGROUND_SLOTS` | `1` | Concurrent other expensive foreground operation, including rendering and send. |
| `CAPACITY_BACKGROUND_SLOTS` | `1` | Concurrent account job across all background worker types. |
| `CAPACITY_INGEST_IDENTITY_SLOTS` | `1` | Control connections reserved for ingest credential lookup. |
| `CAPACITY_IDENTITY_SLOTS` | `1` | Control connections for ordinary identity work and worker enumeration. |
| `CAPACITY_LIFECYCLE_SLOTS` | `1` | Control connections for access, credential and lifecycle mutations. |
| `CAPACITY_MAIL_SLOTS` | `2` | Control connections reserved for admitted security-mail final checks. |
| `CAPACITY_AUTH_INGEST_SLOTS` | `2` | Concurrent ingest authentication operations. |
| `CAPACITY_AUTH_INTERACTIVE_SLOTS` | `1` | Concurrent interactive form parsing, password work or OIDC exchange. |
| `CAPACITY_AUTH_INGEST_PENDING` | `8` | Maximum ingest authentication requests waiting for a verifier. |
| `CAPACITY_INGEST_PENDING` | `4` | Maximum waiting authenticated intake tickets. |
| `CAPACITY_ROUTINE_PENDING` | `4` | Maximum waiting ordinary runtime borrows. |
| `CAPACITY_NAVIGATION_PENDING` | `4` | Maximum waiting navigation tickets from distinct accounts. |
| `CAPACITY_FOREGROUND_PENDING` | `4` | Maximum waiting expensive-operation tickets. |
| `CAPACITY_IDENTITY_PENDING` | `4` | Maximum waiting ordinary control borrows. |
| `CAPACITY_INGEST_IDENTITY_PENDING` | `1` | Maximum waiting ingest credential lookup. |
| `CAPACITY_AUTH_INGEST_WAIT_S` | `1` | Maximum ingest authentication admission wait in seconds. |
| `CAPACITY_INGEST_IDENTITY_WAIT_S` | `0.25` | Maximum ingest credential lookup wait in seconds. |
| `CAPACITY_INGEST_WAIT_S` | `0.25` | Total intake admission wait in seconds. |
| `CAPACITY_ROUTINE_WAIT_S` | `1` | Total ordinary runtime admission wait in seconds. |
| `CAPACITY_NAVIGATION_WAIT_S` | `1` | Total navigation admission wait in seconds, at most one. |
| `CAPACITY_FOREGROUND_WAIT_S` | `2` | Total expensive-operation admission wait in seconds. |
| `CAPACITY_IDENTITY_WAIT_S` | `1` | Total ordinary control admission wait in seconds. |
| `CAPACITY_AUTH_BODY_TIMEOUT_S` | `15` | Total interactive form receipt deadline in seconds. |
| `CAPACITY_INGEST_BODY_TIMEOUT_S` | `15` | Total authenticated intake body receipt deadline in seconds. |
| `CAPACITY_IMPORT_BODY_TIMEOUT_S` | `60` | Total owned upload or full-result form receipt deadline in seconds. |
| `CAPACITY_RESPONSE_TIMEOUT_S` | `60` | Total owned response transmission deadline in seconds. |
| `CAPACITY_ROUTINE_SQL_TIMEOUT_S` | `5` | Transaction-local routine/intake statement timeout in seconds. |
| `CAPACITY_OPERATION_SQL_TIMEOUT_S` | `15` | Transaction-local navigation/foreground/background statement timeout in seconds. |
| `CAPACITY_LOCK_TIMEOUT_S` | `1` | Transaction-local runtime lock timeout in seconds. |
| `CAPACITY_AUTH_FORM_MAX_BYTES` | `65536` | Maximum authentication and small edit form bytes; input is never truncated. |
| `CAPACITY_BASIC_HEADER_MAX_BYTES` | `8192` | Maximum Basic credential header bytes before decoding. |
| `CAPACITY_MULTIPART_OVERHEAD_BYTES` | `65536` | Multipart envelope allowance above the portable bundle byte cap. |
| `CAPACITY_MULTIPART_MAX_FIELDS` | `16` | Maximum fields in an owned multipart upload. |
| `CAPACITY_MULTIPART_MAX_FILES` | `1` | Maximum files in an owned multipart upload. |

Slot counts must be positive. Runtime reservations cannot exceed six and
control reservations cannot exceed five. Routine, navigation, foreground,
background and both identity lane maxima remain one, mail at most two, and authentication at most two ingest plus one interactive
operation. Pending queues cannot exceed four except ingest authentication
(eight) and ingest identity lookup (one). Authentication waits cannot exceed
one second for verification admission or 250 ms for identity lookup. Navigation
waits cannot exceed one second; waits and deadlines must stay positive and
finite. Before a request takes an auth ticket,
the server checks the failed-auth limiter and rejects an oversized Basic header
without decoding it. Pending pre-authentication tickets contain no credential or
account identity, and a waiting request rechecks the limiter before verification
begins. Authenticated tickets retain a validated immutable account principal
for per-account admission limits.
Authentication forms, headers and multipart allowances cannot exceed the
listed defaults. Invalid settings prevent startup. Connection reservations
are independent; idle positions are not borrowed by another class.
Avatar uploads retain their configured file cap, with the same multipart
allowance and upload deadline used for portable input. Authenticated bulk
selection forms retain their existing field limits and use the upload deadline.
Those bulk forms have no new aggregate byte cap; one admitted request can still
consume substantial memory or temporary spool space within those limits.

Temporary capacity pressure returns `503` with `Retry-After: 1`. Browser/API
responses use `capacity_busy`; import contention retains `import_busy`.
Recovery acknowledgements and already committed actions keep their existing
outcomes. Keep non-secret form input when retrying; passwords, tokens and
uploads are not retained or automatically replayed. Oversized OwnTracks input
keeps its successful poison-input acknowledgement. Failed-auth maps retain at
most 1,024 active client keys and fail closed for new keys when full.

Defaults permit at most five independent lifecycle leases (navigation,
foreground, background and two security-mail owners). The six runtime, five
control and five lease connections total at most 16 serving connections.
Migration/maintenance connections and PostgreSQL administrative
headroom are separate. These limits bound concurrency and waits, not physical
memory or total detector job duration. Large ledgers can still need substantial
memory. Background types rotate after each completed device, snap trip, geocode
coordinate, email kind or retention batch (at most 1,000 expired raw rows).
Accounts rotate between those units, and ready backlogs continue without
waiting for a periodic sweep. A whole detector device, complete report or SMTP
transport can still take a long time; this is not a universal job deadline.
Reverse-geocode retries persist per rounded endpoint coordinate. Transient
failures defer that coordinate with exponential backoff from 60 seconds to a
3,600-second maximum, allowing other due coordinates to proceed. Discovery
processes at most 500 trips per turn and resumes its cursor after restart.
Multi-account activation remains separately controlled.

OIDC is optional. Set all three required provider values together and register
`https://your-domain/auth/callback`. Existing password accounts can link the
configured identity from Account Security while signed in with a password.
Invitation redemption without a password and OIDC-only method management are
currently exercised only in the controlled activated fixture; normal installs
retain the single-account guard until supported activation is released. When
enabled, OIDC-only security actions require a provider that honors `max_age=0`
and returns a valid `auth_time`; otherwise these actions fail closed.

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
| `RAW_MESSAGE_RETENTION_DAYS` | `365` | Deletes old raw messages and their replay receipts, refunding logical usage. Values at or below 0 disable pruning. Points, trips, and other derived data are unaffected. Backup retention is independent. |

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
| `OSRM_MAX_COORDS` | `250` | Maximum coordinates materialized and sent in one OSRM match request; integer from 2 through 10,000. |

Prepare a regional dataset before setting these values. See
[Self-hosted OSRM road-snapping](osrm.md).

### Reverse geocoding

| Variable | Default | Purpose |
|---|---:|---|
| `GEOCODE_PROVIDER` | unset | `geoapify` or `nominatim`. For compatibility, an API key with no provider selects Geoapify. |
| `GEOCODE_API_KEY` | unset | Geoapify API key. |
| `GEOCODE_NOMINATIM_URL` | unset | Base URL of an operator-controlled Nominatim instance. The public OpenStreetMap Nominatim service is not a supported bulk backend. |
| `GEOCODE_OMIT_COUNTRY` | `United States of America` | Exact trailing country label removed from returned addresses. Set an empty value to retain it. |
| `GEOCODE_MIN_INTERVAL_S` | `1.0` | Minimum interval between request starts, shared by background reverse lookups, address autocomplete and diagnostic checks. |

Address search and reverse lookups share one FIFO provider pacer. Waiting
reverse lookups release background ownership before pacing, and provider
failures still consume their interval. Address search may therefore wait
behind an earlier lookup. This interval is operator configuration, not a
claim about a hosted provider's usage policy.

Provider HTTP operations have a 15-second total network deadline through
response completion, in addition to shorter socket timeouts. Decompressed
responses are limited to 8 MiB for OSRM, 256 KiB for geocoding/autocomplete
and 64 KiB for notification responses. Oversize/timeout failures retain
retryable work and graceful routing/address fallback; responses are never
clipped, failed sends are not marked delivered and transient geocode failures
are not cached as permanent missing addresses. SMTP keeps its existing
15-second socket timeout, which does not bound the whole transport.

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
| `APP_URL` | unset | Public HTTPS base URL placed in reminder, email confirmation and password reset links. It must be an absolute `http` or `https` URL with a host and no user information, query or fragment; otherwise emailed security links are disabled. It does not configure the reverse proxy. |

### Email

Set `SMTP_HOST` and `EMAIL_FROM` for the shared transport, and set `APP_URL` to
the public HTTPS base URL. These three values are required to send current
login-email verification and change challenges and to offer **Forgot
password?**. Challenges and reset links expire after 30 minutes. Email
challenges are sent to the address being verified or changed, and reset links
only to the account's verified login email; neither uses `EMAIL_TO`. A reset
request is answered before any lookup or delivery. Each account accepts at
most one public reset request a minute and five a day, and a repeat request
within 10 minutes of a delivered link does not replace it. Delivery failures
are logged without the address or link. Save digest recipients and delivery choices in
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
