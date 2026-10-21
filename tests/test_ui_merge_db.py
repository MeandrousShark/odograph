from __future__ import annotations

import asyncio
import functools
import json
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.db import make_pool
from app.detector.core import Params
from app.detector.runner import DetectorRunner, reprocess_places_in
from app.ui import make_router
from conftest import reset_account_db, seed_tracking_device
from app.account_context import account_id
from tests.synth import START, Drive, Stationary, _offset, build_track

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")


class FakeSnapWorker:
    def __init__(self):
        self.pokes = 0

    def poke(self):
        self.pokes += 1


@functools.cache
def _routes():
    return tuple(make_router().routes)


def _endpoint():
    for route in _routes():
        if getattr(route, "path", None) == "/trips/merge_selected":
            return route.endpoint
    raise AssertionError("merge_selected route missing")


async def _insert_points(conn, points, device):
    stream = await seed_tracking_device(conn, device)
    for point in points:
        await conn.execute(
            "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, received_at, geom, accuracy_m, velocity_kmh) "
            "VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)",
            (account_id(conn), stream, device, point.t, point.t, point.lon, point.lat, point.accuracy_m, point.velocity_kmh),
        )


async def _trips(conn, device):
    cur = await conn.execute(
        "SELECT id, started_at, ended_at FROM trips WHERE device=%s AND source='detected' ORDER BY started_at",
        (device,),
    )
    return await cur.fetchall()


async def _override_rows(conn, device):
    cur = await conn.execute(
        "SELECT id, kind::text, point_id, range_start, range_end "
        "FROM trip_boundary_overrides WHERE device=%s ORDER BY id",
        (device,),
    )
    return await cur.fetchall()


async def _scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        track = build_track([
            Stationary(900), Drive(km=2), Stationary(1200), Drive(km=2),
            Stationary(1200), Drive(km=2), Stationary(900),
        ])
        other = build_track([Stationary(900), Drive(km=2), Stationary(900)])
        async with pool.connection() as conn:
            await _insert_points(conn, track, "A")
            await _insert_points(conn, other, "B")
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True
        async with pool.connection() as conn:
            a = await _trips(conn, "A")
            b = await _trips(conn, "B")
            manual = await conn.execute(
                "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m) "
                "VALUES (%s, 'A', 'manual', %s, %s, 1000) RETURNING id",
                (account_id(conn), a[0][2], a[1][1]),
            )
            manual_id = (await manual.fetchone())[0]

        handler = _endpoint()
        snap_worker = FakeSnapWorker()
        request = SimpleNamespace(state=SimpleNamespace(account_pool=pool, detector_runner=runner), app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, detector_runner=runner, snap_worker=snap_worker,
        )))
        cases = [
            ([a[0][0], a[2][0]], "contiguous"),
            ([a[0][0], b[0][0]], "same device"),
            ([a[0][0], manual_id], "detected"),
        ]
        for trip_ids, message in cases:
            with pytest.raises(HTTPException, match=message):
                await handler(request, trip_ids, "unclassified", "", "", "keep", {"sub": "test"})

        response = await handler(
            request, [a[0][0], a[1][0]], "business", "  Client visit  ",
            "handler", "keep", {"sub": "test"},
        )
        merged_id = json.loads(response.body)["trip_id"]
        async with pool.connection() as conn:
            after = await _trips(conn, "A")
            row = await conn.execute(
                "SELECT category::text, purpose, notes, tag_source::text FROM trips WHERE id=%s",
                (merged_id,),
            )
            values = await row.fetchone()
        assert len(after) == 2
        assert values == ("business", "Client visit", "handler", "human")
        assert snap_worker.pokes == 1
    finally:
        await raw_pool.close()


def test_merge_handler_validation_and_valid_merge():
    asyncio.run(_scenario())


