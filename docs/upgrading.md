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

## Account migration

The account foundation uses two additive migrations. Migration 020 moves an
existing local administrator into account ID 1 without changing the normalized
email or password hash. Migration 021 adds linked OIDC identity records. No
trip, point, report, or settings row becomes user-owned in this release.

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

Use `reset-password` instead when an account already exists. Both commands read
passwords interactively or from protected standard input and accept no password
argument. After `create-admin`, sign in locally and use Account Security to link
the provider deliberately when it is available. Linking requires the current
local password and a fresh provider authorization. Odograph stores the linked
issuer and subject plus safe display metadata, but does not retain OIDC access,
refresh, or ID tokens after the callback.

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

4. **Pull the pinned image and restart, then verify:**

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
code, into a fresh volume, not running old code against the migrated
database, and not attempting to undo a migration in place.

1. Stop the candidate release:

   ```sh
   docker compose stop app
   ```

2. Check out the exact previous release:

   ```sh
   git checkout vX.Y.Z-1
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
