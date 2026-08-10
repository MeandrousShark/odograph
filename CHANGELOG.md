# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Development history before the first public release is private and is not
reproduced here.

## [Unreleased]

### Added

### Changed

### Fixed

### Security

### Supported upgrade path

- Complete before release: state which prior release or releases may upgrade
  directly, or why a prior-release gate is not applicable.

### Breaking changes

- Complete before release: list every application, database, configuration,
  and operational break, or state explicitly that there are none.

## [0.7.6] - 2026-08-10

Maintenance release: punctuation consistency in the interface and exported
files, plus a large internal refactor and test cleanup. No user-facing feature
and no database migration; the schema stays at 19.

### Changed

- **Standard punctuation across the interface and exported files.** En and em
  dashes in on-screen text and in exported CSV and spreadsheet files are
  replaced with plain ASCII punctuation, so text renders consistently across
  fonts, terminals, and spreadsheet tools.

- **Substantial internal code consolidation.** Duplicated formatting, query,
  notification, report, and structural code now each have a single owner, and
  the test suite was audited and de-duplicated. These are internal changes with
  no effect on behavior.

### Supported upgrade path

- `v0.7.0`, `v0.7.1`, `v0.7.2`, `v0.7.3`, `v0.7.4`, and `v0.7.5` may upgrade
  directly to `v0.7.6`.

### Breaking changes

- None. There is no migration; the schema stays at 19. No configuration
  changes, and no operational changes beyond the usual image pin bump.

## [0.7.5] - 2026-08-09

Internal robustness release: concurrency correctness, background-worker
resilience, and event-loop responsiveness. No user-facing feature and no
database migration; the schema stays at 19.

### Changed

- **Large exports and trip detection no longer block other requests.**
  Building a large CSV or spreadsheet export, serialising a full-data JSON
  export, and running a trip-detection pass now happen off the main
  request-handling thread, so the app stays responsive to other requests
  while that work runs.

- **The Settings workers table shows a new "Last skip" column.** A background
  worker run that is skipped because another run already holds its lock is now
  recorded and shown separately, instead of being indistinguishable from a
  successful run.

### Fixed

- **A trip's snapped route can no longer be overwritten with a stale result.**
  If a trip's points changed through a background detection pass while its
  route was being matched to roads, the finished match could overwrite the
  updated trip with an out-of-date route. The result is now discarded when the
  trip changed underneath it, and the trip is re-matched on the next pass.

- **A hand classification can no longer be silently lost to a background
  detection pass.** Classifying a trip as business or personal at the moment a
  detection pass replaced that trip used to report success while dropping the
  classification. It now reports a clear error so the classification can be
  re-applied, and automatic rule-based tagging can never overwrite a trip that
  was classified by hand.

- **Splitting a trip and editing places or tagging rules are now fully
  transactional.** A trip split now holds the detector lock across its whole
  operation and reads the trip fresh under that lock, and a place or
  tagging-rule change now applies its re-tagging in the same transaction, so a
  failure part way through can no longer leave settings and trip
  classifications inconsistent.

- **The monthly summary and filing-reminder emails no longer risk stalling.**
  Each of these digests used to borrow a second database connection while
  already holding one, which could stall when the connection pool was busy.
  They now do all their work on the single connection they already hold.

- **Background workers fail more safely.** A worker that stops unexpectedly is
  now logged rather than disappearing silently, a failure during application
  startup now cleanly shuts down the database pool and any workers already
  started, and a run skipped for lock contention is no longer counted as a
  success.

- **Several invalid inputs now return a validation error instead of a server
  error.** A tagging rule referring to a non-numeric or since-deleted place,
  and an invalid vehicle filter on the expenses page, now return a clear
  rejection rather than an internal error. The diagnostics command no longer
  aborts when the geocoder is misconfigured; it names the misconfiguration
  instead. The statistics page's "Review" link now points at the review page.

### Supported upgrade path

- `v0.7.0`, `v0.7.1`, `v0.7.2`, `v0.7.3`, and `v0.7.4` may upgrade directly to
  `v0.7.5`.

### Breaking changes

- None. There is no migration; the schema stays at 19. No configuration
  changes, and no operational changes beyond the usual image pin bump.

## [0.7.4] - 2026-08-08

### Fixed

