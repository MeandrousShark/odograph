from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.db import make_pool
from app.detector.runner import reprocess_places
from app.main import make_templates
from app.ui import _fetch_recent_purposes, make_router
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")
TZ = timezone.utc


def _endpoint(path: str, method: str):
    for route in make_router().routes:
        if getattr(route, "path", None) == path and method in (route.methods or set()):
            return route.endpoint
    raise AssertionError(f"{method} route {path} missing")


PURPOSE = _endpoint("/trips/{trip_id}/purpose", "POST")
MANUAL = _endpoint("/trips/manual", "POST")


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
        await reset_db(pool)

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

        await PURPOSE(
            _request(pool), trip_id, purpose="  Client planning  ", user={"sub": "test"},
        )
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT purpose, notes FROM trips WHERE id=%s", (trip_id,))
            assert await cur.fetchone() == ("Client planning", None)

        await MANUAL(
            _request(pool),
            date="2026-02-01",
            start_time="10:00",
            end_time="10:30",
            distance=5.0,
            category="business",
            purpose="  Deliver documents  ",
            notes="weather note",
            vehicle_id="",
            route_mode="none",
            start_place="",
            end_place="",
            start_lat="",
            start_lon="",
            end_lat="",
            end_lon="",
            routed_distance="",
            user={"sub": "test"},
            exclusion="not_deductible",
        )
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT purpose, notes, exclusion::text FROM trips "
                "WHERE started_at='2026-02-01T10:00:00Z'"
            )
            assert await cur.fetchone() == (
                "Deliver documents", "weather note", "not_deductible"
            )

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


async def _purpose_edit_claims_human_ownership_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)

        async with pool.connection() as conn:
            # A rule-owned detected trip -- no geometry needed since there
            # are no tag_rules or places in this scenario, and the point is
            # only what happens to an already-rule-tagged trip.
            cur = await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
                " category, tag_source) "
                "VALUES ('A', 'detected', '2026-01-01T09:00:00Z', "
                "'2026-01-01T09:30:00Z', 1000, 'business', 'rule') RETURNING id"
            )
            trip_id = (await cur.fetchone())[0]

        await PURPOSE(
            _request(pool), trip_id, purpose="  Client visit  ", user={"sub": "test"},
        )
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT purpose, category::text, tag_source::text FROM trips WHERE id=%s",
                (trip_id,),
            )
            assert await cur.fetchone() == ("Client visit", "business", "human"), (
                "editing purpose must claim human ownership, not just change the text"
            )

        # No tag_rules exist, so reprocess_places would revert a still
        # rule-owned trip's category to unclassified. If tag_source is
        # 'human' as asserted above, plan_autotags must skip it entirely.
        await reprocess_places(pool)
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT category::text, tag_source::text FROM trips WHERE id=%s",
                (trip_id,),
            )
            assert await cur.fetchone() == ("business", "human"), (
                "a human-owned trip must survive reprocess_places untouched, "
                "even with no matching rule"
            )
    finally:
        await pool.close()


def test_purpose_edit_claims_human_ownership_and_survives_reprocess():
    """A rule-owned trip whose purpose the user edits must stop being
    rule-owned, or the autotagger could later revert it out from under the
    user with no rule change of their own."""
    asyncio.run(_purpose_edit_claims_human_ownership_scenario())
