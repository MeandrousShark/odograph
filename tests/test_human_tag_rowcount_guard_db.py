"""DB-backed tests for the `_apply_human_tag` rowcount guard.

A detector reconcile pass deletes the trip row it is replacing (then inserts
a fresh row under a new id). If a human-tag UPDATE is mid-flight against the
same row, it can block on that DELETE's row lock and, once the DELETE
commits, complete with rowcount 0 and no error -- silently dropping the
user's classification while still reporting success. These tests hold a
DELETE open on a second connection to reproduce that exact interleaving
(same pattern as the detector-lock scenario in tests/test_runner_db.py) and
confirm the guard turns the race into a 404 instead of a false success, for
both `_apply_human_tag` branches and both HTTP callers.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import psycopg
from fastapi import HTTPException

from app.db import make_pool
from app.main import make_templates
from app.ui import _apply_human_tag, make_router
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

TZ = timezone.utc
BASE = datetime(2026, 1, 1, 9, tzinfo=TZ)


def _endpoint(path: str, method: str):
    for route in make_router().routes:
        if getattr(route, "path", None) == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"route missing: {method} {path}")


TAG = _endpoint("/trips/{trip_id}/tag", "POST")
REVIEW_TAG = _endpoint("/review/{trip_id}/tag", "POST")


def _request(pool):
    config = SimpleNamespace(display_tz=TZ, app_version="test")
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
        )),
        session={"csrf": "test"},
        headers={},
    )


async def _insert_trip(conn, started_at: datetime) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m, category) "
        "VALUES ('phone', 'manual', %s, %s, 1000, 'unclassified') RETURNING id",
        (started_at, started_at + timedelta(minutes=15)),
    )
    return (await cur.fetchone())[0]


async def _apply_human_tag_guard_scenario():
    """Direct, non-concurrent proof of the chokepoint itself: once the row
    is gone, both `_apply_human_tag` branches must 404 rather than silently
    return. Without the rowcount guard this raises nothing at all.
    """
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            trip_id = await _insert_trip(conn, BASE)
            await conn.execute("DELETE FROM trips WHERE id = %s", (trip_id,))

        for update_purpose in (False, True):
            async with pool.connection() as conn:
                with pytest.raises(HTTPException) as exc:
                    await _apply_human_tag(
                        conn, trip_id, "business", "a purpose",
                        update_purpose=update_purpose,
                    )
                assert exc.value.status_code == 404
                assert exc.value.detail == "No such trip"
    finally:
        await pool.close()


def test_apply_human_tag_guards_both_branches_against_vanished_row():
    asyncio.run(_apply_human_tag_guard_scenario())


async def _run_blocked_and_release(pool, trip_id, coro_factory):
    """Hold an uncommitted DELETE of `trip_id` on a second connection,
    start the human-tag call, confirm it genuinely blocks on the row lock
    (not just races past a plain SELECT), then commit the DELETE so the
    blocked UPDATE resumes and finds zero matching rows.
    """
    holder = await psycopg.AsyncConnection.connect(TEST_DB)
    try:
        await holder.execute("DELETE FROM trips WHERE id = %s", (trip_id,))

        task = asyncio.create_task(coro_factory())
        await asyncio.sleep(0.5)
        assert not task.done(), (
            "the human-tag UPDATE should block on the detector's uncommitted "
            "DELETE, not race past it and see the row already gone"
        )

        await holder.commit()
        return task
    finally:
        await holder.close()


async def _review_tag_race_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            trip_id = await _insert_trip(conn, BASE)

        request = _request(pool)
        task = await _run_blocked_and_release(
            pool, trip_id,
            lambda: REVIEW_TAG(
                request, trip_id, "business", "", "", "", "", {"sub": "test"}, q="",
            ),
        )
        with pytest.raises(HTTPException) as exc:
            await asyncio.wait_for(task, timeout=5)
        assert exc.value.status_code == 404
        assert exc.value.detail == "No such trip"
    finally:
        await pool.close()


def test_review_tag_trip_returns_404_not_silent_success_under_delete_race():
    """review_tag_trip's downstream card fetch does not re-check the tagged
    trip's existence, so before the guard this exact race returned 200 with
    the tag silently dropped. This is the update_purpose=True branch.
    """
    asyncio.run(_review_tag_race_scenario())


async def _tag_trip_race_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            trip_id = await _insert_trip(conn, BASE)

        request = _request(pool)
        task = await _run_blocked_and_release(
            pool, trip_id,
            lambda: TAG(request, trip_id, "business", {"sub": "test"}),
        )
        with pytest.raises(HTTPException) as exc:
            await asyncio.wait_for(task, timeout=5)
        assert exc.value.status_code == 404
    finally:
        await pool.close()


def test_tag_trip_returns_404_not_silent_success_under_delete_race():
    """The plain (list-view) branch of the same race, update_purpose=False."""
    asyncio.run(_tag_trip_race_scenario())


async def _happy_path_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            tag_trip_id = await _insert_trip(conn, BASE)
            review_trip_id = await _insert_trip(conn, BASE + timedelta(hours=1))

        request = _request(pool)
        tag_response = await TAG(request, tag_trip_id, "business", {"sub": "test"})
        assert tag_response.status_code == 200
        assert f'<article id="trip-{tag_trip_id}"' in tag_response.body.decode()

        review_response = await REVIEW_TAG(
            request, review_trip_id, "personal", "", "", "", "", {"sub": "test"}, q="",
        )
        assert review_response.status_code == 200

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT category::text, tag_source::text FROM trips WHERE id = %s",
                (tag_trip_id,),
            )
            assert await cur.fetchone() == ("business", "human")
            cur = await conn.execute(
                "SELECT category::text, tag_source::text FROM trips WHERE id = %s",
                (review_trip_id,),
            )
            assert await cur.fetchone() == ("personal", "human")
    finally:
        await pool.close()


def test_tag_and_review_tag_still_succeed_on_a_live_trip():
    asyncio.run(_happy_path_scenario())
