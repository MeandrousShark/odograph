from __future__ import annotations

import logging
import pathlib
import re

from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"

MIGRATION_FILENAME_RE = re.compile(r"^\d+_")

# These use PostgreSQL's single-bigint namespace. Its two-int namespace is
# separate: every detector/import/structural mutation must keep this same key.
DETECTOR_ADVISORY_LOCK_KEY = 0x6D696C6531  # "mile1"
TRACKING_PROVISION_LOCK_KEY = 0x6D696C6537
NUDGE_ADVISORY_LOCK_KEY = 901405
ODOMETER_REMINDER_ADVISORY_LOCK_KEY = 901406
EMAIL_DIGEST_ADVISORY_LOCK_KEY = 901407
RUN_MIGRATIONS_ADVISORY_LOCK_KEY = 901408

ROLE_SETUP_ADVISORY_LOCK_KEY = 901409

# Two-int namespace. Hash collisions only serialize unrelated email targets.
INVITATION_EMAIL_LOCK_CLASS_ID = 901410


def make_pool(database_url: str) -> AsyncConnectionPool:
    # max_size 6: up to 3 concurrent borrowers now (detector run, snap
    # worker run, an in-flight UI request), each potentially wanting more
    # than one connection briefly.
    return AsyncConnectionPool(database_url, min_size=1, max_size=6, open=False)


class MigrationRoleError(RuntimeError):
    """The migration role would be subject to forced row-level security."""


async def check_migration_role(conn) -> None:
    """Refuse a migration role that forced row-level security would filter.

    Owned tables force their account policies even on their owner, so a data
    migration run by a plain owner role would silently touch no rows.
    """
    cur = await conn.execute(
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
    )
    row = await cur.fetchone()
    if row is None or not row[0]:
        raise MigrationRoleError(
            "database migrations require a DATABASE_URL role that is a superuser "
            "or has BYPASSRLS; the configured role is neither"
        )


async def _fetch_schema_version(conn) -> int:
    cur = await conn.execute("SELECT COALESCE(max(version), 0) FROM schema_migrations")
    row = await cur.fetchone()
    return row[0]


async def run_migrations(pool: AsyncConnectionPool, config=None) -> None:
    paths = sorted(MIGRATIONS_DIR.glob("*.sql"))
    for path in paths:
        if not MIGRATION_FILENAME_RE.match(path.name):
            raise ValueError(
                f"migration filename {path.name!r} does not start with a "
                "numeric NNN_ prefix"
            )

    async with pool.connection() as conn:
        await check_migration_role(conn)
        # Transaction-scoped, not session-scoped `pg_advisory_lock`: it
        # releases automatically on commit *or* rollback, so a migration
        # that fails mid-loop can never leave the lock held and wedge every
        # later process's startup (same reasoning as app/detector/runner.py's
        # detector lock docstring). Blocking, not try-lock, because two
        # app processes starting simultaneously must both come up correctly:
        # the second blocks here until the first's transaction commits, then
        # finds every migration already recorded in schema_migrations below
        # and applies none, rather than racing the version check and
        # double-applying one.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(%s)", (RUN_MIGRATIONS_ADVISORY_LOCK_KEY,)
        )
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version int PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        cur = await conn.execute("SELECT version FROM schema_migrations")
        applied = {row[0] for row in await cur.fetchall()}
        for path in paths:
            version = int(path.name.split("_", 1)[0])
            if version in applied:
                continue
            log.info("applying migration %s", path.name)
            if version == 26:
                from app.ownership import preflight_ownership
                await preflight_ownership(conn)
            await conn.execute(path.read_text())
            if version == 26:
                from app.ownership import import_legacy_configuration
                await import_legacy_configuration(conn, config)
            await conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
            )
