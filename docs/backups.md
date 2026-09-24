# Backups and disaster recovery

See [security.md](security.md) for the hardening checklist item on encrypting
and controlling access to these archives, and the compromise runbook that
references this document's disaster-recovery steps.

This covers `scripts/backup_database.sh` and `scripts/restore_database.sh`:
what they capture, how to schedule and encrypt backups, what to do about
`.env`, and how to recover a broken installation without touching the data
you're trying to save. Run Compose commands from the directory containing
your `compose.yaml`; direct-container commands can run from any installation
checkout.

## What the archive covers, and what it doesn't

`scripts/backup_database.sh` produces a PostgreSQL custom-format `pg_dump`
archive of the complete `mileage` database. It includes the schema, PostGIS
objects, trips, GPS points, raw ingest messages, account credential hashes,
linked identity metadata, account settings, tracking devices and credential
hashes, places, tagging rules, vehicles, expenses, odometer readings,
overrides, caches, and worker delivery ledgers. It also includes the protected
`odograph_service` schema containing the application's database credential
state. Treat the entire archive as sensitive and encrypt off-host copies.

Archives made before schema 27 can also contain the configuration message
that OwnTracks' Publish Settings button sends, which includes the tracker's
plaintext password. Schema 27 deletes those stored messages, and a restored
older archive loses them again when the application next starts, but the
archive file itself keeps them. Protect older archives accordingly, or
replace the tracking credential in Tracking settings if one may have been
exposed.

The dump has no table-level allowlist. That means a future schema change cannot
silently add an application table that the backup leaves out.
The script requires the instance database identity with unrestricted backup
access and records `backup_scope=full-instance` in its manifest. It refuses
an account-scoped runtime identity; a dump made through that identity is not
a complete installation backup.

Three things are deliberately **not** in the archive:

