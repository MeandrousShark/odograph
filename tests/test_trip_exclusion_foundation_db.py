"""DB-backed regression tests for the trip `exclusion` column.

These cases prove that human-owned exclusion state survives detector and
autotag processing rather than relying on the omission accidentally.

Most cases here need a real Postgres+PostGIS instance and are skipped unless
``TEST_DATABASE_URL`` is set, same convention as tests/test_runner_db.py. The
target database is truncated and reset on each run -- point it at a
throwaway DB only.

    TEST_DATABASE_URL=postgresql://mileage:pw@127.0.0.1:5432/mileage pytest tests/test_trip_exclusion_foundation_db.py

One case (the `_write_trip` source guard, at the bottom) needs neither a
database nor a subprocess, so it carries its own `pytest.mark.unit` instead
of inheriting this file's default `db` tier from the `_db.py` filename
convention (tests/conftest.py's `pytest_collection_modifyitems`), and runs
even when TEST_DATABASE_URL is unset.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest
from psycopg.rows import dict_row

from app.db import make_pool
from app.detector.core import Params
from app.detector.runner import DetectorRunner, reprocess_places
from app.ui import TRIP_COLUMNS
from conftest import reset_db
from tests.synth import Drive, Stationary, build_track

TEST_DB = os.environ.get("TEST_DATABASE_URL")
# Applied per-test (not as a module-level pytestmark) so the source-only
# guard test at the bottom keeps running without TEST_DATABASE_URL, the
# same split tests/test_worker_lifecycle.py uses for its own mix of DB and
# non-DB cases in one file.
db_only = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

DEVICE = "TESTDEV"
ROOT = Path(__file__).resolve().parent.parent


async def _insert_points(conn, points) -> None:
    for p in points:
        await conn.execute(
            "INSERT INTO points (device, recorded_at, received_at, geom, "
            " accuracy_m, velocity_kmh) "
            "VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)",
            (DEVICE, p.t, p.t, p.lon, p.lat, p.accuracy_m, p.velocity_kmh),
        )


async def _detected_trip_ids_by_start(conn) -> list[int]:
    cur = await conn.execute(
        "SELECT id FROM trips WHERE device = %s AND source = 'detected' "
        "ORDER BY started_at",
        (DEVICE,),
    )
    return [row[0] for row in await cur.fetchall()]


async def _exclusion_of(conn, trip_id: int) -> str | None:
    cur = await conn.execute("SELECT exclusion::text FROM trips WHERE id = %s", (trip_id,))
    return (await cur.fetchone())[0]


async def _build_two_trip_device(conn) -> list[int]:
    """Stay -> drive -> stay -> drive -> stay: two detectable trips, the
    same shape tests/test_runner_db.py's incremental-reprocess scenario
    uses, so a later windowed reprocess has a real "middle stay" to rewind
    to.
    """
    pts = build_track([
        Stationary(1200), Drive(km=2), Stationary(1200),
        Drive(km=2), Stationary(1200),
    ])
    await _insert_points(conn, pts)


async def _run_survives_windowed_reprocess_scenario() -> None:
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            await _build_two_trip_device(conn)

        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            trip_ids = await _detected_trip_ids_by_start(conn)
        assert len(trip_ids) == 2
        last_trip_id = trip_ids[-1]

        # Set the exclusion directly so this scenario isolates detector
        # preservation from the independently tested UI write paths.
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE trips SET exclusion = 'not_my_vehicle' WHERE id = %s",
                (last_trip_id,),
            )

        # A late point landing in the last stay makes the next run
        # incremental, rewinding to the middle stay -- which brackets the
        # last trip, so it's re-matched and rewritten (not left untouched
        # by falling outside the window).
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE points SET received_at = now() WHERE device = %s AND "
                "recorded_at = (SELECT max(recorded_at) FROM points WHERE device = %s)",
                (DEVICE, DEVICE),
            )
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            trip_ids_after = await _detected_trip_ids_by_start(conn)
            assert last_trip_id in trip_ids_after, (
                "the excluded trip must still be matched to the same row, "
                "not deleted and reinserted, for this to prove anything"
            )
            assert await _exclusion_of(conn, last_trip_id) == "not_my_vehicle"
    finally:
        await pool.close()


@db_only
def test_exclusion_survives_windowed_reprocess_of_its_own_window():
    asyncio.run(_run_survives_windowed_reprocess_scenario())


async def _run_survives_full_device_reprocess_scenario() -> None:
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            await _build_two_trip_device(conn)

        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            trip_ids = await _detected_trip_ids_by_start(conn)
        assert len(trip_ids) == 2
        first_trip_id = trip_ids[0]

        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE trips SET exclusion = 'not_deductible' WHERE id = %s",
                (first_trip_id,),
            )

        await runner.reprocess_device_now(DEVICE)

        async with pool.connection() as conn:
            trip_ids_after = await _detected_trip_ids_by_start(conn)
            assert first_trip_id in trip_ids_after, (
                "an unchanged full reprocess must still match the same row"
            )
            assert await _exclusion_of(conn, first_trip_id) == "not_deductible"
    finally:
        await pool.close()


@db_only
def test_exclusion_survives_full_device_reprocess():
    asyncio.run(_run_survives_full_device_reprocess_scenario())


async def _run_survives_autotag_pass_scenario() -> None:
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)

        async with pool.connection() as conn:
            # A rule-owned detected trip with no geometry and no tag_rules
            # present: reprocess_places (which calls resolve_and_autotag)
            # will revert its category to unclassified, a real UPDATE on
            # this exact row -- not a no-op that would prove nothing about
            # whether that UPDATE also happens to overwrite exclusion.
            cur = await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
                " category, tag_source, exclusion) "
                "VALUES ('A', 'detected', '2026-01-01T09:00:00Z', "
                "'2026-01-01T09:30:00Z', 1000, 'business', 'rule', 'not_deductible') "
                "RETURNING id"
            )
            trip_id = (await cur.fetchone())[0]

        await reprocess_places(pool)

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT category::text, tag_source::text, exclusion::text "
                "FROM trips WHERE id = %s",
                (trip_id,),
            )
            category, tag_source, exclusion = await cur.fetchone()
        assert (category, tag_source) == ("unclassified", None), (
            "the autotag pass must have actually rewritten this row for "
            "the exclusion assertion below to mean anything"
        )
        assert exclusion == "not_deductible"
    finally:
        await pool.close()


@db_only
def test_exclusion_survives_autotag_pass():
    asyncio.run(_run_survives_autotag_pass_scenario())


async def _run_trip_columns_selects_exclusion_scenario() -> None:
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m) "
                "VALUES ('manual', 'manual', '2026-01-01T09:00:00Z', "
                "'2026-01-01T09:30:00Z', 1000) RETURNING id"
            )
            no_exclusion_id = (await cur.fetchone())[0]
            cur = await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m, exclusion) "
                "VALUES ('manual', 'manual', '2026-01-02T09:00:00Z', "
                "'2026-01-02T09:30:00Z', 1000, 'not_my_vehicle') RETURNING id"
            )
            excluded_id = (await cur.fetchone())[0]

            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(f"SELECT {TRIP_COLUMNS} FROM trips WHERE id = %s", (no_exclusion_id,))
            no_exclusion_row = await cur.fetchone()
            await cur.execute(f"SELECT {TRIP_COLUMNS} FROM trips WHERE id = %s", (excluded_id,))
            excluded_row = await cur.fetchone()

        assert "exclusion" in no_exclusion_row
        assert no_exclusion_row["exclusion"] is None
        assert excluded_row["exclusion"] == "not_my_vehicle"
    finally:
        await pool.close()


@db_only
def test_trip_columns_selects_exclusion_and_defaults_to_none():
    asyncio.run(_run_trip_columns_selects_exclusion_scenario())


@pytest.mark.unit
def test_write_trip_update_statement_does_not_mention_exclusion():
    """`_write_trip`'s matched-trip UPDATE (app/detector/runner.py, currently
    around line 402) deliberately touches only detector-derived fields --
    started_at/ended_at/geometry/distance/point_count/has_gap/detector_version
    plus the snap-preservation columns -- and never category, tag_source,
    purpose, or vehicle_id. That's exactly why those survive a reprocess
    untouched; a new column inherits the same protection for free only as
    long as nobody adds it here. Reading the source guards that invariant
    directly rather than relying only on the scenario tests above, which
    would only ever prove today's fixtures happen to avoid the bug, not that
    the statement itself is safe.
    """
    source = (ROOT / "app" / "detector" / "runner.py").read_text()
    match = re.search(r"    async def _write_trip\(.*?\n^async def ", source, re.DOTALL | re.MULTILINE)
    assert match, "could not locate _write_trip in app/detector/runner.py to guard"
    assert "exclusion" not in match.group(), (
        "_write_trip now mentions 'exclusion' -- if that's the matched-trip "
        "UPDATE, it will silently overwrite a human's exclusion tag on every "
        "detector reprocess of that trip's window"
    )
