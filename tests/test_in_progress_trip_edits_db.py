"""Delete, merge and split refuse a trip whose end the detector has not settled."""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.account_context import account_id
from app.detector.core import Params
from app.detector.runner import DetectorRunner
from app.ui._common import IN_PROGRESS_DETAIL
from conftest import reset_account_db, seed_tracking_device
from personal_support import personal_request
from tests.synth import Drive, Stationary, build_track
from tests.test_detector_arrival_db import _insert, _trips
from tests.test_detector_turn_limit_db import _run
from tests.test_trip_delete_db import _endpoint

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL for disposable DB tests")

USER = {"sub": "test"}


class _Snap:
    def poke(self):
        pass


def _request(pool, runner):
    return personal_request(SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, detector_runner=runner, snap_worker=_Snap(),
            config=SimpleNamespace(detector_params=Params()),
        )),
        headers={},
    ))


async def _two_trips(raw):
    """A settled trip, then a trip ending two minutes into a visit."""
    pool = await reset_account_db(raw)
    pts = build_track([
        Stationary(1200), Drive(km=5.0), Stationary(1200), Drive(km=3.0), Stationary(120),
    ])
    pts = pts[:-1] + [replace(pts[-1], trigger="v")]
    async with pool.connection() as conn:
        device = await seed_tracking_device(conn, "phone")
        await _insert(conn, device, pts)
    runner = DetectorRunner(pool, Params())
    assert await runner.run_once()
    async with pool.connection() as conn:
        settled, in_progress = [row[0] for row in await _trips(conn)]
    return pool, runner, device, pts, settled, in_progress


async def _refused(call):
    with pytest.raises(HTTPException) as refused:
        await call
    assert (refused.value.status_code, refused.value.detail) == (409, IN_PROGRESS_DETAIL)


async def _interior_point(pool, trip_id):
    async with pool.connection() as conn:
        return (await (await conn.execute(
            "SELECT p.id FROM points p JOIN trips t ON t.id = p.trip_id WHERE t.id = %s "
            "AND p.recorded_at > t.started_at AND p.recorded_at < t.ended_at "
            "ORDER BY p.recorded_at OFFSET 5 LIMIT 1", (trip_id,))).fetchone())[0]


def test_delete_merge_and_split_refuse_an_in_progress_trip():
    async def body(raw):
        pool, runner, _device, _pts, settled, in_progress = await _two_trips(raw)
        request = _request(pool, runner)
        point_id = await _interior_point(pool, in_progress)

        await _refused(_endpoint("/trips/{trip_id}/delete")(request, in_progress, USER))
        await _refused(_endpoint("/trips/batch_delete")(request, [settled, in_progress], USER))
        await _refused(_endpoint("/trips/{trip_id}/merge_prev")(
            request, in_progress, "keep", "", "", "keep", USER))
        await _refused(_endpoint("/trips/{trip_id}/merge_next")(
            request, settled, "keep", "", "", "keep", USER))
        await _refused(_endpoint("/trips/merge_selected")(
            request, [settled, in_progress], "keep", "", "", "keep", USER))
        await _refused(_endpoint("/trips/{trip_id}/split")(request, in_progress, point_id, USER))

        async with pool.connection() as conn:
            assert [row[0] for row in await _trips(conn)] == [settled, in_progress]
            overrides = (await (await conn.execute(
                "SELECT count(*) FROM trip_boundary_overrides WHERE account_id=%s",
                (account_id(conn),))).fetchone())[0]
        assert overrides == 0
    _run(body)


def test_a_settled_trip_before_it_can_still_be_deleted():
    async def body(raw):
        pool, runner, _device, _pts, settled, in_progress = await _two_trips(raw)
        response = await _endpoint("/trips/{trip_id}/delete")(_request(pool, runner), settled, USER)
        assert response.status_code == 204
        async with pool.connection() as conn:
            assert [row[0] for row in await _trips(conn)] == [in_progress]
    _run(body)


def test_an_older_trip_without_a_stay_row_is_not_in_progress():
    """Only the device's latest trip can be in progress."""
    async def body(raw):
        pool, runner, device, _pts, settled, _in_progress = await _two_trips(raw)
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM stays s USING trips t WHERE t.id=%s AND s.account_id=t.account_id "
                "AND s.tracking_device_id=%s AND s.started_at=t.ended_at", (settled, device))
        response = await _endpoint("/trips/{trip_id}/delete")(_request(pool, runner), settled, USER)
        assert response.status_code == 204
    _run(body)


def test_the_trip_can_be_edited_once_its_end_stay_is_settled():
    async def body(raw):
        pool, runner, device, pts, _settled, in_progress = await _two_trips(raw)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE detector_state SET last_run_at=now() WHERE account_id=%s AND tracking_device_id=%s",
                (account_id(conn), device))
        async with pool.connection() as conn:
            await _insert(conn, device, [replace(pts[-1], t=pts[-1].t + timedelta(hours=1), trigger=None)])
        assert await runner.run_once()
        response = await _endpoint("/trips/{trip_id}/delete")(_request(pool, runner), in_progress, USER)
        assert response.status_code == 204
    _run(body)