async def _reprocess_failure_rolls_back_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        track = build_track([
            Stationary(900), Drive(km=2), Stationary(1200), Drive(km=2), Stationary(900),
        ])
        async with pool.connection() as conn:
            await _insert_points(conn, track, "A")
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True
        async with pool.connection() as conn:
            a = await _trips(conn, "A")
        assert len(a) == 2

        # An opposing force-split override sitting inside the range the
        # merge's override-write phase would suppress -- exactly what a
        # failed merge must NOT actually delete.
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM points WHERE account_id=%s AND device='A' "
                "AND recorded_at BETWEEN %s AND %s ORDER BY recorded_at LIMIT 1",
                (account_id(conn), a[0][2], a[1][1]),
            )
            force_point_id = (await cur.fetchone())[0]
            await conn.execute(
                "INSERT INTO trip_boundary_overrides (account_id, tracking_device_id, device, kind, point_id) "
                "SELECT account_id, tracking_device_id, device, 'force', id FROM points "
                "WHERE account_id=%s AND id=%s",
                (account_id(conn), force_point_id),
            )
            before_overrides = await _override_rows(conn, "A")
            before_trips = await _trips(conn, "A")

        # Force the reprocess step -- which in the fixed handler runs on the
        # SAME connection/transaction as the override writes above it -- to
        # fail. If it isn't actually atomic, the override writes already
        # executed real SQL and would still land, independent of this.
        async def _broken_reprocess_device_in(conn, device):
            raise RuntimeError("forced reprocess failure mid-merge")

        runner.reprocess_device_in = _broken_reprocess_device_in

        handler = _endpoint()
        snap_worker = FakeSnapWorker()
        request = SimpleNamespace(state=SimpleNamespace(account_pool=pool, detector_runner=runner), app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, detector_runner=runner, snap_worker=snap_worker,
        )))
        with pytest.raises(RuntimeError, match="forced reprocess failure"):
            await handler(
                request, [a[0][0], a[1][0]], "business", "Client visit",
                "handler", "keep", {"sub": "test"},
            )

        async with pool.connection() as conn:
            after_overrides = await _override_rows(conn, "A")
            after_trips = await _trips(conn, "A")
        assert after_overrides == before_overrides, (
            "a failed reprocess must roll back the merge's override writes too "
            "(both the suppress insert and the force delete)"
        )
        assert after_trips == before_trips, "trips must be untouched by a rolled-back merge"
        assert snap_worker.pokes == 0
    finally:
        await raw_pool.close()


def test_merge_rolls_back_atomically_when_reprocess_fails():
    """Override writes, reprocess, and the tag UPDATE run
    in one transaction, so a failure in the reprocess step leaves no partial
    state -- not a committed suppress-override, not a deleted force-override,
    not a changed trip."""
    asyncio.run(_reprocess_failure_rolls_back_scenario())


async def _set_vehicle(conn, trip_id, vehicle_id):
    await conn.execute("UPDATE trips SET vehicle_id = %s WHERE id = %s", (vehicle_id, trip_id))