- **Numeric fields no longer accept the special values "not a number" or
  "infinity".** A mileage rate, an odometer reading, or a place radius could
  be submitted as one of these values and slip past the "must be positive"
  check, because neither that check nor the database catches them. They then
  produced blank or nonsensical figures in reports and exports. Each of these
  fields now rejects such a value with a validation error. A place's
  latitude or longitude that falls outside the valid range is likewise
  rejected now, rather than being silently adjusted to a different location,
  and a mileage rate set through the optional per-year environment variable is
  ignored with a logged warning if it is not a positive finite number.

- **Merging trips no longer silently changes a trip's business or personal
  classification.** Choosing "Keep" when merging now genuinely keeps each
  trip's existing category and its manual-or-automatic ownership, instead of
  quietly resetting the merged trip to unclassified and locking it. Merging
  from a trip's own page no longer defaults to reclassifying the result as
  business. And editing a trip's purpose now marks that trip as classified by
  hand, so a later automatic re-tag cannot revert the change.

- **The exported spreadsheet's two mileage-deduction totals now always
  agree.** The Trips sheet added up each trip's already-rounded deduction
  while the Summary sheet rounded once at the end, so the same workbook could
  show two totals that differed by a cent. Both are now derived the same way
  and match exactly.

- **The annual odometer coverage summary now recognises a fully covered
  year.** When a vehicle had odometer readings on the year's opening and
  closing boundaries, the report still described the coverage as spanning only
  part of the year, because the boundary reading was left out of the
  calculation. That reading is now included, so a fully bracketed year is
  reported as such.

- **A manual trip time that does not exist because of the spring
  daylight-saving change is now rejected.** On the day clocks jump forward,
  the skipped hour (for example 02:30 where the clock goes straight from 02:00
  to 03:00) has no real moment. Entering such a time used to store a different
  instant and display an hour off what was typed. It is now rejected with a
  clear message so the recorded time is always the one entered. Times during
  the autumn change, and overnight trips, are unaffected.

### Security

- **Sign-in, setup, ingest, and form submissions no longer fail with a server
  error on non-ASCII input.** A password, setup token, ingest credential, or
  security token containing non-ASCII characters caused an internal error
  instead of a clean rejection. That error also skipped the failed-attempt
  rate limiter, and an administrator email containing non-ASCII characters
  saved during first-run setup could permanently prevent sign-in. These inputs
  are now compared correctly and rejected cleanly, the rate limiter records
  the attempt, and a non-ASCII administrator email is refused at setup.

- **Signing in through the identity provider now starts a fresh session.** The
  callback that completes single sign-on now discards any pre-existing session
  contents and issues a new security token before establishing the signed-in
  session, closing a session-fixation vector and matching the local sign-in
  path.

- **Password hashing and verification now run off the main request loop**, so
  a sign-in or first-run setup can no longer briefly stall other requests
  while the password is processed.

### Supported upgrade path

- `v0.7.0`, `v0.7.1`, `v0.7.2`, and `v0.7.3` may upgrade directly to `v0.7.4`.

### Breaking changes

- None. There is no migration; the schema stays at 19. No configuration
  changes, and no operational changes beyond the usual image pin bump.

## [0.7.3] - 2026-08-08

### Fixed

- **Editing a place or a tagging rule no longer erases imported trips'
  locations and categories.** Trips brought in from a portable export carry
  their place labels and categories directly, because the export format holds
  no map coordinates. Any place or rule change in Settings re-derives every
  trip's start and end place from its coordinates, and imported trips have
  none, so the app wrote an empty answer over the labels the import had set. A
  second step then reverted every affected trip that a rule had tagged back to
  unclassified, because no rule matched a trip with no places. An imported
  history could silently lose its place names and its business or personal
  categories, dropping those trips out of the mileage totals, after an
  ordinary Settings edit. Imported trips are now left alone by that
  re-derivation, matching the protection they already had from the trip
  detector. Trips recorded normally are re-derived exactly as before.

- **A trip's "merge with previous" and "merge with next" buttons no longer
  appear when the only neighbouring trip is an imported one.** The buttons
  showed whenever any adjacent trip existed, but merging deliberately refuses
  to touch imported trips, so in that situation the button could only fail.
  This was most visible on the first normally recorded trip after an import.

- **A malformed message from a tracking device can no longer stall every
  later location update.** OwnTracks holds onto a message and retries it when
  the server reports an error, which is what stops a brief outage from losing
  a drive. Several malformed messages made the server report an error every
  time, so the device retried the same message indefinitely and every reading
  queued behind it was stuck with it. Trips simply stopped appearing, with
  nothing to indicate why. The affected shapes were a timestamp reported in
  milliseconds instead of seconds (a common device misconfiguration) or
  otherwise outside a sane range, a timestamp that was not a number at all, a
  trigger field arriving as something other than text, and any message
  carrying a value the message store cannot hold: a not-a-number or infinite
  value, a NUL character, or an unpaired surrogate inside a text field. Every
  one of these is now discarded quietly, the way other malformed input was
  already handled, so a single bad message can no longer block the messages
  behind it. Valid content that merely looks unusual, an emoji in a device
  name for example, is unaffected and still stored.

