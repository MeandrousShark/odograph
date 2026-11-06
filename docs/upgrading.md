# Upgrading

There are two ways to stay current: track the `v1` image and pull, or pin an
exact release and change the pin. Both start with a backup. Each release's
notes are on the
[GitHub Releases page](https://github.com/MeandrousShark/odograph/releases)
and in [CHANGELOG.md](../CHANGELOG.md); anything a release changes on its
own is listed at the top of its notes.

## Before any upgrade

Take a backup and check it, and keep a current copy of `.env`
([Protecting `.env`](backups.md#protecting-env)):

```sh
scripts/backup_database.sh --output backups/pre-upgrade.dump
scripts/restore_database.sh --verify-only backups/pre-upgrade.dump
```

Keep that archive until the new release has run for a while. It is the only
way back (see [Rollback](#rollback)). To try a release against a copy of
your data first, `scripts/rehearse_upgrade.sh` restores the archive into a
disposable, network-isolated project and reports what the upgrade changed.

## Track `v1`

Most installs can follow `v1`, which always points at the newest stable 1.x
release. Add a `compose.override.yaml` next to `compose.yaml` once:

```yaml
services:
  app:
    image: ghcr.io/meandrousshark/odograph:v1
    pull_policy: missing
```

To upgrade, back up, record the digest you are running (for
[Rollback](#rollback)), then pull and recreate the app:

```sh
docker image inspect --format '{{index .RepoDigests 0}}' ghcr.io/meandrousshark/odograph:v1
docker pull ghcr.io/meandrousshark/odograph:v1
docker compose up -d --force-recreate --no-deps app
```

`pull_policy: missing` keeps a plain `up` from upgrading, which is also why
the pull is a direct `docker pull` (`docker compose pull` would skip an image
that is already present). With Podman, use `podman image inspect`,
`podman pull` and `podman-compose`.
`--force-recreate` matters there: podman-compose otherwise restarts the old
container, because the image name has not changed.

Every 1.x release keeps these promises, and a release that cannot is a 2.0:

- Pulling a newer `v1` image into a working 1.x install, with its existing
  `compose.yaml` and `.env`, keeps it working with no manual step. Release
  preflight drills this from v1.0.0's files.
- Anything the release does on its own, such as reprocessing trips or
  changing a default, is listed at the top of its release notes.
- The database image never changes by pulling the app. It stays pinned in
  your `compose.yaml` until you take a newer one and follow
  [Database image changes](#database-image-changes).

When a release's notes say its database image was rebuilt or changed, check
out that release and follow [Database image changes](#database-image-changes).
Until you do, the database keeps its old image and pulling `v1` keeps
working.

This path does not check the image signature: `verify_image.sh` verifies
exact release tags only. `v1` never points at a prerelease or a 2.x release,
and there is no `latest` tag. A `vX.Y` alias follows the newest patch of one
minor release.

## Pin an exact release

The canonical checkout pins an exact release tag, and Compose never pulls
the app image itself (`pull_policy: never`). `scripts/verify_image.sh` pulls
the tag and checks its signature with
[cosign](https://docs.sigstore.dev/cosign/system_config/installation/).

1. Read the notes of every release between yours and the target. Any 1.x
   release upgrades directly to any later one.
2. Back up as [above](#before-any-upgrade).
3. Check out the target release:

   ```sh
   git fetch --tags
   git checkout vX.Y.Z
   ```

4. **If the database image is unchanged, verify the app image, start it and
   check health.** If the target's `compose.yaml` changes `db.image`, stop
   and follow [Database image changes](#database-image-changes) instead.

   ```sh
   scripts/verify_image.sh
   docker compose up -d
   until curl -fsS http://127.0.0.1:8077/healthz; do sleep 5; done
   ```

   Do not continue if `verify_image.sh` fails. Startup applies migrations
   before `/healthz` answers; follow `docker compose logs -f app` for a long
   one. Then sign in and check a recent trip.

With Podman, use `podman-compose` and
`CONTAINER_RUNTIME=podman scripts/verify_image.sh`. A
[minimal Compose installation](install-compose.md) runs `./verify_image.sh`
from its own directory.

## What happens on its own

- **Migrations** run at startup in one transaction. A failed migration rolls
  back completely, so the database is unchanged: read the error, fix the
  cause, and start again or go back to the previous image. A migration that
  commits is permanent; there are no down migrations.
- **Newer databases are refused.** Since 1.0, an image refuses to start
  against a database a newer release has migrated, naming both schema
  versions.
- **Detector version changes**, below.

## Detector version changes

When a release changes `DETECTOR_VERSION`, the detector reprocesses every
enabled, unrevoked device's history after the upgrade. Trips that still
match keep their tags; a trip that is no longer detected is deleted and its
expenses are detached, and only the pre-upgrade backup brings it back. With
`DETECTOR_TURN_MAX_POINTS` set, a device over the limit is deferred and
logged; run `python -m app.reprocess_detector --account ACCOUNT_ID` in the
app container for each one
([Large detector windows](configuration.md#large-detector-windows)).

## Database image changes

A release that changes `db.image` says which kind of change it is. Both
kinds need the backup and restore scripts, and neither happens by pulling
the app.

### Database image security rebuilds

The same PostgreSQL and PostGIS versions with refreshed packages, so the new
image runs on the existing volume:

```sh
docker compose stop app
scripts/backup_database.sh --output backups/pre-upgrade.dump
scripts/restore_database.sh --verify-only backups/pre-upgrade.dump
docker compose exec -T db psql -U mileage -d mileage -Atc 'SELECT version(), postgis_full_version()' > versions-before.txt
docker compose stop db
git fetch --tags && git checkout vX.Y.Z
scripts/verify_image.sh
docker compose pull db
docker compose up -d db
until docker compose exec -T db pg_isready -U mileage -d mileage; do sleep 2; done
db_image="$(sed -n '/^  db:/,/^  [^ ]/s/^    image: *//p' compose.yaml)"
test "$(docker inspect --format '{{.Image}}' "$(docker compose ps -q db)")" = \
  "$(docker image inspect --format '{{.Id}}' "$db_image")" && echo "db runs $db_image"
docker compose exec -T db psql -U mileage -d mileage -Atc 'SELECT version(), postgis_full_version()' | diff versions-before.txt -
```

The image check must print `db runs`, since the version check alone cannot
tell a swap from the old image still running, and `diff` must print
nothing. If either check fails, do not start the app: stop the database,
check out the previous release and start its database again on the same
volume. When both pass, start the app with `docker compose up -d`.

With Podman, use `podman-compose`, `podman inspect` and
`podman image inspect`, and find the database container with
`podman ps -q --filter label=com.docker.compose.project=PROJECT --filter label=com.docker.compose.service=db`,
where `PROJECT` is `COMPOSE_PROJECT_NAME` from `.env`, or the directory name
when it is not set.

### Upgrading the PostGIS database image

A version change needs a fresh database: back up, start the target release
in a new Compose project and restore into it. The old project and volume
stay untouched until the new one passes its checks. Never run
`docker compose down -v`, reuse the old volume or attempt a downgrade.

1. Stop writes and back up under the old project:

   ```sh
   docker compose stop app
   scripts/backup_database.sh --output backups/pre-upgrade.dump
   scripts/restore_database.sh --verify-only backups/pre-upgrade.dump
   docker compose stop db
   cp .env .env.pre-upgrade
   ```

2. Check out the target and give it a new project name: set
   `COMPOSE_PROJECT_NAME` in `.env` to a new unused value (replace the line,
   or add one if there is none), changing nothing else. Without a new name,
   the new database image would start on the old volume.

   ```sh
   git fetch --tags && git checkout vX.Y.Z
   unset COMPOSE_PROJECT_NAME
   docker compose config >/dev/null
   scripts/verify_image.sh
   ```

3. Start the new database, restore, then start the app:

   ```sh
   docker compose up -d db
   scripts/restore_database.sh backups/pre-upgrade.dump
   docker compose up -d app
   until curl -fsS http://127.0.0.1:8077/healthz; do sleep 5; done
   ```

If OSRM is enabled, provision the new project's `osrmdata` volume before
enabling its profile. To go back, stop the new project, restore `.env` from
`.env.pre-upgrade`, check out the previous release and start the old project,
which still holds the pre-upgrade data.

## Rollback

Migrations are forward-only, so going back means restoring the pre-upgrade
backup with the previous release into a fresh Compose project, as
[Disaster recovery](backups.md#disaster-recovery) describes. Everything
written after that backup is lost. Running an older image against a migrated
database is not supported, and 1.0 and later refuse to start that way.

On the pinned path, copy `scripts/verify_image.sh` outside the checkout
before checking out the previous release, since an older release may not
contain it, and run the copy before `up`. On the `v1` path, point the
override's image at the digest you recorded.

## Support policy

The latest 1.x release is supported, best effort, by one maintainer (see
[SECURITY.md](../SECURITY.md)). Fixes ship as new releases, not backports.
Each 1.x release is drilled from v1.0.0 and from the previous release, and
from v1.0.0's own `compose.yaml` and `.env` with only the app image changed.
After 2.0 ships, 1.x gets critical security fixes for 6 months.

Upgrading from 0.x, or from an install older than the account ownership
migration (schema 26), follows the
[v1.4.1 upgrade guide](https://github.com/MeandrousShark/odograph/blob/v1.4.1/docs/upgrading.md#upgrading-to-10).
