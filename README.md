# Odograph

Odograph is a self-hosted mileage tracker fed by [OwnTracks](https://owntracks.org/)
in HTTP mode. Ingests location fixes, detects trips via stay-point clustering, tags them
as business/personal (or manually when detection misses a drive), and provides a
full trip ledger with filtering, bulk editing, detailed maps, mileage/deduction
reports, expense tracking, and odometer reconciliation. Optional add-ons include
self-hosted OSRM road-snapping, reverse geocoding (Geoapify or a self-hosted
Nominatim), and ntfy/email reminders for trips that still need tagging. See
[docs/privacy.md](docs/privacy.md) for exactly which of these send data
outside your instance, and under what configuration, and
[docs/security.md](docs/security.md) for the trust model, entry-point
security, and an operator hardening checklist.

**This is a US tax tool.** Odograph computes mileage deductions from IRS
standard mileage rates. Outside the US, that figure is not merely
unlocalized. It's wrong. If you're not a US filer, the deduction reports
this project produces are not useful to you.

## Install

### Requirements

- A Linux host with Git and either Docker Engine with the Compose v2 plugin
  (`docker compose`) or Podman with podman-compose 1.3.0 or newer
  (`podman-compose`). Legacy `docker-compose` v1 is not supported.
- OpenSSL or Python 3 on the host to generate secrets.
- A domain name whose DNS points to the host, plus a TLS-terminating reverse
  proxy. Browser sessions use Secure cookies, so the production UI must be
  reached over HTTPS; `http://127.0.0.1:8077` is only a local health and proxy
  upstream address.
- At least 2 GB RAM and 5 GB free disk for the baseline app, database, and
  initial data. SSD-class storage is recommended. Low-memory boards such as a
  1 GB Raspberry Pi 3 are untested and unsupported. Long retention increases
  database use.
- Optional OSRM road data requires substantially more memory and disk depending
  on the region. Process the extract on a larger machine, then copy the
  resulting dataset to the host that will run it.
- The `app` container runs as fixed non-root UID/GID `10001:10001` with an
  empty capability set. It writes nothing to disk, so this only matters if you
  bind-mount a host directory into it yourself: make sure that path is
  readable (and writable, if applicable) by UID/GID 10001.

Linux is what this project is tested on and what it supports. Nothing in the
design is Linux-specific beyond that: these are ordinary Linux container
images, so Docker Desktop or `podman machine` on macOS or Windows will very
likely work. It is simply not tested, so it is not claimed. If you try it and
something breaks, open an issue. I have both platforms available and am glad
to help track a problem down; I just don't test them ahead of time. Note that
the phone posts location fixes continuously, so whatever the operating system,
a machine that sleeps makes a poor host.

### Clean-host quickstart

Clone the repository and check out the latest release tag shown on the GitHub
Releases page. Keeping the checkout on an exact tag makes the installed source
reproducible:

```sh
git clone https://github.com/MeandrousShark/odograph.git
cd odograph
git checkout "$(git tag --sort=-v:refname | head -n 1)"
scripts/generate_env.sh
```

Edit `.env` and set `DISPLAY_TZ` to your
[IANA timezone name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones).
The generated file is already a working local-login configuration: its three
OIDC variables are unset and its four required secrets are populated. Leave
`FORWARDED_ALLOW_IPS=*` unchanged only while the compose port remains bound to
host loopback as shipped.

Pull the image pinned by the checked-out release and start the baseline stack
with one of the supported Compose commands:

```sh
# Docker Compose v2
docker compose pull app
docker compose up -d
docker compose ps

# Or Podman Compose
podman-compose pull app
podman-compose up -d
podman-compose ps
```

Only run one pair. The database and application should report healthy; this
local diagnostic should then succeed:

```sh
curl -fsS http://127.0.0.1:8077/healthz
```

Next, configure your domain and TLS reverse proxy to send traffic to
`127.0.0.1:8077`, following [the reverse-proxy guide](docs/reverse-proxy.md).
Confirm `https://mileage.example.com/healthz` works before browser setup.
Do not use the plain-HTTP loopback URL for `/setup` or `/login`: production
session cookies are intentionally Secure and will not work there.

Read the generated setup token without copying any other secret:

```sh
sed -n 's/^ADMIN_TOKEN=//p' .env
```

Open `https://mileage.example.com/setup`, enter that token, and create the
single local administrator. Sign in with the email and password you chose.
The token is automatically consumed on success. For defense in depth, set
`ADMIN_TOKEN=` in `.env`, then recreate the app so it loads the change:

```sh
# Docker Compose v2
docker compose up -d --force-recreate app

# Or Podman Compose
podman-compose up -d --force-recreate app
```

Again, run only the command for your runtime. `/setup` returns 404 after the
token is removed and the app is recreated. Compose stores database data in the
named `dbdata` volume, so ordinary restarts, recreates, and `down`/`up` cycles
without `-v` preserve it; see [Backups and disaster recovery](docs/backups.md)
for tested backup, restore, and recovery procedures. Do not use `down -v`
unless you intend to delete the database.

When a new release comes out, see [Upgrading](docs/upgrading.md) and take and
verify a fresh backup before changing the checkout or pulling the new image.

Finally, follow [Connecting OwnTracks](docs/owntracks.md) to configure the
phone, send a synthetic test track, verify the first fix on Settings, and
remove the test data.

Before relying on the instance day to day, run through
[the security hardening checklist](docs/security.md#hardening-checklist)
against this exact installation.

### Password recovery and session revocation

To reset the local administrator password, put a new high-entropy value in
`ADMIN_TOKEN` and recreate the app so it loads the changed `.env`. Open
`/setup`, reset the password, clear `ADMIN_TOKEN`, and recreate the app again.
Each successful setup token can be used only once.

A password reset does not invalidate an already-issued signed session cookie.
Recreating or restarting the app with the same `SESSION_SECRET` does not
invalidate it either. If you need to revoke every existing browser session,
also replace `SESSION_SECRET` with a new high-entropy value before the final
app recreation. This signs everyone out; it does not affect stored data.

### Optional integrations

The baseline stack needs none of these:

- OIDC can be enabled as an alternative login by setting all three OIDC
  variables together. Register
  `https://mileage.example.com/auth/callback` with the provider.
- Reverse geocoding and address search are enabled by setting
  `GEOCODE_PROVIDER` to `geoapify` (hosted; also needs `GEOCODE_API_KEY`) or
  `nominatim` (self-hosted only; also needs `GEOCODE_NOMINATIM_URL`, which
  ships with no default). Left unset with `GEOCODE_API_KEY` set, it resolves
  to `geoapify` for compatibility with configs from before this setting
  existed. An existing `.env` needs no change. See
  [the privacy guide](docs/privacy.md) for exactly what each provider
  receives before enabling either.
- ntfy reminders and SMTP email are likewise enabled only when their
  corresponding `.env` variables are set. See
  [the privacy guide](docs/privacy.md) before enabling external services.
- Self-hosted OSRM road snapping is an optional compose profile. It requires a
  separately prepared regional dataset before starting `--profile osrm`. The
  app ships no default region. See [docs/osrm.md](docs/osrm.md) for choosing
  an extract, provisioning it, and sizing a host for it.

## Contributing

Interested in running the test suite or working on the code itself? See
[CONTRIBUTING.md](CONTRIBUTING.md) for development setup, running tests, and
local development instructions.

## Support

This project is self-hosted software, not a hosted service. Only the latest
release is supported, by one maintainer on a best-effort basis. There is no
service-level agreement, guaranteed response time, or promise of help operating
custom infrastructure. See [SECURITY.md](SECURITY.md) for security-reporting
and supported-version details and [CONTRIBUTING.md](CONTRIBUTING.md) for the
project scope.

## AI assistance

AI tools were used as development assistants on this project: implementation,
debugging, testing, review, and documentation. I directed the product and
design decisions, and I reviewed and tested the resulting work before release.

Every release is signed and traceable to the exact source commit and CI run
that built it. The verification commands are in
[docs/releasing.md](docs/releasing.md).

## License

AGPL-3.0. In short: you're free to use, modify, and self-host this project, but
if you run a modified version as a network service that other people use, you
must offer that version's source to those users. See [LICENSE](LICENSE) for the
full text.

Copyright (C) 2026 Michael Hannon.
