# Odograph

[![License: AGPL-3.0](https://img.shields.io/github/license/MeandrousShark/odograph)](LICENSE)
[![Latest release](https://img.shields.io/github/v/release/MeandrousShark/odograph)](https://github.com/MeandrousShark/odograph/releases/latest)
[![Test](https://github.com/MeandrousShark/odograph/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/MeandrousShark/odograph/actions/workflows/test.yml)

Odograph is a self-hosted mileage tracker. Your phone sends its location to
your own server through the free [OwnTracks](https://owntracks.org/) app,
Odograph turns that into trips with routes on a map, and you mark each one
business or personal. You can also add or edit trips by hand, track vehicle
expenses, compare tracked mileage against your odometer, get reminders about
trips that still need a category, and calculate estimated IRS mileage
deductions.

![Odograph dashboard with synthetic data](docs/images/usage-dashboard.png)

*Example dashboard using synthetic locations and account data.*

1. Self-host Odograph.
2. Connect your phone through OwnTracks.
3. Drive normally, then review and categorize trips.

Start with [Install](#install), then see [Use Odograph](docs/usage.md).

> [!NOTE]
> **US users only**: Odograph's deduction reports use IRS standard mileage rates. Trip tracking still works anywhere, but the tax figures are only meaningful for US filers.

## How well does detection work?

**What happens when GPS reception disappears?** A gap alone does not end a
trip. When points resume farther along, it stays one trip, measured in a
straight line across the gap, with a **Recording gap** badge if it lasted over
10 minutes. A gap of five minutes or more averaging under about 4.5 mph looks
like a stop, so the trip splits and those miles are lost.

**Does it distinguish driving from walking?** Mostly by speed, and not by
travel mode. A normal walk is not a trip, but a bike ride, a run, a bus or a
train will show up as one, and you can delete it or mark it **Not one of my
vehicles**. Sitting still for five minutes or more ends a trip, and Merge
fixes that.

**Can I correct an incorrectly detected trip?** Yes. You can set its details,
merge or split trips, delete and restore them, and add manual trips. You
cannot edit a detected trip's times or distance, so if those are wrong, delete
it and add a manual trip.

**How much battery does the tracking consume?** It depends on the phone app
and its mode. On the maintainer's iPhone, OwnTracks regularly accounts for 2
to 4% of total battery use on days with about 75 miles of driving, using
regions to switch modes. That is one phone rather than a benchmark, and we
have not measured Android.

The [full answers](docs/detection.md) cover each case, including phones that
lose data service.

## Why Odograph

- **Your location history stays with you.** Everything lives in your own
  database, not a vendor's account. The app has no telemetry, analytics or
  update checks: the server makes no outbound request unless you turn on an
  optional integration, and by default your browser fetches map tiles from
  OpenStreetMap while you view a map. The [privacy guide](docs/privacy.md)
  explains exactly what each option sends.
- **No subscription.** Free, open source software under the AGPL-3.0, with no
  per-user or per-vehicle fees.
- **No custom tracking app.** Any phone running the free
  [OwnTracks](https://owntracks.org/) app can feed it, or
  [Traccar Client](docs/traccar.md).
- **Built for review, not data entry.** Trips are detected automatically, and
  the review queue lets you mark each one in a few taps.
- **Exports you can hand off.** Reports download as XLSX workbooks, and the
  trip archive as CSV or XLSX.

The tradeoff is that you run the server: you keep it online, keep it updated
and keep backups. Odograph is maintained by one person on a best-effort basis
(see [Support](#support)).

## A quick look

New trips land in the review queue. Each card shows the route, and you set the
category, vehicle and purpose:

![Review card for a synthetic trip, with its route map, category, vehicle and purpose fields](docs/images/usage-review.png)

The Trips archive lists every trip with search and filters, and exports
whatever you have filtered:

![Trips archive listing a week of synthetic personal and unclassified trips, with category controls](docs/images/usage-trips.png)

Reports show your mileage, business share, estimated deduction, and month and
vehicle breakdowns, and download as [workbooks](docs/usage.md#report-workbooks):

![Annual mileage report for synthetic data, with summary totals, monthly deduction table and per-vehicle breakdown](docs/images/usage-report.png)

Deduction figures are estimates for US filers, not tax advice; see
[US deduction limits](docs/usage.md#us-deduction-limits).

## Install

The recommended path is the full release checkout below. If you already manage
Compose, see the [minimal Compose installation](docs/install-compose.md).

### What you need

- A Linux server or VM, AMD64 or ARM64, that stays awake, with about 2 GB of RAM
  and 5 GB of free disk
- Git, `curl`, and OpenSSL or Python 3
- [Docker Engine](https://docs.docker.com/engine/install/) with the
  [Compose v2 plugin](https://docs.docker.com/compose/install/), or
  [Podman](https://podman.io/docs/installation) with
  [podman-compose](https://github.com/containers/podman-compose) 1.3.0 or newer.
  The old `docker-compose` v1 will not work.
- [cosign](https://docs.sigstore.dev/cosign/system_config/installation/), to
  verify the release image
- A domain name and a reverse proxy that terminates HTTPS

Docker Desktop or `podman machine` on macOS or Windows will probably work, but
they are not tested. If you hit a problem, open an issue and I will help track
it down.

## Quick start

Pick a version from the
[Releases page](https://github.com/MeandrousShark/odograph/releases) and use
that exact tag for `vX.Y.Z`:

```sh
git clone --branch vX.Y.Z --depth 1 https://github.com/MeandrousShark/odograph.git
cd odograph
scripts/generate_env.sh
```

That writes a working `.env`. Set your
[timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) in it.
Browser signup is already disabled:

```env
DISPLAY_TZ=America/Los_Angeles
INITIAL_ADMIN_SIGNUP=0
```

### Start Odograph

Pull the release image and verify its signature. This stops with an error if
the check fails:

```sh
scripts/verify_image.sh
```

Then start the services:

```sh
docker compose up -d
# or: podman-compose up -d
```

The app image is the exact release tag pinned by the checked-out release, and
Compose starts only the verified local image. When `docker compose ps` shows
both services `healthy`, check that the app answers:

```sh
curl -fsS http://127.0.0.1:8077/healthz
```

### Create your account before public access

This reads the email and password interactively:

```sh
docker compose exec app python -m app.manage_account create-admin
# or: podman-compose exec app python -m app.manage_account create-admin
```

Keep `INITIAL_ADMIN_SIGNUP=0`, so the public `/signup` route never lets an
unknown visitor create the first account.

## Set up HTTPS

Point your reverse proxy at `127.0.0.1:8077` and give it your domain. The
[reverse proxy guide](docs/reverse-proxy.md) has worked examples. Browser
sign-in requires HTTPS, because session cookies are marked Secure. Check
`https://mileage.example.com/healthz`, then sign in at
`https://mileage.example.com/login`.

## Connect OwnTracks

Create a device in **Settings > Tracking**, copy its username and one-time
password into OwnTracks, and follow the [OwnTracks guide](docs/owntracks.md) to
send a test track. Odograph then detects trips on its own, and
[Use Odograph](docs/usage.md) covers the rest.

The phone posts straight to `https://your-domain/ingest` with no third-party
relay, so it must reach the same HTTPS address as your browser and trust its
certificate. The server does not need to be on the public internet: a private
network or tunnel can work if it ends in an HTTPS proxy that forwards to
`127.0.0.1:8077`, though Odograph does not test or document a specific VPN or
tunnel product. If something sits in front of your reverse proxy, read
[Trusting forwarded headers](docs/reverse-proxy.md#trusting-forwarded-headers).

## After you install

- **Backups:** run `scripts/backup_database.sh --output backups/first-install.dump`,
  then keep the archive and an encrypted copy of `.env` off this host. See
  [Backups and disaster recovery](docs/backups.md).
- **Security:** there is one administrator and invited members, with no open
  signup. Do not run Odograph as a public multi-tenant service. Work through the
  [hardening checklist](docs/security.md#hardening-checklist).
- **Optional features:** [OIDC sign-in](docs/oidc.md), linked from
  **Account Settings**; reverse geocoding; ntfy or email reminders; and
  [OSRM](docs/osrm.md) road snapping. Odograph needs none of them. See the
  [configuration reference](docs/configuration.md#external-services).

## Password recovery

Use **Forgot password?** on the sign-in page if your email is verified, or
[reset it from the server](docs/accounts.md#recover-from-the-server).

## Updating

Follow the [upgrading guide](docs/upgrading.md): back up, verify, then move to
an exact release tag. Any 1.x release upgrades directly to any later 1.x
release. Moving back means restoring a backup.

## Contributing

Development happens here through pull requests, and `main` may contain
unreleased work. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and project
scope, and [AI and contributions](ai-contribution.md).

## Support

Odograph is maintained by one person on a best-effort basis, with no guaranteed
response time. Check [Known issues](docs/known-issues.md), then open a GitHub
issue for bugs, features or setup help. See the
[support policy](docs/upgrading.md#support-policy) and
[SECURITY.md](SECURITY.md).

## License

AGPL-3.0: you are free to use, modify and self-host this project, but if you run
a modified version as a network service for other people, you must offer them
its source. See [LICENSE](LICENSE).

Copyright (C) 2026 Michael Hannon.