### Supported upgrade path

- `v0.7.0`, `v0.7.1`, and `v0.7.2` may upgrade directly to `v0.7.3`.

### Breaking changes

- None. There is no migration; the schema stays at 19. No configuration
  changes, and no operational changes beyond the usual image pin bump.

## [0.7.2] - 2026-08-07

### Fixed

- **The review page's map now appears with every trip.** Classifying a trip
  as business or personal, or skipping it, brings up the next unclassified
  trip. Its card used to arrive with everything else present but no map, and
  only reloading the page brought the route back. This mattered because the
  review page is meant to be worked through one trip after another from the
  keyboard, and the map is the main thing a trip is judged by. The map now
  appears with every trip as you work through them, with no reloading needed.
- The dashboard's "Add manual trip" link took you to the trips page but left
  the form collapsed, so the action looked like it had done nothing. It now
  arrives with the manual-trip form already open. This works whether or not
  JavaScript is enabled.
- The first column of every table now lines up with the heading above it and
  with the surrounding text. Previously it sat slightly further right than
  its own heading; it was most visible under "By vehicle" on the report page
  and under "Top named routes" and "Most-used places" on the stats page, and
  it applied to the settings page's tables too.
- The report page offered a link to the next tax year even before that year
  had started, which led to an empty report and a dead end. That link is now
  shown but disabled until the year begins, matching how the dashboard
  already treats the coming week. The current year is worked out in the
  display timezone, so it changes over at midnight where the operator is, not
  in UTC. Going to a future year directly by URL still works, which matters
  if a forward-dated expense has put records there.

### Changed

- **Settings now leads with settings.** The diagnostic information and the
  device status block have moved into a single collapsed section at the
  bottom of the page. Everything that was there before is still there, one
  click away.

### Added

- **Signed-in pages now carry a footer showing the running application
  version**, the quickest way to confirm what an installation is running. It
  does not appear on the sign-in or first-run setup pages, so the version is
  not disclosed to anyone who has not signed in, and `/healthz` continues to
  report no version, schema, or component identifiers. The footer carries no
  external links.

### Supported upgrade path

- Upgrade directly from 0.7.1 by pulling the new image. This release **runs
  no database migration**; the schema stays at 19 and no configuration change
  is required. Because nothing touches the database, rolling back is a matter
  of repointing at the previous image.

### Breaking changes

- None. There is no application, database, configuration, or operational
  break in this release.

## [0.7.1] - 2026-08-07

### Fixed

- **Signing out now signs you out.** On an installation using OIDC without a
  local administrator, the sign-out button appeared to do nothing. It really
  did clear the application's session, but the page it then sent the browser
  to redirected straight back to the identity provider, which still held its
  own sign-in and returned the browser with a fresh session before anything
  was visible. Sign-out now ends on the sign-in page, as it reads.
- The sign-in page offers the OIDC button on an installation that has OIDC
  configured but no local administrator. That combination previously rendered
  neither a password form nor an OIDC button, only the message that no
  administrator had been set up. It went unnoticed because that installation
  never reached the page before this release.
- `scripts/upgrade_check.sh` can now verify a release that runs a database
  migration. Its final comparison required the whole database manifest to be
  unchanged across the upgrade, and the manifest begins with the schema
  version, so any release running a migration failed the check by definition.
  The comparison now excludes the schema version, which the same step already
  asserts separately against the candidate's migration count. A second fix
  stops the `--keep` teardown message from crashing on an unset variable.

### Changed

- **The sign-in page is always shown.** An installation with OIDC configured
  and no local administrator used to be sent straight to the identity
  provider when it needed to sign in. It now sees the sign-in page with a
  "Sign in with OIDC" button on it. This is one extra click per sign-in and
  it is what makes signing out work, since a page that redirects on sight
  cannot show anyone that they are signed out. Installations that already
  showed a sign-in page are unaffected, no configuration changes, and every
  sign-in that worked before still works.
- `docs/releasing.md` now requires running `pip-audit` against the exact tree
  a release is about to tag. The dependency scan runs inside the release
  workflow, so a vulnerability found there burns an immutable tag that has
  already been pushed.

