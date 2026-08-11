# Odograph

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

Your data stays on your own server unless you turn on an external service such
as hosted geocoding. The [privacy guide](docs/privacy.md) explains exactly what
each option sends outside your instance. The
[security guide](docs/security.md) covers the trust model and a hardening
checklist.

> **US users only:** Odograph's deduction reports use IRS standard mileage
> rates. Trip tracking still works anywhere, but the tax figures are only
> meaningful for US filers.

## Install

### What you need

- A Linux server or VM
- Git
- Either Docker Engine with the Compose v2 plugin (`docker compose`) or Podman
  with podman-compose 1.3.0 or newer. The old `docker-compose` v1 will not work.
- OpenSSL or Python 3 on the host, used to generate your secrets
- A domain name pointing at the host
- A reverse proxy that terminates HTTPS
- About 2 GB of RAM and 5 GB of free disk to start

Linux is what Odograph is tested and supported on. Docker Desktop or
`podman machine` on macOS or Windows will probably work, but they are not
tested, so they are not claimed. If you try one and hit a problem, open an
issue and I will help track it down.

Your phone posts location updates all day, so Odograph works best on a machine
that stays awake rather than one that sleeps.

## Quick start

Pick the version you want from the GitHub Releases page, then clone the
repository and check out that exact release tag:

```sh
git clone https://github.com/MeandrousShark/odograph.git
cd odograph
git checkout vX.Y.Z
```

Generate your configuration:

```sh
scripts/generate_env.sh
```

Open `.env` and set your timezone:

```env
DISPLAY_TZ=America/Los_Angeles
```

Use your own [IANA timezone name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones).

That generated file is already a working local-login setup. All three required
secrets are filled in and every optional integration is switched off. You only
need the [configuration reference](docs/configuration.md) when you want to
change something.

### Start Odograph

With Docker:

```sh
docker compose up -d
```

Or with Podman:

```sh
podman-compose up -d
```

Compose downloads the immutable image tag pinned by the checked-out release, so
you always get exactly the version you checked out.

#### Tracking patch releases instead

The registry also publishes a floating tag for each minor version, such as
`v0.8`, which always points at the newest patch within it. To follow that
instead of a fixed patch, create `compose.override.yml` next to `compose.yaml`:

```yaml
services:
  app:
    image: ghcr.io/meandrousshark/odograph:v0.8
```

Both Compose frontends pick that file up automatically. A `pull` then brings in
`v0.8.1` when it ships, while moving to a new minor version stays a deliberate
choice. Read the release notes before pulling either way.

There is no `latest` tag on purpose. Minor releases are where database
migrations land, and Odograph's migrations are one-way: once one runs, going
back means restoring a backup rather than switching the image. Crossing a minor
version unattended is a good way to discover that the hard way.

Check that both services came up:

```sh
docker compose ps
# or: podman-compose ps
```

And that the app answers on the host:

```sh
curl -fsS http://127.0.0.1:8077/healthz
```

That address is for local health checks and your reverse proxy only. It is not
how you use Odograph day to day.

## Set up HTTPS

Point your reverse proxy at `127.0.0.1:8077` and give it your domain, for
example `https://mileage.example.com`. The
[reverse proxy guide](docs/reverse-proxy.md) has worked examples.

Odograph requires HTTPS for browser sign-in, because its session cookies are
marked Secure. Confirm this works before going further:

```text
https://mileage.example.com/healthz
```

## Create your account

Open `https://mileage.example.com/signup` and create your administrator
account, then sign in.

Only the first account can be created this way. Once it exists, public signup
closes on its own. There is no setup token to copy and no container to
recreate afterward.

You can manage your password and optional sign-in providers later under
**Settings, then Account Security**.

## Connect OwnTracks

Follow the [OwnTracks guide](docs/owntracks.md) to connect your phone. It walks
through sending a test track, confirming Odograph received it, and deleting the
test data afterward.

Once that works, Odograph starts detecting trips on its own.