- **`.env`.** It holds instance database, session, OIDC, and optional-service
  secrets and configuration. The file itself is not stored in PostgreSQL;
  personal settings and the legacy ingest credential are imported into the
  database once during the ownership migration. The backup script never reads
  or prints `.env`. See
  [Protecting `.env`](#protecting-env) below. Losing it is a real recovery
  problem even though the database restores fine on its own.
- **The optional `osrmdata` volume.** It holds reproducible OSRM routing
  artifacts built from a regional road extract, not data this application
  generated. If you use the `osrm` compose profile, reprovision it with the
  same one-time extract/partition/customize step described in
  [Self-hosted OSRM road-snapping](osrm.md#provisioning) rather than backing
  it up.
- **The PostGIS-managed `tiger`, `tiger_data`, and `topology` schemas.**
  Database images may create these on a fresh volume. When an archive declares
  their extensions but omits the schemas, the restore script creates the
  missing schemas within the restore transaction. Application data and the
  protected application credential schema are fully included above.

Copying the live `dbdata` volume directly (filesystem copy, snapshot, `tar`
of the volume mount, etc.) is not a supported backup path. PostgreSQL's data
directory isn't guaranteed consistent unless you stop the database or use a
tool that understands its write-ahead log, so an unsynchronized copy can look
complete and still be unusable when you actually need it. The logical dump
these scripts produce is transactionally consistent, portable across hosts
and container runtimes, and inspectable before you ever touch a database.

## One-off backup

The scripts autodetect `docker compose` or `podman-compose` on `PATH`, the
same way `scripts/send_test_track.sh` does. Set `COMPOSE_CMD` to force one
explicitly, for example on a host with both installed:

```sh
COMPOSE_CMD="docker compose" scripts/backup_database.sh
# or
COMPOSE_CMD="podman-compose" scripts/backup_database.sh
```

With only one Compose frontend on `PATH`, autodetection is enough:

```sh
scripts/backup_database.sh
```

This runs `pg_dump` against the live `db` service without stopping the app,
writes `backups/mileage-<UTC timestamp>.dump` plus a `.sha256` checksum
sidecar and a non-secret `.manifest` file (creation time, schema version,
Postgres/PostGIS versions, source ref), and sets the `backups/` directory to
mode `700`. It refuses to overwrite an existing archive, sidecar, or
manifest, and it never puts a password on the command line or prints
anything from `.env`. If any step fails, cleanup removes the partial output.
There's never an archive on disk that looks successful but isn't.

Pick your own output path with `--output`:

```sh
scripts/backup_database.sh --output /path/to/backups/pre-upgrade.dump
```

### Direct container backup without Compose

Production installs managed by Podman quadlets may not have a `compose.yaml`
or a Compose frontend. Pass the running database container name instead:

```sh
sudo scripts/backup_database.sh --container db --output /safe/path/pre-upgrade.dump
```

This mode autodetects `podman` first, then `docker`, and invokes the runtime's
`exec -i` for the dump, archive validation, and manifest queries. Set
`CONTAINER_RUNTIME` to force a runtime when both are installed, for example
`CONTAINER_RUNTIME=podman`. It writes the same archive, checksum, and manifest
artifacts and applies the same refusal-to-overwrite and cleanup guarantees as
the Compose mode. `scripts/restore_database.sh` remains Compose-only.

## Verifying an archive

`--verify-only` checks that the archive still matches its checksum sidecar and
that its internal table of contents (`pg_restore --list`) is readable. It does
not restore data or prove that a fresh database can accept the archive. It
needs the `db` service running, since that's where the pinned `pg_restore`
binary comes from, but it makes no changes to that database:

```sh
docker compose up -d db   # or: podman-compose up -d db
scripts/restore_database.sh --verify-only backups/mileage-20260719T030000Z.dump
```

Run this after every backup you intend to rely on, and again right before any
restore. A restore drill below is the check that proves the archive can be
opened by a fresh application database.

## Restoring

Restores are deliberately fresh-target-only: the script refuses to run while
the `app` service is up, and refuses any target database that already
contains application relations (including a pre-existing `schema_migrations`
table). There is no in-place overwrite and no `--replace` flag. Restoring
into a used database means creating a new, empty one first (see
[Disaster recovery](#disaster-recovery) below for the case where the old data
needs to survive alongside it).

For a target you already know is empty (a brand-new install, or a fresh
`dbdata` volume in a new Compose project), stop the app and restore:

```sh
docker compose stop app
docker compose up -d db
scripts/restore_database.sh backups/mileage-20260719T030000Z.dump
docker compose up -d app
```

The restore first renders the complete archive with `pg_restore` into a private
temporary directory. Allow free local disk space for two uncompressed SQL
copies of the archive. It then applies the required extension schemas and
archive SQL using `psql --single-transaction` with `ON_ERROR_STOP=1`, so a
failure rolls back both schema preparation and restored data. Temporary files
are removed on exit or interruption. On success it runs
`ANALYZE`, prints the restored schema version, and compares it against the
archive's manifest if one is next to it (a mismatch is a warning, not a
failure, since the restore already committed). It does not start the app for
you; start it yourself once you're satisfied, so its own startup migrations
(if the target release differs from the one that made the backup) run under
your observation.

For a schema-26 or later archive containing `odograph_service`, use the matching
application image and scripts. Before loading SQL, the script creates the
required roles with login disabled. After the data commits, a one-off app
command reconstructs ownership, permissions, and restricted-role credentials
from the archived state, then validates the security contract, including
enforced RLS from schema 28. An archive taken before schema 28 must be restored
with its matching image, then upgraded. Older archives do not run these
commands.

If the security reconstruction fails after SQL commits, keep the app stopped.
The script reports that data was restored but security validation failed;
this is not a successful restore. Correct the image or configuration and
restore into another fresh target. Do not grant broad permissions or change
RLS manually to get past the check.

When changing the PostGIS database image, keep the old project and volume
stopped and restore into a fresh project with a new `COMPOSE_PROJECT_NAME`.
Use the target release's restore script after starting only its database. Do
not reuse the old `dbdata` volume, swap `PGDATA` directories, or run
`docker compose down -v` against the old project. The complete migration and
rollback sequence is in
[Upgrading the PostGIS database image](upgrading.md#upgrading-the-postgis-database-image).

An operator who has separately verified a relocated archive's integrity can
skip the checksum check with `--skip-checksum` (archive-structure validation
still always runs). This is not valid together with `--verify-only`.

## Protecting `.env`

Keep a separate, encrypted copy of `.env`: a password manager, a
GPG-encrypted file, or an encrypted backup tool's repository (see
[Encryption and off-host copies](#encryption-and-off-host-copies)) all work.
It's the only place several of your instance's secrets exist outside memory,
and the database archive above never contains it.

What losing a given value actually costs, if you don't have a copy:

- **`POSTGRES_PASSWORD`** only matters if you still have the *original*
  `dbdata` volume. The Postgres image reads it when it initializes an empty
  data directory; changing `.env` later does not change the existing role
  password. If the values no longer match, reset the role password inside the
  container instead of editing `.env`. Restoring into a fresh volume avoids
  this problem: put the new value in the fresh `.env`, and the database role
  is initialized from it.
- **`SESSION_SECRET`** is safely regenerable. Put a new high-entropy value
  in `.env` and recreate the app. This signs every existing browser session
  out at once; it doesn't touch stored data.
- **Local administrator credentials** live in the database, not `.env`.
  Restore them with the database archive. If the password is lost, run
  `python -m app.manage_account reset-password` inside the app container;
  the reset invalidates previously issued sessions.
- **Tracking credentials** are stored as hashes in schema-26 backups, so
  restored devices keep working with their existing credentials. The old
  `INGEST_PASSWORD` is imported only once; changing `.env` cannot rotate or
  recreate it afterward. Use Tracking settings to replace a lost or revoked
  credential, then update the affected device. With an older backup made
  before schema 26, preserve its original `INGEST_USERNAME` and `INGEST_PASSWORD`
  until the ownership migration imports them. See [Connecting OwnTracks](owntracks.md).
- **`OIDC_ISSUER` / `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET`** have to be
  re-obtained from your identity provider; nothing here regenerates them
  locally.
- **The geocoder** needs `GEOCODE_PROVIDER` put back regardless of which one
  you run, plus whatever that provider needs: an operator on Geoapify also
  needs `GEOCODE_API_KEY` re-obtained from Geoapify, the same as the keys
  below; an operator on self-hosted Nominatim needs only
  `GEOCODE_NOMINATIM_URL` pointed back at their own instance. There's no
  key to re-obtain, since that provider never issued one.
- **Optional-service keys** (`NTFY_TOKEN` / `NTFY_USERNAME` /
  `NTFY_PASSWORD`, `SMTP_USERNAME` / `SMTP_PASSWORD`) have to be re-obtained
  from their respective providers the same way.

## Recovering when only the database dump survived

If `.env` is gone and no encrypted copy exists, generate a fresh one
(`scripts/generate_env.sh`) and expect the following, on top of the restored
data itself:

- Every existing browser session is invalidated: the new `SESSION_SECRET`
  can't validate cookies signed by the old one, so everyone (including you)
  has to sign in again.
- Schema-26 backups retain existing tracking credential hashes, so devices
  with their original credentials can continue posting. For an older backup,
  preserve the old ingest credentials if available; otherwise, use the old
  release's replacement credentials on every device before upgrading.
- OIDC, ntfy, and SMTP all need their values re-entered from their
  respective providers before those integrations work again; the app runs
  fine without any of them in the meantime.
- The geocoder needs `GEOCODE_PROVIDER` set again either way. An operator on
  Geoapify also needs `GEOCODE_API_KEY` re-entered from Geoapify; an
  operator on self-hosted Nominatim just needs `GEOCODE_NOMINATIM_URL`
  pointed back at their own instance, nothing to re-obtain from a third
  party.
- `osrmdata` was never in the database dump regardless of what happened to
  `.env`, so self-hosted road-snapping needs its one-time regional extract
  prepared again before you re-enable that profile.

None of this affects the restored trips, points, places, vehicles, or
expenses. It's entirely about re-establishing access and external
integrations around data that's already back.

## Scheduling

### systemd timer

`/etc/systemd/system/mileage-backup.service`:

```ini
[Unit]
Description=Odograph database backup

[Service]
Type=oneshot
WorkingDirectory=/opt/odograph
User=mileage
ExecStart=/opt/odograph/scripts/backup_database.sh
```

Replace `WorkingDirectory` with your checkout and `User` with the account
that owns it and can run your Compose frontend (a member of the `docker`
group, or a rootless Podman user).

`/etc/systemd/system/mileage-backup.timer`:

```ini
[Unit]
Description=Daily Odograph database backup

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

`Persistent=true` runs a missed backup (e.g. the host was off at 03:00) at
the next boot instead of silently skipping a day. Enable it:

```sh
sudo systemctl enable --now mileage-backup.timer
sudo systemctl list-timers mileage-backup.timer
```

### cron equivalent

```cron
0 3 * * * cd /opt/odograph && scripts/backup_database.sh >> /var/log/mileage-backup.log 2>&1
```

## Encryption and off-host copies

A local, unencrypted `backups/` directory is not a complete backup strategy.
It doesn't survive host loss and, since these archives contain location
history, it deserves the same at-rest protection as `.env`. Compose an
established backup tool around the script rather than scripting your own
upload; neither of these scripts, nor any other part of this project, embeds
cloud-provider credentials.

[restic](https://restic.net/) example, run right after a backup:

```sh
export RESTIC_REPOSITORY=/mnt/backup-target/mileage-restic   # or an sftp:/rest: URL
export RESTIC_PASSWORD_FILE=/etc/odograph/restic-password
scripts/backup_database.sh
restic backup backups/
restic forget --keep-daily 7 --keep-weekly 5 --keep-monthly 12 --prune
```

[Borg](https://www.borgbackup.org/) equivalent:

```sh
scripts/backup_database.sh
borg create --stats /mnt/backup-target/mileage-borg::mileage-{now:%Y-%m-%dT%H%M%S} backups/
borg prune --keep-daily 7 --keep-weekly 5 --keep-monthly 12 /mnt/backup-target/mileage-borg
```

Both tools encrypt the repository, deduplicate across runs, and support
pushing to remote storage. Configure that transport and any credentials
through the tool's own configuration (environment file, credential helper,
etc.), not by editing these project scripts.

## Retention

The `--keep-daily 7 --keep-weekly 5 --keep-monthly 12` figures above are
illustrative, not a recommendation. Choose a window that fits your own
privacy and recovery needs: shorter retention limits how long a compromised
backup repository would expose your location history, while longer retention
gives you more time to notice something's wrong before the only good copy
ages out.

This is a different knob from `RAW_MESSAGE_RETENTION_DAYS`, which only
prunes the raw ingest payload table (`raw_messages`) inside the *live*
database. It never touches points, trips, or any other derived data, and
defaults to 365 days. Backup retention is independent of it: a dump taken
before a raw message aged out still contains that row, and keeps containing
it for as long as your backup retention policy keeps that dump around. If
raw-message retention matters to you for privacy reasons, remember that your
backup archives are a separate, parallel copy of that same location data
with its own lifetime.

## Restore drills

Periodically prove a backup restores, not just that the file exists. Run this
from the installation checkout, with the archive and its .sha256 sidecar
available. The drill uses a generated project name, a temporary full Compose
file in the checkout, and a separate loopback port. It checks that the project
resources do not already exist before it starts, so cleanup can target only
resources created by this drill.

Copy the whole bash block. Set ARCHIVE to the dump you want to test and set
DRILL_PORT to a free loopback port before running it. Choose one Compose
frontend in COMPOSE_CMD. The value is exported inside a subshell, so it does
not change the frontend selected by the rest of your shell:

```bash
(
set -euo pipefail
COMPOSE_CMD="docker compose"  # change to "podman-compose" when needed
case "$COMPOSE_CMD" in
  "docker compose") compose=(docker compose); runtime=(docker) ;;
  podman-compose) compose=(podman-compose); runtime=(podman) ;;
  *) echo "choose docker compose or podman-compose in COMPOSE_CMD" >&2; exit 1 ;;
esac
export COMPOSE_CMD
run_compose() { "${compose[@]}" "$@"; }
resource_exists() {
  "${runtime[@]}" "$1" inspect "$2" >/dev/null 2>&1
}
ARCHIVE=backups/mileage-20260719T030000Z.dump
DRILL_PORT=18077
DRILL_PROJECT="odograph-drill-$(date -u +%Y%m%d%H%M%S)-$$"
if [ ! -r "$ARCHIVE" ] || [ ! -r "${ARCHIVE}.sha256" ]; then
  echo "error: ARCHIVE and its .sha256 sidecar must both be readable." >&2
  exit 1
fi
drill_compose="$(mktemp "$PWD/.compose.drill.XXXXXX")"
started=0
cleanup() {
  status=$?
  trap - EXIT
  if [ "$started" -eq 1 ]; then
    if ! run_compose down -v; then
      echo "error: drill cleanup failed for project $DRILL_PROJECT" >&2
      status=1
    fi
  fi
  rm -f -- "$drill_compose"
  exit "$status"
}
trap cleanup EXIT

if ! command -v ss >/dev/null 2>&1; then
  echo "error: ss is required to check the drill port before starting." >&2
  exit 1
fi
if ss -ltn | grep -Eq "[.:]${DRILL_PORT}[[:space:]]"; then
  echo "error: 127.0.0.1:${DRILL_PORT} is already in use; choose a free loopback port and update DRILL_PORT." >&2
  exit 1
fi

for volume in "${DRILL_PROJECT}_dbdata" "${DRILL_PROJECT}_osrmdata"; do
  if resource_exists volume "$volume"; then
    echo "error: refusing to use existing volume $volume" >&2
    exit 1
  fi
done
for network in "${DRILL_PROJECT}_default" "${DRILL_PROJECT}-default"; do
  if resource_exists network "$network"; then
    echo "error: refusing to use existing network $network" >&2
    exit 1
  fi
done
for container in \
  "${DRILL_PROJECT}_db_1" "${DRILL_PROJECT}_app_1" \
  "${DRILL_PROJECT}-db-1" "${DRILL_PROJECT}-app-1"; do
  if resource_exists container "$container"; then
    echo "error: refusing to use existing container $container" >&2
    exit 1
  fi
done

binding_count="$(grep -Fc '127.0.0.1:8077:8000' compose.yaml || true)"
if [ "$binding_count" -ne 1 ]; then
  echo "error: expected exactly one canonical 127.0.0.1:8077:8000 binding." >&2
  exit 1
fi
sed "s/127.0.0.1:8077:8000/127.0.0.1:${DRILL_PORT}:8000/" \
  compose.yaml > "$drill_compose"
export COMPOSE_FILE="$drill_compose"
export COMPOSE_PROJECT_NAME="$DRILL_PROJECT"
run_compose config >/dev/null
wait_for_healthy() {
  local service="$1" output=""
  for ((attempt = 1; attempt <= 60; attempt++)); do
    output="$(run_compose ps 2>&1 || true)"
    if printf '%s\n' "$output" | grep -qiE "$service.*\\(healthy\\)"; then
      return 0
    fi
    sleep 2
  done
  echo "error: $service did not report healthy." >&2
  printf '%s\n' "$output" >&2
  return 1
}
started=1
run_compose up -d db
wait_for_healthy db
scripts/restore_database.sh "$ARCHIVE"
run_compose up -d app
wait_for_healthy db
wait_for_healthy app
curl -fsS "http://127.0.0.1:${DRILL_PORT}/healthz"
run_compose exec -T db psql -U mileage -d mileage -c \
  "SELECT max(version) AS schema_version FROM schema_migrations;
   SELECT count(*) AS trips FROM trips;"
)
```

The restore command keeps its default checksum verification. It checks the
archive's .sha256 sidecar and its internal table of contents before loading
the fresh database. Compare the schema version and trip count with the
expected backup data.

Wait for both services to report healthy before the health request. The
loopback HTTP URL is suitable for health and database checks only. Browser
login cannot be verified there because the application's Secure cookies
require HTTPS. This drill verifies the restored database and application
health, not HTTPS browser login. Do not publish the drill port beyond loopback.

The subshell keeps COMPOSE_FILE, COMPOSE_PROJECT_NAME, and the selected
frontend scoped to the drill. Its cleanup uses that generated project name and
temporary Compose file, and runs only after the preflight checks found no
matching containers, volumes, or networks. It removes the temporary file and
the drill project's resources; it does not unset or run down -v against your
normal project.

## Disaster recovery

Use this runbook when the running installation is broken badly enough that
you don't trust its `dbdata` volume: corruption, a bad manual change, a
failed upgrade you don't want to chase. The old volume stays exactly as it
is until you've verified the new one and deliberately choose to delete the
old one; nothing here deletes it for you.

1. **Leave the old volume alone.** If the app or db containers are still
   running against it, stop them, but don't run `down -v` or otherwise touch
   the volume itself.

2. **Create a new Compose project pointing at a fresh volume**, using the
   same checkout and `compose.yaml`. `COMPOSE_PROJECT_NAME` (a standard
   Compose environment variable, not specific to this project) namespaces
   volumes, networks, and containers separately from your default project:

   ```sh
   export COMPOSE_PROJECT_NAME=mileage-recovery
   docker compose up -d db
   ```

3. **Restore into it.** The restore script's own guardrails are your safety
   net here: it refuses to run if this "fresh" target somehow already has
   application data:

   ```sh
   scripts/restore_database.sh backups/mileage-20260719T030000Z.dump
   ```

4. **Start the app only after the restore succeeds**, and verify it before
   cutting over:

   ```sh
   docker compose up -d app
   curl -fsS http://127.0.0.1:8077/healthz
   ```

   Then, before retiring anything:
   - Confirm the restore script's printed schema version matches what you
     expect.
   - Sign in as your local administrator (or through OIDC, if configured).
   - Open the trip list and confirm representative trips, places, and
     vehicles look right.
   - Confirm `/ingest` still accepts a point. `scripts/send_test_track.sh`
     is the fastest way (see [Connecting OwnTracks](owntracks.md)).
   - Confirm background workers started (check `docker compose logs app` /
     `podman-compose logs app` for detector/snap/retention worker startup
     lines).

5. **Only once all of that checks out**, retire the old volume yourself.
   This project never deletes a volume automatically. If the recovery
   project should become your primary installation going forward, that's an
   ordinary Compose administration step (renaming, or just adopting
   `COMPOSE_PROJECT_NAME=mileage-recovery` as the one you keep using) outside
   the scope of this guide.

If `.env` didn't survive whatever went wrong either, see
[Recovering when only the database dump survived](#recovering-when-only-the-database-dump-survived)
above. The database itself restores the same way regardless.