### Security

- Signing out ends this application's session. It does **not** end the
  identity provider's session, because the application performs no
  provider-side sign-out. After signing out, signing back in through OIDC may
  not prompt for credentials at all, since the provider still considers the
  browser signed in. To end both, sign out of the identity provider as well.
  The README's "Password recovery and session revocation" section now says
  so. This is unchanged behaviour that was previously undocumented, not a new
  limitation.

### Supported upgrade path

- Upgrade directly from 0.7.0. This release **runs no database migration**;
  the schema stays at 19 and no `.env` change is required. Pull the new image
  and recreate the app. Rolling back to 0.7.0 is a plain image change with no
  database work, since nothing about the stored data differs between the two.

### Breaking changes

- None. There is no application, database, configuration, or operational
  break. The sign-in page appearing where an automatic redirect used to
  happen is a visible change in behaviour, described under Changed, but it
  breaks no configuration or contract and every existing way of signing in
  still works.

## [0.7.0] - 2026-08-06

### Added

- Portable data export and import, so an installation's records can move to
  another instance or leave the application in a readable form. This is
  separate from the existing tax-report CSV/XLSX export, which produces
  formatted reports rather than a re-importable copy. A new "Data export /
  import" section on the Settings page drives both sides.
- Export downloads the whole ledger as a single versioned JSON file:
  vehicles, places, auto-tag rules, mileage rates, trips, expenses, odometer
  readings, and the application settings row. Rows reference each other
  through file-local ids rather than database ids, and expense amounts travel
  as decimal strings so a currency value cannot drift by a cent in a round
  trip.
- Import reads that file back, allocating fresh database ids and rewriting
  every reference through them, so a target instance whose id sequences sit
  at different values is safe. The whole import runs in one transaction: it
  either applies completely or leaves the database untouched. A "Dry run"
  option validates a file and reports exactly what it would do without
  writing anything.
- Three limits are deliberate in this first version and are reported rather
  than worked around. The file does not carry trip route geometry or raw
  location points, so an imported trip keeps its ledger fields, its start and
  end places, and the distance a report counts, but not its mapped path.
  Import requires a clean target: an instance that has been migrated but has
  no trips, expenses, odometer readings, or places yet, and still has only
  the vehicle and auto-tag rules a fresh install seeds. It refuses anything
  else and names what it found rather than merging. Import also requires the
  file's schema version to match the target's exactly.

### Changed

- `.env.example` now documents `INGEST_USERNAME` and
  `RAW_MESSAGE_RETENTION_DAYS`, and no longer lists
  `FULL_REPROCESS_WARN_POINTS`, which only selected the severity of a log
  line. No application default changed, and an existing `.env` needs no edit.

### Security

- `cryptography` moves from 49.0.0 to 50.0.0 in `requirements.lock`, picking
  up the fix for PYSEC-2026-3552. The flaw is a Bleichenbacher oracle in that
  library's PKCS#7 decryption helpers, reachable only by an application that
  decrypts attacker-supplied S/MIME `EnvelopedData` and reflects the outcome.
  Odograph never calls those helpers: `cryptography` is present only as
  Authlib's dependency for verifying OIDC tokens, so no installation was
  exposed. The upgrade keeps the release's blocking dependency scan clean
  without recording an acceptance.

### Supported upgrade path

- Upgrade directly from 0.6.1. Unlike 0.6.1, this release **runs a database
  migration** (schema 18 to 19) the first time the new app starts. Take a
  backup with `scripts/backup_database.sh` and verify it before pulling the
  new image, following the procedure in `docs/upgrading.md`. No `.env` change
  is required.

### Breaking changes

- None. There is no application, configuration, or operational break. The
  migration only adds one column to `trips` with a default, so it needs no
  operator action and rewrites no existing data. Migrations are forward-only:
  rolling back to 0.6.1 means restoring the pre-upgrade backup, not reversing
  the migration.

## [0.6.1] - 2026-08-03

### Changed

- The optional `osrm` service now checks `OSRM_DATASET` when it starts,
  rather than Compose requiring the variable to render the file. Starting
  `--profile osrm` before provisioning a dataset still gets an explanatory
  message: the container exits immediately instead of serving an empty
  `/data`. It just no longer affects anyone who leaves the profile off.

### Fixed

