# Configuration reference

Every setting Odograph reads from `.env`. For a new install, follow the
[README setup steps](../README.md#quick-start).

`scripts/generate_env.sh` writes a working `.env` with mode 600: the secrets
are filled in and every optional integration is off. It refuses to replace an
existing `.env`. Switches use `1` for on and `0` for off. After editing
`.env`, recreate the app; a plain restart does not reload it:

```sh
docker compose up -d --force-recreate --no-deps app
```

With Podman, use `podman-compose`. `--force-recreate` matters there:
podman-compose otherwise keeps the old container, because `compose.yaml` has
not changed.

Settings marked "upgrade import" are read once, when an installation is
upgraded from a release before 1.0; see
[Personal preferences](#personal-preferences).

## Baseline and database

| Variable | Default | Effect |
|---|---|---|
| `POSTGRES_PASSWORD` | generated, required | Password of the Compose database role `mileage`; Compose builds `DATABASE_URL` from it. Keep it URI-safe. |
| `DATABASE_URL` | set by `compose.yaml` | Privileged database URL for migrations, role setup and backups. Under Compose, a value in `.env` is overridden. |
| `DISPLAY_TZ` | `UTC` (`.env.example`: `America/New_York`) | IANA timezone suggested at first-account setup and given to `create-admin`; upgrade import. An invalid name stops startup. |
| `FORWARDED_ALLOW_IPS` | empty (generated: `*`) | Proxies trusted for forwarded client and scheme headers. `*` is safe only while the app port stays bound to loopback behind one proxy. See [Trusting forwarded headers](reverse-proxy.md#trusting-forwarded-headers). |
| `PREPARATION_SPOOL_DIR` | `odograph-preparation-<uid>` in the temp directory | Spool for prepared reports, exports and mail. It must be a directory, not a symlink, owned by the app user with mode `0700`. |
| `APP_VERSION` | `dev` | Set by the release image; shown in diagnostics. Do not override. |
| `APP_GIT_REVISION` | `unknown` | Set by the release image; shown in diagnostics. Do not override. |

`POSTGRES_PASSWORD` is read only when the database volume is first created.
Changing it later does not change the role's password; see
[Protecting `.env`](backups.md#protecting-env).

Odograph runs as one application process. Startup refuses
`WEB_CONCURRENCY` or `UVICORN_WORKERS` set to anything but `1`.

## Account ownership and database roles

The Compose database needs nothing here. An external PostgreSQL 16 with
PostGIS must meet these conditions, or startup refuses to serve:

- The `DATABASE_URL` role is a superuser. Odograph uses it briefly to
  migrate under forced row-level security and to create and manage its own
  roles, then serves requests as restricted roles.
- The roles `odograph_migrate`, `odograph_bootstrap`, `odograph_control` and
  `odograph_runtime` belong to this one installation. Roles are cluster-wide:
  use a separate cluster for a second installation or a restore drill, not a
  second database in the same cluster.
- The restricted roles' passwords are generated and stored in the database,
  so a full backup contains them. A database archive does not contain the
  cluster's role definitions; restore with
  [`scripts/restore_database.sh`](backups.md#restoring), which recreates
  them.

On every start Odograph validates the roles, grants, ownership and row-level
security, and refuses to serve if they do not match.

### Logical storage accounting

Odograph counts the bytes each account stores and can cap them. Limits are
off unless `STORAGE_ACCOUNT_LIMIT_BYTES` is set: nothing is refused for
storage, and **Settings** shows usage only.

| Variable | Default | Effect |
|---|---|---|
| `STORAGE_ACCOUNT_LIMIT_BYTES` | unset (no limits) | Default allowance for each member account. |
| `STORAGE_RAW_LIMIT_BYTES` | `268435456` (256 MiB) | Part of the allowance for raw messages. Needs an account limit. |
| `STORAGE_ENHANCEMENT_LIMIT_BYTES` | `134217728` (128 MiB) | Part of the allowance for snapped routes and cached addresses. Needs an account limit. |
| `STORAGE_INSTANCE_BUDGET_BYTES` | `12884901888` (12 GiB) | Total that all account allowances may add up to. Not database free space. |
| `STORAGE_INSTANCE_RESERVE_BYTES` | `2147483648` (2 GiB) | Part of the budget never granted to an account. |

Startup refuses a raw or enhancement limit without an account limit, a value
that is not a positive integer, a part larger than the account limit, a
reserve not smaller than the budget, or allowances whose sum exceeds budget
minus reserve. The administrator is never limited. An administrator can give
a member their own limit, or none, in **Accounts**
([Edit a member's storage limit](accounts.md#edit-a-members-storage-limit)).

Each limited account holds a grant from the budget, so the budget caps how
many members can join: with a 2 GiB account limit, the defaults fund five.
Invitations are refused while no grant is left.

At a limit, new data that would grow the account is refused or paused;
nothing stored is deleted, and reads, exports and cleanup still work. A
tracker is answered `503` with `Retry-After` and resends later.

Startup checks the usage counters against stored data, which takes longer on
a large history, and refuses to serve if they disagree.

## Personal preferences

Each account saves its own timezone, notification destinations and choices,
reminder hours, mileage rates and vehicle defaults in **Settings**, without a
restart. `.env` holds only the operator's services and limits.

When an installation is upgraded from a release before 1.0, its account
takes the values of `DISPLAY_TZ`, `NTFY_TOPIC`, the personal `EMAIL_*`
settings, the reminder hours and switches, and valid `MILEAGE_RATE_<YEAR>`
values once. Later `.env` edits to them have no effect.

## Authentication and ingest

| Variable | Default | Effect |
|---|---|---|
| `SESSION_SECRET` | generated, required | Signs browser session cookies. Startup refuses fewer than 32 characters. Changing it signs out every browser. |
| `INITIAL_ADMIN_SIGNUP` | `0` | `1` allows browser signup of the first administrator while no account exists. |
| `INGEST_USERNAME` | `owntracks` | Upgrade import of the old shared tracking login. Devices get credentials in **Settings > Tracking**. |
| `INGEST_PASSWORD` | empty (generated) | Upgrade import of the old shared tracking password. Editing it later changes nothing. |
| `LOGIN_AUTH_MAX_FAILURES` | `10` | Failed sign-in and password checks allowed per client address per window. |
| `LOGIN_AUTH_WINDOW_S` | `900` | Window for the sign-in limit, in seconds. |
| `INGEST_AUTH_MAX_FAILURES` | `10` | Failed tracker logins allowed per client address per window. A blocked client gets `503` with `Retry-After`. |
| `INGEST_AUTH_WINDOW_S` | `900` | Window for the tracker limit, in seconds. |
| `INGEST_MAX_BODY_BYTES` | `65536` | Largest tracker request. A larger one is logged and dropped with a success response, so the phone does not resend it. |
| `OIDC_ISSUER` | unset | Exact issuer URL of the OIDC provider. |
| `OIDC_CLIENT_ID` | unset | OIDC client ID. |
| `OIDC_CLIENT_SECRET` | unset | OIDC client secret. |
| `ALLOWED_EMAIL` | unset | Lets an upgraded OIDC-only installation claim its account once. It does not limit OIDC sign-in. |
| `MOBILE_SIGNIN` | `0` | `1` allows new mobile app sign-ins, over HTTPS. `0` refuses new ones; signed-in apps keep working. See [Mobile app sign-in](accounts.md#mobile-app-sign-in-and-signed-in-apps). |
| `DEV_NO_AUTH` | `0` | `1` turns authentication off, for development and the local trial. Never set it on a deployed instance. |
| `DEMO_MODE` | `0` | `1` shows a demo banner, for the local trial. Startup refuses it without `DEV_NO_AUTH=1`. |
| `ADMIN_TOKEN` | ignored | Obsolete. Odograph never reads it; remove it. |

Set all three `OIDC_*` values or none; startup refuses a partial set. See
[OIDC sign-in](oidc.md).

### Shared capacity

Odograph limits how much work runs at once, so one upload, report or
background job cannot take every database connection. A request that cannot
start in time gets `503` with `Retry-After: 1`.

Slot, pending and size limits can only be lowered from their defaults, except
`CAPACITY_LIFECYCLE_SLOTS` as noted. Slots must be at least 1, pending counts
at least 0. Runtime slots (ingest, routine, navigation, foreground,
background) may total at most six, control slots (ingest identity, identity,
lifecycle, mail) at most five. Waits and timeouts must be positive and can be
raised, except where a maximum is listed. Startup refuses anything else.

| Variable | Default | Effect |
|---|---|---|
| `CAPACITY_INGEST_SLOTS` | `2` | Runtime connections for tracker uploads. |
| `CAPACITY_ROUTINE_SLOTS` | `1` | Runtime connections for small reads and edits. |
| `CAPACITY_NAVIGATION_SLOTS` | `1` | Concurrent full-week dashboard loads. |
| `CAPACITY_FOREGROUND_SLOTS` | `1` | Concurrent reports, exports and large edits. |
| `CAPACITY_BACKGROUND_SLOTS` | `1` | Concurrent background jobs. |
| `CAPACITY_INGEST_IDENTITY_SLOTS` | `1` | Control connections for tracker credential lookup. |
| `CAPACITY_IDENTITY_SLOTS` | `1` | Control connections for other identity work. |
| `CAPACITY_LIFECYCLE_SLOTS` | `1` | Control connections for account and credential changes. Can be `2` if `CAPACITY_MAIL_SLOTS` is `1`. |
| `CAPACITY_MAIL_SLOTS` | `2` | Control connections for security email. |
| `CAPACITY_AUTH_INGEST_SLOTS` | `2` | Concurrent tracker credential checks. |
| `CAPACITY_AUTH_INTERACTIVE_SLOTS` | `1` | Concurrent sign-in form, password or OIDC work. |
| `CAPACITY_AUTH_INGEST_PENDING` | `8` | Tracker credential checks allowed to wait. |
| `CAPACITY_INGEST_PENDING` | `4` | Tracker uploads allowed to wait. |
| `CAPACITY_ROUTINE_PENDING` | `4` | Small requests allowed to wait. |
| `CAPACITY_NAVIGATION_PENDING` | `4` | Dashboard loads allowed to wait. |
| `CAPACITY_FOREGROUND_PENDING` | `4` | Reports and exports allowed to wait. |
| `CAPACITY_IDENTITY_PENDING` | `4` | Identity requests allowed to wait. |
| `CAPACITY_INGEST_IDENTITY_PENDING` | `1` | Tracker credential lookups allowed to wait. |
| `CAPACITY_AUTH_INGEST_WAIT_S` | `1` | Longest wait for a tracker credential check; at most `1`. |
| `CAPACITY_INGEST_IDENTITY_WAIT_S` | `0.25` | Longest wait for a tracker credential lookup; at most `0.25`. |
| `CAPACITY_INGEST_WAIT_S` | `0.25` | Longest wait for a tracker upload. |
| `CAPACITY_ROUTINE_WAIT_S` | `1` | Longest wait for a small request. |
| `CAPACITY_NAVIGATION_WAIT_S` | `1` | Longest wait for a dashboard load; at most `1`. |
| `CAPACITY_FOREGROUND_WAIT_S` | `2` | Longest wait for a report or export. |
| `CAPACITY_IDENTITY_WAIT_S` | `1` | Longest wait for an identity request. |
| `CAPACITY_AUTH_BODY_TIMEOUT_S` | `15` | Time allowed to receive a sign-in form. |
| `CAPACITY_INGEST_BODY_TIMEOUT_S` | `15` | Time allowed to receive a tracker upload. |
| `CAPACITY_IMPORT_BODY_TIMEOUT_S` | `60` | Time allowed to receive an import or avatar upload. |
| `CAPACITY_RESPONSE_TIMEOUT_S` | `60` | Time allowed to send a report or export. |
| `CAPACITY_ROUTINE_SQL_TIMEOUT_S` | `5` | Statement timeout for small requests and uploads. |
| `CAPACITY_OPERATION_SQL_TIMEOUT_S` | `15` | Statement timeout for reports, exports and background jobs. |
| `CAPACITY_LOCK_TIMEOUT_S` | `1` | Database lock timeout. |
| `CAPACITY_AUTH_FORM_MAX_BYTES` | `65536` | Largest sign-in or small edit form. |
| `CAPACITY_BASIC_HEADER_MAX_BYTES` | `8192` | Largest Basic authentication header. |
| `CAPACITY_MULTIPART_OVERHEAD_BYTES` | `65536` | Upload envelope allowed above the file limit. |
| `CAPACITY_MULTIPART_MAX_FIELDS` | `16` | Most fields in an upload. |
| `CAPACITY_MULTIPART_MAX_FILES` | `1` | Most files in an upload. |

These limits bound concurrency, not memory: a large history can still need
substantial memory.

## Detector behavior

Changes apply to future detector runs; raw points are never changed.
[How well does detection work?](detection.md) explains the behavior.

| Variable | Default | Effect |
|---|---|---|
| `MAX_ACCURACY_M` | `100` | Drops fixes with reported accuracy worse than this, in meters. |
| `MAX_SPEED_MS` | `60` | Drops a point implying a faster speed, in m/s, from the last kept point. |
| `STAY_RADIUS_M` | `150` | Radius of a stationary stay, in meters. |
| `STAY_MIN_DURATION_S` | `300` | Shortest stay, in seconds; also the window that averages walking speed. |
| `WALK_MAX_SPEED_MS` | `2.0` | Fastest average speed, in m/s, still treated as an on-foot stay. |
| `WALK_LEG_MAX_SPEED_MS` | `3.0` | Fastest single leg an on-foot stay can absorb. At or below `WALK_MAX_SPEED_MS`, this is off. |
| `MIN_TRIP_DISTANCE_M` | `300` | Shorter movement between stays is jitter, not a trip. |
| `GAP_FLAG_THRESHOLD_S` | `600` | Flags a trip with a gap between points longer than this, in seconds. |
| `DETECT_DEBOUNCE_S` | `60` | Quiet time after the newest point before detection runs. |
| `DETECT_SWEEP_S` | `900` | Interval of the periodic detection catch-up. |
| `FULL_REPROCESS_WARN_POINTS` | `500000` | A full reprocess of a device with at least this many points logs a warning. |
| `DETECTOR_TURN_MAX_POINTS` | `0` (unlimited) | Most points one background detector run loads for a device. Startup refuses a negative value. |

### Large detector windows

With `DETECTOR_TURN_MAX_POINTS` set, a device whose next run would load more
points is deferred: nothing is written for it, one warning names the account
and device IDs, and the admin **Settings** page lists it under Workers.
Tracking continues. A device that stays in one place keeps one growing stay,
so it can be deferred too. It stays deferred until you raise the limit and
restart, or run its work without a limit:

```sh
docker compose exec app python -m app.reprocess_detector --account ACCOUNT_ID --device DEVICE_ID
# or every device of the account with pending work:
docker compose exec app python -m app.reprocess_detector --account ACCOUNT_ID
```

The command handles one device per transaction and exits non-zero if the
account is unknown or disabled, or any device fails, for example on its
storage limit.

## UI, reports, and imports

| Variable | Default | Effect |
|---|---|---|
| `TRIPS_PAGE_SIZE` | `25` | Trips shown per month before **Load more**. Lower values become 1. |
| `MISSING_TRIP_GAP_M` | `500` | Distance between trips, in meters, that flags a possible missing trip. `0` turns it off. |
| `MAP_TILE_URL` | OpenStreetMap | Map tile URL template. Its origin is allowed in the Content Security Policy. |
| `MAP_TILE_ATTRIBUTION` | OpenStreetMap | HTML attribution shown with the tiles. |
| `HSTS_MAX_AGE` | `0` (off) | Sends HSTS with this max-age on HTML responses to HTTPS requests. Prefer setting it at the proxy. |
| `PORTABLE_IMPORT_MAX_BYTES` | `52428800` (50 MiB) | Largest portable import file. |
| `ACCOUNT_AVATAR_MAX_BYTES` | `512000` | Largest avatar upload. Images are also limited to 8192 pixels a side and 16,777,216 pixels in total. |
| `MILEAGE_RATE_<YEAR>` | unset | Upgrade import of a mileage rate for that year. Rates are managed in **Settings**. |

A reverse proxy must accept a request body above the import limit; see
[Reverse proxy and TLS](reverse-proxy.md).

## Retention

| Variable | Default | Effect |
|---|---|---|
| `RAW_MESSAGE_RETENTION_DAYS` | `365` | Deletes raw tracker messages older than this many days. `0` or less keeps them. Points and trips are not affected. |

Backups keep what they held; see [What the archive
covers](backups.md#what-the-archive-covers-and-what-it-doesnt).

## External services

Each service is off until its settings are set. [privacy.md](privacy.md)
lists what each one sends. Outgoing HTTP to OSRM, the geocoder and ntfy
honors `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`
and `SSL_CERT_DIR`; SMTP does not.

### Road snapping and manual trip routing

| Variable | Default | Effect |
|---|---|---|
| `OSRM_URL` | unset | OSRM base URL; turns on road snapping, routed manual trips and suggested missing-trip distances. With the Compose profile, `http://osrm:5000`. |
| `OSRM_DATASET` | unset | Dataset served by the Compose `osrm` profile, as printed by `scripts/provision_osrm.sh`. |
| `OSRM_MIN_CONFIDENCE` | `0.5` | Lowest match confidence accepted for a snapped route. |
| `OSRM_MAX_COORDS` | `250` | Most coordinates in one match request, 2 to 10000; startup refuses others. |
| `SNAP_DEBOUNCE_S` | `15` | Quiet time before new trips are snapped. |
| `SNAP_SWEEP_S` | `300` | Interval of the periodic snapping catch-up. |

See [Self-hosted OSRM road-snapping](osrm.md).

### Reverse geocoding

| Variable | Default | Effect |
|---|---|---|
| `GEOCODE_PROVIDER` | unset | `geoapify` or `nominatim`. Startup refuses any other value. |
| `GEOCODE_API_KEY` | unset | Geoapify API key. |
| `GEOCODE_NOMINATIM_URL` | unset | Your own Nominatim instance; startup refuses `nominatim` without it. |
| `GEOCODE_OMIT_COUNTRY` | `United States of America` | Country name removed from the end of addresses. Empty keeps it. |
| `GEOCODE_MIN_INTERVAL_S` | `1.0` | Least time between provider requests, shared by address lookups and address search. |
| `GEOCODE_DEBOUNCE_S` | `15` | Quiet time before new trip endpoints are looked up. |
| `GEOCODE_SWEEP_S` | `300` | Interval of the periodic lookup catch-up. |

A `GEOCODE_API_KEY` with no `GEOCODE_PROVIDER` selects Geoapify. Leave both
`GEOCODE_PROVIDER` and `GEOCODE_API_KEY` unset to turn geocoding off. OSM's
public Nominatim usage policy forbids this kind of bulk lookup; run your own.

### ntfy

Set `NTFY_URL`; each account saves its topic and reminders in **Settings**.

| Variable | Default | Effect |
|---|---|---|
| `NTFY_URL` | unset | ntfy server base URL. |
| `NTFY_TOPIC` | unset | Upgrade import of the topic: 1 to 64 letters, digits, `-` or `_`. Startup refuses other values. |
| `NTFY_TOKEN` | unset | Bearer token. |
| `NTFY_USERNAME` | unset | Basic authentication username. |
| `NTFY_PASSWORD` | unset | Basic authentication password. A username and password take precedence over a token. |
| `APP_URL` | unset | Public base URL for links in reminders and email, such as `https://mileage.example.com`. |

An `APP_URL` that is not an absolute `http` or `https` URL without user
information, query or fragment is logged at startup and turns emailed links
off.

### Email

**Forgot password?** and login-email confirmation
([Account management](accounts.md)) need `SMTP_HOST`, `EMAIL_FROM` and a
valid `APP_URL`. Digests and reminders need `SMTP_HOST` and `EMAIL_FROM`;
each account saves its recipient and choices in **Settings**. A digest whose
send fails or times out is retried the next hour, which can deliver it
twice.

| Variable | Default | Effect |
|---|---|---|
| `SMTP_HOST` | unset | SMTP server hostname. |
| `SMTP_PORT` | `587` | SMTP server port. |
| `SMTP_USERNAME` | unset | SMTP username. |
| `SMTP_PASSWORD` | unset | SMTP password. |
| `SMTP_SECURITY` | `starttls` | `starttls`, `ssl` or `none`, lowercase. Another value fails when sending, not at startup. Use `none` only for a trusted local relay. |
| `SMTP_TLS_INSECURE` | `0` | `1` skips certificate verification. Only for a local relay with a self-signed certificate. |
| `EMAIL_FROM` | unset | Sender address. |
| `EMAIL_TO` | unset | Upgrade import of the digest recipient. |
| `EMAIL_WEEKLY_NUDGE` | `0` | Upgrade import of the weekly unclassified-trip email. |
| `EMAIL_MONTHLY_SUMMARY` | `1` | Upgrade import of the monthly summary email. |
| `EMAIL_FILING_REMINDER` | `1` | Upgrade import of the annual filing reminder email. |
| `EMAIL_ODOMETER_REMINDER` | `0` | Upgrade import of the quarterly odometer reminder email. |

## Worker scheduling

All of these are upgrade imports; each account sets its own in **Settings**,
in its own timezone.

| Variable | Default | Effect |
|---|---|---|
| `NUDGE_WEEKLY_HOUR` | `18` | Sunday hour of the weekly unclassified-trip reminder. |
| `ODOMETER_REMINDER` | `1` when `NTFY_URL` and `NTFY_TOPIC` are set | Quarterly ntfy odometer reminder. |
| `ODOMETER_REMINDER_HOUR` | `9` | Hour of the odometer reminder on the first day of a quarter. |
| `EMAIL_DIGEST_HOUR` | `9` | Hour of the email digests and reminders. |
| `EMAIL_FILING_REMINDER_MMDD` | `01-15` | Month and day of the annual filing reminder. |