async def _vehicle_tristate_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)

        def _two_trip_track():
            return build_track([
                Stationary(900), Drive(km=2), Stationary(1200), Drive(km=2), Stationary(900),
            ])

        devices = ["K", "E", "N", "X"]
        async with pool.connection() as conn:
            for device in devices:
                await _insert_points(conn, _two_trip_track(), device)
            second_vehicle = await conn.execute(
                "INSERT INTO vehicles (account_id, name) VALUES (%s, 'Second Car') RETURNING id",
                (account_id(conn),)
            )
            second_vehicle_id = (await second_vehicle.fetchone())[0]

        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        handler = _endpoint()
        request = SimpleNamespace(state=SimpleNamespace(account_pool=pool, detector_runner=runner), app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, detector_runner=runner
        )))

        async with pool.connection() as conn:
            trips_by_device = {d: await _trips(conn, d) for d in devices}
            for d in devices:
                assert len(trips_by_device[d]) == 2

        # "keep" must reproduce today's inheritance behavior exactly: don't
        # touch vehicle_id at all. Direct handler calls can't literally omit
        # the form field (Form(...)'s sentinel is only resolved by FastAPI's
        # own request parsing, not a plain function call), so "keep" is
        # passed explicitly here; `test_merge_endpoint_vehicle_default.py`
        # separately asserts that all three merge endpoints declare "keep"
        # as the actual wire-level default a genuinely missing field
        # resolves to.
        k_ids = [r[0] for r in trips_by_device["K"]]
        async with pool.connection() as conn:
            for trip_id in k_ids:
                await _set_vehicle(conn, trip_id, 1)
        response = await handler(
            request, k_ids, "unclassified", "", "", "keep", {"sub": "test"},
        )
        k_merged = json.loads(response.body)["trip_id"]

        # An explicit vehicle id joins the same UPDATE the tag/purpose/notes
        # already use, still inside the merge's one transaction.
        e_ids = [r[0] for r in trips_by_device["E"]]
        response = await handler(
            request, e_ids, "unclassified", "", "", str(second_vehicle_id), {"sub": "test"},
        )
        e_merged = json.loads(response.body)["trip_id"]

        # The empty string is a real, distinct choice ("no vehicle"), not a
        # synonym for "keep".
        n_ids = [r[0] for r in trips_by_device["N"]]
        async with pool.connection() as conn:
            for trip_id in n_ids:
                await _set_vehicle(conn, trip_id, 1)
        response = await handler(
            request, n_ids, "unclassified", "", "", "", {"sub": "test"},
        )
        n_merged = json.loads(response.body)["trip_id"]

        async with pool.connection() as conn:
            rows = await conn.execute(
                "SELECT id, vehicle_id FROM trips WHERE id = ANY(%s)",
                ([k_merged, e_merged, n_merged],),
            )
            by_id = {r[0]: r[1] for r in await rows.fetchall()}
        assert by_id[k_merged] == 1, "keep (field omitted) must preserve the inherited vehicle_id"
        assert by_id[e_merged] == second_vehicle_id
        assert by_id[n_merged] is None

        # A nonexistent vehicle id must 400 and leave the merge entirely
        # unapplied -- same atomicity guarantee as the reprocess-failure
        # case, just triggered by the final UPDATE
        # instead of the reprocess step.
        x_ids = [r[0] for r in trips_by_device["X"]]
        async with pool.connection() as conn:
            before_overrides = await _override_rows(conn, "X")
            before_trips = await _trips(conn, "X")
        with pytest.raises(HTTPException, match="No such vehicle"):
            await handler(
                request, x_ids, "unclassified", "", "", "999999", {"sub": "test"},
            )
        async with pool.connection() as conn:
            after_overrides = await _override_rows(conn, "X")
            after_trips = await _trips(conn, "X")
        assert after_overrides == before_overrides
        assert after_trips == before_trips
    finally:
        await raw_pool.close()


def test_merge_vehicle_tristate():
    """A digit vehicle_id sets it, "" nulls it, and "keep" (or the field
    being absent, matching a stale client) leaves whatever reconcile's
    longest-trip inheritance already put there. A nonexistent vehicle id
    400s without applying any part of the merge."""
    asyncio.run(_vehicle_tristate_scenario())


async def _set_category(conn, trip_id, category, tag_source=None):
    await conn.execute(
        "UPDATE trips SET category = %s, tag_source = %s WHERE id = %s",
        (category, tag_source, trip_id),
    )


