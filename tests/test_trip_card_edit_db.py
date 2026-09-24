"""DB-backed contracts for trip-card fragments and atomic editing."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.db import make_pool
from app.account_context import account_id
from personal_support import fixture_device, personal_request
from app.main import make_templates
from app.rates import METERS_PER_MILE
from app.ui import make_router
from conftest import reset_account_db


TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")
TZ = ZoneInfo("America/Los_Angeles")
USER = {"sub": "test"}
# A dashboard_week value that would be visible verbatim in the response body
# if a route ever forgot to normalize it before handing it to a template.
MALFORMED_WEEK = '2026-07-13"><script>alert(1)</script>'


def _endpoint(path: str, method: str):
    for route in make_router().routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"route missing: {method} {path}")


CARD = _endpoint("/trips/{trip_id}/card", "GET")
EDIT = _endpoint("/trips/{trip_id}/edit", "GET")
SAVE = _endpoint("/trips/{trip_id}/edit", "POST")
TAG = _endpoint("/trips/{trip_id}/tag", "POST")
NOTES = _endpoint("/trips/{trip_id}/notes", "POST")
PURPOSE = _endpoint("/trips/{trip_id}/purpose", "POST")
VEHICLE = _endpoint("/trips/{trip_id}/vehicle", "POST")
EXCLUSION = _endpoint("/trips/{trip_id}/exclusion", "POST")


def _request(pool):
    config = SimpleNamespace(display_tz=TZ, app_version="test")
    return personal_request(SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
        )),
        session={"csrf": "test"},
        headers={},
    ))


async def _save(request, trip_id, **overrides):
    values = {
        "category": "unclassified", "purpose": "", "notes": "", "vehicle_id": "",
        "date": "", "start_time": "", "end_time": "", "distance": "", "exclusion": "",
        "dashboard_week": "",
    }
    values.update(overrides)
    return await SAVE(request, trip_id, user=USER, **values)


async def _insert_manual(conn, **overrides):
    values = {
        "started_at": datetime(2026, 7, 14, 16, tzinfo=timezone.utc),
        "ended_at": datetime(2026, 7, 14, 17, tzinfo=timezone.utc),
        "distance_m": 1000,
        "category": "unclassified",
        "purpose": None,
        "notes": None,
        "vehicle_id": None,
    }
    values.update(overrides)
    row = await conn.execute(
        "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m, category,"
        " purpose, notes, vehicle_id) VALUES (%s, 'manual', 'manual', %s, %s, %s, %s, %s, %s, %s) "
        "RETURNING id",
        (account_id(conn), *(tuple(values.values())),),
    )
    return (await row.fetchone())[0]


async def _scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        request = _request(pool)

        async with pool.connection() as conn:
            active_id = (await (await conn.execute(
                "INSERT INTO vehicles (account_id, name) VALUES (%s, 'Active car') RETURNING id", (account_id(conn),)
            )).fetchone())[0]
            inactive_id = (await (await conn.execute(
                "INSERT INTO vehicles (account_id, name, active) VALUES (%s, 'Retired car', false) "
                "RETURNING id", (account_id(conn),)
            )).fetchone())[0]
            manual_id = await _insert_manual(conn, vehicle_id=inactive_id)
            detected_id = (await (await conn.execute(
                "INSERT INTO trips (account_id, tracking_device_id, device, source, started_at, "
                "ended_at, start_geom, end_geom, distance_m, point_count, path, has_gap, category, "
                "purpose, notes, vehicle_id, detector_version, snap_status) VALUES (%s, %s, "
                "'phone', 'detected', '2026-07-14T18:00:00Z', '2026-07-14T19:00:00Z', "
                "ST_SetSRID(ST_MakePoint(-122.3, 47.6), 4326)::geography, "
                "ST_SetSRID(ST_MakePoint(-122.2, 47.7), 4326)::geography, 3200, 44, "
                "ST_GeomFromText('LINESTRING(-122.3 47.6,-122.2 47.7)', 4326), true, 'business', "
                "'Original purpose', 'Original notes', %s, 2, 'failed') RETURNING id",
                (account_id(conn), await fixture_device(conn, 'phone'), active_id,),
            )).fetchone())[0]

        card = await CARD(request, detected_id, USER)
        card_body = card.body.decode()
        assert f'<article id="trip-{detected_id}"' in card_body
        # The archive row's overflow menu no longer carries its own "View
        # details" item; the whole row is a stretched link to the same
        # destination instead (covered directly in test_trip_row_template.py).
        assert "View details" not in card_body
        assert f'<a class="trip-archive-row-link" href="/trips/{detected_id}">' in card_body
        assert "View route" not in card_body
        dashboard_card = await CARD(request, detected_id, USER, "2026-07-13")
        dashboard_card_body = dashboard_card.body.decode()
        assert 'class="dashboard-trip-row' in dashboard_card_body
        assert 'class="card trip-card' not in dashboard_card_body
        assert 'hx-get="/trips/%s/edit?dashboard_week=2026-07-13"' % detected_id in dashboard_card_body
        malformed_card_body = (
            await CARD(request, detected_id, USER, MALFORMED_WEEK)
        ).body.decode()
        today_iso = datetime.now(TZ).date().isoformat()
        assert MALFORMED_WEEK not in malformed_card_body
        assert 'hx-get="/trips/%s/edit?dashboard_week=%s"' % (detected_id, today_iso) \
            in malformed_card_body
        manual_edit = await EDIT(request, manual_id, USER)
        manual_body = manual_edit.body.decode()
        assert 'name="date"' in manual_body
        assert "Retired car (inactive)" in manual_body
        detected_edit = await EDIT(request, detected_id, USER)
        assert 'name="date"' not in detected_edit.body.decode()
        dashboard_edit = await EDIT(request, detected_id, USER, "2026-07-13")
        dashboard_edit_body = dashboard_edit.body.decode()
        assert 'name="dashboard_week" value="2026-07-13"' in dashboard_edit_body
        assert 'hx-get="/trips/%s/card?dashboard_week=2026-07-13"' % detected_id in dashboard_edit_body
        malformed_edit_body = (
            await EDIT(request, detected_id, USER, MALFORMED_WEEK)
        ).body.decode()
        today_iso = datetime.now(TZ).date().isoformat()
        assert MALFORMED_WEEK not in malformed_edit_body
        assert 'name="dashboard_week" value="%s"' % today_iso in malformed_edit_body
        for endpoint in (CARD, EDIT):
            with pytest.raises(HTTPException) as exc:
                await endpoint(request, 999999, USER)
            assert exc.value.status_code == 404

        async with pool.connection() as conn:
            before = await (await conn.execute(
                "SELECT device, source::text, started_at, ended_at, distance_m, point_count, "
                "ST_AsText(start_geom::geometry), ST_AsText(end_geom::geometry), ST_AsText(path), "
                "has_gap, detector_version, snap_status::text FROM trips WHERE id = %s",
                (detected_id,),
            )).fetchone()
        response = await _save(
            request, detected_id, category="personal", purpose="  New purpose  ",
            notes="  New notes  ", vehicle_id=str(active_id), date="bad",
            start_time="bad", end_time="bad", distance="nan", exclusion="not_deductible",
        )
        assert response.status_code == 200 and "trip-archive-row" in response.body.decode()
        async with pool.connection() as conn:
            after = await (await conn.execute(
                "SELECT device, source::text, started_at, ended_at, distance_m, point_count, "
                "ST_AsText(start_geom::geometry), ST_AsText(end_geom::geometry), ST_AsText(path), "
                "has_gap, detector_version, snap_status::text FROM trips WHERE id = %s",
                (detected_id,),
            )).fetchone()
            human = await (await conn.execute(
                "SELECT category::text, exclusion::text, purpose, notes, vehicle_id, tag_source::text "
                "FROM trips WHERE id = %s", (detected_id,),
            )).fetchone()
        assert after == before
        assert human == (
            "personal", "not_deductible", "New purpose", "New notes", active_id, "human"
        )

        dashboard_response = await _save(
            request, detected_id, category="business", purpose="Dashboard edit",
            notes="Dashboard notes", vehicle_id=str(active_id), exclusion="",
            dashboard_week="2026-07-13",
        )
        assert dashboard_response.status_code == 200
        assert dashboard_response.headers["hx-refresh"] == "true"
        dashboard_body = dashboard_response.body.decode()
        assert 'class="dashboard-trip-row' in dashboard_body
        assert 'class="card trip-card' not in dashboard_body

        malformed_save_response = await _save(
            request, detected_id, category="business", purpose="Dashboard edit",
            notes="Dashboard notes", vehicle_id=str(active_id), exclusion="",
            dashboard_week=MALFORMED_WEEK,
        )
        malformed_save_body = malformed_save_response.body.decode()
        today_iso = datetime.now(TZ).date().isoformat()
        assert MALFORMED_WEEK not in malformed_save_body
        assert 'hx-get="/trips/%s/edit?dashboard_week=%s"' % (detected_id, today_iso) \
            in malformed_save_body

        response = await EXCLUSION(request, detected_id, "not_my_vehicle", USER)
        assert "Not one of my vehicles" in response.body.decode()
        await EXCLUSION(request, detected_id, "", USER)
        async with pool.connection() as conn:
            assert await (await conn.execute(
                "SELECT exclusion FROM trips WHERE id = %s", (detected_id,)
            )).fetchone() == (None,)
        with pytest.raises(HTTPException) as exc:
            await EXCLUSION(request, detected_id, "bogus", USER)
        assert exc.value.status_code == 400

        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE trips SET category = 'unclassified', purpose = NULL, tag_source = 'rule' "
                "WHERE id = %s", (detected_id,)
            )
        # Notes and vehicle aren't tax-classification decisions, so editing
        # them stays narrow: no tag_source write, unlike purpose/tag below.
        for response in (
            await NOTES(request, detected_id, "Narrow notes", USER),
            await VEHICLE(request, detected_id, str(active_id), USER),
        ):
            assert f'<article id="trip-{detected_id}"' in response.body.decode()
        async with pool.connection() as conn:
            narrow_owner = await (await conn.execute(
                "SELECT tag_source::text FROM trips WHERE id = %s", (detected_id,)
            )).fetchone()
        assert narrow_owner == ("rule",)

        # Purpose is a human decision the autotagger must never silently
        # revert, so editing it claims ownership the same way tag/save/
        # batch-update already do.
        purpose_response = await PURPOSE(request, detected_id, "Narrow purpose", USER)
        assert f'<article id="trip-{detected_id}"' in purpose_response.body.decode()
        async with pool.connection() as conn:
            purpose_owner = await (await conn.execute(
                "SELECT tag_source::text FROM trips WHERE id = %s", (detected_id,)
            )).fetchone()
        assert purpose_owner == ("human",)

        async with pool.connection() as conn:
            await conn.execute("UPDATE trips SET tag_source = 'rule' WHERE id = %s", (detected_id,))
        tagged = await TAG(request, detected_id, "business", USER)
        assert f'<article id="trip-{detected_id}"' in tagged.body.decode()
        async with pool.connection() as conn:
            tagged_owner = await (await conn.execute(
                "SELECT tag_source::text FROM trips WHERE id = %s", (detected_id,)
            )).fetchone()
        assert tagged_owner == ("human",)

        await _save(
            request, manual_id, category="business", purpose="Delivery", notes="night run",
            vehicle_id="", date="2026-07-14", start_time="23:30", end_time="01:00",
            distance="8.5",
        )
        async with pool.connection() as conn:
            manual = await (await conn.execute(
                "SELECT device, source::text, started_at AT TIME ZONE %s, ended_at AT TIME ZONE %s, "
                "distance_m, start_geom IS NULL, end_geom IS NULL, path IS NULL, detector_version, "
                "tag_source::text FROM trips WHERE id = %s",
                (TZ.key, TZ.key, manual_id),
            )).fetchone()
        assert manual[:4] == (
            "manual", "manual", datetime(2026, 7, 14, 23, 30), datetime(2026, 7, 15, 1, 0)
        )
        assert manual[4] == pytest.approx(8.5 * METERS_PER_MILE)
        assert manual[5:] == (True, True, True, 0, "human")

        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE trips SET tag_source = 'rule', category = 'business', purpose = 'Keep' "
                "WHERE id = %s", (manual_id,)
            )
        await _save(
            request, manual_id, category="business", purpose="Keep", notes="notes only",
            vehicle_id=str(active_id), date="2026-07-14", start_time="23:30",
            end_time="01:00", distance="8.5",
        )
        async with pool.connection() as conn:
            owner = await (await conn.execute(
                "SELECT tag_source::text FROM trips WHERE id = %s", (manual_id,)
            )).fetchone()
        assert owner == ("rule",)

        async with pool.connection() as conn:
            snapshot = await (await conn.execute(
                "SELECT started_at, ended_at, distance_m, category::text, purpose, notes, vehicle_id, "
                "tag_source::text FROM trips WHERE id = %s", (manual_id,)
            )).fetchone()
        invalid_cases = [
            {"category": "bogus"},
            {"vehicle_id": "999999"},
            {"date": "bad"},
            {"distance": "inf"},
        ]
        valid_manual = {
            "category": "business", "purpose": "Keep", "notes": "notes only",
            "vehicle_id": str(active_id), "date": "2026-07-14", "start_time": "23:30",
            "end_time": "01:00", "distance": "8.5",
        }
        for invalid in invalid_cases:
            response = await _save(request, manual_id, **{**valid_manual, **invalid})
            assert response.status_code == 200
            assert 'role="alert"' in response.body.decode()
            async with pool.connection() as conn:
                current = await (await conn.execute(
                    "SELECT started_at, ended_at, distance_m, category::text, purpose, notes, vehicle_id, "
                    "tag_source::text FROM trips WHERE id = %s", (manual_id,)
                )).fetchone()
            assert current == snapshot

        dashboard_invalid = await _save(
            request, manual_id, **{
                **valid_manual, "date": "bad", "dashboard_week": "2026-07-13",
            }
        )
        dashboard_invalid_body = dashboard_invalid.body.decode()
        assert dashboard_invalid.status_code == 200
        assert 'role="alert"' in dashboard_invalid_body
        assert 'name="dashboard_week" value="2026-07-13"' in dashboard_invalid_body
        assert 'hx-get="/trips/%s/card?dashboard_week=2026-07-13"' % manual_id in dashboard_invalid_body

        malformed_invalid = await _save(
            request, manual_id, **{
                **valid_manual, "date": "bad", "dashboard_week": MALFORMED_WEEK,
            }
        )
        malformed_invalid_body = malformed_invalid.body.decode()
        today_iso = datetime.now(TZ).date().isoformat()
        assert malformed_invalid.status_code == 200
        assert 'role="alert"' in malformed_invalid_body
        assert MALFORMED_WEEK not in malformed_invalid_body
        assert 'name="dashboard_week" value="%s"' % today_iso in malformed_invalid_body
        assert 'hx-get="/trips/%s/card?dashboard_week=%s"' % (manual_id, today_iso) \
            in malformed_invalid_body

        async with pool.admin_pool.connection() as conn:
            await conn.execute(
                "CREATE FUNCTION remove_edit_vehicle() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN DELETE FROM vehicles WHERE id = NEW.vehicle_id; RETURN NEW; END $$"
            )
            await conn.execute(
                "CREATE TRIGGER remove_edit_vehicle BEFORE UPDATE ON trips FOR EACH ROW "
                "WHEN (NEW.vehicle_id IS DISTINCT FROM OLD.vehicle_id) EXECUTE FUNCTION remove_edit_vehicle()"
            )
        async with pool.connection() as conn:
            race_vehicle = (await (await conn.execute(
                "INSERT INTO vehicles (account_id, name) VALUES (%s, 'Race car') RETURNING id", (account_id(conn),)
            )).fetchone())[0]
        try:
            response = await _save(request, manual_id, **{**valid_manual, "vehicle_id": str(race_vehicle)})
            assert 'role="alert"' in response.body.decode()
            async with pool.connection() as conn:
                current = await (await conn.execute(
                    "SELECT started_at, ended_at, distance_m, category::text, purpose, notes, vehicle_id, "
                    "tag_source::text FROM trips WHERE id = %s", (manual_id,)
                )).fetchone()
                race_still_exists = await (await conn.execute(
                    "SELECT 1 FROM vehicles WHERE id = %s", (race_vehicle,)
                )).fetchone()
            assert current == snapshot
            assert race_still_exists == (1,)
        finally:
            # The reset between tests truncates data but leaves schema
            # objects alone (see tests/conftest.py), so a trigger/function
            # created here to simulate the race must be dropped here too,
            # not left for a later test's reset to clean up.
            async with pool.admin_pool.connection() as conn:
                await conn.execute("DROP TRIGGER remove_edit_vehicle ON trips")
                await conn.execute("DROP FUNCTION remove_edit_vehicle()")
    finally:
        await raw_pool.close()


def test_card_fragments_and_atomic_source_specific_editing():
    asyncio.run(_scenario())
