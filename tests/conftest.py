"""Shared fixtures for tests/*_db.py.

Every DB-backed test opens its own psycopg pool inside its own
``asyncio.run()`` (see any ``tests/test_*_db.py``'s ``_scenario`` helper): a
psycopg async pool is bound to the event loop that opened it, and
``asyncio.run()`` creates a fresh loop on every call. That rules out a single
session-scoped *pool* shared across tests without either rewriting every test
onto one shared loop or adding pytest-asyncio, and neither is in scope here.
What this module shares instead is the one-time migration work and the reset
between tests.

Import this module by its bare name, ``from conftest import ...``, not as
``tests.conftest``. pytest collects ``tests/conftest.py`` as the top-level
module ``conftest`` (``tests/`` has no ``__init__.py``), so a dotted import
creates a second, independent module object whose module-level state (the
seed snapshot and schema-object baseline below) was never populated by the
session fixture.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import psycopg
from psycopg import sql
import pytest

from app.db import make_pool, run_migrations
from app.account_context import AccountPool, AccountPrincipal, account_id

TEST_DB = os.environ.get("TEST_DATABASE_URL")

# A persistent QA database, `mileage_devsite`, lives on the same machine and
# holds hand-built data that is expensive to recreate. A stray
# TEST_DATABASE_URL must never let a reset below truncate it, so the allowed
# name is spelled out rather than inferred from convention.
_ALLOWED_DATABASE_NAMES = {"mileage"}

_RESET_EXCLUDED_TABLES = {"spatial_ref_sys", "schema_migrations"}

# {table: (columns, rows)} for every table a fresh migration run leaves
# non-empty, captured once per session immediately after that run (see
# `_migrated_schema` below) and restored after every per-test truncate.
# Fresh migrations currently seed detector_state, mileage_rates, tag_rules,
# vehicles, and app_settings, but this dict is populated from the catalog
# rather than hardcoded, so a later migration's seed data is picked up
# automatically.
class SeedSnapshot(dict):
    """Rows and sequence state, including empty tables whose seeds were retired."""

    def __init__(self):
        super().__init__()
        self.sequences = {}


_seed_snapshot = SeedSnapshot()

# Every function/trigger name attached to something in schema public right
# after the session's one migration run. TRUNCATE removes rows, not schema
# objects, so a test that creates a trigger or function and does not drop
# it again would otherwise leak into every later test in the session. This
# is a checked invariant, not a cleanup mechanism: reset_db() raises if the
# current set has grown past this baseline, so the leak fails loudly in the
# test that caused it rather than as an unrelated failure elsewhere.
_schema_object_baseline: set[str] = set()


def _database_name(pool) -> str | None:
    return psycopg.conninfo.conninfo_to_dict(pool.conninfo).get("dbname")


def _check_allowed_database(pool) -> None:
    name = _database_name(pool)
    if name not in _ALLOWED_DATABASE_NAMES:
        raise RuntimeError(
            f"refusing to reset database {name!r}: not in the test allowlist "
            f"{sorted(_ALLOWED_DATABASE_NAMES)} (see tests/conftest.py)"
        )


async def _public_tables(conn) -> list[str]:
    cur = await conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    return [row[0] for row in await cur.fetchall()]


async def _table_columns(conn, table: str) -> list[str]:
    cur = await conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    )
    return [row[0] for row in await cur.fetchall()]


async def _reset_target_tables(conn) -> list[str]:
    tables = await _public_tables(conn)
    return [t for t in tables if t not in _RESET_EXCLUDED_TABLES]


async def _truncate_all(conn) -> None:
    tables = await _reset_target_tables(conn)
    if not tables:
        return
    quoted = ", ".join(f'"{t}"' for t in tables)
    await conn.execute(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE")


async def _schema_object_names(conn) -> set[str]:
    """Every function and non-internal trigger name in schema public.

    `tgisinternal` triggers are Postgres's own foreign-key enforcement,
    created and dropped alongside the constraint itself, never by a test
    directly, so they are excluded to keep the baseline focused on the
    ad hoc objects a test might create.
    """
    cur = await conn.execute(
        "SELECT p.proname FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public'"
    )
    functions = {f"function:{row[0]}" for row in await cur.fetchall()}
    cur = await conn.execute(
        "SELECT t.tgname FROM pg_trigger t "
        "JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND NOT t.tgisinternal"
    )
    triggers = {f"trigger:{row[0]}" for row in await cur.fetchall()}
    return functions | triggers


async def _check_no_leaked_schema_objects(conn) -> None:
    leaked = sorted(await _schema_object_names(conn) - _schema_object_baseline)
    if leaked:
        raise RuntimeError(
            f"schema objects created since the session baseline were never "
            f"dropped: {leaked}. reset_db() only truncates data; a test that "
            f"creates a trigger or function must drop it before finishing "
            f"(see tests/test_trip_delete_db.py's _delete_rollback_scenario "
            f"for the pattern)."
        )


async def capture_seed_snapshot(pool) -> dict[str, tuple[list[str], list[tuple]]]:
    """Return every public table's full contents, keyed by table name, for
    every table that currently has at least one row.

    Called once per session right after the one migration run. Also called
    directly by the equivalence test in tests/test_conftest_db.py, which
    needs its own independently-timed snapshot (values like a `DEFAULT
    now()` column would otherwise differ from the session's) rather than
    reusing `_seed_snapshot`.
    """
    snapshot = SeedSnapshot()
    async with pool.connection() as conn:
        for table in await _reset_target_tables(conn):
            columns = await _table_columns(conn, table)
            quoted_columns = ", ".join(f'"{c}"' for c in columns)
            cur = await conn.execute(f'SELECT {quoted_columns} FROM "{table}"')
            rows = await cur.fetchall()
            if rows:
                snapshot[table] = (columns, rows)
        cur = await conn.execute("SELECT sequencename FROM pg_sequences WHERE schemaname='public'")
        for (sequence,) in await cur.fetchall():
            cur = await conn.execute(sql.SQL("SELECT last_value,is_called FROM {}").format(sql.Identifier("public", sequence)))
            snapshot.sequences[sequence] = await cur.fetchone()
    return snapshot


async def _restore_seed_snapshot(
    conn, snapshot: dict[str, tuple[list[str], list[tuple]]]
) -> None:
    for table, (columns, rows) in snapshot.items():
        quoted_columns = ", ".join(f'"{c}"' for c in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        # OVERRIDING SYSTEM VALUE: a captured identity column's id must be
        # reinserted verbatim (RESTART IDENTITY reset its sequence back to
        # the start), not replaced with a freshly generated one. It is a
        # harmless no-op on the tables here with no identity column at all.
        async with conn.cursor() as cur:
            await cur.executemany(
                f'INSERT INTO "{table}" ({quoted_columns}) OVERRIDING SYSTEM VALUE '
                f'VALUES ({placeholders})',
                rows,
            )
        for column in columns:
            cur = await conn.execute(
                "SELECT pg_get_serial_sequence(%s, %s)", (table, column)
            )
            sequence = (await cur.fetchone())[0]
            if sequence is None:
                continue
            # Same fix migrations/020_accounts.sql applies after its own
            # explicit-id insert: restoring ids via OVERRIDING SYSTEM VALUE
            # does not advance the sequence, so the next app-generated id
            # would collide with a restored one without this.
            await conn.execute(
                f'SELECT setval(%s, COALESCE((SELECT max("{column}") FROM "{table}"), 1), '
                f'EXISTS (SELECT 1 FROM "{table}"))',
                (sequence,),
            )
    for sequence, (value, called) in getattr(snapshot, "sequences", {}).items():
        await conn.execute("SELECT setval(%s,%s,%s)", ("public." + sequence, value, called))


async def reset_db(pool) -> None:
    """Truncate every table in schema public except spatial_ref_sys and
    schema_migrations, then restore the rows a fresh migration run seeds.

    Replaces every test file's old private `_reset_schema`/`_reset` helper
    (drop schema, replay all migrations). Callable directly from a test's
    own asyncio.run(), not only as first-thing setup: a handful of tests
    reset mid-scenario too (importing over an existing dataset, for
    instance).
    """
    _check_allowed_database(pool)
    async with pool.connection() as conn:
        functions = await _schema_object_names(conn)
    # Migration-machinery tests deliberately replay SQL without role setup.
    # Restore the complete test schema before ordinary application fixtures.
    required = {"function:bootstrap_first_account", "function:assert_account_active",
                "function:assert_tracking_credential"}
    if not required <= functions:
        await full_schema_reset(pool)
    async with pool.connection() as conn:
        await _check_no_leaked_schema_objects(conn)
        await _truncate_all(conn)
        await _restore_seed_snapshot(conn, _seed_snapshot)


async def drop_and_recreate_schema(pool) -> None:
    """Bare DROP SCHEMA/CREATE SCHEMA, with no migration replay.

    Used only by the migration-machinery tests (tests/test_migration_013_db.py,
    tests/test_migration_014_db.py, tests/test_migrations_concurrency_db.py,
    and tests/test_accounts_db.py's partial-replay scenario), which are
    testing the migration runner itself or a specific migration's
    data-preserving behavior and genuinely need a real drop, not the
    truncate-and-restore reset above.
    """
    _check_allowed_database(pool)
    async with pool.connection() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS odograph_service CASCADE; DROP SCHEMA public CASCADE; CREATE SCHEMA public;")


async def full_schema_reset(pool) -> None:
    """Drop, recreate, and replay every migration from scratch.

    Same migration-machinery-only callers as drop_and_recreate_schema above.
    """
    await drop_and_recreate_schema(pool)
    await run_migrations(pool)
    async with pool.connection() as conn:
        for filename in ("account_bootstrap.sql", "account_admission.sql", "tracking_admission.sql"):
            await conn.execute((Path(__file__).resolve().parents[1] / "scripts" / "sql" / filename).read_text())


async def bootstrap_test_account(pool, *, owner_id: int = 41, email="development@localhost.invalid") -> AccountPool:
    """Create a real owner explicitly; no production default owner is added."""
    from app.accounts import create_admin
    from app.local_auth import hash_password
    async with pool.connection() as conn:
        await conn.execute("SELECT setval(pg_get_serial_sequence('accounts','id'), %s, false)", (owner_id,))
        # Existing personal fixtures name their default vehicle explicitly as 1.
        await conn.execute("SELECT setval(pg_get_serial_sequence('vehicles','id'), 1, false)")
        account = await create_admin(conn, email, hash_password("test-password"))
    return AccountPool(pool, AccountPrincipal(account["id"], account["is_enabled"], account["auth_version"]))


async def reset_account_db(pool, **kwargs) -> AccountPool:
    await reset_db(pool)
    return await bootstrap_test_account(pool, **kwargs)


async def seed_tracking_device(conn, label="phone", *, device_id=None) -> int:
    """Fixture writer with explicit ownership and stable stream identity."""
    owner = account_id(conn)
    if device_id is None:
        cur = await conn.execute("INSERT INTO tracking_devices(account_id,label) VALUES(%s,%s) RETURNING id", (owner, label))
    else:
        cur = await conn.execute("INSERT INTO tracking_devices(account_id,label,id) VALUES(%s,%s,%s) RETURNING id", (owner, label, device_id))
    stream = (await cur.fetchone())[0]
    await conn.execute("INSERT INTO detector_state(account_id,tracking_device_id) VALUES(%s,%s)", (owner, stream))
    return stream


# Every collected case must carry exactly one of these three, so a coder
# session can select a cheap subset instead of the full suite (enforced by
# tests/test_tier_markers.py's
# test_every_collected_case_has_exactly_one_tier_marker).
_TIER_MARKER_NAMES = {"unit", "ops", "db"}

# Files whose cases shell out to a script/binary, or read repo configuration
# (workflow YAML, compose, the Dockerfile, dependency lock files, or
# cross-file path references) as the thing under test, rather than
# exercising app logic directly. Kept as an explicit list rather than a
# second filename convention: unlike the "_db" suffix, "shells out or reads
# repo metadata" has no single reliable name pattern to key off.
_OPS_MODULE_STEMS = {
    "test_archive_client_js",
    "test_backup_restore_scripts",
    "test_ci_workflow",
    "test_compose_config",
    "test_dependency_locks",
    "test_deployment_contract",
    "test_devsite_script",
    "test_generate_env_script",
    "test_no_dashes",
    "test_provision_osrm_script",
    "test_public_docs",
    "test_public_tree",
    "test_release_contract",
    "test_release_notes",
    "test_release_preflight",
    "test_release_workflow",
    "test_security_workflow",
    "test_stale_module_references",
    "test_test_db_script",
    "test_upgrade_check_script",
    "test_version_identity",
}


def pytest_collection_modifyitems(items: list) -> None:
    """Give every collected case exactly one tier marker: unit, ops, or db.

    The "_db" filename suffix is a reliable signal for most DB-backed cases,
    and an explicit module list covers the rest of what shells out or reads
    repo metadata; everything left over is unit. Both signals are wrong for
    a few cases -- test_event_loop_offload.py, and three cases within
    test_worker_lifecycle.py, need TEST_DATABASE_URL despite not matching
    either convention -- so those declare pytest.mark.db directly, and a
    case that already carries a tier marker of its own is left alone here
    rather than overridden.

    Must run before pytest's own -m/-k selection, which is also implemented
    as a pytest_collection_modifyitems hook: a marker only affects -m
    filtering if it exists by the time that hook runs. conftest.py hooks
    run ahead of pytest's builtin ones by default, but this is exercised
    directly (pytest -m unit/-m ops/-m db each selecting the right count)
    rather than relied on implicitly.
    """
    for item in items:
        if _TIER_MARKER_NAMES & {mark.name for mark in item.iter_markers()}:
            continue
        stem = item.path.stem
        if stem.endswith("_db"):
            item.add_marker(pytest.mark.db)
        elif stem in _OPS_MODULE_STEMS:
            item.add_marker(pytest.mark.ops)
        else:
            item.add_marker(pytest.mark.unit)


@pytest.fixture(scope="session", autouse=True)
def _migrated_schema() -> None:
    """Establish canonical schema state once for the whole test session, and
    capture both the seed snapshot every per-test reset_db() restores and
    the schema-object baseline it checks against.

    Drops and replays rather than assuming the target database is already
    clean or already at the current version: a reused disposable container,
    or one shared across runs, may carry migration-created objects without a
    schema_migrations table, which would make a bare run_migrations() see
    version 0, replay 001_init.sql, and die on a duplicate-object error. It
    would also mean capturing whatever that container's last session left
    behind and restoring it every reset as if it were seed data. One
    drop-and-replay per session is negligible next to the per-test saving,
    and it makes the session's starting state independent of container
    history.

    Runs in its own throwaway event loop, matching every *_db.py test's own
    asyncio.run() convention: psycopg's async pool is bound to the loop
    that opened it, so this cannot share a loop, or a pool, with the tests
    that follow.
    """
    if not TEST_DB:
        return

    async def _run() -> None:
        pool = make_pool(TEST_DB)
        await pool.open(wait=True)
        try:
            await full_schema_reset(pool)
            snapshot = await capture_seed_snapshot(pool)
            _seed_snapshot.update(snapshot)
            _seed_snapshot.sequences.update(snapshot.sequences)
            async with pool.connection() as conn:
                _schema_object_baseline.update(await _schema_object_names(conn))
        finally:
            await pool.close()

    asyncio.run(_run())
