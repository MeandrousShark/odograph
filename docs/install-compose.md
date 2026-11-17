# Minimal Compose installation

If you already run Compose, you can install Odograph from two release files,
`compose.yaml` and `.env.example`, instead of the recommended
[full release checkout](../README.md#quick-start). This route exists from the
first release that attaches `compose.yaml`; an earlier release keeps its own
guide at its tag.

## What you need

- Docker Compose v2, or podman-compose 1.3.0 or newer
- `curl` and `openssl`
- A Linux host (AMD64 or ARM64) with about 2 GB of RAM and 5 GB of free disk
- A domain and a reverse proxy that terminates HTTPS

## Install

Replace `vX.Y.Z` with the release and set `TZ_NAME` to your
[IANA timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)
first, since the administrator account takes it. Run this where no `odograph`
directory exists yet:

```sh
(
    set -eu
    umask 077
    mkdir odograph
    cd odograph
    curl -fsSLO https://github.com/MeandrousShark/odograph/releases/download/vX.Y.Z/compose.yaml
    curl -fsSLO https://raw.githubusercontent.com/MeandrousShark/odograph/vX.Y.Z/.env.example
    TZ_NAME=America/Los_Angeles
    pg_password=$(openssl rand -hex 32)
    session_secret=$(openssl rand -hex 32)
    set -C
    {
        grep -v -e '^POSTGRES_PASSWORD=$' -e '^SESSION_SECRET=$' -e '^DISPLAY_TZ=' .env.example
        printf 'COMPOSE_PROJECT_NAME=odograph\nDISPLAY_TZ=%s\n' "$TZ_NAME"
        printf 'POSTGRES_PASSWORD=%s\n' "$pg_password"
        printf 'SESSION_SECRET=%s\n' "$session_secret"
    } > .env
)
install_status=$?
[ "$install_status" -eq 0 ] && cd odograph && docker compose up -d
```

Wait for the app, then create the administrator (it prompts for the email
and password):

```sh
(
    i=0
    until curl -fsS http://127.0.0.1:8077/healthz; do
        [ $((i += 1)) -lt 36 ] || { echo 'not healthy after 3 minutes; read: docker compose logs app db' >&2; exit 1; }
        sleep 5
    done
) && docker compose exec app python -m app.manage_account create-admin
```

`mkdir` refuses an existing directory, `set -e` stops on a failed download or
secret, `set -C` refuses to overwrite a file, and `umask 077` makes `.env`
mode 0600; Compose starts only if all of that succeeded. If it stops after
creating `odograph`, remove that new directory and run it again. The secrets
reach `.env` only through the shell's `printf` builtin, never a process
argument; `grep` and `printf` work the same on Linux and macOS. The fixed
project name keeps the database volume if you move the directory, and
`INITIAL_ADMIN_SIGNUP=0` keeps browser signup closed. The wait covers startup
migrations, which run before `/healthz` answers.

`compose.yaml` comes from the release page, not the repository: that copy
pins the app image by digest and pulls it. Take every file from the same
release. The blocks are POSIX `sh` (run them in `sh` or `bash`, not fish);
with Podman, use `podman-compose` for `docker compose` throughout. Keep a copy
of `.env` safe ([Protecting `.env`](backups.md#protecting-env)); no backup
contains it.

## Then

Point your reverse proxy at `127.0.0.1:8077`
([Reverse proxy and TLS](reverse-proxy.md)). That port listens on loopback
only, which is what makes the shipped `FORWARDED_ALLOW_IPS=*` safe; see
[Trusting forwarded headers](reverse-proxy.md#trusting-forwarded-headers) for
any other topology. Then connect a phone with [OwnTracks](owntracks.md) or
[Traccar Client](traccar.md).

## What this route leaves out

- **Backups**: none until you add the backup script, and no restore. A
  hand-written `pg_dump` is no substitute: the restore script recreates the
  database roles and checks row-level security.
- **The test-track sender and OSRM data.**
- **The image signature check**: the image is pinned by digest, but its
  [cosign](https://docs.sigstore.dev/cosign/system_config/installation/)
  signature is not checked; `scripts/verify_image.sh` in the full checkout
  does that.
- **The upgrade drill**: `upgrade_check.sh` needs a full checkout.

## Add these later

Download from the same release into the directory holding `compose.yaml`,
`chmod 700 ./*.sh`, and run each as `./NAME.sh` from there where its guide
says `scripts/NAME.sh`. Skip `scripts/verify_image.sh`: this `compose.yaml`
pulls the pinned image itself. First set
`RAW=https://raw.githubusercontent.com/MeandrousShark/odograph/vX.Y.Z/scripts`.

| Add | Download | Guide |
| --- | --- | --- |
| Backup and restore | `curl -fsSLO "$RAW/backup_database.sh" -O "$RAW/restore_database.sh"` | [Backups](backups.md) |
| OSRM | `curl -fsSLO "$RAW/provision_osrm.sh"` | [Provisioning](osrm.md#provisioning) |
| Test track | `curl -fsSLO "$RAW/send_test_track.sh" --create-dirs -o sql/cleanup_test_device.sql "$RAW/sql/cleanup_test_device.sql"` | [Verify your setup](owntracks.md#verify-your-setup) |

After provisioning OSRM and setting the two values it prints in `.env`, run
`docker compose --profile osrm up -d`. podman-compose does not recreate the
app to load `OSRM_URL`, so run `podman-compose --profile osrm up -d osrm`,
then `podman-compose up -d --force-recreate app`. The test track also needs
`bash` and `python3`.

## Upgrading

Read the notes of every release up to the target. If they change the
database image, stop and follow
[Database image changes](upgrading.md#database-image-changes) instead.
Otherwise, with the backup and restore scripts in place and `vX.Y.Z` set to
the target:

```sh
(
    set -eu
    ./backup_database.sh --output backups/pre-vX.Y.Z.dump
    ./restore_database.sh --verify-only backups/pre-vX.Y.Z.dump
    cp .env backups/pre-vX.Y.Z.env
    cp compose.yaml compose.yaml.previous
    curl -fsSL -o compose.yaml.next https://github.com/MeandrousShark/odograph/releases/download/vX.Y.Z/compose.yaml
    mv compose.yaml.next compose.yaml
    docker compose up -d
    i=0
    until curl -fsS http://127.0.0.1:8077/healthz; do
        [ $((i += 1)) -lt 36 ] || { echo 'not healthy after 3 minutes; read: docker compose logs app' >&2; exit 1; }
        sleep 5
    done
)
```

If the wait gives up after 3 minutes, read `docker compose logs app`. Once
the app is healthy, download the new release's copies of your scripts.
Migrations are forward-only: going back
means restoring that backup with `compose.yaml.previous` into a fresh project
([Rollback](upgrading.md#rollback)), losing everything written after it. To
follow `v1` instead, the `compose.override.yaml` from
[Track `v1`](upgrading.md#track-v1) works on this layout too.
