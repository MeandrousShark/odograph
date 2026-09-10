"""DB-backed tests for tests/conftest.py's shared reset machinery.

Not testing app/ code: these prove the reset helper itself is safe (refuses
an out-of-allowlist database) and behavior-preserving (a truncate-and-restore
reproduces exactly what a real drop-and-replay produces), since every other
DB-backed test file depends on both properties silently.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.db import MIGRATIONS_DIR, make_pool
from conftest import (
    _reset_target_tables,
    _restore_seed_snapshot,
    _table_columns,
    _truncate_all,
    capture_seed_snapshot,
    drop_and_recreate_schema,
    full_schema_reset,
    reset_db,
)

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)


class _FakePool:
    """A pool double whose conninfo names a disallowed database, so the
    allowlist guard is proven without ever touching a real connection."""

    def __init__(self, database_url: str) -> None:
        self.conninfo = database_url

    def connection(self):
        raise AssertionError(
            "reset_db touched the pool before checking its database name"
        )


def test_reset_db_refuses_a_database_outside_the_allowlist():
    # mileage_devsite is the persistent QA database name a later task adds
    # on the same machine -- the exact case this guard exists to stop.
    pool = _FakePool("postgresql://mileage:testpw@127.0.0.1:5432/mileage_devsite")
    with pytest.raises(RuntimeError, match="mileage_devsite"):
        asyncio.run(reset_db(pool))


async def _leaked_schema_object_scenario() -> None:
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            await conn.execute(
                "CREATE FUNCTION _leak_probe() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RETURN NEW; END $$"
            )
            await conn.execute(
                "CREATE TRIGGER _leak_probe BEFORE INSERT ON vehicles "
                "FOR EACH ROW EXECUTE FUNCTION _leak_probe()"
            )
        try:
            with pytest.raises(RuntimeError, match="_leak_probe"):
                await reset_db(pool)
        finally:
            # Undo the leak deliberately created above -- this test proves
            # the detector, it must not itself become the next thing it
            # would have caught.
            async with pool.connection() as conn:
                await conn.execute("DROP TRIGGER _leak_probe ON vehicles")
                await conn.execute("DROP FUNCTION _leak_probe()")
    finally:
        await pool.close()


def test_reset_db_raises_when_a_schema_object_leaks():
    asyncio.run(_leaked_schema_object_scenario())


def _sorted_tables(dump):
    # Row order isn't a contract of either reset path, only content is, so
    # sort by repr (works across mixed/NULL-containing tuples, unlike a
    # direct tuple sort) before comparing.
    return {
        table: (columns, sorted(rows, key=repr))
        for table, (columns, rows) in dump.items()
    }


async def _dump_state(pool) -> dict:
    async with pool.connection() as conn:
        dump: dict[str, tuple[list[str], list[tuple]]] = {}
        for table in await _reset_target_tables(conn):
            columns = await _table_columns(conn, table)
            quoted = ", ".join(f'"{c}"' for c in columns)
            cur = await conn.execute(f'SELECT {quoted} FROM "{table}"')
            dump[table] = (columns, await cur.fetchall())
        cur = await conn.execute(
            "SELECT sequencename, last_value FROM pg_sequences "
            "WHERE schemaname = 'public' ORDER BY sequencename"
        )
        sequences = await cur.fetchall()
    return {"tables": dump, "sequences": sequences}


async def _equivalence_scenario() -> None:
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        # Ground truth: what a real, from-scratch migration replay produces.
        await full_schema_reset(pool)
        replay_dump = await _dump_state(pool)

        # What every test's actual reset produces. Capture this run's own
        # snapshot rather than reusing the session's: a `DEFAULT now()`
        # column would otherwise differ from wall-clock drift since session
        # start alone, which would be a false mismatch, not a real one.
        # Then truncate and restore it exactly as reset_db does.
        local_snapshot = await capture_seed_snapshot(pool)
        async with pool.connection() as conn:
            await _truncate_all(conn)
            await _restore_seed_snapshot(conn, local_snapshot)
        restored_dump = await _dump_state(pool)

        assert _sorted_tables(restored_dump["tables"]) == _sorted_tables(
            replay_dump["tables"]
        )
        assert restored_dump["sequences"] == replay_dump["sequences"]
    finally:
        await pool.close()


def test_truncate_and_restore_matches_a_real_drop_and_replay():
    asyncio.run(_equivalence_scenario())


async def _recovers_from_a_dirty_container_scenario() -> None:
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        # Reproduce the exact failure this guards against: a database that
        # already carries migration-created objects but no schema_migrations
        # table, e.g. a disposable container reused across sessions. A bare
        # run_migrations() would see version 0 here and die trying to
        # replay 001_init.sql over objects that already exist.
        await drop_and_recreate_schema(pool)
        async with pool.connection() as conn:
            await conn.execute((MIGRATIONS_DIR / "001_init.sql").read_text())

        # The fix: establishing canonical state means dropping first, not
        # assuming, so this must succeed regardless of what was there.
        await full_schema_reset(pool)
        expected_version = len(list(MIGRATIONS_DIR.glob("*.sql")))
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT COALESCE(max(version), 0) FROM schema_migrations"
            )
            assert (await cur.fetchone())[0] == expected_version

        # A following reset_db must also work cleanly against that
        # established state: still at the expected version, and queryable.
        await reset_db(pool)
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT COALESCE(max(version), 0) FROM schema_migrations"
            )
            assert (await cur.fetchone())[0] == expected_version
            cur = await conn.execute("SELECT name FROM vehicles")
            assert [row[0] for row in await cur.fetchall()] == ["My Car"]
    finally:
        await pool.close()


def test_reset_recovers_a_canonical_database_from_a_dirty_container():
    asyncio.run(_recovers_from_a_dirty_container_scenario())