async def _category_tristate_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)

        def _two_trip_track():
            return build_track([
                Stationary(900), Drive(km=2), Stationary(1200), Drive(km=2), Stationary(900),
            ])

        devices = ["K", "P", "Z"]
        async with pool.connection() as conn:
            for device in devices:
                await _insert_points(conn, _two_trip_track(), device)

        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        handler = _endpoint()
        request = SimpleNamespace(state=SimpleNamespace(account_pool=pool, detector_runner=runner), app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, detector_runner=runner
        )))

        async with pool.connection() as conn:
            trips_by_device = {d: await _trips(conn, d) for d in devices}
            for d in devices:
                assert len(trips_by_device[d]) == 2

        # "keep" with a NULL tag_source leaves reconcile's own longest-trip
        # inheritance untouched -- neither category nor tag_source is
        # written, so the merge's final UPDATE alone doesn't human-lock a
        # trip the user didn't reclassify. A rule-tagged survivor is
        # restored by the merge itself (see the rule-tagged cases below).
        k_ids = [r[0] for r in trips_by_device["K"]]
        async with pool.connection() as conn:
            for trip_id in k_ids:
                await _set_category(conn, trip_id, "business", None)
        response = await handler(
            request, k_ids, "keep", "", "", "keep", {"sub": "test"},
        )
        k_merged = json.loads(response.body)["trip_id"]

        # An explicit real category still joins the same UPDATE the
        # purpose/notes already use and claims human ownership, unchanged
        # from before category became tri-state.
        p_ids = [r[0] for r in trips_by_device["P"]]
        response = await handler(
            request, p_ids, "personal", "", "", "keep", {"sub": "test"},
        )
        p_merged = json.loads(response.body)["trip_id"]

        async with pool.connection() as conn:
            rows = await conn.execute(
                "SELECT id, category::text, tag_source::text FROM trips WHERE id = ANY(%s)",
                ([k_merged, p_merged],),
            )
            by_id = {r[0]: (r[1], r[2]) for r in await rows.fetchall()}
        assert by_id[k_merged] == ("business", None), (
            "keep must preserve both the inherited category and its tag_source, "
            "not human-lock it"
        )
        assert by_id[p_merged] == ("personal", "human")

        # An unknown category (not "keep", not a real CATEGORIES value) must
        # still 400 and leave the merge entirely unapplied.
        z_ids = [r[0] for r in trips_by_device["Z"]]
        async with pool.connection() as conn:
            before_overrides = await _override_rows(conn, "Z")
            before_trips = await _trips(conn, "Z")
        with pytest.raises(HTTPException, match="Unknown category"):
            await handler(
                request, z_ids, "not-a-real-category", "", "", "keep", {"sub": "test"},
            )
        async with pool.connection() as conn:
            after_overrides = await _override_rows(conn, "Z")
            after_trips = await _trips(conn, "Z")
        assert after_overrides == before_overrides
        assert after_trips == before_trips
    finally:
        await raw_pool.close()


def test_merge_category_tristate():
    """A real category value sets it and claims human ownership; "keep"
    (the endpoints' own Form default) leaves a NULL-source survivor's
    category and tag_source both untouched, so a merge alone can never
    human-lock a trip the user didn't actually reclassify. Rule-tagged
    survivors are covered by test_merge_keep_rule_tagged_survivor. An
    unrecognized category still 400s without applying any part of the
    merge."""
    asyncio.run(_category_tristate_scenario())


# --- B29: durable Keep and blank purpose/notes ------------------------

def _request(pool, runner):
    return SimpleNamespace(state=SimpleNamespace(account_pool=pool, detector_runner=runner), app=SimpleNamespace(state=SimpleNamespace(
        pool=pool, detector_runner=runner
    )))


async def _tag(conn, trip_id):
    cur = await conn.execute("SELECT category::text, tag_source::text FROM trips WHERE id = %s", (trip_id,))
    return await cur.fetchone()


async def _rule_keep_scenario(a_to_c_rule, pre_human, category="keep"):
    """Two trips A->B and B->C, each matched by a rule to 'personal'. The
    merged trip runs A->C; `a_to_c_rule` controls what (if anything) matches
    it. Returns (pre-merge survivor tag, merged tag before and after a
    places reprocess)."""
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        lat, lon = START
        spots = [_offset(lat, lon, east, 0) for east in (0, 3000, 5000)]
        async with pool.connection() as conn:
            ids = []
            for name, (plat, plon) in zip("ABC", spots):
                cur = await conn.execute(
                    "INSERT INTO places (account_id, name, geom) VALUES (%s, %s, "
                    "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) RETURNING id",
                    (account_id(conn), name, plon, plat),
                )
                ids.append((await cur.fetchone())[0])
            a, b, c = ids
            rules = [(a, b, "personal"), (b, c, "personal")]
            if a_to_c_rule:
                rules.append((a, c, a_to_c_rule))
            for a_place, b_place, rule_category in rules:
                await conn.execute(
                    "INSERT INTO tag_rules (account_id, a_place, b_place, category) VALUES (%s, %s, %s, %s)",
                    (account_id(conn), a_place, b_place, rule_category),
                )
            await _insert_points(conn, build_track([
                Stationary(900), Drive(km=3), Stationary(1200), Drive(km=2), Stationary(900),
            ]), "R")

        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True
        async with pool.connection() as conn:
            trips = await _trips(conn, "R")
            assert len(trips) == 2
            trip_ids = [t[0] for t in trips]
            if pre_human:
                for trip_id in trip_ids:
                    await _set_category(conn, trip_id, "personal", "human")
            for trip_id in trip_ids:
                assert await _tag(conn, trip_id) == ("personal", "human" if pre_human else "rule")

        response = await _endpoint()(
            _request(pool, runner), trip_ids, category, "", "", "keep", {"sub": "test"},
        )
        merged_id = json.loads(response.body)["trip_id"]
        assert merged_id == trip_ids[0], "reconcile keeps the longest trip's row"
        async with pool.connection() as conn:
            merged = await _tag(conn, merged_id)
            await reprocess_places_in(conn)
        async with pool.connection() as conn:
            return merged, await _tag(conn, merged_id)
    finally:
        await raw_pool.close()


