"""DB-backed tests for POST /settings/import/data (and its round trip with
GET /settings/export/data).

Same conventions as tests/test_vehicles_db.py: skipped unless
TEST_DATABASE_URL is set, a fresh schema per test via _reset_schema, a bare
FastAPI app driven through httpx.ASGITransport, and the CSRF token scraped
from a rendered page (app/ui.py's router is included here too, purely to
render /settings and get a real token -- the import route itself is
app/portable.py's).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from psycopg.rows import dict_row
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

from app.auth import AuthRedirect
from app.db import make_pool, run_migrations
from app.detector.core import Params
from app.detector.runner import DetectorRunner
from app.main import make_templates
from app.portable import SEEDED_TAG_RULES, SEEDED_VEHICLE, _tag_rule_sort_key
from app.portable import make_router as make_portable_router
from app.rates import YearRate, deduction
from app.ui import make_router as make_ui_router
from app.vehicles import create_vehicle

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

CSRF_RE = re.compile(r'X-CSRF-Token": "([^"]+)"')


async def _reset_schema(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await run_migrations(pool)


def _bare_app(pool, *, dev_no_auth: bool = True, portable_import_max_bytes: int = 50 * 1024 * 1024) -> FastAPI:
    app = FastAPI()
    app.state.pool = pool
    app.state.config = SimpleNamespace(
        dev_no_auth=dev_no_auth, display_tz=timezone.utc,
        geocode_provider=None, app_version="test", app_git_revision="test",
        portable_import_max_bytes=portable_import_max_bytes,
    )
    app.state.templates = make_templates(app.state.config)
    app.add_middleware(SessionMiddleware, secret_key="test-secret", same_site="lax", https_only=False)

    @app.exception_handler(AuthRedirect)
    async def _auth_redirect(request, exc):
        return RedirectResponse("/login", status_code=303)

    app.include_router(make_ui_router())
    app.include_router(make_portable_router())
    return app


def _scenario(coro_factory) -> None:
    async def run():
        pool = make_pool(TEST_DB)
        await pool.open(wait=True)
        try:
            await _reset_schema(pool)
            await coro_factory(pool)
        finally:
            await pool.close()

    asyncio.run(run())


async def _csrf(client: httpx.AsyncClient) -> str:
    page = await client.get("/settings")
    return CSRF_RE.search(page.text).group(1)


async def _export(client: httpx.AsyncClient) -> dict:
    response = await client.get("/settings/export/data")
    assert response.status_code == 200
    return response.json()


async def _import(
    client: httpx.AsyncClient, csrf: str, bundle: dict, *, dry_run: bool = False,
    override_csrf: str | None = None,
) -> httpx.Response:
    files = {"file": ("bundle.json", json.dumps(bundle).encode("utf-8"), "application/json")}
    data = {"csrf_token": override_csrf if override_csrf is not None else csrf}
    if dry_run:
        data["dry_run"] = "1"
    return await client.post("/settings/import/data", data=data, files=files)


def _minimal_bundle(schema_version: int) -> dict:
    # A bundle must carry at least one vehicle (normalize_bundle rejects an
    # empty vehicles list), so every test using this helper gets one by
    # default even though most of them don't otherwise care about vehicles.
    return {
        "format": "odograph-portable",
        "format_version": 1,
        "schema_version": schema_version,
        "exported_at": "2026-08-05T00:00:00+00:00",
        "vehicles": [{
            "$id": 1, "name": "Car", "make": None, "model": None, "plate": None,
            "is_default": True, "active": True,
        }],
        "places": [], "tag_rules": [], "mileage_rates": [],
        "trips": [], "expenses": [], "odometer_readings": [],
        "settings": {"auto_assign_default_vehicle": False},
    }


def _minimal_bundle_text(
    *, vehicles="[]", places="[]", tag_rules="[]", mileage_rates="[]",
    trips="[]", expenses="[]", odometer_readings="[]",
) -> str:
    """Builds bundle JSON as raw text rather than through json.dumps(dict),
    so a test can plant a bare NaN/Infinity token exactly as a hand-crafted
    or corrupted upload would carry it, independent of whatever this
    Python version's json.dumps happens to do with a float('nan') today.
    """
    return (
        '{"format":"odograph-portable","format_version":1,"schema_version":21,'
        '"exported_at":"2026-08-05T00:00:00+00:00",'
        f'"vehicles":{vehicles},"places":{places},"tag_rules":{tag_rules},'
        f'"mileage_rates":{mileage_rates},"trips":{trips},"expenses":{expenses},'
        f'"odometer_readings":{odometer_readings},'
        '"settings":{"auto_assign_default_vehicle":false}}'
    )


async def _import_raw(client: httpx.AsyncClient, csrf: str, text: str) -> httpx.Response:
    files = {"file": ("bundle.json", text.encode("utf-8"), "application/json")}
    return await client.post(
        "/settings/import/data", data={"csrf_token": csrf}, files=files,
    )


async def _row_counts(conn) -> dict[str, int]:
    counts = {}
    for table in ("vehicles", "places", "tag_rules", "trips", "expenses", "odometer_readings"):
        cur = await conn.execute(f"SELECT count(*) FROM {table}")
        counts[table] = (await cur.fetchone())[0]
    return counts


def _comparable(bundle: dict) -> dict:
    """Strips $id and resolves vehicle/place references to names, so a
    bundle exported before an import and one exported after it (necessarily
    carrying different real ids) can be compared by content.
    """
    vehicle_names = {v["$id"]: v["name"] for v in bundle["vehicles"]}
    place_names = {p["$id"]: p["name"] for p in bundle["places"]}

    def veh(v):
        return vehicle_names[v] if v is not None else None

    def plc(p):
        return place_names[p] if p is not None else None

    return {
        "vehicles": sorted(
            ({k: v for k, v in row.items() if k != "$id"} for row in bundle["vehicles"]),
            key=lambda r: r["name"],
        ),
        "places": sorted(
            ({k: v for k, v in row.items() if k != "$id"} for row in bundle["places"]),
            key=lambda r: r["name"],
        ),
        "tag_rules": sorted(
            (
                {**row, "a_place": plc(row["a_place"]), "b_place": plc(row["b_place"])}
                for row in bundle["tag_rules"]
            ),
            key=lambda r: (r["a_place"] or "", r["a_kind"] or "", r["b_place"] or "", r["b_kind"] or "", r["category"]),
        ),
        "mileage_rates": sorted(bundle["mileage_rates"], key=lambda r: r["year"]),
        "trips": sorted(
            (
                {
                    **{k: v for k, v in row.items() if k != "$id"},
                    "vehicle": veh(row["vehicle"]),
                    "start_place": plc(row["start_place"]),
                    "end_place": plc(row["end_place"]),
                }
                for row in bundle["trips"]
            ),
            key=lambda r: r["started_at"],
        ),
        "expenses": sorted(
            ({**row, "vehicle": veh(row["vehicle"])} for row in bundle["expenses"]),
            key=lambda r: (r["incurred_on"], r["amount"]),
        ),
        "odometer_readings": sorted(
            ({**row, "vehicle": veh(row["vehicle"])} for row in bundle["odometer_readings"]),
            key=lambda r: r["recorded_at"],
        ),
        "settings": bundle["settings"],
    }


def _business_deduction_total(bundle: dict) -> float:
    rates = {
        r["year"]: YearRate(r["rate_per_mi"], r["rate_h2_per_mi"], r["h2_start_month"])
        for r in bundle["mileage_rates"]
    }
    total = 0.0
    for trip in bundle["trips"]:
        if trip["category"] != "business":
            continue
        started = datetime.fromisoformat(trip["started_at"])
        d = deduction(trip["distance_m"], started.year, rates, started.month)
        if d is not None:
            total += d
    return round(total, 2)


async def _populate_source(pool) -> None:
    async with pool.connection() as conn:
        truck_id = await create_vehicle(conn, "Truck", make="Ford", model="F150")

        office_cur = await conn.execute(
            "INSERT INTO places (name, kind, geom, radius_m) "
            "VALUES ('Office', 'work', ST_SetSRID(ST_MakePoint(-122.30, 47.60), 4326)::geography, 100) "
            "RETURNING id",
        )
        office_id = (await office_cur.fetchone())[0]
        depot_cur = await conn.execute(
            "INSERT INTO places (name, kind, geom, radius_m) "
            "VALUES ('Depot', 'other', ST_SetSRID(ST_MakePoint(-122.35, 47.65), 4326)::geography, 200) "
            "RETURNING id",
        )
        depot_id = (await depot_cur.fetchone())[0]

        # A custom rule alongside the two seeded defaults.
        await conn.execute(
            "INSERT INTO tag_rules (a_place, b_kind, category) VALUES (%s, 'other', 'business')",
            (office_id,),
        )

        await conn.execute(
            "INSERT INTO trips (device, source, started_at, ended_at, distance_m, category, "
            " purpose, notes, vehicle_id, start_place_id, end_place_id, tag_source) "
            "VALUES ('phone1', 'detected', '2026-06-15T15:00:00+00:00', "
            " '2026-06-15T15:30:00+00:00', 16093.44, 'business', 'Client visit', 'parked on 3rd', "
            " %s, %s, %s, 'human')",
            (truck_id, office_id, depot_id),
        )
        await conn.execute(
            "INSERT INTO trips (device, source, started_at, ended_at, distance_m, category, vehicle_id) "
            "VALUES ('manual', 'manual', '2026-06-16T12:00:00+00:00', "
            " '2026-06-16T12:20:00+00:00', 8046.72, 'personal', %s)",
            (truck_id,),
        )
        await conn.execute(
            "INSERT INTO expenses (vehicle_id, incurred_on, category, amount, treatment, notes) "
            "VALUES (%s, '2026-06-01', 'fuel', 45.67, 'business_use_allocated', 'receipt 1')",
            (truck_id,),
        )
        await conn.execute(
            "INSERT INTO odometer_readings (vehicle_id, recorded_at, odometer_m, note) "
            "VALUES (%s, '2026-01-01T00:00:00+00:00', 160934.4, 'new year')",
            (truck_id,),
        )
        await conn.execute(
            "INSERT INTO mileage_rates (year, rate_per_mi) VALUES (2024, 0.6550)"
        )
        await conn.execute(
            "UPDATE app_settings SET auto_assign_default_vehicle = true WHERE id = 1"
        )


def test_round_trip_preserves_ledger_content_and_report_totals():
    async def run(pool):
        await _populate_source(pool)

        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            source_bundle = await _export(client)

        await _reset_schema(pool)

        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = await _csrf(client)
            response = await _import(client, csrf, source_bundle)
            assert response.status_code == 200
            body = response.json()
            assert body["ok"] is True
            assert body["dry_run"] is False
            assert body["counts"] == {
                "vehicles": 2, "places": 2, "tag_rules": 3, "mileage_rates": 3,
                "trips": 2, "expenses": 1, "odometer_readings": 1,
            }
            imported_bundle = await _export(client)

        assert _comparable(imported_bundle) == _comparable(source_bundle)
        assert _business_deduction_total(imported_bundle) == _business_deduction_total(source_bundle)

    _scenario(run)


def test_detector_run_after_import_does_not_delete_imported_trips():
    """Regression test for the bug where the detector's reconcile pass
    treated every imported source='detected' trip as stale and deleted it.

    detector_state.detector_version starts at 0 on a fresh instance, so an
    operator's first detector run after pointing a phone at the new instance
    is a full reprocess (DETECTOR_VERSION bump path) covering all of
    history. An imported trip has no backing points in this instance, so it
    can never match anything plan_reconcile derives from real points --
    without the `imported` column excluding it from that reconcile set, the
    whole imported detected-trip history would be wiped by this first run.
    """
    async def run(pool):
        bundle = {
            "format": "odograph-portable", "format_version": 1, "schema_version": 21,
            "exported_at": "2026-08-05T00:00:00+00:00",
            "vehicles": [{
                "$id": 1, "name": "Car", "make": None, "model": None, "plate": None,
                "is_default": True, "active": True,
            }],
            "places": [], "tag_rules": [], "mileage_rates": [],
            "trips": [
                {
                    "$id": 1, "device": "phone1", "source": "detected",
                    "started_at": "2025-08-06T15:00:00+00:00",
                    "ended_at": "2025-08-06T15:40:00+00:00",
                    "distance_m": 32000.0, "has_gap": False, "category": "business",
                    "purpose": "Client visit", "notes": "a year of history",
                    "vehicle": 1, "start_place": None, "end_place": None,
                    "tag_source": "human",
                },
                {
                    "$id": 2, "device": "phone1", "source": "manual",
                    "started_at": "2025-08-07T15:00:00+00:00",
                    "ended_at": "2025-08-07T15:20:00+00:00",
                    "distance_m": 8000.0, "has_gap": False, "category": "business",
                    "purpose": "Manual one", "notes": None,
                    "vehicle": 1, "start_place": None, "end_place": None,
                    "tag_source": "human",
                },
            ],
            "expenses": [], "odometer_readings": [],
            "settings": {"auto_assign_default_vehicle": False},
        }

        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = await _csrf(client)
            response = await _import(client, csrf, bundle)
        assert response.status_code == 200
        assert response.json()["ok"] is True

        # One ordinary day of new tracking for the same device: a stay, a
        # drive, a stay -- enough for the detector to assemble one new trip.
        base = datetime(2026, 8, 6, 16, 0, tzinfo=timezone.utc)
        rows = []
        for i in range(12):  # 60 min stay at origin
            rows.append((base + timedelta(minutes=i * 5), 47.60, -122.30))
        for i in range(1, 11):  # driving
            rows.append((base + timedelta(minutes=60 + i), 47.60 + 0.004 * i, -122.30))
        for i in range(12):  # 60 min stay at destination
            rows.append((base + timedelta(minutes=75 + i * 5), 47.64, -122.30))

        async with pool.connection() as conn:
            for t, lat, lon in rows:
                await conn.execute(
                    "INSERT INTO points (device, recorded_at, geom, accuracy_m) "
                    "VALUES ('phone1', %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, 5)",
                    (t, lon, lat),
                )

        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT source::text, category::text, purpose, notes FROM trips "
                "WHERE device = 'phone1' AND purpose = 'Client visit'"
            )
            imported_detected = await cur.fetchall()
            cur = await conn.execute(
                "SELECT source::text, category::text, purpose, notes FROM trips "
                "WHERE device = 'phone1' AND purpose = 'Manual one'"
            )
            imported_manual = await cur.fetchall()
            cur = await conn.execute(
                "SELECT count(*) FROM trips WHERE device = 'phone1' AND source = 'detected' "
                "AND purpose IS DISTINCT FROM 'Client visit'"
            )
            newly_detected_count = (await cur.fetchone())[0]

        assert imported_detected == [("detected", "business", "Client visit", "a year of history")]
        assert imported_manual == [("manual", "business", "Manual one", None)]
        assert newly_detected_count == 1

    _scenario(run)


def _two_imported_detected_trips_bundle() -> dict:
    return {
        "format": "odograph-portable", "format_version": 1, "schema_version": 21,
        "exported_at": "2026-08-05T00:00:00+00:00",
        "vehicles": [{
            "$id": 1, "name": "Car", "make": None, "model": None, "plate": None,
            "is_default": True, "active": True,
        }],
        "places": [], "tag_rules": [], "mileage_rates": [],
        "trips": [
            {
                "$id": 1, "device": "phone1", "source": "detected",
                "started_at": "2025-08-06T15:00:00+00:00",
                "ended_at": "2025-08-06T15:20:00+00:00",
                "distance_m": 16000.0, "has_gap": False, "category": "unclassified",
                "purpose": None, "notes": None,
                "vehicle": 1, "start_place": None, "end_place": None, "tag_source": None,
            },
            {
                "$id": 2, "device": "phone1", "source": "detected",
                "started_at": "2025-08-06T15:30:00+00:00",
                "ended_at": "2025-08-06T15:50:00+00:00",
                "distance_m": 16000.0, "has_gap": False, "category": "unclassified",
                "purpose": None, "notes": None,
                "vehicle": 1, "start_place": None, "end_place": None, "tag_source": None,
            },
        ],
        "expenses": [], "odometer_readings": [],
        "settings": {"auto_assign_default_vehicle": False},
    }


def test_merge_selected_refuses_imported_trips_with_clear_400_not_500():
    """Regression test: before app/ui.py's merge path excluded imported
    trips, _merge_trips_core would suppress a "stay" between two imported
    trips that has no points to back it (an imported trip carries no points
    in this instance), reprocess, then fail its own final lookup with an
    opaque HTTPException(500, "Merge did not produce the expected trip").
    The new early guard must refuse before any of that runs, and leave the
    imported trips and the (empty) trip_boundary_overrides table untouched.
    """
    async def run(pool):
        # _merge_trips_core reads request.app.state.detector_runner
        # unconditionally, even on a path that fails before ever using it --
        # _bare_app doesn't set one since no other route here needs it.
        app = _bare_app(pool)
        app.state.detector_runner = DetectorRunner(pool, Params())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = await _csrf(client)
            response = await _import(client, csrf, _two_imported_detected_trips_bundle())
            assert response.status_code == 200
            assert response.json()["ok"] is True

            async with pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT id FROM trips WHERE device = 'phone1' ORDER BY started_at"
                )
                trip_ids = [r[0] for r in await cur.fetchall()]
            assert len(trip_ids) == 2

            merge_response = await client.post(
                "/trips/merge_selected",
                data={"trip_ids": trip_ids, "category": "unclassified"},
                headers={"X-CSRF-Token": csrf},
            )

        assert merge_response.status_code == 400
        detail = merge_response.json()["detail"].lower()
        assert "import" in detail and "cannot be merged" in detail

        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM trip_boundary_overrides")
            assert (await cur.fetchone())[0] == 0
            cur = await conn.execute(
                "SELECT count(*) FROM trips WHERE device = 'phone1' AND source = 'detected'"
            )
            assert (await cur.fetchone())[0] == 2

    _scenario(run)


def test_merge_next_refuses_an_imported_trip_with_clear_400_not_500():
    """Same regression as the merge_selected case above, for the single-trip
    merge_next/merge_prev path (_merge_with_neighbor), which has its own
    early imported check separate from _merge_trips_core's.
    """
    async def run(pool):
        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = await _csrf(client)
            response = await _import(client, csrf, _two_imported_detected_trips_bundle())
            assert response.status_code == 200
            assert response.json()["ok"] is True

            async with pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT id FROM trips WHERE device = 'phone1' ORDER BY started_at"
                )
                trip_ids = [r[0] for r in await cur.fetchall()]

            merge_response = await client.post(
                f"/trips/{trip_ids[0]}/merge_next",
                data={"category": "unclassified"},
                headers={"X-CSRF-Token": csrf},
            )

        assert merge_response.status_code == 400
        detail = merge_response.json()["detail"].lower()
        assert "import" in detail and "cannot be merged" in detail

    _scenario(run)


def test_import_into_non_clean_target_is_refused_and_leaves_target_unchanged():
    async def run(pool):
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m, category) "
                "VALUES ('manual', 'manual', '2026-01-01T00:00:00+00:00', "
                " '2026-01-01T00:30:00+00:00', 1000, 'unclassified')"
            )
            before = await _row_counts(conn)

        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = await _csrf(client)
            response = await _import(client, csrf, _minimal_bundle(21))

        assert response.status_code == 409
        body = response.json()
        assert body["ok"] is False
        assert body["error"] == "target_not_clean"
        assert "trips" in body["conflicts"]

        async with pool.connection() as conn:
            assert await _row_counts(conn) == before

    _scenario(run)


def test_import_into_target_with_leftover_points_is_refused():
    """Regression test: an operator who pointed a phone at the new instance
    to confirm ingest works, then deleted the resulting trips from the UI,
    still has rows in points. Without counting this table, the import would
    proceed and the next detector pass would manufacture trips from those
    points alongside the imported ledger.
    """
    async def run(pool):
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO points (device, recorded_at, geom, accuracy_m) "
                "VALUES ('phone1', '2026-01-01T00:00:00+00:00', "
                " ST_SetSRID(ST_MakePoint(-122.3, 47.6), 4326)::geography, 5)"
            )

        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = await _csrf(client)
            response = await _import(client, csrf, _minimal_bundle(21))

        assert response.status_code == 409
        body = response.json()
        assert body["ok"] is False
        assert body["error"] == "target_not_clean"
        assert "points" in body["conflicts"]

    _scenario(run)


@pytest.mark.parametrize("mutate,expected_field", [
    (lambda b: b.__setitem__("format", "something-else"), "format"),
    (lambda b: b.__setitem__("format_version", 999), "format_version"),
])
def test_wrong_format_or_version_rejected(mutate, expected_field):
    async def run(pool):
        bundle = _minimal_bundle(21)
        mutate(bundle)

        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = await _csrf(client)
            response = await _import(client, csrf, bundle)

        assert response.status_code == 400
        body = response.json()
        assert body["ok"] is False
        assert body["error"] == "malformed_bundle"
        assert any(expected_field in issue for issue in body["issues"])

        async with pool.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM vehicles")).fetchone())[0] == 1

    _scenario(run)


def test_mismatched_schema_version_rejected():
    async def run(pool):
        bundle = _minimal_bundle(999)

        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = await _csrf(client)
            response = await _import(client, csrf, bundle)

        assert response.status_code == 409
        body = response.json()
        assert body["ok"] is False
        assert body["error"] == "schema_version_mismatch"

        async with pool.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM vehicles")).fetchone())[0] == 1

    _scenario(run)


def test_duplicate_mileage_rate_year_rejected():
    async def run(pool):
        bundle = _minimal_bundle(21)
        bundle["mileage_rates"] = [
            {"year": 2026, "rate_per_mi": 0.7, "rate_h2_per_mi": None, "h2_start_month": None},
            {"year": 2026, "rate_per_mi": 0.75, "rate_h2_per_mi": None, "h2_start_month": None},
        ]

        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = await _csrf(client)
            response = await _import(client, csrf, bundle)

        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "malformed_bundle"
        assert any("mileage_rates" in issue and "duplicate" in issue for issue in body["issues"])

        async with pool.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM mileage_rates")).fetchone())[0] == 2

    _scenario(run)


def test_duplicate_odometer_reading_vehicle_and_time_rejected():
    async def run(pool):
        bundle = _minimal_bundle(21)
        bundle["vehicles"] = [{
            "$id": 1, "name": "Car", "make": None, "model": None, "plate": None,
            "is_default": True, "active": True,
        }]
        reading = {
            "vehicle": 1, "recorded_at": "2026-01-01T00:00:00+00:00",
            "odometer_m": 1000.0, "note": None,
        }
        bundle["odometer_readings"] = [reading, dict(reading)]

        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = await _csrf(client)
            response = await _import(client, csrf, bundle)

        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "malformed_bundle"
        assert any("odometer_readings" in issue and "duplicate" in issue for issue in body["issues"])

        async with pool.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM odometer_readings")).fetchone())[0] == 0

    _scenario(run)


def test_duplicate_place_name_rejected():
    async def run(pool):
        bundle = _minimal_bundle(21)
        bundle["places"] = [
            {"$id": 1, "name": "Home", "kind": "home", "lat": 47.6, "lon": -122.3, "radius_m": 150},
            {"$id": 2, "name": "Home", "kind": "other", "lat": 47.7, "lon": -122.4, "radius_m": 150},
        ]

        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = await _csrf(client)
            response = await _import(client, csrf, bundle)

        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "malformed_bundle"
        assert any("places" in issue and "duplicate" in issue for issue in body["issues"])

        async with pool.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM places")).fetchone())[0] == 0

    _scenario(run)


def test_dangling_dollar_id_reference_rejected():
    async def run(pool):
        bundle = _minimal_bundle(21)
        bundle["trips"] = [{
            "$id": 1, "device": "phone1", "source": "manual",
            "started_at": "2026-01-01T00:00:00+00:00", "ended_at": "2026-01-01T00:30:00+00:00",
            "distance_m": 1000.0, "has_gap": False, "category": "unclassified",
            "purpose": None, "notes": None, "vehicle": 999, "start_place": None,
            "end_place": None, "tag_source": None,
        }]

        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = await _csrf(client)
            response = await _import(client, csrf, bundle)

        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "malformed_bundle"
        assert any("vehicle" in issue and "unknown" in issue for issue in body["issues"])

        async with pool.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM trips")).fetchone())[0] == 0

    _scenario(run)


NON_FINITE_BUNDLE_OVERRIDES = [
    (
        {"places": '[{"$id":1,"name":"Home","kind":"home","lat":47.6,"lon":-122.3,"radius_m":NaN}]'},
        "places[0].radius_m",
    ),
    (
        {"places": '[{"$id":1,"name":"Home","kind":"home","lat":NaN,"lon":-122.3,"radius_m":150}]'},
        "places[0].lat/lon",
    ),
    (
        {"trips": '[{"$id":1,"device":"phone1","source":"manual",'
                   '"started_at":"2026-01-01T00:00:00+00:00","ended_at":"2026-01-01T00:30:00+00:00",'
                   '"distance_m":Infinity,"has_gap":false,"category":"unclassified",'
                   '"purpose":null,"notes":null,"vehicle":null,"start_place":null,'
                   '"end_place":null,"tag_source":null}]'},
        "trips[0].distance_m",
    ),
    (
        {"mileage_rates": '[{"year":2026,"rate_per_mi":Infinity,"rate_h2_per_mi":null,'
                           '"h2_start_month":null}]'},
        "mileage_rates[0].rate_per_mi",
    ),
    (
        {
            "vehicles": '[{"$id":1,"name":"Car","make":null,"model":null,"plate":null,'
                        '"is_default":true,"active":true}]',
            "odometer_readings": '[{"vehicle":1,"recorded_at":"2026-01-01T00:00:00+00:00",'
                                  '"odometer_m":NaN,"note":null}]',
        },
        "odometer_readings[0].odometer_m",
    ),
]


@pytest.mark.parametrize("overrides,expected_issue_substring", NON_FINITE_BUNDLE_OVERRIDES)
def test_non_finite_float_rejected(overrides, expected_issue_substring):
    """json.loads accepts the bare NaN/Infinity tokens JSON itself doesn't
    allow, and a plain `< 0`/`<= 0` bound check lets a non-finite value
    straight through -- these must be caught by normalize_bundle before any
    query runs, same as any other malformed field.
    """
    async def run(pool):
        text = _minimal_bundle_text(**overrides)

        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = await _csrf(client)
            response = await _import_raw(client, csrf, text)

        assert response.status_code == 400
        body = response.json()
        assert body["ok"] is False
        assert body["error"] == "malformed_bundle"
        assert any(expected_issue_substring in issue for issue in body["issues"])

        async with pool.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM trips")).fetchone())[0] == 0
            assert (await (await conn.execute("SELECT count(*) FROM places")).fetchone())[0] == 0

    _scenario(run)


def test_dry_run_produces_same_summary_and_leaves_target_unchanged():
    async def run(pool):
        await _populate_source(pool)

        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            bundle = await _export(client)

        await _reset_schema(pool)

        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = await _csrf(client)

            async with pool.connection() as conn:
                before = await _row_counts(conn)

            dry_response = await _import(client, csrf, bundle, dry_run=True)
            assert dry_response.status_code == 200
            dry_body = dry_response.json()
            assert dry_body["ok"] is True
            assert dry_body["dry_run"] is True

            async with pool.connection() as conn:
                assert await _row_counts(conn) == before

            real_response = await _import(client, csrf, bundle)
            assert real_response.status_code == 200
            real_body = real_response.json()
            assert real_body["dry_run"] is False
            assert real_body["counts"] == dry_body["counts"]

    _scenario(run)


@pytest.mark.parametrize("bad_token", ["wrong-token", ""])
def test_import_requires_csrf_token(bad_token):
    async def run(pool):
        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _csrf(client)  # primes the session's real token
            response = await _import(
                client, "unused", _minimal_bundle(21), override_csrf=bad_token
            )
        assert response.status_code == 403

        async with pool.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM vehicles")).fetchone())[0] == 1

    _scenario(run)


def test_import_requires_authentication():
    async def run(pool):
        transport = httpx.ASGITransport(app=_bare_app(pool, dev_no_auth=False))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=False,
        ) as client:
            files = {"file": ("bundle.json", json.dumps(_minimal_bundle(21)).encode(), "application/json")}
            response = await client.post(
                "/settings/import/data", data={"csrf_token": "x"}, files=files,
            )
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    _scenario(run)


def test_oversized_upload_rejected_before_json_parsing():
    """Exercises the outer guard, _reject_oversized_import_upload: httpx
    computes an exact Content-Length for a `files=` upload like this one
    (the body is fully buffered up front, not streamed), so the guard sees a
    declared size over the limit and 413s before request.form() is ever
    called -- see the no-Content-Length variant below for the inner
    _read_capped_upload path this same 16-byte limit exercises instead.
    """
    async def run(pool):
        transport = httpx.ASGITransport(app=_bare_app(pool, portable_import_max_bytes=16))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = await _csrf(client)
            # Not valid JSON at all -- a 413 (not a 400 "invalid_json") proves
            # the size cap is enforced before json.loads ever runs on this.
            files = {"file": ("bundle.json", b"not json, and also way over sixteen bytes", "application/octet-stream")}
            response = await client.post(
                "/settings/import/data", data={"csrf_token": csrf}, files=files,
            )
        assert response.status_code == 413

    _scenario(run)


def test_oversized_upload_with_no_content_length_falls_through_to_inner_cap():
    """The outer guard can only act on a declared Content-Length. A request
    built from a generator body, as this one is, is the one shape httpx will
    send without computing one at all (real 'Transfer-Encoding: chunked'
    behavior, verified below) -- so this proves _read_capped_upload, the
    route's pre-existing defense in depth, still catches an oversized upload
    on its own when the outer guard has nothing to check.
    """
    async def run(pool):
        transport = httpx.ASGITransport(app=_bare_app(pool, portable_import_max_bytes=16))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = await _csrf(client)

            boundary = "----innercapboundary"
            body = (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="csrf_token"\r\n\r\n'
                f"{csrf}\r\n"
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="file"; filename="bundle.json"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
                "not json, and also way over sixteen bytes"
                f"\r\n--{boundary}--\r\n"
            ).encode("utf-8")

            async def body_gen():
                yield body

            request = client.build_request(
                "POST", "/settings/import/data",
                content=body_gen(),
                headers={"content-type": f"multipart/form-data; boundary={boundary}"},
            )
            assert "content-length" not in request.headers, (
                "test is only meaningful if httpx really sent no Content-Length"
            )
            response = await client.send(request)

        assert response.status_code == 413

    _scenario(run)


def test_seeded_constants_match_a_freshly_migrated_database():
    """SEEDED_VEHICLE/SEEDED_TAG_RULES are hand-copied from what
    008_vehicles.sql and 003_places.sql insert, with nothing tying the two
    together in code. Pins them against a real freshly migrated database
    (the same one _scenario's _reset_schema gives every other test in this
    file) so a future migration edit to either surfaces here, not as an
    opaque target_not_clean on every genuinely clean instance's first
    import.
    """
    async def run(pool):
        async with pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT name, make, model, plate, is_default, active FROM vehicles"
            )
            vehicles = await cur.fetchall()
            assert vehicles == [dict(SEEDED_VEHICLE)]

            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT a_place, a_kind::text AS a_kind, b_place, b_kind::text AS b_kind, "
                "category::text AS category FROM tag_rules"
            )
            rules = await cur.fetchall()
            expected_rules = [dict(r) for r in SEEDED_TAG_RULES]
            assert (
                sorted(rules, key=_tag_rule_sort_key)
                == sorted(expected_rules, key=_tag_rule_sort_key)
            )

    _scenario(run)