## Backups

Before you rely on Odograph, make a backup and prove it restores:

```sh
scripts/backup_database.sh --output backups/first-install.dump
scripts/restore_database.sh --verify-only backups/first-install.dump
```

Then pick somewhere encrypted and off this host to keep them, on a schedule.
[Backups and disaster recovery](docs/backups.md) covers both.

Your data lives in the Compose `dbdata` volume. Restarts, container recreates,
and ordinary `down` then `up` cycles all keep it. What destroys it is:

```sh
docker compose down -v
```

or the Podman equivalent. Only run that when you actually mean to erase the
database.

## Security

Before relying on your installation, work through the
[security hardening checklist](docs/security.md#hardening-checklist).

If you change the default networking, also review
[reverse proxy trust](docs/reverse-proxy.md#trusting-forwarded-headers).

The app container runs as a fixed non-root user, UID and GID `10001`, and
writes nothing to disk. That only matters if you mount a host directory into
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

OIDC is a second way to sign in to the administrator account you already have,
not a separate account.

Register `https://mileage.example.com/auth/callback` with your provider, set
the three OIDC variables, then link it from **Settings, then Account Security**
while signed in with your password. Linking asks for that password and a fresh
authorization from the provider.

Afterward either method signs you into the same account. The link follows the
provider's issuer and subject, so it survives your email address changing
there. See the
[authentication settings](docs/configuration.md#authentication-and-ingest) for
details, and [Upgrading](docs/upgrading.md) if you are moving an older
OIDC-only installation.

### Reverse geocoding

Odograph supports Geoapify as a hosted provider and Nominatim as a self-hosted
one. Read the
[configuration reference](docs/configuration.md#external-services) and the
[privacy guide](docs/privacy.md) before turning either on.

### OSRM

OSRM is optional and entirely self-hosted. It needs road data prepared for your
region ahead of time, and it can want considerably more memory and disk than
Odograph itself. See the [OSRM guide](docs/osrm.md).

## Password recovery

While signed in, change your password under **Settings, then Account
Security**.

If you are locked out, reset it from the server and type the new password when
prompted:

```sh
docker compose exec app python -m app.manage_account reset-password
# or: podman-compose exec app python -m app.manage_account reset-password
```

Use `create-admin` instead when no account exists yet and public signup is
closed.

Both a password change and an operator reset sign out your other Odograph
sessions. The browser that made the change stays signed in.

If you use OIDC, signing out of Odograph does not sign you out of your identity
provider, so signing back in may not prompt you at all. Sign out of the
provider separately to end that session. Odograph does not keep provider tokens
after sign-in.

## Updating

When a new release comes out, follow the [upgrading guide](docs/upgrading.md).
Take a fresh backup and verify it first, then move from one exact release tag
to the next. Watch the GitHub Releases page to hear about new versions.

## Contributing

Want to run the test suite or work on the code? See
[CONTRIBUTING.md](CONTRIBUTING.md) for development setup and local
instructions.

## Support

Odograph is self-hosted open-source software, not a hosted service. Only the
latest release is supported, by one maintainer, on a best-effort basis. There
is no service-level agreement or guaranteed response time.

Open a GitHub issue for bugs and feature requests. See
[SECURITY.md](SECURITY.md) for security reporting and supported versions, and
[CONTRIBUTING.md](CONTRIBUTING.md) for project scope.

## AI assistance

AI tools were used as development assistants on this project: implementation,
debugging, testing, review, and documentation. I directed the product and
design decisions, and I reviewed and tested the resulting work before release.

Every release is signed and traceable to the exact source commit and CI run
that built it. The verification commands are in
[docs/releasing.md](docs/releasing.md).

## License

AGPL-3.0. In short: you are free to use, modify, and self-host this project,
but if you run a modified version as a network service that other people use,
you must offer that version's source to those users. See [LICENSE](LICENSE) for
the full text.

Copyright (C) 2026 Michael Hannon.
