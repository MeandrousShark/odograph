from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.db import make_pool, run_migrations
from app.main import make_templates
from app.ui import _fetch_recent_purposes, make_router

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")
TZ = timezone.utc


def _endpoint(path: str):
    for route in make_router().routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"route {path} missing")


PURPOSE = _endpoint("/trips/{trip_id}/purpose")
MANUAL = _endpoint("/trips/manual")


def _request(pool):
    config = SimpleNamespace(display_tz=TZ, app_version="test")
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
        )),
        session={"csrf": "test"},
    )


async def _scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await run_migrations(pool)

        async with pool.connection() as conn:
            migration = await conn.execute(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='trips' AND column_name='purpose'"
            )
            assert await migration.fetchone() == ("text", "YES")
            cur = await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m) "
                "VALUES ('manual', 'manual', '2026-01-01T09:00:00Z', "
                "'2026-01-01T09:30:00Z', 1000) RETURNING id"
            )
            trip_id = (await cur.fetchone())[0]

        await PURPOSE(_request(pool), trip_id, "  Client planning  ", {"sub": "test"})
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT purpose, notes FROM trips WHERE id=%s", (trip_id,))
            assert await cur.fetchone() == ("Client planning", None)

        await MANUAL(
            _request(pool), "2026-02-01", "10:00", "10:30", 5.0, "business",
            "  Deliver documents  ", "weather note", "", {"sub": "test"},
        )
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT purpose, notes FROM trips WHERE started_at='2026-02-01T10:00:00Z'"
            )
            assert await cur.fetchone() == ("Deliver documents", "weather note")

            await conn.execute(
                "UPDATE trips SET purpose='Client planning', updated_at='2026-01-01T00:00:00Z' "
                "WHERE id=%s", (trip_id,),
            )
            await conn.execute(
                "UPDATE trips SET updated_at='2026-02-02T00:00:00Z' "
                "WHERE purpose='Deliver documents'"
            )
            await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m, purpose, updated_at) "
                "VALUES ('manual', 'manual', '2026-03-01T10:00:00Z', '2026-03-01T10:30:00Z', "
                "1000, 'Client planning', '2026-03-02T00:00:00Z')"
            )
            recent = await _fetch_recent_purposes(conn)
            assert recent == ["Client planning", "Deliver documents"]
    finally:
        await pool.close()


def test_purpose_migration_crud_manual_entry_and_recent_reuse():
    asyncio.run(_scenario())
