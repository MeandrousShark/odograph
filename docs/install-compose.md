# Minimal Compose installation

If you already manage Docker Compose or podman-compose, you can install
Odograph using only the `compose.yaml` and `.env.example` files from one
exact release. The recommended path remains the [full release
checkout](../README.md#quick-start), which also provides the guarded secret,
backup, restore, test-track, OSRM, and upgrade helpers.

Do not mix files from different releases. Use a published immutable release tag
for both downloads, and never use `main` or a moving image tag for this setup.
Replace `vX.Y.Z` below with the exact release tag you selected.

## Before you start

You need:

- A Linux host with Docker Engine and the Compose v2 plugin. Podman with
  podman-compose 1.3.0 or newer is supported as a substitution.
- `curl`, plus OpenSSL or Python 3 for generating the three secrets.
- A dedicated directory that you own, a domain pointing at the host, and a
  reverse proxy that can terminate HTTPS.
- About 2 GB of RAM and 5 GB of free disk to start.

The application port is bound to host loopback only. Keep it that way when
using the shipped `FORWARDED_ALLOW_IPS=*` setting. If your proxy runs on
another host or in a container, follow the [forwarded-header trust
guidance](reverse-proxy.md#trusting-forwarded-headers) instead.

## Download the two files

Choose a dedicated directory. Replace `/path/to/odograph` with a directory you
own, then run:

```sh
VERSION=vX.Y.Z
INSTALL_DIR=/path/to/odograph

(
    set -e
    mkdir -p "$INSTALL_DIR"
    chmod 700 "$INSTALL_DIR"
    cd "$INSTALL_DIR" || {
        printf '%s\n' "cannot enter $INSTALL_DIR" >&2
        exit 1
    }

    if [ -e compose.yaml ] || [ -L compose.yaml ] ||
       [ -e .env ] || [ -L .env ]; then
        printf '%s\n' 'compose.yaml or .env already exists; refusing to overwrite an installation.' >&2
        exit 1
    fi

    umask 077
    compose_tmp=
    env_tmp=
    cleanup() {
        [ -z "$compose_tmp" ] || rm -f -- "$compose_tmp"
        [ -z "$env_tmp" ] || rm -f -- "$env_tmp"
    }
    trap cleanup EXIT HUP INT TERM

    compose_tmp="$(mktemp "$INSTALL_DIR/.compose.yaml.download.XXXXXX")"
    env_tmp="$(mktemp "$INSTALL_DIR/.env.download.XXXXXX")"
    curl -fsSL "https://raw.githubusercontent.com/MeandrousShark/odograph/${VERSION}/compose.yaml" -o "$compose_tmp"
    curl -fsSL "https://raw.githubusercontent.com/MeandrousShark/odograph/${VERSION}/.env.example" -o "$env_tmp"

    mv -- "$compose_tmp" compose.yaml
    mv -- "$env_tmp" .env
    trap - EXIT HUP INT TERM
    chmod 600 .env
)
```

The download block leaves your shell in its original directory. Stop if it
reports an error; otherwise, edit `.env` inside your chosen installation
directory. The two URLs contain the same release tag. Keep that tag recorded
with the installation so a later upgrade can select the correct release notes and
matching helper scripts.

## Fill in `.env` by hand

Open `.env` in an editor. Set a stable Compose project name and an explicit
IANA timezone. Paste fresh random values into the three secret fields. Do not
put shell expressions such as `$(openssl rand ...)` in `.env`; Compose does
not run them.

Generate each value separately and copy the output into the matching field:

```sh
openssl rand -hex 32
```

If OpenSSL is unavailable, use:

```sh
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

Use one fresh value for each field. The database password must be hexadecimal
or another URI-safe value because the canonical Compose file interpolates it
into `DATABASE_URL`.

The relevant values should look like this after editing, with the example
secrets replaced by your copied output:

```dotenv
COMPOSE_PROJECT_NAME=odograph
POSTGRES_PASSWORD=PASTE_A_FRESH_URI_SAFE_VALUE_HERE
INGEST_PASSWORD=PASTE_A_FRESH_VALUE_HERE
SESSION_SECRET=PASTE_A_FRESH_VALUE_HERE
INITIAL_ADMIN_SIGNUP=0
DISPLAY_TZ=America/Los_Angeles
FORWARDED_ALLOW_IPS=*
```

Leave optional integrations unset until you have read the
[configuration reference](configuration.md) and [privacy guide](privacy.md).
Keep the project name unchanged if you move the directory. Compose uses it to
name the resources that include the persistent `dbdata` volume.

After editing, protect both the directory and the file:

```sh
cd /path/to/odograph || exit 1
chmod 700 .
chmod 600 .env
```

Keep an encrypted copy of `.env` with your backup records. The database backup
does not contain it.

## Start locally and create the administrator

Validate the rendered configuration, pull the image pinned by the downloaded
Compose file, and start the services:

```sh
docker compose config >/dev/null
docker compose pull
docker compose up -d
```

Wait until both `db` and `app` report `healthy`, then check the loopback health
endpoint:

```sh
docker compose ps
curl -fsS http://127.0.0.1:8077/healthz
```

If either service is unhealthy, inspect the focused logs before continuing:

```sh
docker compose logs app db
```

Create the only administrator while the application is still reachable only
locally:

```sh
docker compose exec app python -m app.manage_account create-admin
```

The command prompts for the administrator email and password, and accepts no
password argument. `INITIAL_ADMIN_SIGNUP=0` keeps browser signup disabled, so
the public `/signup` route cannot let an unknown visitor create the first
account. Run this command before configuring public proxy access.

With Podman, replace each `docker compose` command above with
`podman-compose`. The account command and its interactive prompts are the same.

## Add HTTPS access

Configure your existing reverse proxy to terminate TLS and proxy to
`127.0.0.1:8077`. The [reverse proxy guide](reverse-proxy.md) has Caddy and
nginx examples. Do not publish port 8077 on a public interface.

After DNS and the certificate are ready, check the public health endpoint:

```text
https://mileage.example.com/healthz
```

Replace the hostname with yours, then visit the same HTTPS address's `/login`
page. Browser sessions require HTTPS because their cookies are marked Secure.
If the proxy is not on the same host, or if it runs in a container, change
`FORWARDED_ALLOW_IPS` as described in the trust guidance before relying on
client IP based controls.

## Connect OwnTracks

Follow [Connecting OwnTracks](owntracks.md) with these values:

- URL: `https://mileage.example.com/ingest`
- Username: `owntracks`, unless you set `INGEST_USERNAME` yourself
- Password: the `INGEST_PASSWORD` value from `.env`
- A stable short device ID for the phone

Confirm that the device appears under Settings > Diagnostics > Device status
and that its newest location time advances. Location points can arrive before the
detector has enough quiet time to create a completed trip. The two-file path
does not include `scripts/send_test_track.sh`; use a real phone for this check
or obtain the matching helper from the full release checkout.

Then continue with [Use Odograph](usage.md) for vehicle setup, trip review,
corrections, and reports.

## Limits and upgrades

These two files are enough for the baseline application, but they intentionally
omit the repository's operator toolkit:

- `scripts/generate_env.sh` and its refusal-to-overwrite secret safeguards
- guarded backup and fresh-target restore commands
- the synthetic OwnTracks test-track sender and cleanup command
- OSRM dataset provisioning
- `scripts/upgrade_check.sh` and other release tooling
- local copies of the security, privacy, backup, recovery, and upgrade guides

Do not treat hand-written `pg_dump` or `pg_restore` commands as equivalent to
the guarded scripts. Decide how you will protect important data before relying
on this installation. You can obtain the scripts and guides from the same
exact release tag, or use the [full release checkout](../README.md#quick-start).
Use [Backups and disaster recovery](backups.md) to create a verified archive
and to test a restore into a fresh target.

For an upgrade, read every applicable release note first. Create and verify a
pre-upgrade backup, and keep a protected copy of `.env`. Preserve the same
`COMPOSE_PROJECT_NAME` and the existing named volumes. Obtain the target
release's `compose.yaml` from its exact immutable tag, then replace the current
file and pull the new app image from the installation directory. Replace
`/path/to/odograph` below with that directory:

```sh
TARGET_VERSION=vX.Y.Z
(
    set -e
    cd /path/to/odograph || exit 1
    compose_tmp=
    cleanup() {
        [ -z "$compose_tmp" ] || rm -f -- "$compose_tmp"
    }
    trap cleanup EXIT HUP INT TERM

    compose_tmp="$(mktemp .compose.yaml.upgrade.XXXXXX)"
    curl -fsSL "https://raw.githubusercontent.com/MeandrousShark/odograph/${TARGET_VERSION}/compose.yaml" -o "$compose_tmp"
    mv -- "$compose_tmp" compose.yaml
    trap - EXIT HUP INT TERM

    docker compose pull app
    docker compose up -d
    docker compose ps
    curl -fsS http://127.0.0.1:8077/healthz
)
```

The upgrade and rollback contract is the same as the full installation:
migrations are forward-only, a verified pre-upgrade backup is required, and
rollback means restoring that backup into a fresh target with the previous
release. Never run `docker compose down -v` unless you intend to erase the
database. For the supported procedure, use [Upgrading](upgrading.md) after
obtaining the target release's matching scripts and guides.
