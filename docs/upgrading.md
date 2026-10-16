# Upgrading

The canonical installation runs the exact published image pinned by
`compose.yaml`. Upgrading means taking and verifying a fresh backup, checking
out the newer exact release tag, and pulling that tag's image. Do not edit the
Compose pin to a moving alias such as `latest`. See
[Floating image aliases](#floating-image-aliases) for the aliases that do
exist.

Read [Backups and disaster recovery](backups.md) first. Every upgrade here
depends on having a verified backup and a protected `.env` copy before you
touch anything.

## Forward-only migrations

Startup applies every pending numbered SQL migration in
[`migrations/`](../migrations) in order, inside a single transaction guarded
by a database advisory lock (so two app instances starting at once can't
race each other). If a migration fails partway through, the whole
transaction (including any earlier migrations that succeeded in that same
startup attempt) rolls back and the database is exactly as it was before
you started. A migration that finishes and commits, though, is permanent:
there are no down migrations, and nothing in this project undoes a
successfully applied migration in place.

Because of that, running an older release's code against a database that a
newer release has already migrated is not supported, even on the rare
occasion an additive migration might happen to tolerate it. The one exception
is a release pair whose notes explicitly document it as drilled; see
[Support policy](#support-policy). Treat "the migration succeeded" as a
one-way door. The supported way back is restoring the pre-upgrade backup,
described in [Rollback](#rollback) below.

## Upgrading the PostGIS database image

When a target release's `compose.yaml` uses the signed native PostgreSQL
16.15/PostGIS 3.6.4 index
`ghcr.io/meandrousshark/odograph-postgis@sha256:c4c1184f0c4d899f65c3df9ea6d296c3a3a5f1b233090651ac49429b4e2100b4`,
an installation that still uses the older `postgis/postgis:16-3.4` image must
be treated as a database-image migration. A normal `docker compose up -d`
against the existing project is not the migration procedure.

Keep the previous exact release tag, the original `.env`, the old database
image reference, the stopped old project and its volume, and the verified
pre-upgrade archive until the new project has passed its checks. Do not run
`docker compose down -v`, reuse the old `dbdata` volume, swap `PGDATA`
directories, or attempt an in-place downgrade. If the old Compose file uses an
explicit external volume, configure the target with a new unused volume name;
project-scoped and external target volumes must both be distinct from the old
volume. Never attach the target to the old volume.

A fresh project also gets a fresh `osrmdata` volume and network. If OSRM is
enabled, preserve the old routing volume and reverse-proxy or other Compose
overrides, provision the target `osrmdata` volume before enabling the profile,
and do not start the optional service against an empty volume. Keep the
existing `OSRM_URL` setting only when the target service or its endpoint is
ready.

1. **Read the release notes and stop writes before the final backup.** Keep the
   old project name unchanged while making the backup:

   ```sh
   docker compose stop app
   scripts/backup_database.sh --output backups/pre-upgrade.dump
   scripts/restore_database.sh --verify-only backups/pre-upgrade.dump
   docker compose stop db
   cp .env .env.pre-upgrade
   unset COMPOSE_PROJECT_NAME
   ```

   The app stays stopped during the final logical backup. Keep the archive,
   checksum, manifest, and `.env.pre-upgrade` protected. `.env.pre-upgrade` is
   a copy of your secrets: protect it like `.env`. The repository's
   `.gitignore` ignores it, so it is never committed.

2. **Check out the exact target release and create a fresh project name.** The
   target Compose file supplies the new immutable database image. Replace the
   existing `COMPOSE_PROJECT_NAME` line in `.env` with a new unused value. Do
   not regenerate `.env` and do not add a second project-name line:

   ```sh
   git fetch --tags
   git checkout vX.Y.Z
   # Edit .env: replace the existing COMPOSE_PROJECT_NAME line, or add exactly
   # one line if it is absent, using a new unused project name.
   # Keep every secret and application setting unchanged.
   unset COMPOSE_PROJECT_NAME
   # Do not use a `-p` override.
   docker compose config >/dev/null
   ```

3. **Initialize and restore the fresh target database before starting the app:**

   ```sh
   docker compose up -d db
   docker compose ps
   scripts/restore_database.sh backups/pre-upgrade.dump
   docker compose up -d app
   docker compose ps
   healthy=0; for i in $(seq 1 60); do curl -fsS http://127.0.0.1:8077/healthz && { healthy=1; break; }; sleep 5; done
   [ "$healthy" = 1 ] || echo "app did not become healthy; check: docker compose logs app" >&2
   ```

   The app needs a few seconds to start listening, and longer while the first
   start applies migrations, so the loop polls for up to five minutes. For a
   large database, follow `docker compose logs -f app` and repeat the check.
   Confirm the target database and app are healthy, then sign in and check a
   recent trip, a place, a vehicle, and an authenticated `/ingest` request.
   The restore script from the target release must run against the fresh
   target database. The old project and volume remain available for recovery.

For a Podman installation, replace each `docker compose` command with
`podman-compose`. This procedure is also the model used by the release
preflight database-image migration drill.

## Upgrading to 1.0

This section is for installations moving from a 0.x release to Odograph 1.0.
It adds to, and does not replace, the [upgrade procedure](#upgrade-procedure)
below. Read the release notes for every release between yours and 1.0, since
they can contain manual steps.

### Supported starting versions

Any published v0.10.x or v0.11.x release upgrades directly to 1.0. (v0.10.1
was never published.) An older installation first upgrades to v0.11.3,
following that release's documented upgrade path, and then upgrades to 1.0.

An installation upgrading from v0.11.0 or earlier that has not applied the
v0.11.1 road-snap correction must apply it. Take and verify the pre-upgrade
backup first, then apply the correction while the database is still at
schema 25, before the first 1.0 start. With the database image migration, the
old database is stopped after the backup, so run both commands against the
restored target database, after `scripts/restore_database.sh` and before
`docker compose up -d app`. The commands save the values the correction
changes first, so it can be reversed without restoring a backup. The first
streams the CSV to the host, so it lands in the current host directory (a
`\copy` to a file would write inside the database container and be lost when
it is recreated):

```sh
docker compose exec -T db psql -U mileage -d mileage -c "\copy (SELECT id, distance_snapped_m FROM trips WHERE distance_snapped_m IS NOT NULL AND distance_m > 0 AND distance_snapped_m / distance_m < 0.85) TO STDOUT WITH CSV HEADER" > snap-correction.csv

docker compose exec -T db psql -U mileage -d mileage -c "UPDATE trips SET distance_snapped_m = NULL
WHERE distance_snapped_m IS NOT NULL
  AND distance_m > 0
  AND distance_snapped_m / distance_m < 0.85;"
```

Keep `snap-correction.csv` with your backups. The saved file lives outside
the database, so it adds no table that a later upgrade would have to account
for (see the `public` schema item below).

If you restore the pre-upgrade backup to recover, the correction is not in
it, so apply it again afterward. The same statements are valid on a database
that has already reached schema 45, if you find you skipped them.

### Operator impact

Migrations 026 to 044 predate migration classes, so they carry no per-migration
label. Together they change the following for an operator. Migration 045 is
class `rewrite`: it removes the database guard that limited an installation to
one account, so the administrator can invite members. It changes no stored
rows.

1. **The database role must be a superuser or have `BYPASSRLS`.** Every
   startup checks this before it touches the database, and refuses to run
   migrations as any other role. Row-level security is forced on account-owned
   tables from schema 28, and a role that is subject to it would make a data
   migration silently skip rows. The canonical Compose `mileage` role is the
   database's bootstrap superuser, so it already qualifies. If you use an
   external database with a restricted `DATABASE_URL` role, change the role
   before upgrading. A refusal here happens before anything is applied.
2. **The first startup is one long run.** It applies migrations 026 to 045 in
   a single transaction under the migration advisory lock. The app is not
   healthy until that run finishes, so the container can report unhealthy
   meanwhile. Follow `docker compose logs -f app` rather than restarting it.
   Do not interrupt the run: stopping it rolls the whole transaction back,
   leaves the database as it was, and the next start repeats the work from the
   beginning. Duration grows with the size of the history. Migration 026
   rewrites every point, stay and trip row and builds new indexes, and the
   storage accounting passes below scan every stored table. Keep comfortably
   more free disk than the pre-upgrade archive's size.
3. **Storage accounting is backfilled (migration 040).** The migration creates
   the per-account and per-device usage tables and adds triggers to every
   account-owned table, then runs one pass over all of those tables to compute
   each row's logical storage charge and fill the counters. The pass reads
   every row, so its time is driven mainly by the number of raw messages and
   points, and by the size of stored payloads and trip path geometry.
   Migration 042 does a second similar pass: it records a replay receipt with
   a SHA-256 of each device-attributed raw message payload, then reconciles
   the counters again. After the migrations commit, startup verifies the
   counters against the stored data once more, on this and every later start.
   These are logical charges, not disk bytes. See
   [Logical storage accounting](configuration.md#logical-storage-accounting).
4. **Migration 042 checks the storage budget.** It grants every existing
   account the default 2 GiB allowance and stops if those grants together
   exceed the funded budget (the default 12 GiB budget less the 2 GiB
   reserve, so more than five accounts). It uses these built-in defaults, not
   your `STORAGE_*` settings, which are applied after migrations commit. The
   failure reads `storage capacity exceeded: existing account grants exceed
   instance budget`. Installations on supported 0.x releases have a single
   account, so this is not expected to fire. If it does, the transaction rolls
   back and the database is unchanged on its previous schema, as described in
   [Failure guidance](#failure-guidance). Changing `STORAGE_*` does not help.
   Do not delete accounts or edit grants by hand: return to the old project as
   described under [Failure guidance](#failure-guidance), keep the verified
   backup, and report the problem with the number of rows in `accounts`. Separately, if your `STORAGE_*` values leave less
   grant room than your accounts need, startup fails after the migrations have
   committed with `application database setup failed`. Correct the values in
   `.env` and restart; no restore is needed, but the previous image can no
   longer run against that database.
5. **Migration 027 permanently deletes some raw messages.** It deletes every
   `raw_messages` row whose OwnTracks payload type is `dump` or
   `configuration`. Those messages carry the tracker's configuration,
   including its username, password and URL, and nothing reads them. Earlier
   releases stored every authenticated payload, so such rows may exist. The
   deletion cannot be undone in the live database. Your pre-upgrade backup and
   older backups still contain them; see
   [Backups](backups.md#what-the-archive-covers-and-what-it-doesnt).
6. **The database image changes.** Odograph 1.0 uses the pinned native
   PostGIS image, so an installation on the older `postgis/postgis:16-3.4`
   image must follow
   [Upgrading the PostGIS database image](#upgrading-the-postgis-database-image).
   A normal `docker compose up -d` against the existing project is not the
   migration. The long first startup above happens when the app starts against
   the restored target database.
7. **The `public` schema must hold only Odograph's relations.** After the
   migrations, startup requires the tables, views, materialized views and
   foreign tables in `public` (apart from objects owned by an extension, such
   as PostGIS's `spatial_ref_sys`) to match the application's list exactly.
   Move any table you added to another schema, or drop it after backing it up,
   before upgrading. To list the candidates:

   ```sh
   docker compose exec -T db psql -U mileage -d mileage -c "SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','f') AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e') ORDER BY 1"
   ```

   Anything in that list that the migrations in [`migrations/`](../migrations)
   do not create is extra, except `schema_migrations`, which startup creates
   itself. If you miss one, the first startup applies the
   migrations and then refuses with `application database security contract
   mismatch: relation set: unexpected [...]` naming it. The database is
   already migrated at that point and the previous image can no longer run
   against it. Move or drop the relation and restart; no restore is needed.
8. **Existing data is assigned to the established account (schema 26).**
   An installation with data but no account must establish one on the
   previous release first. See
   [Account ownership migration](#account-ownership-migration-schema-26).
9. **`SESSION_SECRET` must be at least 32 characters.** Startup refuses a
   shorter value. Replace it before upgrading, for example with
   `openssl rand -base64 32`; this signs out every browser.

Rehearse the upgrade against a copy of your own backup before you upgrade the
live installation, to learn the first-start duration and catch any refusal
above. See
[Rehearse an upgrade against your own backup](#rehearse-an-upgrade-against-your-own-backup).

### Recovery

No exact-image rollback is supported across 0.x to 1.0. Recovery means
restoring the verified pre-upgrade backup into a fresh volume under the old
image. That loses every write made after the backup. The steps are in
[Rollback](#rollback). While the stopped old project and its volume are
untouched, you can instead stop the target project (`docker compose stop app
db` while `.env` still names it), restore `.env` from `.env.pre-upgrade`,
check out the previous release and start that project again; it still holds
the pre-upgrade data.

## Rehearse an upgrade against your own backup

`scripts/rehearse_upgrade.sh` shows how a target release behaves on your data
before you upgrade. It restores a verified backup into a new, disposable
Compose project that uses the target's database image (the same fresh-volume
path as [Upgrading the PostGIS database image](#upgrading-the-postgis-database-image)),
starts the target release against that copy, and writes a summary:

```sh
git fetch --tags
scripts/backup_database.sh --output backups/rehearsal.dump
scripts/rehearse_upgrade.sh --archive backups/rehearsal.dump \
  --candidate vX.Y.Z --candidate-image ghcr.io/meandrousshark/odograph:vX.Y.Z
```

`--candidate` supplies the target's Compose file and scripts.
`--candidate-image` runs that exact published image; without it, the script
builds the app from the target's source. If your installed release predates
the script, run it from a separate clone of the repository and pass the
archive's full path. The script prints the summary path, or writes to
`--output PATH`. It exits non-zero when any check fails.

The summary lists how long each migration took, the time from start to
healthy or failure, the startup and `application_roles verify` results, the
schema version before and after, row counts per table with their change, and
any public relation the target does not know, which makes startup refuse. It
also names the image identities, the loopback port and the environment
source. It never contains row values, email addresses, coordinates or log
lines.

The rehearsal is isolated from your installation:

- It uses a new project named `odorehearse<number>` and refuses to run if
  anything with that name exists. It never reads your `.env` or touches your
  running project, its volume or any other project.
- The project network is internal, so the app and its workers cannot reach
  the internet. Only one loopback health port, never 8077, is published.
- The environment is generated with fresh secrets and contains no SMTP,
  geocoding, routing, OIDC, ntfy or other outbound credentials.
- On success or failure, it removes its containers, volume, network and any
  image it built. `--keep` leaves them in place and saves raw app logs in its
  scratch directory. Those logs can contain personal data; delete them when
  you are done.

For an archive below schema 26, migration 026 imports personal preferences
and mileage-rate overrides from the environment. To rehearse that import,
copy only the relevant lines into a separate file and pass
`--legacy-env FILE`. Allowed keys are `DISPLAY_TZ`, `NTFY_TOPIC`,
`NUDGE_WEEKLY_HOUR`, `ODOMETER_REMINDER`, `ODOMETER_REMINDER_HOUR`,
`EMAIL_TO`, `EMAIL_WEEKLY_NUDGE`, `EMAIL_MONTHLY_SUMMARY`,
`EMAIL_FILING_REMINDER`, `EMAIL_ODOMETER_REMINDER`, `EMAIL_DIGEST_HOUR`,
`EMAIL_FILING_REMINDER_MMDD` and `MILEAGE_RATE_<YEAR>`. Any other non-empty
key is refused. If ntfy was configured without an explicit
`ODOMETER_REMINDER`, add `ODOMETER_REMINDER=1` to match the real import. The
old ingest password is never copied; the rehearsal imports a synthetic one.

The rehearsal needs free disk for a full copy of your database in its new
volume, plus the archive rendered to SQL in a temporary directory. Set
`TMPDIR` to a directory with room for it. Migrations that rewrite large tables
can take a long time; `--health-timeout SECONDS` (default 3600) sets how long
to wait.

A rehearsal is not a backup. It never changes the archive and keeps nothing
after cleanup. Take and verify the pre-upgrade backup as described below
even after a passing rehearsal.


## Upgrade procedure

1. **Confirm your current exact release and read the release notes for every
   release between it and your target**, in order. Each release's notes state
   its supported upgrade path and any manual steps. From 1.0, any 1.x release
   upgrades directly to any later 1.x release, including skipped ones. To
   move from 0.x to 1.0, read
   [Upgrading to 1.0](#upgrading-to-10) first. See
   [Support policy](#support-policy).

2. **Confirm the host is ready, then take and verify a fresh backup:**

   ```sh
   docker compose ps                                 # or: podman-compose ps
   curl -fsS http://127.0.0.1:8077/healthz
   scripts/backup_database.sh --output backups/pre-upgrade.dump
   scripts/restore_database.sh --verify-only backups/pre-upgrade.dump
   ```

   Confirm both services report healthy and the host has comfortably more
   free disk than that archive's size, a rough proxy for your database
   size, and worth having headroom for since some migrations temporarily
   need room for both an old and a rewritten copy of a table. Also make sure
   your separate encrypted copy of `.env` is current (see
   [Protecting `.env`](backups.md#protecting-env)). The upgrade doesn't
   change `.env`, but the rollback path in this guide assumes you still have
   it.

3. **Check out the exact target release:**

   ```sh
   git fetch --tags
   git checkout vX.Y.Z
   ```

   Checking out an exact tag, rather than a moving branch, gives you the
   matching Compose file, operational scripts, and release notes. Its app
   service pins the corresponding immutable image tag.

4. **If the database image is unchanged, pull the pinned app image and restart,
   then verify:**

   If the target Compose file changes the `db.image` value, stop here and use
   [Upgrading the PostGIS database image](#upgrading-the-postgis-database-image)
   above. Do not run `up -d` against the old project for that transition.

   ```sh
   docker compose pull app
   docker compose up -d
   docker compose ps
   docker compose logs app
   healthy=0; for i in $(seq 1 60); do curl -fsS http://127.0.0.1:8077/healthz && { healthy=1; break; }; sleep 5; done
   [ "$healthy" = 1 ] || echo "app did not become healthy; check: docker compose logs app" >&2
   ```

   Wait for both services to report healthy and check the app logs for
   migration output (`applying migration NNN_...`) and any error. Confirm
   the schema is at the version the release notes expect. The restored
   schema version printed by `scripts/restore_database.sh` after a restore
   is the same figure a fresh backup's `.manifest` records, so you can
   compare it directly with a quick backup-and-inspect if the release notes
   cite a schema version:

   ```sh
   scripts/backup_database.sh --output backups/schema-check.dump
   grep schema_version backups/schema-check.dump.manifest
   ```

   Then sign in and spot-check representative data (a recent trip, a place,
   a vehicle) before you consider the upgrade successful.

   Contributors deliberately testing a source-built candidate use the
   explicit build override instead of the canonical image pull:

   ```sh
   docker compose -f compose.yaml -f compose.build.override.yml up -d --build
   # or:
   podman-compose -f compose.yaml -f compose.build.override.yml up -d --build
   ```

   This is not the normal operator upgrade path.

5. **Keep the pre-upgrade archive** (`backups/pre-upgrade.dump` from step 2,
   plus its `.sha256` and `.manifest`) until the new release has run through
   an observation window you're comfortable with. A few days to a couple of
   weeks is a reasonable starting point, longer if the release notes flag
   anything you want to watch closely. Don't let your normal backup
   retention expire it before that window closes.

## Account ownership migration (schema 26)

Schema 26 assigns existing personal data to the installation's established
account. It preserves that account's actual ID, local password and linked
identity, along with record IDs, trip geometry, edits, categories, exclusions,
and detector progress. Registration remains closed after the first account.
The application uses restricted database roles and explicit account ownership.
From schema 28, row-level security is enabled and forced on account-owned
tables. The `DATABASE_URL` role must be a superuser or have BYPASSRLS.

Before this upgrade, establish the account on the previous release if the
installation contains data but has no account. Sign in through the existing
provider and complete account establishment, or use the previous release's
supported `create-admin` command. The migration refuses to guess an owner
for populated accountless databases. Do not delete data or insert account
ID 1 by hand to bypass that check.

Stop the app, ingest and background writes before the final pre-upgrade
backup. Keep that verified archive, its sidecars, the previous exact image,
and the original `.env` until acceptance and the observation window finish.
If this upgrade also changes the database image, follow the fresh-volume
procedure above.

The migration imports effective personal settings and mileage-rate overrides
from the old configuration once. Afterward, change personal preferences in
Settings; editing their old environment variables does not overwrite stored
preferences. Instance transport settings, provider secrets, and worker
intervals remain environment configuration.

Existing tracker labels become account-owned devices. Existing Basic ingest
credentials are imported once as a legacy adapter, preserving configured
phones through the upgrade. Device credentials issued in Tracking settings
identify their device without trusting the payload's tracker label. Revoking
the adapter or a device credential stays effective across restart; leaving
old environment values in place does not recreate it.

After startup, sign in with the original credentials and check representative
trips, exclusions, places, vehicles, mileage rates and notification settings.
Submit an authenticated point from an existing phone and confirm it appears
under the intended device. Check the security contract with:

```sh
docker compose exec -T app python -m app.application_roles verify
```

Use the matching release's backup and restore scripts for schema-26 archives;
they include protected role state and reconstruct permissions before startup.
A failed security-contract check blocks startup and must be investigated,
not repaired by granting runtime access to the database owner. If rollback is
needed after migration commits, restore the verified pre-upgrade archive into
a fresh database using the previous exact image. Starting that old image
against the migrated database is unsupported for 0.x to 1.0.

## Detector version changes

When a release changes `DETECTOR_VERSION`, the background detector reprocesses
every device from its first point after the upgrade. With
`DETECTOR_TURN_MAX_POINTS` set, a device with more points than the limit is
deferred and logged instead. Run
`python -m app.reprocess_detector --account ACCOUNT_ID` inside the app
container for each account named in those warnings. See
[Large detector windows](configuration.md#large-detector-windows).

## Failure guidance

**A failed migration** (startup logs an error and the app doesn't come up
healthy): the transaction rolled back automatically, so the database is
unchanged. You have not lost anything and you have not partially migrated
anything. Read the actual error before doing anything else; don't restart
the app repeatedly hoping a transient failure resolves itself, since a
migration failure is almost always deterministic (a data shape the migration
didn't expect, insufficient privileges, disk space) and will fail the same
way every time until the underlying cause is fixed. If you can't resolve it
promptly after an application-only upgrade, check out the previous exact
release, pull its pinned image, and restart on the database that's still on
the old schema, since nothing committed. After a database-image migration,
including 0.x to 1.0, do not start the previous release on the target
database: the new image initialized it, and the older PostGIS libraries do not
match it. Stop the target project first (`docker compose stop app db` while
`.env` still names it, so it releases port 8077 and stops restarting), then
restore `.env` from `.env.pre-upgrade`, check out the previous release and
start the stopped old project again, which still holds the pre-upgrade data,
or follow
[Rollback after a database-image migration](#rollback-after-a-database-image-migration).

**A successful migration followed by an application failure** (migrations
completed, but the app won't start cleanly, crashes under load, or otherwise
misbehaves for reasons unrelated to the schema): don't downgrade the code
and try to run it against the now-migrated database. That combination is
unsupported unless the release notes document that exact pair as drilled (see
[Support policy](#support-policy)). The reliable path is the same either way:
follow [Rollback](#rollback) below.

## Rollback

Rollback means restoring the pre-upgrade backup with the previous release's
code, into a fresh volume, not running old code against the migrated database,
and not attempting to undo a migration in place. A database-image migration
also requires a fresh old-image project. Running the previous exact image
against the migrated database is supported only where a release's notes
document that release pair as drilled; see [Support policy](#support-policy).
None exists for 0.x to 1.0.

Odograph 1.0 and later refuse to start against a database migrated by a newer
release. Startup, and the maintenance commands for account management,
detector reprocessing, role verification and restore finalization, stop
before changing anything and report that the database schema version is newer
than the image's newest migration, naming both numbers. A restore of a newer
release's backup under an older image fails at finalization, leaving the
restored data in a target that must not be used. Either run the newer release
that migrated the database, or restore the verified pre-upgrade backup under
this release as described below. Images before 1.0 do not have this check, so
running an older 0.x image against a migrated database remains unsupported
even if it appears to start.

### Rollback after a database-image migration

1. Stop the target app and database, leaving the target volume available for
   inspection if needed:

   ```sh
   docker compose stop app db
   ```

2. Check out the previous exact release. Restore the original environment from
   `.env.pre-upgrade`, then replace its `COMPOSE_PROJECT_NAME` line with a
   different new unused rollback project name. Do not point the rollback at the
   old project or its old volume:

   ```sh
   git checkout vPREVIOUS
   cp .env.pre-upgrade .env
   # Edit .env: replace the existing COMPOSE_PROJECT_NAME line, or add exactly
   # one line if it is absent, using a fresh rollback project name.
   # Do not use a shell COMPOSE_PROJECT_NAME value or a `-p` override.
   unset COMPOSE_PROJECT_NAME
   docker compose config >/dev/null
   ```

3. Start only the old-image database, restore the original pre-upgrade archive,
   then start and verify the old application:

   ```sh
   docker compose up -d db
   scripts/restore_database.sh backups/pre-upgrade.dump
   docker compose up -d app
   docker compose ps
   healthy=0; for i in $(seq 1 60); do curl -fsS http://127.0.0.1:8077/healthz && { healthy=1; break; }; sleep 5; done
   [ "$healthy" = 1 ] || echo "app did not become healthy; check: docker compose logs app" >&2
   ```

   Sign in and check representative data before switching traffic. Keep the
   old volume and the failed target volume until recovery is complete.

Every write after the pre-upgrade backup, including writes made by the target
release before rollback, is lost. This is the expected result of restoring a
point-in-time logical backup.

### Rollback after an application-only upgrade

1. Stop the candidate release:

   ```sh
   docker compose stop app
   ```

2. Check out the exact previous release:

   ```sh
   git checkout vPREVIOUS
   docker compose pull app
   ```

3. Restore the pre-upgrade dump into a **fresh** volume/Compose project, per
   [Disaster recovery](backups.md#disaster-recovery). The same
   fresh-target-only restore path applies here; there is no supported way to
   restore over the now-migrated database in place.

4. Verify the restored instance (health, schema version, login,
   representative data, same checks as upgrade step 4) before switching
   production traffic back to it.

Everything committed to the database after the pre-upgrade backup was taken
is lost by this rollback: any trips, edits, or ingested points from the
upgrade attempt onward don't exist in the restored copy. That's the
unavoidable cost of restoring a point-in-time backup rather than undoing a
migration that was never designed to be undone.

## Support policy

The latest 1.x release is supported, on a best-effort basis by one
maintainer. See [SECURITY.md](../SECURITY.md) and
[README.md](../README.md#support). Fixes ship as a new release, not as
backports to older tags.

- After v2.0.0 ships, v1 receives critical security fixes for 6 months, then
  is unsupported.
- 0.x releases are unsupported once 1.0 ships. To leave one, see
  [Upgrading to 1.0](#upgrading-to-10).

Within 1.x:

- Any 1.x release upgrades directly to any later 1.x release, including
  skipped releases. Read the notes of each release in between.
- Each 1.x release is drilled from v1.0.0 and from the previous release.
- Exact-image rollback, running the previous image against the upgraded
  database, is supported only for a release pair that a release's notes
  explicitly document as drilled. By default no pair is.
- Otherwise, recovery is restoring the verified pre-upgrade backup as
  described in [Rollback](#rollback), or a corrective forward release.

## Floating image aliases

Exact version tags such as `vX.Y.Z` are immutable. The registry also carries
floating aliases for operators who choose them deliberately: `vX.Y` follows the
newest patch of a minor release, and from 1.0 `v1` follows the newest verified
stable 1.x release. `v1` never points at a prerelease or a 2.x release. The
canonical Compose file pins an exact tag and does not use either.

Pulling a floating alias again selects the current release, not the one you
ran before. Record the installed image digest before upgrading so you can
identify what you were running (replace `IMAGE` with the image reference you
run):

```sh
docker image inspect --format '{{index .RepoDigests 0}}' IMAGE
# or: podman image inspect --format '{{index .RepoDigests 0}}' IMAGE
```

## Account migration for older installations

This section mainly applies when upgrading an older installation across the
account transition. For a normal upgrade, read the release notes and continue
to [Upgrade procedure](#upgrade-procedure). Return here only when those notes
mention migrations 020 or 021.

The account foundation uses two additive migrations. Migration 020 moves an
existing local administrator into account ID 1 without changing the normalized
email or password hash. Migration 021 adds linked OIDC identity records. No
trip, point, report, or settings row becomes user-owned in those two migrations;
schema 26 performs the later ownership migration described above.

An existing local administrator is migrated to account ID 1 with the same
normalized email and password hash. The old password continues to work. An
obsolete `ADMIN_TOKEN` entry may remain in an existing `.env`; the application
ignores it, and `/setup` no longer exists. The value is not printed by
diagnostics and cannot enable signup or password reset.

`INITIAL_ADMIN_SIGNUP` defaults to disabled when absent. Upgrading an existing
OIDC-only installation therefore does not open public registration. While no
account exists, signup stays disabled, and OIDC remains configured, sign in
with the existing provider and continue at `/account/establish`. That one-time
transition creates the local administrator credentials and links the current
provider identity in one transaction. Afterward, local and OIDC login reach the
same account. The durable link uses the provider's exact issuer and subject;
matching email addresses do not create or select links.

Because both account migrations are forward-only, an upgrade that commits
them cannot be rolled back by starting the previous image against that same
database. Restoring the verified pre-upgrade archive into a fresh volume is
the supported rollback path. That restore also removes any account or linked
identity created after the backup, along with every other post-backup write.

`ALLOWED_EMAIL`, when configured, gates only this narrow legacy transition. It
does not authorize a linked OIDC login. Without it, any identity accepted by
the configured provider can reach the transition, preserving the installation's
previous provider-trust boundary. If that boundary is too broad, or the
provider is unavailable, create the missing account from inside the
application container instead:

```sh
docker compose exec app python -m app.manage_account create-admin
# or: podman-compose exec app python -m app.manage_account create-admin
```

Use `list-accounts` and then `reset-password ACCOUNT_ID` instead when an account
already exists. Both commands read
passwords interactively or from protected standard input and accept no password
argument. After `create-admin`, sign in locally and use Account Settings to link
the provider deliberately when it is available. Linking requires the current
local password and a fresh provider authorization. Odograph stores the linked
issuer and subject plus safe display metadata, but does not retain OIDC access,
refresh, or ID tokens after the callback.
