# Odograph

[![License: AGPL-3.0](https://img.shields.io/github/license/MeandrousShark/odograph)](LICENSE)
[![Latest release](https://img.shields.io/github/v/release/MeandrousShark/odograph)](https://github.com/MeandrousShark/odograph/releases/latest)
[![Test](https://github.com/MeandrousShark/odograph/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/MeandrousShark/odograph/actions/workflows/test.yml)

Odograph is a self-hosted mileage tracker that uses
[OwnTracks](https://owntracks.org/) to record your drives automatically.

It can:

- Detect trips from your phone's location history
- Mark trips as business or personal
- Let you add or edit trips by hand
- Show trip routes on a map
- Track vehicle expenses
- Calculate estimated IRS mileage deductions
- Compare tracked mileage against your vehicle's odometer
- Remind you about trips that still need to be categorized

Your stored trips, points, places, vehicles, expenses, and reports stay on
your own server. Map pages request tiles from OpenStreetMap by default, from
your browser while you view a map. The [privacy guide](docs/privacy.md)
explains exactly what each option sends outside your instance. The
[security guide](docs/security.md) covers the trust model and a hardening
checklist.

![Odograph dashboard with synthetic data](docs/images/usage-dashboard.png)

*Example dashboard using synthetic locations and account data.*

Start with [Install](#install), then see [Use Odograph](docs/usage.md).

> [!NOTE]
> **US users only**: Odograph's deduction reports use IRS standard mileage rates. Trip tracking still works anywhere, but the tax figures are only meaningful for US filers.

## Why Odograph

- **Your location history stays with you.** Trips, points and reports live in
  your own database, not in a vendor's account.
- **No subscription.** Odograph is free, open source software under the
  AGPL-3.0, with no per-user or per-vehicle fees.
- **No custom tracking app.** Any phone running the free
  [OwnTracks](https://owntracks.org/) app can feed it.
- **Built for review, not data entry.** Trips are detected automatically, and
  the review queue lets you mark each one business or personal in a few taps.
- **Exports you can hand off.** Annual and date-range reports download as
  XLSX workbooks, and the trip archive downloads as CSV or XLSX. See
  [What the report looks like](#what-the-report-looks-like).

The tradeoff is that you run the server: you keep it online, keep it updated
and keep backups. Odograph is maintained by one person on a best-effort basis
(see [Support](#support)).

## A quick look

New trips land in the review queue. Each card shows the route, and you set the
category, vehicle and purpose, then move to the next trip:

![Review card for a synthetic trip, with its route map, category, vehicle and purpose fields](docs/images/usage-review.png)

The Trips archive lists every trip with search and filters, and exports
whatever you have filtered:

![Trips archive listing a week of synthetic personal and unclassified trips, with category controls](docs/images/usage-trips.png)

## What the report looks like

The annual report shows business and personal miles (and any non-deductible
miles), your business share, the estimated deduction at your saved IRS
standard mileage rates, and month and vehicle breakdowns. Quarter and custom
date-range reports show the same totals for the dates you pick.

![Annual mileage report for synthetic data, with summary totals, monthly deduction table and per-vehicle breakdown](docs/images/usage-report.png)

**Export XLSX** downloads the report as a workbook:

- **Summary:** business, personal, non-deductible and total miles, business
  share, the rates applied, the total deduction and trip count, then a
  month-by-month table (trips, business miles, rate, deduction), a per-vehicle
  table, and any caveats to review before filing. The annual workbook adds
  odometer reconciliation and a standard versus actual expense comparison
  when you have entered the data for them.
- **Trips:** one row per trip with date, start and end time, duration, start
  and end location, distance in miles and kilometers, category, exclusion,
  vehicle, purpose, notes, gap, source and deduction, ending in a total row.
- **Expenses** (annual report only): date, vehicle, category, amount, tax
  treatment and notes, ending in a total row.

The Trips archive's **Export** menu downloads the trips you have filtered
with the same columns, as CSV or XLSX. See
[Taking data out](docs/usage.md#taking-data-out) for every export.

Deduction figures are estimates for US filers, not tax advice. Odograph does
not decide whether a trip is deductible or recommend a filing method; see
[US deduction limits](docs/usage.md#us-deduction-limits) and review the
report's caveats before you file.

## Install

The recommended path is the full release checkout and
`scripts/generate_env.sh` walkthrough below. If you already manage Compose,
you can use the [minimal Compose installation](docs/install-compose.md) with
only the release's Compose file, `.env.example` and image verification script.

### What you need

- A Linux server or VM
- Git
- Either [Docker Engine](https://docs.docker.com/engine/install/) with the
  [Compose v2 plugin](https://docs.docker.com/compose/install/) (`docker
  compose`) or [Podman](https://podman.io/docs/installation) with
  [podman-compose](https://github.com/containers/podman-compose) 1.3.0 or
  newer. The old `docker-compose` v1 will not work.
- OpenSSL or Python 3 on the host, used to generate your secrets
- `curl`, for health checks
- [cosign](https://docs.sigstore.dev/cosign/system_config/installation/), which
  `scripts/verify_image.sh` uses to verify the release image
- A domain name pointing at the host
- A reverse proxy that terminates HTTPS
- About 2 GB of RAM and 5 GB of free disk to start

Linux is what Odograph is tested and supported on. Docker Desktop or
`podman machine` on macOS or Windows will probably work, but they are not
tested, so they are not claimed. If you try one and hit a problem, open an
issue and I will help track it down.

This checkout's `compose.yaml` uses the signed native PostgreSQL
16.15/PostGIS 3.6.4 image for Linux AMD64 and ARM64. When a target release
uses it, an installation that still uses the older `postgis/postgis:16-3.4`
image must read the
[PostGIS database image upgrade procedure](docs/upgrading.md#upgrading-the-postgis-database-image)
before starting the target release.

Your phone posts location updates all day, so Odograph works best on a machine
that stays awake rather than one that sleeps.

## Quick start

Pick a published version from the
[GitHub Releases page](https://github.com/MeandrousShark/odograph/releases).
Replace `vX.Y.Z` below with that exact release tag:

```sh
git clone --branch vX.Y.Z --depth 1 https://github.com/MeandrousShark/odograph.git
cd odograph
scripts/generate_env.sh
```

Open `.env` and set your timezone before starting the stack. Browser signup
is already disabled:

```env
DISPLAY_TZ=America/Los_Angeles
INITIAL_ADMIN_SIGNUP=0
```

Use your own [IANA timezone name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones).
This sets the first-account suggestion. After setup, **Settings > Preferences**
controls the saved timezone and **Settings > Notifications** the notification
preferences.

That generated file is already a working local-login setup. All three required
secrets are filled in and every optional integration is switched off. You only
need the [configuration reference](docs/configuration.md) when you want to
change something.

### Start Odograph

First pull and verify the release image. The script reads the image from
`compose.yaml`, pulls that exact tag and checks its signature against this
project's release workflow with cosign. It stops with an error if the check
fails:

```sh
scripts/verify_image.sh
```

It uses `docker` when installed and otherwise `podman`; set
`CONTAINER_RUNTIME=podman` to choose Podman explicitly.

The script verifies only the app image in `./compose.yaml`; an override file or
`COMPOSE_FILE` setting that replaces the app image is not verified.

Then start the services. With Docker:

```sh
docker compose up -d
```

Or with Podman:

```sh
podman-compose up -d
```

The app image is the exact release tag pinned by the checked-out release, and
`scripts/verify_image.sh` verified it by digest. `compose.yaml` never pulls the
app image itself, so Compose starts only the verified local image or fails; it
does not fetch an unverified one.

Run `docker compose ps` until both services report `healthy`:

```sh
docker compose ps
# or: podman-compose ps
```

Then check that the app answers on the host:

```sh
curl -fsS http://127.0.0.1:8077/healthz
```

That address is for local health checks and your reverse proxy only. It is not
how you use Odograph day to day.

### Create your account before public access

Create the first administrator from the running app container. This reads the
email and password interactively and accepts no password argument:

```sh
docker compose exec app python -m app.manage_account create-admin
# or: podman-compose exec app python -m app.manage_account create-admin
```

Keep `INITIAL_ADMIN_SIGNUP=0`. The account command works with browser signup
disabled, so the public `/signup` route never lets an unknown visitor create
the first account.

This command is for initial setup only. Once bootstrap completes, deleting the
original administrator does not reopen first-account signup or make
`create-admin` available again. If an account exists, the command refuses to
replace it. For an account that still exists, see
[Password recovery](#password-recovery).

## Set up HTTPS

Point your reverse proxy at `127.0.0.1:8077` and give it your domain, for
example `https://mileage.example.com`. The
[reverse proxy guide](docs/reverse-proxy.md) has worked examples.

Odograph requires HTTPS for browser sign-in, because its session cookies are
marked Secure. Confirm this works before going further:

```text
https://mileage.example.com/healthz
```

Then visit `https://mileage.example.com/login` and sign in with the
administrator account you created above.

You can manage your password and optional sign-in providers later under
**Settings, then Account Settings**.

## Connect OwnTracks

Follow the [OwnTracks guide](docs/owntracks.md) to connect your phone. It walks
through sending a test track, confirming Odograph received it, and deleting the
test data afterward.

Create a device in **Settings > Tracking**, then copy its issued username and
one-time password into OwnTracks. Each credential identifies one device;
`tid` is a label. Once that works, Odograph starts detecting trips on its own.

### How your phone reaches the server

OwnTracks runs in HTTP mode and posts each location update straight to your
instance at `https://your-domain/ingest`. There is no third-party relay. Each
request carries that device's username and password as HTTP Basic auth. For
a device credential issued in Settings, the credential, not the `tid`,
decides which account and device the points belong to.

For that to work, the phone must be able to reach the same HTTPS address you
use in the browser, and it must trust that address's certificate. The server
does not need to be on the public internet: a private network or tunnel
can work, as long as it ends in an HTTPS proxy that forwards to
`127.0.0.1:8077`. Odograph does not test or document a specific VPN or
tunnel product.

If anything else sits in front of your reverse proxy, such as a tunnel, CDN or
second proxy, read
[Trusting forwarded headers](docs/reverse-proxy.md#trusting-forwarded-headers)
and set `FORWARDED_ALLOW_IPS` to match. Set `APP_URL` to the same HTTPS
address so reminder and email links point at it.

## Use Odograph

After you sign in, follow [Use Odograph](docs/usage.md) for a task-oriented
guide to vehicles, trips, review, expenses, odometer readings, reports, and
exports.

## Backups

Before you rely on Odograph, make a backup:

```sh
scripts/backup_database.sh --output backups/first-install.dump
```

The script validates the archive table of contents and writes a checksum
sidecar plus a non-secret manifest. `scripts/restore_database.sh --verify-only`
checks that checksum and table of contents without restoring
anything, so it is useful after copying an archive but does not prove that a
database can be recovered. For a real restore test, follow the fresh-target
procedure in [Backups and disaster recovery](docs/backups.md). Keep the archive,
its `.sha256` and `.manifest` files, and an encrypted copy of `.env` somewhere
off this host.

Your data lives in the Compose `dbdata` volume. Restarts, container recreates,
and ordinary `down` then `up` cycles all keep it. What destroys it is:

```sh
docker compose down -v
```

or the Podman equivalent. Only run that when you actually mean to erase the
database.

## Security

An installation has one administrator and any members the administrator
invites; there is no open signup. The administrator invites members from
**Accounts**; see
[Administrator account management](docs/accounts.md#administrator-account-management).
Account ownership is explicit in queries,
restricted database roles are active, and row-level security is enabled and
forced on every account-owned table. The operator is trusted; do not run
Odograph as a public multi-tenant service. See the
[database role contract](docs/configuration.md#account-ownership-and-database-roles).

Before relying on your installation, work through the
[security hardening checklist](docs/security.md#hardening-checklist).

If you change the default networking, also review
[reverse proxy trust](docs/reverse-proxy.md#trusting-forwarded-headers).

The app container runs as a fixed non-root user, UID and GID `10001`, with a
read-only root filesystem; only a `/tmp` tmpfs is writable. That only matters if you mount a host directory into
it yourself, in which case that path has to be readable, and writable if it is
written to, by `10001`.

## Optional features

Odograph needs none of these. Every setting is in the
[configuration reference](docs/configuration.md#external-services), and if a
feature sends anything off your server the [privacy guide](docs/privacy.md)
spells out what.

- **OIDC sign-in** through a compatible identity provider
- **Reverse geocoding** to turn coordinates into readable addresses
- **ntfy or email reminders** for trips still waiting to be categorized
- **OSRM** for self-hosted road snapping and better route maps

### OIDC

- A second way to sign in to an existing account, using one provider per
  instance.
- Register `https://mileage.example.com/auth/callback` with your provider and
  set the three OIDC variables. Then link it from **Account Settings** with
  your current password.
- The link follows the provider's issuer and subject, so it survives an email
  change at the provider.

See [OIDC sign-in](docs/oidc.md) for setup, linking and sign-out behavior.

### Reverse geocoding

Odograph supports Geoapify as a hosted provider and Nominatim as a self-hosted
one. Read the
[configuration reference](docs/configuration.md#external-services) and the
[privacy guide](docs/privacy.md) before turning either on. Address search in
Settings and in the manual trip form's Search mode shares the
`GEOCODE_MIN_INTERVAL_S` pacer with background reverse lookups.

### OSRM

OSRM is optional and entirely self-hosted. It needs road data prepared for your
region ahead of time, and it can want considerably more memory and disk than
Odograph itself. See the [OSRM guide](docs/osrm.md).

## Password recovery

While signed in, change your password in **Settings > Account Settings**. If
your login email is verified, use **Forgot password?** on the sign-in page for
an emailed reset link. Otherwise, find the account ID and reset it from the
server:

```sh
docker compose exec app python -m app.manage_account list-accounts
docker compose exec app python -m app.manage_account reset-password ACCOUNT_ID
# or: podman-compose exec app python -m app.manage_account reset-password ACCOUNT_ID
```

See [Account management](docs/accounts.md) for the details, session effects,
login email changes and sign out everywhere.

## Updating

When a new release comes out, follow the [upgrading guide](docs/upgrading.md).
Take a fresh backup and verify it first, then move to the exact release tag you
want. Any 1.x release upgrades directly to any later 1.x release. Moving from
0.x is covered in [Upgrading to 1.0](docs/upgrading.md#upgrading-to-10).
Watch the GitHub Releases page to hear about new versions.

The registry also publishes floating tags: one for each minor version, such as
`v1.0`, which points at the newest patch within that minor release, and `v1`,
which points at the newest verified stable 1.x release. Use a floating tag only
when you have chosen that update policy deliberately and have read the release
notes. There is no `latest` tag. Pulling a floating tag again selects the
current release, not the one you ran before, so record the installed image
digest first; see
[Floating image aliases](docs/upgrading.md#floating-image-aliases). Releases
can add one-way database migrations, so moving back means restoring a backup
rather than switching the image.

## Contributing

Development happens in this repository through pull requests. Its `main`
branch may contain unreleased work, so use an exact release tag for
installations. Want to run the test suite or work on the code? See
[CONTRIBUTING.md](CONTRIBUTING.md) for development setup and local instructions.
AI-assisted contributions are welcome under the same standard as any other; see
[AI and contributions](ai-contribution.md).

## Support

Odograph is self-hosted open-source software, not a hosted service. The
latest 1.x release is supported, by one maintainer, on a best-effort basis.
There is no service-level agreement or guaranteed response time. After v2.0.0
ships, v1 receives critical security fixes for 6 months, and 0.x releases are
unsupported once 1.0 ships. See the
[support policy](docs/upgrading.md#support-policy).

Check [Known issues](docs/known-issues.md) for confirmed problems in
published releases and their workarounds. Open a GitHub issue for bugs and
feature requests. See [SECURITY.md](SECURITY.md) for security reporting and
supported versions, and [CONTRIBUTING.md](CONTRIBUTING.md) for project scope.

## License

AGPL-3.0. In short: you are free to use, modify, and self-host this project,
but if you run a modified version as a network service that other people use,
you must offer that version's source to those users. See [LICENSE](LICENSE) for
the full text.

Copyright (C) 2026 Michael Hannon.
