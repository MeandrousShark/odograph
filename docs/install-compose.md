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
- `curl` and Python 3 for writing generated secrets directly to `.env`.
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

## Configure `.env`

From the installation directory, fill the blank secret fields without printing
their values or putting them on process arguments. This command refuses to
replace an existing nonblank secret:

```sh
python3 - <<'PYENV'
from pathlib import Path
import os
import re
import secrets

path = Path(".env")
text = path.read_text()
for key in ("POSTGRES_PASSWORD", "INGEST_PASSWORD", "SESSION_SECRET"):
    pattern = rf"(?m)^{key}=$"
    if len(re.findall(pattern, text)) != 1:
        raise SystemExit(f"Expected exactly one blank {key}; existing values were not changed")
    text = re.sub(pattern, f"{key}={secrets.token_hex(32)}", text)
os.chmod(path, 0o600)
path.write_text(text)
PYENV
```

Then open `.env` in an editor and set a stable Compose project name and an
explicit IANA timezone. Keep the generated secrets private. The relevant
fields are shown below with placeholders, not values to copy over your file.
`INGEST_PASSWORD` is retained for legacy-upgrade compatibility; after fresh
setup, issue device credentials in Tracking. `DISPLAY_TZ` supplies the initial
account timezone; Settings controls it afterward.

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

This checkout's `compose.yaml` uses the signed native PostgreSQL 16.15/PostGIS
3.6.4 image
`ghcr.io/meandrousshark/odograph-postgis@sha256:b352024dd6f9ca2ba0f1e7125dcfdcf78b824f2cbe86edf89559e1ddf4d80241` for
Linux AMD64 and ARM64. When a target release uses it, an installation that
still uses `postgis/postgis:16-3.4` must follow
[Upgrading the PostGIS database image](upgrading.md#upgrading-the-postgis-database-image)
before starting the target project.

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
- Username and password: create a device in **Settings > Tracking** and use
  the issued values. The password is displayed once.
- Tracker ID (`tid`): a short label; the issued credential selects the device.

Confirm that the device appears under Settings > Device status
and that its newest location time advances. Location points can arrive before the
detector has enough quiet time to create a completed trip. The two-file path
does not include `scripts/send_test_track.sh`; use a real phone for this check
or obtain the matching helper from the full release checkout.

Then continue with [Use Odograph](usage.md) for vehicle setup, trip review,
corrections, and reports.

## Limits and upgrades

Startup uses the privileged database URL to migrate and provision managed
restricted roles, then closes the setup connection. Personal data is explicitly
account-scoped and enforced by row-level security; the singleton account
guard remains. Follow the
[database role and preference contract](configuration.md#account-ownership-and-database-roles)
when configuring an external database or upgrading an existing installation.

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

For an upgrade, read every applicable release note first. Obtain the full
release checkout or the target release's matching backup and restore scripts,
then follow [Upgrading](upgrading.md). Create and verify a pre-upgrade logical
backup and keep a protected copy of `.env`. Replace `/path/to/odograph` below
with this installation directory to download the target Compose file:

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
    mv -- "$compose_tmp" compose.yaml.target
    trap - EXIT HUP INT TERM

    docker compose -f compose.yaml.target config >/dev/null
)
```

The download leaves the old `compose.yaml`, `.env`, project, and database volume
untouched, and saves the target file as `compose.yaml.target`. Do not run
`docker compose up -d` yet. If the target's `db.image` differs from
the current installation, including the transition from
`postgis/postgis:16-3.4` to the signed native image above, use the
[PostGIS database image migration](upgrading.md#upgrading-the-postgis-database-image)
procedure first. It stops the app for the final backup, preserves the old
project and volume, then replaces `compose.yaml` only after the backup and
stop steps. It creates a fresh project name in the existing `.env`, starts only
the target database, restores with the target script, and starts the app after
the restore checks pass. Replace the existing `COMPOSE_PROJECT_NAME` line, or
add exactly one line if it is absent; do not regenerate `.env`, leave a shell
`COMPOSE_PROJECT_NAME` set, use `-p`, or reuse the old volume.

After the old app and database are stopped, and the protected pre-upgrade
backup and `.env` copy are ready, replace the old Compose file:

```sh
mv compose.yaml.target compose.yaml
```

Only when the database image is unchanged may you complete an application-only
upgrade with:

```sh
docker compose pull app
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:8077/healthz
```

Migrations are forward-only. Rollback always restores the verified backup into
a fresh target with the previous release, and writes made after that backup are
lost. Never run `docker compose down -v` unless you intend to erase the
database.
