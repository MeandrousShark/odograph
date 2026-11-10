"""GET /places/heat through the real application: account isolation, both
trip ends, the archive's filters, local-date range edges, stay clipping and
the response headers. The display timezone is America/Los_Angeles."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

import app.ui.places
from app.account_context import account_id
from app.place_heat import LAT_STEP, cell_centre, cell_index
from conftest import seed_tracking_device
from test_api_trips_db import OTHER, TEST_DB, TZ_NAME, _run

pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")
TZ = ZoneInfo(TZ_NAME)
MARCH = {"from": "2025-03-01", "to": "2025-03-31"}

# Raw points about a kilometre apart, so each lands in its own cell.
A, B, D, E = (47.6012, -122.3304), (47.6113, -122.3107), (47.6214, -122.2908), (47.6315, -122.2709)
F, G, H, I = (47.6416, -122.2510), (47.6517, -122.2311), (47.6618, -122.2112), (47.6719, -122.1913)
_C_ROW, _C_COL = cell_index(47.5911, -122.3505)
_C_LAT, _C_LON = cell_centre(_C_ROW, _C_COL)
# Two raw points a quarter cell either side of one cell's centre.
C1, C2 = (_C_LAT + LAT_STEP / 4, _C_LON), (_C_LAT - LAT_STEP / 4, _C_LON)


def _cell(point, weight):
    return [*cell_centre(*cell_index(*point)), weight]


def _cells(*cells):
    return sorted(cells)


def _wkt(point):
    return None if point is None else f"POINT({point[1]} {point[0]})"


async def _trip(conn, started_at, start, end, *, category="business", exclusion=None,
                vehicle_id=None) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m, "
        " category, exclusion, vehicle_id, start_geom, end_geom) "
        "VALUES (%s, 'manual', 'manual', %s, %s, 1000, %s, %s, %s, "
        " ST_GeomFromText(%s, 4326)::geography, ST_GeomFromText(%s, 4326)::geography) RETURNING id",
        (account_id(conn), started_at, started_at + timedelta(minutes=20), category, exclusion,
         vehicle_id, _wkt(start), _wkt(end)),
    )
    return (await cur.fetchone())[0]


async def _device(conn, label) -> int:
    device = await seed_tracking_device(conn, label)
    # Stored stays are bounded by the device's retained points.
    await conn.execute(
        "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, geom) "
        "SELECT %s, %s, 'phone', %s + n * interval '1 minute', "
        "ST_SetSRID(ST_MakePoint(-122.33, 47.60), 4326)::geography FROM generate_series(1, 20) n",
        (account_id(conn), device, _local(2025, 1, 1)),
    )
    return device


async def _stay(conn, device, started_at, ended_at, point) -> None:
    await conn.execute(
        "INSERT INTO stays (account_id, tracking_device_id, device, started_at, ended_at, "
        " centroid, point_count) VALUES (%s, %s, 'phone', %s, %s, "
        " ST_GeomFromText(%s, 4326)::geography, 3)",
        (account_id(conn), device, started_at, ended_at, _wkt(point)),
    )


async def _vehicle(conn, name) -> int:
    cur = await conn.execute(
        "INSERT INTO vehicles (account_id, name) VALUES (%s, %s) RETURNING id",
        (account_id(conn), name),
    )
    return (await cur.fetchone())[0]


async def _heat(state, **params):
    response = await state.client.get("/places/heat", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _local(*args):
    return datetime(*args, tzinfo=TZ)


def test_visits_count_both_ends_with_the_archive_filters():
    async def check(state):
        async with state.pools[state.owner].connection() as conn:
            car = await _vehicle(conn, "Heat Car")
            van = await _vehicle(conn, "Heat Van")
            await _trip(conn, _local(2025, 3, 10, 9), A, B, vehicle_id=car)
            await _trip(conn, _local(2025, 3, 11, 9), C1, C2, category="personal")
            await _trip(conn, _local(2025, 3, 12, 9), A, B, exclusion="not_my_vehicle", vehicle_id=car)
            await _trip(conn, _local(2025, 3, 13, 9), None, D, category="unclassified",
                        vehicle_id=van)
            await _trip(conn, _local(2025, 3, 14, 9), D, None, category="unclassified",
                        exclusion="not_deductible", vehicle_id=van)
        async with state.pools[OTHER].connection() as conn:
            await _trip(conn, _local(2025, 3, 10, 9), A, E)
            await _trip(conn, _local(2025, 3, 11, 9), E, E, category="personal")

        everything = _cells(_cell(A, 1), _cell(B, 1), [_C_LAT, _C_LON, 2], _cell(D, 2))
        response = await state.client.get("/places/heat", params=MARCH)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store, private"
        assert response.headers["content-type"] == "application/json"
        assert response.json() == {
            "mode": "visits", "from": "2025-03-01", "to": "2025-03-31",
            "cells": everything, "max": 2,
        }
        # A single endpoint comes back at its cell's centre, never its raw value.
        assert _cell(A, 1)[:2] != list(A)

        assert (await _heat(state, **MARCH, category="business"))["cells"] == _cells(
            _cell(A, 1), _cell(B, 1))
        assert (await _heat(state, **MARCH, category="personal"))["cells"] == [[_C_LAT, _C_LON, 2]]
        assert (await _heat(state, **MARCH, vehicle="none"))["cells"] == [[_C_LAT, _C_LON, 2]]
        assert (await _heat(state, **MARCH, vehicle=str(van)))["cells"] == [_cell(D, 2)]
        assert (await _heat(state, **MARCH, vehicle=str(car)))["cells"] == _cells(
            _cell(A, 1), _cell(B, 1))
        for junk in ({"category": "nonsense"}, {"vehicle": "nonsense"}, {"category": "", "vehicle": ""}):
            assert (await _heat(state, **MARCH, **junk))["cells"] == everything
        empty = await _heat(state, **MARCH, vehicle=str(car), category="personal")
        assert empty["cells"] == [] and empty["max"] == 0
    _run(check)


def test_visit_range_edges_are_local_dates():
    async def check(state):
        async with state.pools[state.owner].connection() as conn:
            await _trip(conn, _local(2025, 3, 1, 0, 5), F, None)       # in, 08:05Z
            await _trip(conn, _local(2025, 2, 28, 23, 50), G, None)    # out, 2025-03-01 07:50Z
            await _trip(conn, _local(2025, 3, 31, 23, 30), H, None)    # in, 2025-04-01 06:30Z
            await _trip(conn, _local(2025, 4, 1, 0, 10), I, None)      # out, 2025-04-01 07:10Z
        body = await _heat(state, **MARCH)
        assert body["cells"] == _cells(_cell(F, 1), _cell(H, 1))
        assert (await _heat(state, **{"from": "2025-03-31", "to": "2025-03-31"}))["cells"] == [_cell(H, 1)]
    _run(check)


def test_time_mode_clips_stays_to_the_range_and_ignores_trip_filters():
    async def check(state):
        async with state.pools[state.owner].connection() as conn:
            device = await _device(conn, "heat phone")
            car = await _vehicle(conn, "Heat Car")
            # Straddles the start: 22:00 to 02:00 local, 2 h inside.
            await _stay(conn, device, _local(2025, 2, 28, 22), _local(2025, 3, 1, 2), A)
            # Straddles the end: 21:00 to 03:00 local, 3 h inside.
            await _stay(conn, device, _local(2025, 3, 31, 21), _local(2025, 4, 1, 3), B)
            # Two stays in one cell sum.
            await _stay(conn, device, _local(2025, 3, 10, 9), _local(2025, 3, 10, 10), C1)
            await _stay(conn, device, _local(2025, 3, 11, 9), _local(2025, 3, 11, 9, 30), C2)
            # Before the range in local time, inside it in UTC.
            await _stay(conn, device, _local(2025, 2, 28, 20), _local(2025, 2, 28, 23, 59), D)
            # Zero length.
            await _stay(conn, device, _local(2025, 3, 15, 9), _local(2025, 3, 15, 9), E)
            await _trip(conn, _local(2025, 3, 10, 9), F, F, vehicle_id=car)
        async with state.pools[OTHER].connection() as conn:
            other_device = await _device(conn, "other phone")
            await _stay(conn, other_device, _local(2025, 3, 5, 0), _local(2025, 3, 5, 10), A)
            await _stay(conn, other_device, _local(2025, 3, 6, 0), _local(2025, 3, 6, 10), G)

        expected = {
            "mode": "time", "from": "2025-03-01", "to": "2025-03-31",
            "cells": _cells(_cell(A, 7200), _cell(B, 10800), [_C_LAT, _C_LON, 5400]),
            "max": 10800,
        }
        response = await state.client.get("/places/heat", params={**MARCH, "mode": "time"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store, private"
        assert response.json() == expected
        for filters in ({"category": "business"}, {"vehicle": "none"}, {"vehicle": str(car)}):
            assert await _heat(state, **MARCH, mode="time", **filters) == expected
    _run(check)


def test_defaults_validation_and_the_places_context(monkeypatch):
    contexts = []
    original = app.ui.places.render_page

    async def recording(request, template, context, *args, **kwargs):
        contexts.append(context)
        return await original(request, template, context, *args, **kwargs)
    monkeypatch.setattr(app.ui.places, "render_page", recording)

    async def check(state):
        today = datetime.now(TZ).date()
        async with state.pools[state.owner].connection() as conn:
            car = await _vehicle(conn, "Heat Car")
            await _trip(conn, _local(today.year, today.month, today.day, 0, 1), A, None)
            await _trip(conn, _local(today.year - 1, 12, 31, 23, 59), B, None)
        body = await _heat(state)
        assert body == {
            "mode": "visits", "from": f"{today.year}-01-01", "to": today.isoformat(),
            "cells": [_cell(A, 1)], "max": 1,
        }
        for mode in ("visits", "time"):
            widest = await _heat(state, mode=mode, **{"from": "0001-01-01", "to": "9998-12-31"})
            assert widest["to"] == "9998-12-31"
        for params in ({"mode": "bogus"}, {"from": "2025-03-02", "to": "2025-03-01"},
                       {"from": "2025-3-1"}, {"to": "9999-12-31"}):
            response = await state.client.get("/places/heat", params=params)
            assert response.status_code == 422, params
            assert response.headers["cache-control"] == "no-store, private"

        page = await state.client.get("/places")
        assert page.status_code == 200
        context = contexts[-1]
        assert context["heat_from"] == f"{today.year}-01-01"
        assert context["heat_to"] == today.isoformat()
        assert car in {vehicle["id"] for vehicle in context["vehicles"]}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=state.app),
                                     base_url="https://test") as anonymous:
            response = await anonymous.get("/places/heat")
        assert response.status_code != 200 and "cells" not in response.text
    _run(check)
