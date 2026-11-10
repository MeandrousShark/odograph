# Backups and disaster recovery

How to back up the database, check and restore a backup, and keep `.env`
safe. Run the commands from the installation directory, the one holding
`compose.yaml`. The scripts use `docker compose`, or `podman-compose` when
Docker is absent; set `COMPOSE_CMD` to choose.

## What the archive covers, and what it doesn't

`scripts/backup_database.sh` writes a `pg_dump` archive of the whole
`mileage` database: every account, trip, point, raw message and setting,
password and device credential hashes, and the generated passwords of
Odograph's restricted database roles. Treat it as sensitive as `.env`. An
archive made before 1.0 can also hold an OwnTracks tracker's plaintext
password; protect it, or replace that device's credential in
**Settings > Tracking**.

The archive does not hold:

- **`.env`.** Keep your own copy; see [Protecting `.env`](#protecting-env).
- **The `osrmdata` volume.** Rebuild it from a road extract
  ([Provisioning](osrm.md#provisioning)).
- **The cluster's role definitions.** The restore script recreates them.

Deleting data from the live database does not reach existing archives. Raw
messages pruned by `RAW_MESSAGE_RETENTION_DAYS` and purged accounts stay in
older archives until you delete those, and restoring one brings them back.

Your data lives in the Compose `dbdata` volume. Restarts and `down` then `up`
keep it; `docker compose down -v` deletes it. Copying the volume's files is
not a supported backup.

## One-off backup

```sh
scripts/backup_database.sh
```

This dumps the running `db` service without stopping the app and checks the
archive. It writes `backups/mileage-<UTC timestamp>.dump`, a `.sha256`
checksum beside it and a `.manifest` with the schema version, and makes
`backups/` mode `700`. It never reads `.env` and refuses to overwrite an
existing archive. Use `--output PATH` to choose the file. Without Compose,
`--container NAME` dumps a running database container directly.

The archive stays on this host until you copy it off; see
[Encryption and off-host copies](#encryption-and-off-host-copies).

## Scheduling

Run it daily from the crontab of the user that owns the installation and can
run Compose:

```cron
0 3 * * * cd /opt/odograph && scripts/backup_database.sh >> "$HOME/odograph-backup.log" 2>&1
```

## Verifying an archive

```sh
scripts/restore_database.sh --verify-only backups/mileage-20260719T030000Z.dump
```

This checks the archive against its `.sha256` and reads its table of
contents. It needs the `db` service running and changes no database. Run it
after each backup you rely on and before a restore. Only a
[restore drill](#restore-drill) proves the archive restores.

## Restoring

A restore needs an empty database: a new install, or a fresh Compose project
as in [Disaster recovery](#disaster-recovery). Use the checkout of the
release that made the backup, or of the release you are upgrading to.
Compose never pulls the app image, and the restore runs it, so verify it
first:

```sh
docker compose stop app
docker compose up -d db
scripts/verify_image.sh
scripts/restore_database.sh backups/mileage-20260719T030000Z.dump
docker compose up -d app
```

The script refuses to run while the app is running or its state is unclear,
and refuses a database that already holds application data. It checks the
archive's `.sha256` first, which must sit beside it. It needs free temporary
space for two uncompressed SQL copies of the archive. The data loads in one
transaction, so a failure leaves the database empty. It then recreates the
restricted roles and validates the security contract, and prints the
restored schema version. Starting the app runs any migrations the release
adds.

If the script reports that data was restored but security validation failed,
the restore did not succeed. Keep the app stopped and restore into another
fresh database with the matching release. Do not loosen permissions or
row-level security to get past it.

A restore signs out every browser and signed-in app and revokes open
invitations, email confirmations and password reset links. Passwords and
device credentials return as they were in the archive.

## Protecting `.env`

The archive never contains `.env`. Keep an encrypted copy of it off the host,
for example in a password manager. Without it:

- **`POSTGRES_PASSWORD`**: the database role keeps the password it was
  created with. On the original volume, reset the role's password inside the
  `db` container to match a new value. A fresh volume takes the new value.
- **`SESSION_SECRET`**: generate a new one; every browser is signed out.
- **Account passwords and device credentials** are in the database, not
  `.env`. For a lost password, see
  [Recover from the server](accounts.md#recover-from-the-server).
- **`OIDC_CLIENT_SECRET`, `GEOCODE_API_KEY`, ntfy and SMTP credentials** must
  be obtained again from each provider. A self-hosted Nominatim needs only
  its `GEOCODE_NOMINATIM_URL`.

If `.env` is gone, run `scripts/generate_env.sh` and put back each setting
you used ([Configuration reference](configuration.md)). The restored data is
unaffected.

## Encryption and off-host copies

A local `backups/` directory does not survive losing the host, and it holds
location history. Copy archives to encrypted, off-host storage with a backup
tool. For example, with [restic](https://restic.net/), after each backup:

```sh
export RESTIC_REPOSITORY=/mnt/backup-target/odograph-restic
export RESTIC_PASSWORD_FILE=/etc/odograph/restic-password
restic backup backups/
restic forget --keep-daily 7 --keep-weekly 5 --keep-monthly 12 --prune
```

[Borg](https://www.borgbackup.org/) works the same way. Odograph holds no
cloud credentials. Choose how long to keep archives: each one keeps the
location history it holds for as long as it exists.

## Restore drill

Prove an archive restores by loading it into a throwaway project on the same
host. This starts only a second database; production keeps running:

```sh
export COMPOSE_PROJECT_NAME=odograph-drill
docker compose up -d db
scripts/restore_database.sh backups/mileage-20260719T030000Z.dump
docker compose exec -T db psql -U mileage -d mileage -c 'SELECT count(*) FROM trips'
docker compose -p odograph-drill down -v
unset COMPOSE_PROJECT_NAME
```

Check that the schema version and trip count are what you expect. The last
`down -v` names the drill project, so it cannot delete your real volume. If a
step fails, still run the last two lines: a leftover `odograph-drill_dbdata`
volume makes the next drill's restore refuse a non-empty database.

## Disaster recovery

When you no longer trust the `dbdata` volume, restore into a new project and
leave the old volume untouched until the new one checks out.

1. Stop the old project: `docker compose stop`. Do not run `down -v`.
2. Give the install a new project name: set `COMPOSE_PROJECT_NAME` in `.env`
   to a new unused value, such as `odograph-recovery` (replace the line, or
   add one if there is none), then run `unset COMPOSE_PROJECT_NAME`.
   Scheduled backups and every later Compose command use the new project.
3. Restore into it as in [Restoring](#restoring).
4. Check `curl -fsS http://127.0.0.1:8077/healthz`, sign in, look over
   recent trips, and send a test point
   ([Verify your setup](owntracks.md#verify-your-setup)).
5. Delete the old volume yourself, once you no longer need it. Odograph never
   deletes a volume.

If `.env` was lost too, see [Protecting `.env`](#protecting-env).
