"""DB-backed tests that a failing reprocess rolls back the places/rules CRUD
mutation it's paired with -- the same atomicity guarantee
`delete_boundary_override` and `_merge_trips_core` already have, now extended
to create_place/update_place/delete_place/create_rule/delete_rule by sharing
one connection (`reprocess_places_in`) instead of committing the mutation
and reprocessing separately.

The target database is destroyed and recreated. Set TEST_DATABASE_URL only to
a throwaway Postgres/PostGIS instance.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.db import make_pool
from app.ui import make_router
import app.ui.places as ui_module
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

USER = {"sub": "test"}


def _endpoint(path: str):
    for route in make_router().routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"route {path} missing")


def _request(pool):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(pool=pool)), headers={},
    )


class _ForcedReprocessFailure:
    """Context manager that makes app.ui.places's reprocess_places_in raise,
    then restores the original on exit -- so a failure in one scenario can't
    leak into the next."""

    def __enter__(self):
        self._original = ui_module.reprocess_places_in

        async def _broken(conn):
            raise RuntimeError("forced reprocess failure")

        ui_module.reprocess_places_in = _broken
        return self

    def __exit__(self, *exc_info):
        ui_module.reprocess_places_in = self._original


async def _create_place_rollback_scenario() -> None:
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        create = _endpoint("/places")
        with _ForcedReprocessFailure():
            with pytest.raises(RuntimeError, match="forced reprocess failure"):
                await create(
                    _request(pool), name="Home", kind="home", lat=47.6, lon=-122.3,
                    radius_m=150.0, user=USER,
                )
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM places")
            (count,) = await cur.fetchone()
        assert count == 0, "a failed reprocess must roll back the place insert too"
    finally:
        await pool.close()


def test_create_place_rolls_back_when_reprocess_fails():
    asyncio.run(_create_place_rollback_scenario())


async def _delete_place_rollback_scenario() -> None:
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        create = _endpoint("/places")
        delete = _endpoint("/places/{place_id}/delete")
        await create(
            _request(pool), name="Home", kind="home", lat=47.6, lon=-122.3,
            radius_m=150.0, user=USER,
        )
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT id FROM places WHERE name = 'Home'")
            (place_id,) = await cur.fetchone()

        with _ForcedReprocessFailure():
            with pytest.raises(RuntimeError, match="forced reprocess failure"):
                await delete(_request(pool), place_id, USER)

        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM places WHERE id = %s", (place_id,))
            (count,) = await cur.fetchone()
        assert count == 1, "a failed reprocess must roll back the place delete too"
    finally:
        await pool.close()


def test_delete_place_rolls_back_when_reprocess_fails():
    asyncio.run(_delete_place_rollback_scenario())


async def _create_rule_rollback_scenario() -> None:
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        # migrations/003_places.sql seeds two default rules, so the baseline
        # is 2, not 0 -- compare against that baseline rather than assuming
        # an empty table.
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM tag_rules")
            (before_count,) = await cur.fetchone()

        create_rule = _endpoint("/rules")
        with _ForcedReprocessFailure():
            with pytest.raises(RuntimeError, match="forced reprocess failure"):
                await create_rule(
                    _request(pool), a_mode="kind", a_kind="other", a_place="",
                    b_mode="kind", b_kind="other", b_place="",
                    category="business", user=USER,
                )
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM tag_rules")
            (after_count,) = await cur.fetchone()
        assert after_count == before_count, (
            "a failed reprocess must roll back the rule insert too"
        )
    finally:
        await pool.close()


def test_create_rule_rolls_back_when_reprocess_fails():
    asyncio.run(_create_rule_rollback_scenario())


async def _create_rule_rejects_a_deleted_place_scenario() -> None:
    """Not a rollback scenario -- reuses this file's harness to cover a real
    dropdown-staleness race: a place deleted between page-load and form
    submit must surface as a 400 (errors.ForeignKeyViolation on the INSERT),
    not an uncaught 500.
    """
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        create = _endpoint("/places")
        delete = _endpoint("/places/{place_id}/delete")
        await create(
            _request(pool), name="Stale", kind="other", lat=47.6, lon=-122.3,
            radius_m=150.0, user=USER,
        )
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT id FROM places WHERE name = 'Stale'")
            (place_id,) = await cur.fetchone()
        await delete(_request(pool), place_id, USER)

        create_rule = _endpoint("/rules")
        with pytest.raises(HTTPException) as exc:
            await create_rule(
                _request(pool), a_mode="place", a_kind="", a_place=str(place_id),
                b_mode="kind", b_kind="other", b_place="",
                category="business", user=USER,
            )
        assert exc.value.status_code == 400
    finally:
        await pool.close()


def test_create_rule_rejects_a_deleted_place():
    asyncio.run(_create_rule_rejects_a_deleted_place_scenario())


async def _delete_rule_rollback_scenario() -> None:
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        create_rule = _endpoint("/rules")
        delete_rule = _endpoint("/rules/{rule_id}/delete")
        # a_kind/b_kind='other' distinguishes this from migrations/003_places.sql's
        # two seeded rules (home/work and work/work), so the id lookup below is
        # unambiguous.
        await create_rule(
            _request(pool), a_mode="kind", a_kind="other", a_place="",
            b_mode="kind", b_kind="other", b_place="",
            category="business", user=USER,
        )
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM tag_rules WHERE a_kind = 'other' AND b_kind = 'other'"
            )
            (rule_id,) = await cur.fetchone()

        with _ForcedReprocessFailure():
            with pytest.raises(RuntimeError, match="forced reprocess failure"):
                await delete_rule(_request(pool), rule_id, USER)

        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM tag_rules WHERE id = %s", (rule_id,))
            (count,) = await cur.fetchone()
        assert count == 1, "a failed reprocess must roll back the rule delete too"
    finally:
        await pool.close()


def test_delete_rule_rolls_back_when_reprocess_fails():
    asyncio.run(_delete_rule_rollback_scenario())


async def _place_and_trip_tags_stay_consistent_scenario() -> None:
    """The end-to-end acceptance case: a config mutation plus its reprocess
    are one unit, so a failure leaves both the config table and any trip
    tags reprocess would have touched exactly as they were -- not a
    committed place with trips still reflecting the old configuration, nor
    (worse) a rolled-back place with trips that got reprocessed anyway."""
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
                " category, tag_source) "
                "VALUES ('A', 'detected', '2026-01-01T09:00:00Z', "
                "'2026-01-01T09:30:00Z', 1000, 'personal', 'rule') RETURNING id"
            )
            trip_id = (await cur.fetchone())[0]

        create = _endpoint("/places")
        with _ForcedReprocessFailure():
            with pytest.raises(RuntimeError, match="forced reprocess failure"):
                await create(
                    _request(pool), name="Home", kind="home", lat=47.6, lon=-122.3,
                    radius_m=150.0, user=USER,
                )

        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM places")
            (place_count,) = await cur.fetchone()
            cur = await conn.execute(
                "SELECT category::text, tag_source::text FROM trips WHERE id = %s", (trip_id,)
            )
            trip_row = await cur.fetchone()
        assert place_count == 0
        assert trip_row == ("personal", "rule"), (
            "config and trip tags must remain consistent: no orphaned place, "
            "no reprocessed trip"
        )
    finally:
        await pool.close()


def test_failed_reprocess_leaves_config_and_trip_tags_consistent():
    asyncio.run(_place_and_trip_tags_stay_consistent_scenario())
