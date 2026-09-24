# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Development history before the first public release is private and is not
reproduced here.

## [Unreleased]

### Added

- Native signed PostgreSQL 16.15/PostGIS 3.6.4 images for supported Linux
  AMD64 and ARM64 hosts, published as the immutable
  `ghcr.io/meandrousshark/odograph-postgis` multi-architecture index.

### Changed

- The canonical Compose file and CI database checks use the pinned native
  PostGIS image. Existing installations moving from `postgis/postgis:16-3.4`
  must follow the [fresh-target database image migration procedure](docs/upgrading.md#upgrading-the-postgis-database-image)
  before starting the target stack.
- Schema 26 assigns existing personal data, settings, and tracker progress to
  the installation's established account, preserving its IDs and history.
  Requests, workers, and portable operations use explicit account-bound
  connections under restricted database roles whose exact permissions are
  validated at startup. RLS policies are prepared with enforcement disabled,
  and the installation remains single-account. The migration refuses a
  populated database that has no account; follow the
  [schema 26 upgrade notes](docs/upgrading.md#account-ownership-migration-schema-26).
- Personal settings and mileage-rate overrides are imported once from the old
  configuration and are then changed in Settings. Editing their environment
  variables no longer overrides stored preferences.
- Tracker labels become account-owned devices, and Settings > Tracking issues,
  rotates, and revokes per-device credentials. An issued username is a
  readable slug of the device name plus a short random suffix (for example
  `work-iphone-7k3q`), and the setup card adds a Copy button beside the URL,
  username, and password. `INGEST_USERNAME` and `INGEST_PASSWORD` are
  imported once on upgrade as a legacy adapter; editing them later does not
  rotate, restore, or recreate a credential.
- Temporary ingest throttling answers 503 instead of 429, because OwnTracks
  for iOS deletes a queued fix on any 4xx response.
- Portable exports use format 3. Imports still accept format 1 and 2 bundles.
- Schema-26 backups include protected database-role state, and the matching
  restore reconstructs permissions before startup. Rolling back after the
  migration requires restoring the verified pre-upgrade archive with the
  previous exact image.

### Fixed

- Restore legacy PostGIS backups into fresh images that do not pre-create
  extension schemas. Schema preparation and archive restore now share one
  transaction.
- Refuse to attach a newly created QA database container to an orphaned
  persistent volume; recover it with its original image or a verified backup.

### Security

- Ingest stores only `location`, `transition`, `waypoint`, and `waypoints`
  messages. Other message types, including the configuration dump OwnTracks'
  Publish Settings button sends (which contains the tracker's plaintext
  password), are acknowledged and discarded instead of being stored in
  `raw_messages`.
- Schema 27 deletes the `dump` and `configuration` messages that earlier
  releases stored in `raw_messages`. Backups made before upgrading can still
  contain them; see [Backups](docs/backups.md#what-the-archive-covers-and-what-it-doesnt).

## [0.11.2] - 2026-09-23

### Changed

- The inline trip editor on the Dashboard and Trips list now shows the trip's
  date, time, route and distance above the form, so it is clear which trip is
  being edited.

### Fixed

- Split trip works again. Confirm split no longer relies on an
  expression the Content Security Policy blocks, which previously stopped the
  request from being sent in v0.6.0 through v0.11.1. Split mode now explains
  how to pick a point, keeps the map and confirmation together, marks the
  chosen point and shows progress. See [Known issues](docs/known-issues.md).
- Failed requests now show the reason in a banner instead of leaving the page
  unchanged.
- On desktop, the current-page bar under Settings and Account Settings now sits
  on the header line and spans the control, matching the other pages.

### Correcting road-snapped trips from before v0.11.1

If you are upgrading from v0.11.0 or earlier and have not applied the v0.11.1
road-snap correction, apply it now. Save the values it changes first, so the
correction can be reversed without restoring a backup. In `psql`:

```sql
\copy (SELECT id, distance_snapped_m FROM trips WHERE distance_snapped_m IS NOT NULL AND distance_m > 0 AND distance_snapped_m / distance_m < 0.85) TO 'snap-correction.csv' CSV HEADER

UPDATE trips SET distance_snapped_m = NULL
WHERE distance_snapped_m IS NOT NULL
  AND distance_m > 0
  AND distance_snapped_m / distance_m < 0.85;
```

Keep `snap-correction.csv` with your backups. The saved file lives outside
the database, so it adds no table that a later upgrade would have to account
for.

### Supported upgrade path

- `v0.11.1` and `v0.10.2` may upgrade directly to `v0.11.2`. Earlier releases
  should first follow the supported upgrade path to `v0.10.2`.

### Breaking changes

- There are no breaking application, database, configuration, or operational
  changes. Schema version 25, detector version 2, and portable format 2 are
  unchanged.

## [0.11.1] - 2026-09-20

### Fixed

- **A partial road-snap no longer reports itself as the whole trip.** When a
  trace leaves the provisioned OSRM extract, OSRM matches only the spans it
  has road data for and returns null tracepoints for the rest. The length of
  that fragment was stored and used as the trip's distance, so the unmatched
  miles left the displayed figure, every mileage total, and the deduction,
  and the map framed only the matched part. A snapped route covering less
  than 85% of the trip's own raw GPS distance is now recorded without a
  snapped distance, which falls the display back to the raw distance. The
  partial route is still drawn, beneath the raw track rather than in place of
  it, and no longer takes over the map framing. The trip page reads
  **Road-snap incomplete, showing raw GPS distance**. See
  [docs/osrm.md](docs/osrm.md).

### Correcting existing trips

Trips snapped before this release keep their stored partial distance until
they are next re-snapped. To correct them in place, against a database you
have backed up first:

```sql
UPDATE trips SET distance_snapped_m = NULL
WHERE distance_snapped_m IS NOT NULL
  AND distance_m > 0
  AND distance_snapped_m / distance_m < 0.85;
```

There is no schema change and nothing to migrate. The statement is not
reversible without that backup, because it discards the partial value.

### Supported upgrade path

- `v0.11.0` and `v0.10.2` may upgrade directly to `v0.11.1`. Earlier releases
  should first follow the supported upgrade path to `v0.10.2`.

### Breaking changes

- There are no breaking application, database, configuration, or operational
  changes. Schema version 25, detector version 2, and portable format 2 are
  unchanged.

## [0.11.0] - 2026-09-14

### Added

- **Archive matching selection.** **Select all matching** selects every trip
  matching the current filters across unloaded pages and months as a
  request-time snapshot. Explicit selections persist through pagination and
  successful bulk-update refreshes, while filter or history navigation clears
  them.

### Changed

- **Dashboard week navigation.** A **Current week** link appears beside the
  week arrows when viewing another week, and the week heading remains a
  shortcut to the current week.
- **Trips selection actions.** Failed requests and actions preserve the
  selection, and successful **Delete selected** clears it. Desktop keeps the
  bulk controls together; on phones, a compact selected-count strip has an
  **Actions** button that opens a panel with the same controls.

### Security

- Add release preflight checks for native amd64 and arm64 builds, dependency
  and image vulnerabilities, release metadata, and SBOM generation before
  creating an immutable release tag.

### Supported upgrade path

- `v0.10.0` and `v0.10.2` may upgrade directly to `v0.11.0`. Earlier releases
  should first follow the supported upgrade path to `v0.10.0`.

### Breaking changes

- There are no breaking application, database, configuration, or operational
  changes. Schema version 25, detector version 2, and portable format 2 are
  unchanged.

## [0.10.2] - 2026-09-12

### Added

- Delete selected trips from the Trips bulk action bar, with confirmation and
  all-or-nothing deletion that preserves linked expenses and location data.

### Fixed

- Oversized avatar uploads show an error within Account Settings and keep the
  upload form usable.
- Settings and Account Settings show the desktop navigation accent bar when
  they are the current page, with a matching mobile account indicator.
- The mileage rate explanation follows the Settings help-text width while
  keeping the rate table independently scrollable.
- Enable safe-area viewport sizing so mobile navigation can reserve space for
  the home indicator, with content padding for screen cutouts.

### Security

- Refresh the Python 3.13 runtime base image to include current OS package
  security patches.

### Supported upgrade path

- `v0.10.0` may upgrade directly to `v0.10.2`. Earlier releases should first
  follow the supported upgrade path to `v0.10.0`.

### Breaking changes

- There are no breaking application, database, configuration, or operational
  changes.

## [0.10.1] - 2026-09-12

Unpublished: image vulnerability scans failed before release publication. The
immutable source tag is retained; use v0.10.2 instead.

### Added

- Delete selected trips from the Trips bulk action bar, with confirmation and
  all-or-nothing deletion that preserves linked expenses and location data.

### Fixed

- Oversized avatar uploads show an error within Account Settings and keep the
  upload form usable.
- Settings and Account Settings show the desktop navigation accent bar when
  they are the current page, with a matching mobile account indicator.
- The mileage rate explanation follows the Settings help-text width while
  keeping the rate table independently scrollable.
- Enable safe-area viewport sizing so mobile navigation can reserve space for
  the home indicator, with content padding for screen cutouts.

### Supported upgrade path

- `v0.10.0` may upgrade directly to `v0.10.1`. Earlier releases should first
  follow the supported upgrade path to `v0.10.0`.

### Breaking changes

- There are no breaking application, database, configuration, or operational
  changes.

## [0.10.0] - 2026-09-10

Recordkeeping and interface redesign release. Odograph can now distinguish
trips that should not be included in particular mileage totals, attach
trip-specific expenses, and retain optional endpoint labels for manual trips.
The main pages also receive a responsive visual redesign, with a new weekly
Dashboard and denser trip workflows. This release includes migrations 022
through 025.

### Added

- **Trip exclusions.** Mark a trip as `Not one of my vehicles` when it does
  not belong in any vehicle total, or as `My vehicle, someone else drove`
  when the miles belong in the vehicle total but not the mileage deduction.
  Exclusions are available in Trip Detail, inline editing, Review, manual
  entry, batch actions, and archive filters, and are reflected in Dashboard,
  Reports, Stats, expenses, odometer reconciliation, notifications, exports,
  and portable bundles.

- **Trip-linked expenses.** Link an expense to the trip that incurred it and
  create trip-specific expenses from Trip Detail. Vehicle, date, and excluded
  trip conflicts are shown as warnings while preserving both records. A link
  is detached if its trip is deleted, including during reprocessing;
  reprocessing logs the detachment for operator visibility.

- **Account avatars.** Upload, display, and remove an administrator avatar
  from Account Settings. Avatar files are checked for supported image content,
  size, and dimensions before they are stored.

- **Manual trip endpoint labels.** Add optional custom start and end labels to
  unrouted manual trips. Labels are shown in trip details, archive results,
  editing surfaces, exports, portable bundles, and archive search, with a
  100-character limit.

- **Weekly Dashboard.** View weekly mileage totals, category and exclusion
  states, attention items, and dense trip rows with inline classification.

- **Public usage and installation guides.** The README now provides separate
  install and usage paths. New guides cover first use and a minimal two-file
  Compose installation, with updated OwnTracks and operator documentation.

### Changed

- **Interface redesign.** The application has a responsive desktop and mobile
  shell, shared controls and icons, System/Light/Dark themes, and browser-local
  Purple, Blue, Green, and Red accent choices. Dashboard, Trips, Review, Trip
  Detail, Report, Stats, Expenses, Settings, and authentication pages now use
  the redesigned layouts.

- **Trips archive.** The archive now uses dense responsive rows, date presets,
  filter controls, canonical filter URLs, in-place history updates, paginated
  month results, persistent selection for bulk actions, inline classification,
  and a dedicated manual-trip page.

- **Review and Trip Detail.** Review uses explicit Next, Skip, and Undo
  actions with redesigned category controls and mobile behavior. Category
  selection stays as a draft until Next saves it with the other visible fields
  and advances. Review has no custom keyboard shortcuts. Trip Detail provides
  the updated route, expense, edit, and endpoint-label presentation.

- **Portable data compatibility.** The portable bundle format advances from
  format 1 to format 2 to carry trip exclusions and expense links. Format 1
  bundles remain importable, with omitted new fields treated as unset.

### Fixed

- Responsive layouts and touch controls were corrected across the redesigned
  pages, including narrow screens, mobile action areas, native disclosure
  controls, manual-entry controls, file inputs, and expense forms.

- Archive edits, deletion, and classification refresh filtered results and
  month/year totals. Older navigation responses cannot overwrite newer changes.
  Totals continue to exclude `Not one of my vehicles` trips.

- Manual route previews discard stale results when endpoints change and
  preserve distances entered by the user.

- Review saves run in order so older autosaves cannot overwrite newer edits
  or race with Next and Skip.

### Supported upgrade path

- `v0.9.2` may upgrade directly to `v0.10.0`. Earlier releases should first
  upgrade to `v0.9.2`.
- The database schema advances from 21 to 25 through migrations 022, 023,
  024, and 025. The migrations add nullable trip exclusion, expense-link,
  account-avatar, and manual-endpoint-label fields, so existing records retain
  their prior behavior.
- Apply the migrations in order against the existing database. Take and
  validate a database backup before upgrading.

### Breaking changes

- None. There are no required configuration changes, route or API contract
  breaks, new runtime dependencies, or detector-version changes. OSRM remains
  optional.
- The migrations are forward-only. If an application rollback is required
  after migration, restore the validated pre-upgrade database backup and
  discard writes made after the upgrade before starting the earlier version.

## [0.9.2] - 2026-08-26

Release tooling maintenance. This release repairs the disposable upgrade and
rollback drill for the account model introduced in `v0.8.0`, and adds a
verified pre-deploy database backup path for installations managed directly by
Podman or Docker rather than Compose.

There is no application behavior change and no database migration. The schema
remains at 21.

### Added

- **Direct-container database backups.** `scripts/backup_database.sh` now
  accepts `--container NAME` and runs through an explicitly selected or
  autodetected Podman or Docker runtime. The direct-container path retains the
  same archive verification, checksum, manifest, overwrite protection, and
  failure cleanup as the existing Compose path. This gives Quadlet deployments
  a scripted, gated pre-deploy backup without requiring a Compose frontend.

### Fixed

- **Upgrade and rollback drills now support the current account model.** The
  drill capability-detects the legacy `/setup` flow or the `v0.8.0` and later
  `/signup` flow, validates the matching authentication schema after rollback,
  and reports missing environment keys instead of aborting silently. The
  legacy `v0.7.6` to `v0.8.0` path remains supported.

### Supported upgrade path

- `v0.9.1` may upgrade directly to `v0.9.2`. Earlier releases should upgrade
  to `v0.9.1` first.

### Breaking changes

- None. There is no migration; the schema stays at 21. There are no
  application, configuration, runtime-dependency, or detector-version changes.

## [0.9.1] - 2026-08-26

Interface, analytics, and manual-routing release. The trip archive is now
searchable, the Stats page supports longer-horizon analysis and drill-downs,
the review flow is faster from the keyboard, and a manual trip can carry a
router-computed path and distance. Authentication pages and narrow-screen
layouts also receive a consistency and accessibility pass.

There is no database migration in this release; the schema remains at 21.
OSRM remains optional, and every existing workflow continues to work without
it.

`0.9.0` was tagged but never published: its release build was stopped by the
blocking image scan, and a release is recut under a new version rather than
retried under the same one. `0.9.1` is that recut and carries the same
application changes plus the base-image refresh described under Security.

### Added

- **Routed manual trips.** When OSRM is configured, a manual trip can now be
  routed between two named places or two points selected on a map. The form
  previews the route, fills in the routed distance, and lets you override that
  distance before saving. The saved trip carries its route and endpoints, and
  its detail page shows the route on the existing map.

  Routing is an enhancement rather than a requirement. A trip with no route
  selection saves exactly as before. If routing is unavailable, a trip with a
  hand-entered distance still saves without geometry and explains what
  happened; a blank distance returns to the form for correction.

- **Text search across the trip archive.** `/trips` can search notes, business
  purpose, start and end place names, and cached start and end addresses,
  case-insensitively and by substring. Search combines with category, date,
  and vehicle filters and follows the trip set through archive pagination,
  export, and the review flow.

- **Long-horizon Stats views.** The Stats page gains year navigation,
  arbitrary date-range and vehicle filters, a five-year monthly comparison,
  a quarterly business-versus-personal share chart, and a per-vehicle table of
  mileage, expenses, and mileage deduction. Monthly, weekly, and year-over-year
  chart bars link to the corresponding filtered trip archive.

- **One-step review undo.** The review page now has a clickable `Undo (z)`
  action that restores the immediately preceding classification or skip,
  including after the final card. A status message confirms the restored trip
  and count. Undo is intentionally kept in browser memory for the current
  review pass rather than creating durable history.

### Changed

- **Review is more keyboard-friendly and preserves visible edits.** `b`, `p`,
  `s`, and unmodified `z` perform Business, Personal, Skip, and Undo while
  normal browser Ctrl/Cmd+Z behavior and focus and dialog guards remain intact.
  Purpose, Vehicle, and Notes now save atomically with either a classification
  or a skip, and an unassigned trip visually selects the active default vehicle
  until the next action saves it.

- **Stats charts are easier to read and explore.** All four bar charts now
  have numeric Y-axis labels, gridlines, and per-bar hover tooltips. Mileage
  axes use rounded mile values, the category-share chart uses percentages,
  and wide multi-quarter charts allocate enough space to keep their labels
  distinct.

- **Authentication and shared controls use a more consistent layout.** Sign
  in, initial signup, legacy account establishment, and Account Security now
  use centered card surfaces with consistent spacing. Repeated dialog, field,
  vehicle-selection, notes, and card patterns now share common components.
  Editable notes fields have more horizontal padding, Settings action columns
  align consistently, and the dashboard's manual-trip action matches its
  neighboring archive action.

- **The public documentation is easier to follow.** The README was rewritten
  in plain language, the OwnTracks guide now covers practical battery and
  region settings on iOS and Android, and the OSRM, privacy, security, and
  configuration guides describe routed manual trips. The install guide also
  documents floating minor image tags such as `v0.9` for operators who want
  patch updates without crossing a minor release.

- **Contributor test installs are reproducible.** CI and contributor guidance
  now use an exact Python 3.13 test-dependency lock, and a task-isolated helper
  starts disposable PostGIS databases safely for concurrent test runs.

### Fixed

- **OpenStreetMap tiles load again under the application's security headers.**
  The previous `same-origin` referrer policy suppressed the header that the
  public tile service requires, producing a 403 error tile on maps. The policy
  is now `strict-origin-when-cross-origin`, which sends only the deployment
  origin to a cross-origin tile host, never the viewed path or query string.

- **Narrow layouts keep content and controls usable.** Review actions now fit
  at 320px, report tables, the per-vehicle Stats table, and the Diagnostics
  Workers table scroll within their own sections, and native disclosure
  controls meet the existing 44px minimum height with centered labels. The
  trip search and filter bar also wraps without squeezing labels or creating
  horizontal page overflow.

### Security

- **Manual-route preview and saving do not trust the browser.** The preview is
  authenticated, same-origin, and CSRF-protected. Final submission resolves
  the selected endpoints and calls OSRM again on the server instead of
  accepting client-supplied geometry. Coordinates, distances, and GeoJSON are
  validated, and routing failures do not expose the OSRM address,
  configuration, coordinates, or internal exceptions.

- **Refreshed the container base image.** The base is repinned to a current
  `python:3.13-slim` digest, which picks up the distribution's fixes for four
  util-linux advisories, and the image additionally upgrades openssl to the
  version that fixes `CVE-2026-14456`. A digest pin is reproducible but does
  not receive rebuilt packages, so it is now re-resolved as part of preparing
  a release.

- **Removed pip from the published image.** Nothing at runtime resolves or
  installs packages, so pip and its vendored dependencies are no longer
  shipped. This removes the vendored `msgpack` and `setuptools` copies that
  recent pip releases declare in their own SBOM. The image scans with no
  fixable HIGH or CRITICAL findings and no scan acceptances.

### Supported upgrade path

- `v0.8.0` may upgrade directly to `v0.9.0`. Earlier releases should upgrade
  to `v0.8.0` first.

### Breaking changes

- None. There is no migration; the schema stays at 21. There are no required
  configuration changes, no new runtime dependencies, and no detector-version
  change. OSRM remains optional.

## [0.8.0] - 2026-08-11

Installation and sign-in release. A new instance is now set up the way most
self-hosted applications are: obtain the release files, generate `.env`, run
Compose once, and create the administrator account in the browser. There is no
bootstrap token to copy out of a log and no second edit of `.env`. Sign-in also
becomes a single account that can hold both a password and a linked identity
provider, replacing the two unrelated ways into the same instance.

Odograph is still a single-user application. Exactly one account exists, it is
always the administrator, and all trips and settings remain instance-wide. The
database enforces that limit. This release adds the account groundwork that a
future multi-user version can build on, without adding a second user.

This release contains two database migrations and takes the schema from 19 to
21. Read the "Breaking changes" and "Supported upgrade path" sections below
before upgrading, and take a verified backup first.

### Added

- **Create the first administrator in the browser.** A brand-new instance
  offers an account-creation form on first visit, where you choose an email
  address and password. That first account becomes the sole administrator, and
  both signup routes close permanently once it exists. Two people submitting
  the form at the same moment still produce exactly one account.

- **A new Account Security page, linked from Settings.** It shows your login
  email, lets you change your password, shows whether an identity provider is
  configured and linked, and offers the link and unlink actions along with
  short recovery guidance.

- **Optional linked single sign-on.** A signed-in administrator can
  deliberately link the configured OIDC provider to their account by re-entering
  their password and completing a provider authorization. After that, either the
  password or the provider signs you into the same account. The link is anchored
  to the provider's stable issuer and subject, so a changed email address at the
  provider does not break sign-in. A matching email address on its own never
  creates or selects a link. Unlinking asks for your current password and leaves
  password login working.

- **An operator recovery command.** If you are locked out or the provider is
  unavailable, you can create the missing first account or reset the existing
  password from inside the application container:

  ```sh
  docker compose exec app python -m app.manage_account create-admin
  docker compose exec app python -m app.manage_account reset-password
  ```

  The same commands work through `podman-compose exec app`. Passwords are read
  interactively or from protected standard input, never from a command-line
  argument, and no password, hash, or database URL is printed.

- **A grouped environment-variable reference at `docs/configuration.md`.**
  Every supported variable is documented there by area: authentication,
  detector behavior, interface, retention, external services, and worker
  scheduling.

- **A release consistency check.** Release preparation and the release workflow
  now verify that the changelog section, the Compose image tag, the documented
  install instructions, and the version the running application reports all
  agree before anything is published.

### Changed

- **`.env.example` is now the runnable baseline rather than a full catalog.**
  It contains the three generated secrets, the initial-signup setting, the
  display timezone, and the safe loopback proxy default. Optional integrations
  stay unset, and the exhaustive list moved to `docs/configuration.md`. The
  generator still refuses to overwrite an existing `.env`, applies restrictive
  permissions, and never prints a generated secret.

- **The README separates the install path from everything optional.** The
  critical path is a short ordered sequence pinned to an exact release tag,
  followed by a post-install checklist covering the reverse proxy, Account
  Security, OwnTracks, backups, optional services, and security hardening.

- **Sessions now identify an account rather than a standalone login.** Both
  password and provider sign-in produce the same kind of session and the same
  access.

### Removed

- **`ADMIN_TOKEN` and the `/setup` bootstrap page.** Neither is part of
  installation, sign-in, or recovery any more. An existing `.env` that still
  contains `ADMIN_TOKEN` starts normally; the value is ignored, is never
  printed, and cannot enable any authentication path.

### Security

- **Sensitive changes sign out other sessions.** Changing your password,
  resetting it with the operator command, and unlinking the provider all
  invalidate previously issued sessions for the account. The browser that
  changed the password keeps a valid replacement session.

- **Public registration cannot open by accident on upgrade.** The
  `INITIAL_ADMIN_SIGNUP` setting is treated as disabled whenever it is absent,
  so upgrading an existing installation never exposes an account-creation form.
  It is also ignored entirely once an account exists, so there is no cleanup
  step after signup.

- **The database, not only a route check, enforces the single-account limit.**
  A second account cannot be inserted while this constraint exists.

- **Account creation, password changes, recovery, linking, and unlinking are
  CSRF-protected** and use the existing failed-login rate limiter where
  credentials are checked. Failures are generic and do not reveal whether an
  unrelated identity exists.

- **Provider tokens are not retained.** Odograph stores the linked issuer,
  subject, and safe display metadata, and discards access, refresh, and ID
  tokens after the callback. No password material, token, or raw provider claim
  appears in the interface, logs, or errors.

### Supported upgrade path

- `v0.7.6` may upgrade directly to `v0.8.0`. Earlier releases should upgrade to
  `v0.7.6` first.

### Breaking changes

- **Two forward-only database migrations run, taking the schema from 19 to 21.**
  Migration 020 moves an existing local administrator into account ID 1 without
  changing its normalized email or password hash, so the old password keeps
  working. Migration 021 adds the linked-identity table. No trip, point, report,
  or settings row becomes user-owned.

- **Once those migrations commit, starting the previous image against the same
  database is not a supported rollback.** The supported way back is restoring
  the verified pre-upgrade backup into a fresh volume, which also discards any
  account or linked identity created after that backup. Take and verify a
  backup before upgrading.

- **`/setup` no longer exists and `ADMIN_TOKEN` no longer does anything.** Any
  bookmark, script, or automation that relies on either will fail.

- **An installation that previously used the identity provider with no local
  account must complete a one-time transition.** Public signup stays closed on
  upgrade. Sign in with the existing provider and continue at
  `/account/establish`, which sets a local password and links the current
  provider identity together in one step. Where `ALLOWED_EMAIL` is configured it
  continues to gate that one transition; where it is not, any identity the
  provider accepts can reach it, which preserves the installation's existing
  trust boundary. Use the operator command instead if that boundary is too broad
  or the provider is down.

- **`ALLOWED_EMAIL` is no longer an authorization substitute for a linked
  identity.** It is retained only for the legacy transition above and is
  documented as deprecated.

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
