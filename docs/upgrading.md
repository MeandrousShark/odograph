# Upgrading

The canonical installation runs the exact published image pinned by
`compose.yaml`. Upgrading means taking and verifying a fresh backup, checking
out the newer exact release tag, and pulling that tag's image. Do not edit the
Compose pin to a moving alias such as `latest`.

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
occasion an additive migration might happen to tolerate it. Treat "the
migration succeeded" as a one-way door. The supported way back is restoring
the pre-upgrade backup, described in [Rollback](#rollback) below.

## Upgrading the PostGIS database image

When a target release's `compose.yaml` uses the signed native PostgreSQL
16.15/PostGIS 3.6.4 index
`ghcr.io/meandrousshark/odograph-postgis@sha256:b352024dd6f9ca2ba0f1e7125dcfdcf78b824f2cbe86edf89559e1ddf4d80241`,
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
   checksum, manifest, and `.env.pre-upgrade` protected.

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
   docker compose ps db
   scripts/restore_database.sh backups/pre-upgrade.dump
   docker compose up -d app
   docker compose ps
   curl -fsS http://127.0.0.1:8077/healthz
   ```

   Confirm the target database and app are healthy, then sign in and check a
   recent trip, a place, a vehicle, and an authenticated `/ingest` request.
   The restore script from the target release must run against the fresh
   target database. The old project and volume remain available for recovery.

For a Podman installation, replace each `docker compose` command with
`podman-compose`. This procedure is also the model used by the release
preflight database-image migration drill.

## Upgrade procedure

1. **Confirm your current exact release and read every release note between
   it and your target**, in order. If you're several releases behind, plan
   to move through each intervening release rather than jumping straight to
   the newest tag, unless a release's own notes say a direct jump is fine.
   See [Support policy](#support-policy).

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
   curl -fsS http://127.0.0.1:8077/healthz
   ```

   Wait for both services to report healthy and check the app logs for
   migration output (`applying migration NNN_...`) and any error. Confirm
   the schema is at the version the release notes expect. The restored
   schema version printed by `scripts/restore_database.sh` after a restore
   is the same figure a fresh backup's `.manifest` records, so you can
   compare it directly with a quick backup-and-inspect if the release notes
   cite a schema version:

   ```sh
   scripts/backup_database.sh --output /tmp/schema-check.dump
   grep schema_version /tmp/schema-check.dump.manifest
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
against the migrated database is unsupported.

## Failure guidance

**A failed migration** (startup logs an error and the app doesn't come up
healthy): the transaction rolled back automatically, so the database is
unchanged. You have not lost anything and you have not partially migrated
anything. Read the actual error before doing anything else; don't restart
the app repeatedly hoping a transient failure resolves itself, since a
migration failure is almost always deterministic (a data shape the migration
didn't expect, insufficient privileges, disk space) and will fail the same
way every time until the underlying cause is fixed. If you can't resolve it
promptly, check out the previous exact release, pull its pinned image, and
restart on the database that's still on the old schema, since nothing
committed.

**A successful migration followed by an application failure** (migrations
completed, but the app won't start cleanly, crashes under load, or otherwise
misbehaves for reasons unrelated to the schema): don't downgrade the code
and try to run it against the now-migrated database. That combination is
unsupported. The reliable path is the same either way: follow
[Rollback](#rollback) below.

## Rollback

Rollback means restoring the pre-upgrade backup with the previous release's
code, into a fresh volume, not running old code against the migrated database,
and not attempting to undo a migration in place. A database-image migration
also requires a fresh old-image project.

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
   curl -fsS http://127.0.0.1:8077/healthz
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

Only the latest release is supported, on a best-effort basis by one
maintainer. See [SECURITY.md](../SECURITY.md) and
[README.md](../README.md#support). Fixes ship as a new release, not as
backports to older tags. For a release several versions behind the latest,
the supported path is upgrading release-to-release in sequence, applying
each one's migrations and release notes in turn, unless a specific release's
notes explicitly document a supported direct jump.

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