@pytest.mark.parametrize("a_to_c_rule, pre_human, category, expected", [
    # No rule matches the merged endpoints: Keep restores and human-locks,
    # and a later places reprocess leaves it.
    (None, False, "keep", ("personal", "human")),
    # A same-category rule still matches: the rule tag is left alone.
    ("personal", False, "keep", ("personal", "rule")),
    # A different-category rule matches: Keep restores the pre-merge category.
    ("business", False, "keep", ("personal", "human")),
    # A human-tagged survivor is unchanged.
    (None, True, "keep", ("personal", "human")),
    ("business", True, "keep", ("personal", "human")),
    # An explicit category still sets it with human ownership.
    (None, False, "business", ("business", "human")),
])
def test_merge_keep_rule_tagged_survivor(a_to_c_rule, pre_human, category, expected):
    merged, after_reprocess = asyncio.run(_rule_keep_scenario(a_to_c_rule, pre_human, category))
    assert merged == expected
    assert after_reprocess == expected


async def _purpose_notes_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        devices = ["S", "E", "N", "R"]
        async with pool.connection() as conn:
            for device in devices:
                # The middle trip is the longest, so it is the survivor.
                await _insert_points(conn, build_track([
                    Stationary(900), Drive(km=2), Stationary(1200), Drive(km=4),
                    Stationary(1200), Drive(km=2), Stationary(900),
                ]), device)
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            trips = {d: [t[0] for t in await _trips(conn, d)] for d in devices}
            for d in devices:
                assert len(trips[d]) == 3

            async def fill(device, values):
                for trip_id, (purpose, notes) in zip(trips[device], values):
                    await conn.execute(
                        "UPDATE trips SET purpose = %s, notes = %s WHERE id = %s", (purpose, notes, trip_id),
                    )

            # S: the survivor's own values win over the others.
            await fill("S", [("First", "n1"), ("Survivor", "ns"), ("Third", "n3")])
            # E: a blank survivor falls back to the earliest nonblank value,
            # independently for purpose and notes; whitespace counts as blank.
            await fill("E", [("First", None), ("  ", " "), ("Third", "n3")])
            # N: nothing nonblank anywhere stays NULL.
            await fill("N", [(None, None), ("  ", ""), (None, None)])
            # R: nonblank form values replace the survivor's.
            await fill("R", [("First", "n1"), ("Survivor", "ns"), ("Third", "n3")])

        request = _request(pool, runner)
        handler = _endpoint()
        merged = {}
        for device, purpose, notes in [
            ("S", "", "  "), ("E", "", ""), ("N", " ", ""), ("R", "  New purpose ", "new notes"),
        ]:
            response = await handler(request, trips[device], "keep", purpose, notes, "keep", {"sub": "test"})
            merged[device] = json.loads(response.body)["trip_id"]
            assert merged[device] == trips[device][1], "reconcile keeps the longest trip's row"

        async with pool.connection() as conn:
            rows = await conn.execute(
                "SELECT id, purpose, notes FROM trips WHERE id = ANY(%s)", (list(merged.values()),),
            )
            by_id = {r[0]: (r[1], r[2]) for r in await rows.fetchall()}
        assert by_id[merged["S"]] == ("Survivor", "ns")
        assert by_id[merged["E"]] == ("First", "n3")
        assert by_id[merged["N"]] == (None, None)
        assert by_id[merged["R"]] == ("New purpose", "new notes")
    finally:
        await raw_pool.close()


def test_merge_blank_purpose_and_notes_keep_existing_values():
    """A blank purpose or notes field keeps the survivor's value, else the
    earliest nonblank selected value, else NULL; a nonblank value replaces."""
    asyncio.run(_purpose_notes_scenario())
