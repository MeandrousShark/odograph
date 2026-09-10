"""DB-backed regression test: two processes racing run_migrations at startup
must not double-apply a migration (app/db.py's migration advisory lock).

Set TEST_DATABASE_URL only to a throwaway Postgres/PostGIS instance -- the
target database's public schema is dropped and recreated.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.db import MIGRATIONS_DIR, make_pool, run_migrations
from conftest import drop_and_recreate_schema

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)


async def _scenario() -> None:
    setup_pool = make_pool(TEST_DB)
    await setup_pool.open(wait=True)
    try:
        await drop_and_recreate_schema(setup_pool)
    finally:
        await setup_pool.close()

    pool_a = make_pool(TEST_DB)
    pool_b = make_pool(TEST_DB)
    await pool_a.open(wait=True)
    await pool_b.open(wait=True)
    try:
        await asyncio.gather(run_migrations(pool_a), run_migrations(pool_b))

        async with pool_a.connection() as conn:
            cur = await conn.execute("SELECT count(*), count(DISTINCT version) FROM schema_migrations")
            total, distinct = await cur.fetchone()
            assert total == distinct

            expected = len(list(MIGRATIONS_DIR.glob("*.sql")))
            assert total == expected
    finally:
        await pool_a.close()
        await pool_b.close()


def test_concurrent_run_migrations_applies_each_migration_exactly_once():
    asyncio.run(_scenario())