- A clean 0.6.0 installation could not start on either supported container
  runtime. The `osrm` service required `OSRM_DATASET`, which `.env.example`
  ships commented out by design, and Compose interpolates every service in
  the file before it applies profile filtering. Every Compose subcommand,
  `pull` and `up -d` and `ps` alike, failed with `required variable
  OSRM_DATASET is missing a value: set in .env`, even though the `osrm`
  profile was never enabled. The baseline `db` and `app` services now start
  from a freshly generated `.env` with no extra configuration, and the
  backup, restore, and provisioning scripts work again.

### Supported upgrade path

- Upgrade directly from 0.6.0. Pull the new image and recreate the app; no
  migration runs and no `.env` change is required.
- An operator who worked around the defect by setting `OSRM_DATASET` to a
  dummy value in `.env` should remove that line unless a real provisioned
  dataset backs it, so the `osrm` service's own check stays meaningful.

### Breaking changes

- None. Nothing in the application, database schema, configuration, or
  operational procedures breaks relative to 0.6.0.

## [0.6.0] - 2026-07-31

### Added

- Authenticated runtime diagnostics on the settings page and as
  `python -m app.diagnose`: versions, git revision, schema version, detector
  version, database and pool health, migration status, worker state,
  configuration presence, and on-demand OSRM/geocoder/ntfy/SMTP checks.
- `GEOCODE_PROVIDER` selects the reverse-geocoding/address-search provider:
  `geoapify` (hosted, needs `GEOCODE_API_KEY`) or `nominatim` (self-hosted
  only, needs `GEOCODE_NOMINATIM_URL`, which ships with no default). Left
  unset with `GEOCODE_API_KEY` set, it resolves to `geoapify`.
- `GEOCODE_OMIT_COUNTRY`: the trailing country suffix stripped from a
  geocoded address, now configurable and provider-agnostic. Defaults to
  `United States of America`, reproducing prior behavior exactly; set empty
  to disable stripping.
- `scripts/provision_osrm.sh` and `docs/osrm.md`: provisions a self-hosted
  OSRM dataset from an operator-chosen Geofabrik extract, replacing the need
  to inherit the maintainer's own Washington-state example region.
- `MAP_TILE_URL` and `MAP_TILE_ATTRIBUTION`.
- `HSTS_MAX_AGE`, unset by default.
- Multi-architecture `linux/amd64` and `linux/arm64` container release
  builds, with per-architecture SBOMs and cosign keyless signatures.
- PostGIS-backed continuous integration plus blocking dependency and
  container vulnerability scans for release builds.
- Artifact-based backup, upgrade, and rollback rehearsal using an exact
  candidate image tag or digest.
- `docs/security.md`.

### Changed

- Renamed the project from Mileage Tracker to Odograph: display name, page
  titles, notification headers, container identity, and documentation.
- `.env.example` now ships `OSRM_DATASET` commented out with no default
  region, so a fresh `--profile osrm` start without provisioning fails with
  the existing clear `:?` error instead of crash-looping against an empty
  dataset.
- The canonical Compose installation pulls a pinned release image.
  Contributors who build from source use `compose.build.override.yml`
  explicitly.
- The container runs as UID/GID 10001. Compose drops all capabilities and
  sets `no-new-privileges`, a read-only root filesystem, and a `/tmp` tmpfs.

### Fixed

### Security

- Security response headers and a nonce-based CSP on HTML responses.
- `/auth/callback` is rate-limited, checked before the token exchange.
- Startup warning when `FORWARDED_ALLOW_IPS` is `*` or empty outside dev.
- Failed geocoder and OSRM requests no longer log the request URL, which
  carried `GEOCODE_API_KEY` and trip coordinates.

### Supported upgrade path

- This is the first public release of Odograph. There is no prior public
  release to upgrade from; clean installation is the only supported path.
- Prior-release artifact upgrade gate: not applicable. No prior public
  release exists.

### Breaking changes

Nothing breaks relative to a prior public release, because there is none.
The constraints below are listed because a deployment that departs from the
shipped Compose file can trip over them on a first install.

- The container runs as UID/GID 10001 instead of root. A bind-mounted host
  path must be readable, and writable if written to, by UID/GID 10001.
- Compose defaults add a read-only root filesystem and drop all
  capabilities. A deployment that does not use the shipped compose file gets
  neither, and one that writes outside `/tmp` will fail.
- HTML responses carry a CSP with `script-src 'self' 'nonce-…'`. A
  customised template with an inline script or `on*` handler will not
  execute it.
- `img-src` is derived from `MAP_TILE_URL`; a tile host set anywhere other
  than that variable will be blocked.
- The canonical Compose file does not build the application image locally.
  Source builds must include the contributor override documented in the
  README.
- No application-data or database-schema breaking changes are included.
