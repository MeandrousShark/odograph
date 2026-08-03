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
