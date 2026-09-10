"""DB-backed coverage for trips.start_label/end_label
(migrations/025_manual_trip_labels.sql): manual create, atomic add/change/
clear through trip edit, rejection of a label on an ineligible trip through
both handlers, the migration's own constraints against invalid direct
writes, and TRIP_COLUMNS' effective start_place_name/end_place_name
precedence (app/ui/_common.py) that every display consumer relies on.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from psycopg import errors
from psycopg.rows import dict_row

from app.db import make_pool
from app.main import make_templates
from app.ui import TRIP_COLUMNS, make_router
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")
TZ = ZoneInfo("America/Los_Angeles")
USER = {"sub": "test"}


def _endpoint(path: str, method: str):
    for route in make_router().routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"route missing: {method} {path}")


ADD = _endpoint("/trips/manual", "POST")
SAVE = _endpoint("/trips/{trip_id}/edit", "POST")

DEFAULT_ADD_FORM = {
    "date": "2026-07-14", "start_time": "09:00", "end_time": "10:00", "distance": "10",
    "category": "unclassified", "purpose": "", "notes": "", "vehicle_id": "",
    "route_mode": "none", "start_place": "", "end_place": "",
    "start_lat": "", "start_lon": "", "end_lat": "", "end_lon": "",
    "routed_distance": "", "exclusion": "", "start_label": "", "end_label": "",
}

DEFAULT_SAVE_FORM = {
    "category": "unclassified", "purpose": "", "notes": "", "vehicle_id": "",
    "date": "", "start_time": "", "end_time": "", "distance": "", "exclusion": "",
    "start_label": "", "end_label": "", "dashboard_week": "",
}


def _add_request(pool):
    config = SimpleNamespace(osrm_url="", display_tz=TZ, app_version="test")
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, config=config, osrm_http_client=None,
        )),
        session={"csrf": "test"},
    )


def _save_request(pool):
    config = SimpleNamespace(display_tz=TZ, app_version="test")
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
        )),
        session={"csrf": "test"},
        headers={},
    )


async def _add(pool, **overrides):
    values = dict(DEFAULT_ADD_FORM)
    values.update(overrides)
    return await ADD(_add_request(pool), user=USER, **values)


async def _save(pool, trip_id, omit=(), **overrides):
    values = dict(DEFAULT_SAVE_FORM)
    values.update(overrides)
    # `omit` drops a key entirely rather than setting it blank, so the call
    # below reaches save_trip_card without that keyword at all -- the same
    # shape as a real request whose form omits the field, and distinct from
    # submitting it as "".
    for field in omit:
        values.pop(field, None)
    return await SAVE(_save_request(pool), trip_id, user=USER, **values)


async def _fetch_only_trip(pool) -> dict:
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute("SELECT id, start_label, end_label FROM trips")
        rows = await cur.fetchall()
    assert len(rows) == 1, f"expected exactly one trip, found {len(rows)}"
    return rows[0]


async def _fetch_labels(pool, trip_id) -> tuple:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT start_label, end_label FROM trips WHERE id = %s", (trip_id,)
        )
        return await cur.fetchone()


async def _fetch_trip_columns(pool, trip_id) -> dict:
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(f"SELECT {TRIP_COLUMNS} FROM trips WHERE id = %s", (trip_id,))
        return await cur.fetchone()


async def _insert_geocode(conn, lat: float, lon: float, address: str) -> None:
    await conn.execute(
        "INSERT INTO geocode_cache (lat, lon, address) VALUES (%s, %s, %s)",
        (round(lat, 4), round(lon, 4), address),
    )


async def _insert_no_route_manual(conn, **overrides) -> int:
    values = {
        "started_at": "2026-07-14T16:00:00Z",
        "ended_at": "2026-07-14T17:00:00Z",
        "distance_m": 1609.344,
        "category": "unclassified",
        "purpose": "Errand",
        "notes": "Some notes",
        "vehicle_id": None,
    }
    values.update(overrides)
    row = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m, category, "
        "purpose, notes, vehicle_id) VALUES ('manual', 'manual', %s, %s, %s, %s, %s, %s, %s) "
        "RETURNING id",
        tuple(values.values()),
    )
    return (await row.fetchone())[0]


async def _insert_routed_manual(conn) -> int:
    row = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, start_geom, end_geom, "
        "distance_m, category) VALUES ('manual', 'manual', "
        "'2026-07-14T16:00:00Z', '2026-07-14T17:00:00Z', "
        "ST_SetSRID(ST_MakePoint(-122.3, 47.6), 4326)::geography, "
        "ST_SetSRID(ST_MakePoint(-122.2, 47.7), 4326)::geography, 1609.344, 'unclassified') "
        "RETURNING id"
    )
    return (await row.fetchone())[0]


async def _insert_detected(conn) -> int:
    row = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, start_geom, end_geom, "
        "distance_m, point_count, path, has_gap, category, detector_version, snap_status) "
        "VALUES ('phone', 'detected', '2026-07-14T18:00:00Z', '2026-07-14T19:00:00Z', "
        "ST_SetSRID(ST_MakePoint(-122.3, 47.6), 4326)::geography, "
        "ST_SetSRID(ST_MakePoint(-122.2, 47.7), 4326)::geography, 3200, 44, "
        "ST_GeomFromText('LINESTRING(-122.3 47.6,-122.2 47.7)', 4326), true, "
        "'business', 2, 'failed') RETURNING id"
    )
    return (await row.fetchone())[0]


async def _insert_place(conn, name: str, lat: float, lon: float) -> int:
    row = await conn.execute(
        "INSERT INTO places (name, geom) VALUES "
        "(%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) RETURNING id",
        (name, lon, lat),
    )
    return (await row.fetchone())[0]


def _run(coro_factory) -> None:
    async def run():
        pool = make_pool(TEST_DB)
        await pool.open(wait=True)
        try:
            await reset_db(pool)
            await coro_factory(pool)
        finally:
            await pool.close()

    asyncio.run(run())


# --- Manual create ---------------------------------------------------------

@pytest.mark.parametrize("overrides,expected", [
    ({}, (None, None)),
    ({"start_label": "  Grandma's house  "}, ("Grandma's house", None)),
    ({"end_label": "  Work site  "}, (None, "Work site")),
    ({"start_label": "Home", "end_label": "Work"}, ("Home", "Work")),
])
def test_no_route_manual_create_stores_labels(overrides, expected):
    async def scenario(pool):
        response = await _add(pool, **overrides)
        assert response.status_code == 204
        trip = await _fetch_only_trip(pool)
        assert (trip["start_label"], trip["end_label"]) == expected

    _run(scenario)


# --- Trip edit: add/change/clear -------------------------------------------

def test_edit_add_change_clear_labels_leaves_unrelated_fields_untouched():
    async def scenario(pool):
        async with pool.connection() as conn:
            trip_id = await _insert_no_route_manual(conn)

        # distance="1" matches _insert_no_route_manual's distance_m (one
        # mile, METERS_PER_MILE exactly) so resubmitting it on every save
        # reproduces the same stored value instead of drifting from it,
        # letting `baseline` (captured once, before any label edit) stay
        # valid for every snapshot comparison below.
        unrelated_form = {
            "category": "unclassified", "purpose": "Errand", "notes": "Some notes",
            "vehicle_id": "", "date": "2026-07-14", "start_time": "09:00",
            "end_time": "10:00", "distance": "1",
        }

        async def unrelated_snapshot():
            async with pool.connection() as conn:
                return await (await conn.execute(
                    "SELECT started_at, ended_at, distance_m, category::text, purpose, "
                    "notes, vehicle_id FROM trips WHERE id = %s", (trip_id,),
                )).fetchone()

        baseline = await unrelated_snapshot()

        # Add: neither label -> both.
        response = await _save(
            pool, trip_id, **unrelated_form,
            start_label="  Grandma's house  ", end_label="  Work site  ",
        )
        assert response.status_code == 200
        assert await _fetch_labels(pool, trip_id) == ("Grandma's house", "Work site")
        assert await unrelated_snapshot() == baseline

        # Change: both labels -> different values.
        response = await _save(
            pool, trip_id, **unrelated_form, start_label="New start", end_label="New end",
        )
        assert response.status_code == 200
        assert await _fetch_labels(pool, trip_id) == ("New start", "New end")
        assert await unrelated_snapshot() == baseline

        # Clear: both labels -> blank.
        response = await _save(pool, trip_id, **unrelated_form, start_label="", end_label="")
        assert response.status_code == 200
        assert await _fetch_labels(pool, trip_id) == (None, None)
        assert await unrelated_snapshot() == baseline

    _run(scenario)


def test_edit_omitting_both_label_fields_preserves_stored_labels():
    async def scenario(pool):
        async with pool.connection() as conn:
            trip_id = await _insert_no_route_manual(conn)

        unrelated_form = {
            "category": "unclassified", "purpose": "Errand", "notes": "Some notes",
            "vehicle_id": "", "date": "2026-07-14", "start_time": "09:00",
            "end_time": "10:00", "distance": "1",
        }

        # Set a baseline pair of labels the way the add/change/clear test
        # does, so this test can prove an *omitted* field behaves
        # differently from a submitted-blank one, not just from having no
        # prior label at all.
        response = await _save(
            pool, trip_id, **unrelated_form, start_label="Home", end_label="Office",
        )
        assert response.status_code == 200
        assert await _fetch_labels(pool, trip_id) == ("Home", "Office")

        # Neither label field submitted at all: both must survive untouched,
        # unlike submitting them blank (the clear step of
        # test_edit_add_change_clear_labels_leaves_unrelated_fields_untouched).
        response = await _save(
            pool, trip_id, omit=("start_label", "end_label"), **unrelated_form,
        )
        assert response.status_code == 200
        assert await _fetch_labels(pool, trip_id) == ("Home", "Office")

    _run(scenario)


def test_edit_submitting_only_start_label_leaves_end_label_untouched():
    async def scenario(pool):
        async with pool.connection() as conn:
            trip_id = await _insert_no_route_manual(conn)

        unrelated_form = {
            "category": "unclassified", "purpose": "Errand", "notes": "Some notes",
            "vehicle_id": "", "date": "2026-07-14", "start_time": "09:00",
            "end_time": "10:00", "distance": "1",
        }

        response = await _save(
            pool, trip_id, **unrelated_form, start_label="Home", end_label="Office",
        )
        assert response.status_code == 200
        assert await _fetch_labels(pool, trip_id) == ("Home", "Office")

        # end_label omitted entirely (not submitted blank) must leave the
        # stored end_label alone while start_label still changes.
        response = await _save(
            pool, trip_id, omit=("end_label",), **unrelated_form, start_label="New start",
        )
        assert response.status_code == 200
        assert await _fetch_labels(pool, trip_id) == ("New start", "Office")

    _run(scenario)


def test_edit_submitting_only_end_label_leaves_start_label_untouched():
    async def scenario(pool):
        async with pool.connection() as conn:
            trip_id = await _insert_no_route_manual(conn)

        unrelated_form = {
            "category": "unclassified", "purpose": "Errand", "notes": "Some notes",
            "vehicle_id": "", "date": "2026-07-14", "start_time": "09:00",
            "end_time": "10:00", "distance": "1",
        }

        response = await _save(
            pool, trip_id, **unrelated_form, start_label="Home", end_label="Office",
        )
        assert response.status_code == 200
        assert await _fetch_labels(pool, trip_id) == ("Home", "Office")

        # start_label omitted entirely (not submitted blank) must leave the
        # stored start_label alone while end_label still changes.
        response = await _save(
            pool, trip_id, omit=("start_label",), **unrelated_form, end_label="New end",
        )
        assert response.status_code == 200
        assert await _fetch_labels(pool, trip_id) == ("Home", "New end")

    _run(scenario)


def test_edit_rejects_nonblank_label_for_detected_trip():
    async def scenario(pool):
        async with pool.connection() as conn:
            trip_id = await _insert_detected(conn)

        response = await _save(pool, trip_id, category="business", start_label="Somewhere")
        assert response.status_code == 200
        assert 'role="alert"' in response.body.decode()
        assert await _fetch_labels(pool, trip_id) == (None, None)

    _run(scenario)


def test_edit_rejects_nonblank_label_for_routed_manual_trip():
    async def scenario(pool):
        async with pool.connection() as conn:
            trip_id = await _insert_routed_manual(conn)

        response = await _save(
            pool, trip_id, category="unclassified", date="2026-07-14",
            start_time="09:00", end_time="10:00", distance="5", end_label="Somewhere",
        )
        assert response.status_code == 200
        assert 'role="alert"' in response.body.decode()
        assert await _fetch_labels(pool, trip_id) == (None, None)

    _run(scenario)


# --- Migration constraints against direct writes ----------------------------

def test_constraint_rejects_label_on_detected_trip():
    async def scenario(pool):
        async with pool.connection() as conn:
            with pytest.raises(errors.CheckViolation):
                await conn.execute(
                    "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
                    "start_label) VALUES ('phone', 'detected', now(), now(), 100, 'Somewhere')"
                )

    _run(scenario)


def test_constraint_rejects_label_alongside_saved_place_id():
    async def scenario(pool):
        async with pool.connection() as conn:
            place_id = await _insert_place(conn, "Home", 47.6, -122.3)
        async with pool.connection() as conn:
            with pytest.raises(errors.CheckViolation):
                await conn.execute(
                    "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
                    "start_place_id, start_label) VALUES "
                    "('manual', 'manual', now(), now(), 100, %s, 'Somewhere')",
                    (place_id,),
                )

    _run(scenario)


def test_constraint_rejects_label_alongside_geometry():
    async def scenario(pool):
        async with pool.connection() as conn:
            with pytest.raises(errors.CheckViolation):
                await conn.execute(
                    "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
                    "end_geom, end_label) VALUES ('manual', 'manual', now(), now(), 100, "
                    "ST_SetSRID(ST_MakePoint(-122.3, 47.6), 4326)::geography, 'Somewhere')"
                )

    _run(scenario)


def test_constraint_rejects_untrimmed_label():
    async def scenario(pool):
        async with pool.connection() as conn:
            with pytest.raises(errors.CheckViolation):
                await conn.execute(
                    "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
                    "start_label) VALUES ('manual', 'manual', now(), now(), 100, ' Somewhere ')"
                )

    _run(scenario)


def test_constraint_rejects_label_over_100_characters():
    async def scenario(pool):
        async with pool.connection() as conn:
            with pytest.raises(errors.CheckViolation):
                await conn.execute(
                    "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
                    "start_label) VALUES ('manual', 'manual', now(), now(), 100, %s)",
                    ("x" * 101,),
                )

    _run(scenario)


def test_constraint_accepts_label_at_exactly_100_characters():
    async def scenario(pool):
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
                "start_label) VALUES ('manual', 'manual', now(), now(), 100, %s)",
                ("x" * 100,),
            )

    _run(scenario)


# --- Shared display: TRIP_COLUMNS' effective endpoint name ------------------
# app/ui/_common.py resolves start_place_name/end_place_name as
# COALESCE(label, saved-place name) so archive, Dashboard, Review, detail, and
# both edit response cards get the label with no per-consumer change. These
# tests exercise that SQL expression directly, independent of the templates
# that consume it.

def test_effective_endpoint_name_prefers_labels_over_absent_place_and_address():
    async def scenario(pool):
        async with pool.connection() as conn:
            trip_id = await _insert_no_route_manual(conn)
            await conn.execute(
                "UPDATE trips SET start_label = %s, end_label = %s WHERE id = %s",
                ("Grandma's house", "Work site", trip_id),
            )

        row = await _fetch_trip_columns(pool, trip_id)
        assert row["start_place_name"] == "Grandma's house"
        assert row["end_place_name"] == "Work site"
        # Raw columns still come back unmodified: the trip-edit card's
        # prefill and eligibility checks (app/ui/trips.py's
        # _trip_edit_values) depend on the literal stored value, not the
        # resolved display name.
        assert row["start_label"] == "Grandma's house"
        assert row["end_label"] == "Work site"

    _run(scenario)


def test_effective_endpoint_name_uses_saved_place_name_when_no_label():
    async def scenario(pool):
        async with pool.connection() as conn:
            place_id = await _insert_place(conn, "Home", 47.6, -122.3)
            row = await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
                "start_place_id) VALUES ('manual', 'manual', now(), now(), 100, %s) "
                "RETURNING id",
                (place_id,),
            )
            trip_id = (await row.fetchone())[0]

        row = await _fetch_trip_columns(pool, trip_id)
        assert row["start_place_name"] == "Home"
        assert row["start_label"] is None

    _run(scenario)


def test_effective_endpoint_name_falls_back_to_address_when_no_label_or_place():
    async def scenario(pool):
        async with pool.connection() as conn:
            trip_id = await _insert_routed_manual(conn)
            # _insert_routed_manual's start point is (lon -122.3, lat 47.6).
            await _insert_geocode(conn, 47.6, -122.3, "123 Main St")

        row = await _fetch_trip_columns(pool, trip_id)
        assert row["start_place_name"] is None
        assert row["start_address"] == "123 Main St"

    _run(scenario)


def test_clearing_a_label_restores_the_address_fallback_without_deleting_geocode_cache():
    """Criterion 9: clearing a label must not leave the endpoint blank -- it
    has to fall back through the same saved-place/address/coordinate/`--`
    chain describe_endpoint always used, and it must never delete cached
    geocoding data to do it.

    A label can only exist while its endpoint's place id and geometry are
    both null (migrations/025_manual_trip_labels.sql), so a labeled endpoint
    never actually has cached geocoding of its own to restore. This proves
    the more general claim instead: once the label is cleared, nothing about
    this SQL expression (or the row) blocks that endpoint from later showing
    an address, and no address the endpoint ever needs is deleted or
    stranded along the way.
    """
    async def scenario(pool):
        async with pool.connection() as conn:
            trip_id = await _insert_no_route_manual(conn)
            await conn.execute(
                "UPDATE trips SET start_label = %s WHERE id = %s",
                ("Temporary label", trip_id),
            )

        row = await _fetch_trip_columns(pool, trip_id)
        assert row["start_place_name"] == "Temporary label"

        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE trips SET start_label = NULL WHERE id = %s", (trip_id,)
            )

        # NULL, not an empty string: an empty string would still be falsy
        # for describe_endpoint, but a stray "" surviving in this column
        # would be the kind of silent-corruption bug this test exists to
        # catch.
        row = await _fetch_trip_columns(pool, trip_id)
        assert row["start_place_name"] is None
        assert row["start_label"] is None

        # The label's own constraint permits geometry now that the label is
        # gone. Attaching it here (plus a matching cached address) proves
        # clearing the label left the address/coordinate fallback fully
        # reachable, and that this change never touches geocode_cache.
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE trips SET start_geom = "
                "ST_SetSRID(ST_MakePoint(-122.5678, 47.1234), 4326)::geography "
                "WHERE id = %s",
                (trip_id,),
            )
            await _insert_geocode(conn, 47.1234, -122.5678, "456 Reserved Ave")

        row = await _fetch_trip_columns(pool, trip_id)
        assert row["start_place_name"] is None
        assert row["start_address"] == "456 Reserved Ave"
        assert row["start_lat"] == pytest.approx(47.1234)
        assert row["start_lon"] == pytest.approx(-122.5678)

    _run(scenario)
