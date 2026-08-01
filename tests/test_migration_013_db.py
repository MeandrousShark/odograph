from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app.db import make_pool, run_migrations

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)
MIGRATION = Path(__file__).parents[1] / "migrations/013_trim_cached_us_country.sql"


async def _scenario() -> None:
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await run_migrations(pool)

        async with pool.connection() as conn:
            await conn.execute("DELETE FROM schema_migrations WHERE version = 13")
            rows = [
                (47.1, -122.1, "123 Main St, Seattle, WA, United States of America"),
                (47.2, -122.2, "123 Rue de Rivoli, Paris, France"),
                (47.3, -122.3, "United States of America"),
                (47.4, -122.4, "Similar, United States of America "),
                (47.5, -122.5, None),
            ]
            for row in rows:
                await conn.execute(
                    "INSERT INTO geocode_cache (lat, lon, address) VALUES (%s, %s, %s)",
                    row,
                )

        await run_migrations(pool)

        async with pool.connection() as conn:
            version = await conn.execute(
                "SELECT count(*) FROM schema_migrations WHERE version = 13"
            )
            assert await version.fetchone() == (1,)

            # A second execution is harmless by effect. The migration runner
            # itself records versions, but this guards its data statement too.
            await conn.execute(MIGRATION.read_text())
            result = await conn.execute(
                "SELECT address FROM geocode_cache ORDER BY lat"
            )
            assert await result.fetchall() == [
                ("123 Main St, Seattle, WA",),
                ("123 Rue de Rivoli, Paris, France",),
                ("United States of America",),
                ("Similar, United States of America ",),
                (None,),
            ]
    finally:
        await pool.close()


def test_migration_013_trims_only_exact_trailing_us_country_suffix():
    asyncio.run(_scenario())
