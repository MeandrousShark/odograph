# Backups and disaster recovery

See [security.md](security.md) for the hardening checklist item on encrypting
and controlling access to these archives, and the compromise runbook that
references this document's disaster-recovery steps.

This covers `scripts/backup_database.sh` and `scripts/restore_database.sh`:
what they capture, how to schedule and encrypt backups, what to do about
`.env`, and how to recover a broken installation without touching the data
you're trying to save. Run every command below from the directory containing
your `compose.yaml`.

## What the archive covers, and what it doesn't

`scripts/backup_database.sh` produces a PostgreSQL custom-format `pg_dump`
archive of the complete `mileage` database: schema, PostGIS objects, trips,
GPS points and raw ingest messages, local-admin credential hashes, places and
tagging rules, vehicles, expenses, odometer readings, overrides, caches, and
worker delivery ledgers. There's no table-level allowlist, so a future schema
change can't silently ship an unprotected table.

Three things are deliberately **not** in the archive:

- **`.env`.** It holds your database, ingest, session, setup, OIDC, and
  optional-service secrets and configuration, and none of it lives in
  PostgreSQL. The backup script never reads or prints it. See
  [Protecting `.env`](#protecting-env) below. Losing it is a real recovery
  problem even though the database restores fine on its own.
- **The optional `osrmdata` volume.** It holds reproducible OSRM routing
  artifacts built from a regional road extract, not data this application
  generated. If you use the `osrm` compose profile, reprovision it with the
  same one-time extract/partition/customize step described in
  [Self-hosted OSRM road-snapping](osrm.md#provisioning) rather than backing
  it up.
- **The PostGIS-managed `tiger`, `tiger_data`, and `topology` schemas.**
  These are created by the database image itself on every fresh volume, not
  by this application, so a restore target already has them without needing
  anything from the archive. All application data lives in the `public`
  schema, which is fully included above.

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

## Verifying an archive

`--verify-only` checks the checksum sidecar and the archive's internal table
of contents (`pg_restore --list`) without connecting to or changing any
database. It needs the `db` service running, since that's where the pinned
`pg_restore` binary comes from, but nothing else:

```sh
docker compose up -d db   # or: podman-compose up -d db
scripts/restore_database.sh --verify-only backups/mileage-20260719T030000Z.dump
```

Run this after every backup you intend to rely on, and again right before
any restore.

## Restoring

Restores are deliberately fresh-target-only: the script refuses to run while
the `app` service is up, and refuses any target database that already
contains application relations (including a pre-existing `schema_migrations`
table). There is no in-place overwrite and no `--replace` flag. Restoring
into a used database means creating a new, empty one first (see
[Disaster recovery](#disaster-recovery) below for the case where the old data
needs to survive alongside it).

For a target you already know is empty (a brand-new install, or a `dbdata`
volume you just recreated on purpose), stop the app and restore:

```sh
docker compose stop app
docker compose up -d db
scripts/restore_database.sh backups/mileage-20260719T030000Z.dump
docker compose up -d app
```

The restore runs `pg_restore --single-transaction --exit-on-error`, so a
failure mid-restore leaves nothing partially committed. On success it runs
`ANALYZE`, prints the restored schema version, and compares it against the
archive's manifest if one is next to it (a mismatch is a warning, not a
failure, since the restore already committed). It does not start the app for
you; start it yourself once you're satisfied, so its own startup migrations
(if the target release differs from the one that made the backup) run under
your observation.

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
  `dbdata` volume around. The Postgres image applies this variable only when
  initializing an empty data directory, so an existing volume's role
  password was fixed back when that volume was first created, a value that
  no longer matches in `.env` just means the app can't authenticate, and
  fixing it means resetting the role's password directly inside the
  container rather than editing `.env`. Restoring into a fresh volume (the
  supported path throughout this guide) sidesteps the problem entirely: put
  any new value in the fresh `.env` and the database role is initialized
  from it.
- **`SESSION_SECRET`** is safely regenerable. Put a new high-entropy value
  in `.env` and recreate the app. This signs every existing browser session
  out at once; it doesn't touch stored data.
- **`ADMIN_TOKEN`** is safely regenerable. Put a new high-entropy value in
  `.env` and recreate the app to reissue the one-time `/setup` page.
- **`INGEST_PASSWORD`** is safely regenerable, but every OwnTracks device
  needs its password field updated to match before it can post again. See
  [Connecting OwnTracks](owntracks.md).
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
- Every OwnTracks device needs reconfiguring with the new `INGEST_PASSWORD`
  before it can resume posting locations.
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

Periodically prove a backup restores, not just that the file exists. The
cheapest way is a disposable Compose project on the same host, using a
distinct project name so it gets its own volumes:

```sh
export COMPOSE_PROJECT_NAME=mileage-drill
docker compose up -d db
scripts/restore_database.sh --skip-checksum backups/mileage-20260719T030000Z.dump
# (omit --skip-checksum if the archive's .sha256 sidecar is right next to it)
docker compose up -d app
curl -fsS http://127.0.0.1:8077/healthz
```

Sign in, confirm a representative trip or two, then tear the drill down and
remove its volumes once you're satisfied:

```sh
docker compose down -v
unset COMPOSE_PROJECT_NAME
```

Because `COMPOSE_PROJECT_NAME` is unset again afterward, that `down -v`
targets only the disposable `mileage-drill` project's volumes. Your real
installation, running under its own project name, is untouched throughout.

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
