"""DB-backed tests for missing-trip detection.

Covers the threshold boundary, the manual/first-trip/
no-end-geom exclusions, pagination/filter independence, and covering-manual-
trip suppression plus its removal. Like tests/test_geocode_db.py, this needs
a real Postgres+PostGIS and is skipped unless TEST_DATABASE_URL is set.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.rows import dict_row

from app.db import make_pool
from app.missing_trip import missing_trip_badge
from app.ui import TRIP_COLUMNS, _trip_filter_sql
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

UTC = timezone.utc
T0 = datetime(2026, 7, 1, 8, 0, 0, tzinfo=UTC)


async def _insert_detected_trip(
    conn, device, started_at, ended_at,
    start_lat=None, start_lon=None, end_lat=None, end_lon=None,
) -> int:
    """`end_lat`/`end_lon` (or `start_lat`/`start_lon`) left `None` inserts
    a NULL `end_geom`/`start_geom` -- used to exercise the "predecessor
    lacks end_geom" exclusion, which a real detected trip should never
    actually produce.
    """
    start_geom_sql = (
        "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography" if start_lon is not None else "NULL"
    )
    end_geom_sql = (
        "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography" if end_lon is not None else "NULL"
    )
    params = [device, started_at, ended_at]
    if start_lon is not None:
        params += [start_lon, start_lat]
    if end_lon is not None:
        params += [end_lon, end_lat]
    cur = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
        " point_count, detector_version, start_geom, end_geom) "
        f"VALUES (%s, 'detected', %s, %s, 1000, 2, 2, {start_geom_sql}, {end_geom_sql}) "
        "RETURNING id",
        params,
    )
    return (await cur.fetchone())[0]


async def _insert_manual_trip(conn, device, started_at, ended_at) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m) "
        "VALUES (%s, 'manual', %s, %s, 500) RETURNING id",
        (device, started_at, ended_at),
    )
    return (await cur.fetchone())[0]


async def _fetch_row(conn, trip_id: int) -> dict:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(f"SELECT {TRIP_COLUMNS} FROM trips WHERE id = %s", (trip_id,))
    return await cur.fetchone()


async def _scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        # Each scenario group gets its own day, not just its own device: the
        # covering-manual-trip check (below) is deliberately *not* filtered
        # by device -- a manual trip is always stored with device='manual'
        # (app.ui.add_manual_trip), so there is no per-device value to match
        # against, and the spec's own example EXISTS clause has no device
        # filter either. That means groups sharing a timeline could
        # spuriously "cover" each other's gap here in the test even though
        # they never would in the wild (a real device only ever has one
        # gap-producing pair of trips at a time).
        day = lambda n: T0 + timedelta(days=n)  # noqa: E731

        async with pool.connection() as conn:
            # --- Criterion 1: threshold boundary on a real measured gap ---
            t = day(0)
            gap_a = await _insert_detected_trip(
                conn, "GAP", t, t + timedelta(minutes=10),
                47.0000, -122.0000, 47.0000, -122.0000,
            )
            gap_b = await _insert_detected_trip(
                conn, "GAP", t + timedelta(minutes=30), t + timedelta(minutes=40),
                47.0200, -122.0000, 47.0300, -122.0000,
            )
            # Deliberately close successor: below any sane threshold.
            gap_c = await _insert_detected_trip(
                conn, "GAP", t + timedelta(hours=1), t + timedelta(hours=1, minutes=10),
                47.0301, -122.0000, 47.0301, -122.0000,
            )

            # --- Criterion 2: exclusions ---
            t = day(1)
            first_only = await _insert_detected_trip(
                conn, "FIRSTONLY", t, t + timedelta(minutes=10),
                47.0, -122.0, 47.0, -122.0,
            )
            t = day(2)
            manual_dev_a = await _insert_detected_trip(
                conn, "MANUALDEV", t, t + timedelta(minutes=10),
                47.0, -122.0, 47.0, -122.0,
            )
            manual_dev_b = await _insert_manual_trip(
                conn, "MANUALDEV", t + timedelta(minutes=30), t + timedelta(minutes=40),
            )
            t = day(3)
            no_geom_a = await _insert_detected_trip(
                conn, "NOGEOM", t, t + timedelta(minutes=10),
                47.0, -122.0,  # end_lat/end_lon left None -> NULL end_geom
            )
            no_geom_b = await _insert_detected_trip(
                conn, "NOGEOM", t + timedelta(minutes=30), t + timedelta(minutes=40),
                47.5, -122.5, 47.5, -122.5,
            )

            # --- Criterion 3: pagination/filter independence ---
            t = day(4)
            page_a = await _insert_detected_trip(
                conn, "PAGE", t, t + timedelta(minutes=10),
                47.0, -122.0, 47.0, -122.0,
            )
            page_b = await _insert_detected_trip(
                conn, "PAGE", t + timedelta(minutes=30), t + timedelta(minutes=40),
                47.0200, -122.0000, 47.0200, -122.0000,
            )

            # --- Criterion 4: covering-manual-trip suppression + removal ---
            t = day(5)
            cov_a = await _insert_detected_trip(
                conn, "COVER", t, t + timedelta(minutes=10),
                47.0, -122.0, 47.0, -122.0,
            )
            cov_b = await _insert_detected_trip(
                conn, "COVER", t + timedelta(hours=2), t + timedelta(hours=2, minutes=10),
                47.0300, -122.0000, 47.0300, -122.0000,
            )
            covering_manual = await _insert_manual_trip(
                conn, "COVER", t + timedelta(minutes=20), t + timedelta(hours=1),
            )

            # An excluded detected trip must not become the predecessor used
            # for a later dashboard warning.
            t = day(6)
            excluded_prev_a = await _insert_detected_trip(
                conn, "EXCLUDED-PREV", t, t + timedelta(minutes=10),
                47.0, -122.0, 47.0, -122.0,
            )
            excluded_prev_mid = await _insert_detected_trip(
                conn, "EXCLUDED-PREV", t + timedelta(minutes=20), t + timedelta(minutes=30),
                47.02, -122.0, 47.02, -122.0,
            )
            excluded_prev_c = await _insert_detected_trip(
                conn, "EXCLUDED-PREV", t + timedelta(minutes=40), t + timedelta(minutes=50),
                47.0, -122.0, 47.0, -122.0,
            )
            await conn.execute(
                "UPDATE trips SET exclusion = 'not_my_vehicle' WHERE id = %s",
                (excluded_prev_mid,),
            )

        async with pool.connection() as conn:
            row_b = await _fetch_row(conn, gap_b)
            row_c = await _fetch_row(conn, gap_c)
            row_first = await _fetch_row(conn, first_only)
            row_manual_b = await _fetch_row(conn, manual_dev_b)
            row_no_geom_b = await _fetch_row(conn, no_geom_b)
            row_page_b_full = await _fetch_row(conn, page_b)
            row_cov_b = await _fetch_row(conn, cov_b)
            row_excluded_prev_c = await _fetch_row(conn, excluded_prev_c)

        # --- Criterion 1 assertions: real measured gap, exact boundary ---
        assert row_b["prev_end_gap_m"] is not None
        gap_m = row_b["prev_end_gap_m"]
        assert gap_m > 1500  # ~0.02 deg latitude, sanity floor well under the true ~2224m
        assert missing_trip_badge(row_b, threshold_m=gap_m, tz=UTC) is None, \
            "exactly at the threshold must not flag (strictly greater than)"
        assert missing_trip_badge(row_b, threshold_m=gap_m - 1, tz=UTC) is not None, \
            "just above the threshold must flag"
        assert missing_trip_badge(row_b, threshold_m=gap_m + 1, tz=UTC) is None, \
            "just below the threshold must not flag"
        assert missing_trip_badge(row_b, threshold_m=0, tz=UTC) is None, \
            "MISSING_TRIP_GAP_M=0 disables the feature entirely"

        # A close successor never flags regardless of threshold config.
        assert row_c["prev_end_gap_m"] is not None
        assert row_c["prev_end_gap_m"] < 100
        assert missing_trip_badge(row_c, threshold_m=1000.0, tz=UTC) is None

        # --- Criterion 2 assertions ---
        assert row_first["prev_end_gap_m"] is None, "a device's first detected trip has no predecessor"
        assert missing_trip_badge(row_first, threshold_m=1.0, tz=UTC) is None

        assert row_manual_b["prev_end_gap_m"] is None, "a manual row has no start_geom to measure from"
        assert missing_trip_badge(row_manual_b, threshold_m=1.0, tz=UTC) is None

        assert row_no_geom_b["prev_end_gap_m"] is None, \
            "predecessor lacking end_geom must not fall back to an older trip"
        assert missing_trip_badge(row_no_geom_b, threshold_m=1.0, tz=UTC) is None

        # --- Criterion 3: pagination/filter independence ---
        # A WHERE clause that excludes page_a entirely from the outer result
        # set (category filter) must not change page_b's own gap value --
        # the correlated subselect looks at the whole `trips` table, not
        # whatever the caller's WHERE happens to include.
        async with pool.connection() as conn:
            where, params = _trip_filter_sql("business", None, None, None)
            await conn.execute(
                "UPDATE trips SET category = 'business' WHERE id = %s", (page_b,)
            )
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(f"SELECT {TRIP_COLUMNS} FROM trips {where} AND id = %s", [*params, page_b])
            row_page_b_filtered = await cur.fetchone()
        assert row_page_b_filtered["prev_end_gap_m"] == row_page_b_full["prev_end_gap_m"]
        assert row_page_b_full["prev_end_gap_m"] is not None
        assert row_page_b_full["prev_end_gap_m"] > 1500

        # The excluded middle row contributes nothing to the dashboard. The
        # visible successor therefore measures from the earlier normal trip.
        assert row_excluded_prev_c["prev_end_gap_m"] < 100
        assert missing_trip_badge(
            row_excluded_prev_c, threshold_m=1000.0, tz=UTC
        ) is None

        # --- Criterion 4: covering-manual-trip suppression + its removal ---
        assert row_cov_b["prev_end_gap_m"] is not None and row_cov_b["prev_end_gap_m"] > 1500
        assert row_cov_b["missing_trip_covered"] is True
        assert missing_trip_badge(row_cov_b, threshold_m=1000.0, tz=UTC) is None, \
            "a covering manual trip suppresses the badge even though the gap exceeds the threshold"

        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE trips SET exclusion = 'not_my_vehicle' WHERE id = %s",
                (covering_manual,),
            )
            row_cov_b_excluded = await _fetch_row(conn, cov_b)
        assert row_cov_b_excluded["missing_trip_covered"] is False
        assert missing_trip_badge(
            row_cov_b_excluded, threshold_m=1000.0, tz=UTC
        ) is not None, "an excluded manual trip must not suppress a dashboard warning"

        async with pool.connection() as conn:
            await conn.execute("DELETE FROM trips WHERE id = %s", (covering_manual,))
            row_cov_b_after = await _fetch_row(conn, cov_b)
        assert row_cov_b_after["missing_trip_covered"] is False
        badge = missing_trip_badge(row_cov_b_after, threshold_m=1000.0, tz=UTC)
        assert badge is not None, "deleting the covering manual trip brings the badge back"
        assert badge.gap_m == row_cov_b_after["prev_end_gap_m"]
        assert badge.prefill_url.startswith("/trips/manual?manual_date=")
        assert f"bridge_trip={cov_b}" in badge.prefill_url
    finally:
        await pool.close()


def test_missing_trip_detection_acceptance_criteria():
    asyncio.run(_scenario())
