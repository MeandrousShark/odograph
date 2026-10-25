"""API v1 trip writes through the real application: the same durable rows as
the web routes they mirror, partial-update semantics, idempotent creates,
account isolation and bearer-first admission."""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

from app import api_trips, capacity_routes
from app.account_context import AccountPool, AccountPrincipal, account_id
from app.application_roles import application_role_pools
from app.api_errors import ERRORS
from app.api_grants import format_instant
from app.trip_writes import ManualTrip, create_manual_trip, manual_trip_digest
from api_trip_support import BASE
from conftest import seed_tracking_device
from test_api_trips_db import OTHER, TEST_DB, _bearer, _envelope, _insert, _run, _seed, _sql, _without_ids

pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")
FIXTURES = Path(__file__).parent / "fixtures" / "api_v1"
# Columns whose values identify a row rather than describe the trip.
IDENTITY = {"id", "account_id", "created_at", "updated_at", "started_at", "ended_at",
            "tracking_device_id", "idempotency_key", "idempotency_digest"}


def _key() -> str:
    return str(uuid.uuid4())


async def _web_headers(state) -> dict:
    page = (await state.client.get("/trips")).text
    return {
        "X-CSRF-Token": re.search(r'"X-CSRF-Token": "([^"]+)"', page).group(1),
        "X-Odograph-Account": re.search(r'"X-Odograph-Account": "([^"]+)"', page).group(1),
        "HX-Request": "true",
    }


async def _web(state, path, data):
    return await state.client.post(path, data=data, headers=await _web_headers(state))


async def _api(state, method, path, account=None, body=None, headers=None, **kwargs):
    merged = {**_bearer(state.tokens[account or state.owner]), **(headers or {})}
    if body is not None:
        kwargs["json"] = body
    return await state.client.request(method, path, headers=merged, **kwargs)


async def _row(state, trip_id) -> dict | None:
    async with state.raw.connection() as conn:
        row = await (await conn.execute(
            "SELECT row_to_json(t) FROM trips t WHERE id = %s", (trip_id,))).fetchone()
    return None if row is None else row[0]


def _durable(row: dict) -> dict:
    return {key: value for key, value in row.items() if key not in IDENTITY}


async def _usage(state, account=None) -> int:
    async with state.raw.connection() as conn:
        return (await (await conn.execute(
            "SELECT actual_bytes FROM account_usage WHERE account_id = %s",
            (account or state.owner,))).fetchone())[0]


async def _vehicles(state, account=None) -> list[int]:
    async with state.pools[account or state.owner].connection() as conn:
        rows = await (await conn.execute(
            "SELECT id FROM vehicles WHERE account_id = %s ORDER BY id", (account_id(conn),))).fetchall()
    return [row[0] for row in rows]


async def _second_vehicle(state, account=None) -> int:
    async with state.pools[account or state.owner].connection() as conn:
        return (await (await conn.execute(
            "INSERT INTO vehicles (account_id, name) VALUES (%s, 'Van') RETURNING id",
            (account_id(conn),))).fetchone())[0]


async def _device(conn) -> int:
    """The account's settled test device: its retained points arrived days ago."""
    row = await (await conn.execute(
        "SELECT id FROM tracking_devices WHERE account_id = %s AND label = 'settled'",
        (account_id(conn),))).fetchone()
    if row:
        return row[0]
    device = await seed_tracking_device(conn, "settled")
    await conn.execute(
        "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, received_at, geom) "
        "SELECT %s, %s, 'phone', %s + n * interval '1 minute', now() - interval '3 days', "
        "ST_SetSRID(ST_MakePoint(-122.33, 47.60), 4326)::geography FROM generate_series(1, 60) n",
        (account_id(conn), device, BASE - timedelta(days=1)))
    return device


async def _detected(state, started_at, account=None, **values) -> int:
    """A settled detected trip with a route and endpoints."""
    async with state.pools[account or state.owner].connection() as conn:
        columns = {"tracking_device_id": await _device(conn), "device": "phone", "source": "detected",
                   "started_at": started_at, "ended_at": started_at + timedelta(minutes=20),
                   "distance_m": 2400.0, "category": "unclassified", "point_count": 3, **values}
        cur = await conn.execute(
            f"INSERT INTO trips (account_id, {', '.join(columns)}, path, start_geom, end_geom) "
            f"VALUES (%s, {', '.join(['%s'] * len(columns))}, "
            "ST_GeomFromText('LINESTRING(-122.33 47.60, -122.31 47.62)', 4326), "
            "ST_GeomFromText('POINT(-122.33 47.60)', 4326)::geography, "
            "ST_GeomFromText('POINT(-122.31 47.62)', 4326)::geography) RETURNING id",
            [account_id(conn), *columns.values()])
        return (await cur.fetchone())[0]


class _Commits:
    """Requests whose handler reported a committed write."""

    def __init__(self, monkeypatch):
        self.paths: list[str] = []
        original = capacity_routes.mark_committed_write

        def record(request):
            self.paths.append(request.url.path)
            original(request)

        for module in ("app.api_trips", "app.ui.trips", "app.ui.manual"):
            monkeypatch.setattr(f"{module}.mark_committed_write", record)

    def take(self) -> list[str]:
        taken, self.paths = self.paths, []
        return taken


def _web_edit_form(row: dict, **overrides) -> dict:
    form = {"category": row["category"], "purpose": row["purpose"] or "", "notes": row["notes"] or "",
            "vehicle_id": "" if row["vehicle_id"] is None else str(row["vehicle_id"]),
            "exclusion": row["exclusion"] or "", "date": "", "start_time": "", "end_time": "",
            "distance": ""}
    form.update(overrides)
    return form


def test_patch_leaves_the_same_rows_as_the_web_full_edit(monkeypatch):
    async def check(state):
        commits = _Commits(monkeypatch)
        van = await _second_vehicle(state)
        web = await _detected(state, BASE, tag_source="rule", purpose="Old")
        api = await _detected(state, BASE + timedelta(hours=1), tag_source="rule", purpose="Old")
        usage = await _usage(state)
        response = await _web(state, f"/trips/{web}/edit", _web_edit_form(
            await _row(state, web), category="business", purpose="  Client ", notes=" Note ",
            vehicle_id=str(van), exclusion="not_deductible"))
        assert response.status_code == 200 and response.headers["x-archive-write"] == "success"
        web_cost = await _usage(state) - usage
        before, usage = await _row(state, api), await _usage(state)
        response = await _api(state, "PATCH", f"/api/v1/trips/{api}", body={
            "category": "business", "purpose": "  Client ", "notes": " Note ", "vehicle_id": van,
            "exclusion": "not_deductible"})
        assert response.status_code == 200, response.text
        web_row, api_row = await _row(state, web), await _row(state, api)
        assert _durable(api_row) == _durable(web_row)
        assert api_row["tag_source"] == "human" and api_row["purpose"] == "Client"
        assert api_row["updated_at"] != before["updated_at"]
        assert commits.take() == [f"/trips/{web}/edit", f"/api/v1/trips/{api}"]
        assert await _usage(state) - usage == web_cost
        body = response.json()
        assert (body["category"], body["purpose"], body["notes"], body["vehicle_id"], body["exclusion"]) == (
            "business", "Client", "Note", van, "not_deductible")

        # Manual trips: times, distance and labels, as the web form posts them.
        web = await _insert(state, BASE + timedelta(days=1), start_label="A", end_label="B")
        api = await _insert(state, BASE + timedelta(days=1), start_label="A", end_label="B")
        tz_start = "2026-04-15T09:00:00-07:00"
        response = await _web(state, f"/trips/{web}/edit", {
            **_web_edit_form(await _row(state, web)), "date": "2026-04-15", "start_time": "09:00",
            "end_time": "09:45", "distance": "2", "start_label": " Depot ", "end_label": ""})
        assert response.status_code == 200
        response = await _api(state, "PATCH", f"/api/v1/trips/{api}", body={
            "started_at": tz_start, "ended_at": "2026-04-15T16:45:00Z", "distance_m": 2 * 1609.344,
            "start_label": " Depot ", "end_label": None})
        assert response.status_code == 200, response.text
        web_row, api_row = await _row(state, web), await _row(state, api)
        assert _durable(api_row) == _durable(web_row)
        assert (api_row["started_at"], api_row["ended_at"]) == (web_row["started_at"], web_row["ended_at"])
        assert api_row["tag_source"] is None and api_row["start_label"] == "Depot"
        assert api_row["end_label"] is None
        assert commits.take() == [f"/trips/{web}/edit", f"/api/v1/trips/{api}"]
    _run(check)


def test_an_unchanged_patch_writes_nothing_while_the_web_edit_still_saves(monkeypatch):
    async def check(state):
        commits = _Commits(monkeypatch)
        trip = await _detected(state, BASE, tag_source="rule", category="business", purpose="Visit",
                               notes="Kept")
        manual = await _insert(state, BASE + timedelta(days=1), purpose="Errand", start_label="Home")
        stored, manual_stored = await _row(state, trip), await _row(state, manual)
        usage = await _usage(state)
        for trip_id, body in (
            (trip, {}),
            (trip, {"category": "business", "purpose": " Visit ", "notes": "Kept", "vehicle_id": None,
                    "exclusion": None}),
            (manual, {"started_at": format_instant(BASE + timedelta(days=1)),
                      "ended_at": "2026-04-15T15:05:00+00:00", "distance_m": 100,
                      "start_label": "Home", "end_label": "  ", "purpose": "Errand"}),
        ):
            response = await _api(state, "PATCH", f"/api/v1/trips/{trip_id}", body=body)
            assert response.status_code == 200, response.text
            assert response.json()["id"] == trip_id
        assert await _row(state, trip) == stored
        assert await _row(state, manual) == manual_stored
        assert await _usage(state) == usage
        assert commits.take() == []
        # The web full edit keeps refreshing updated_at on every save, and
        # keeps the stored tag source when nothing it owns changed.
        response = await _web(state, f"/trips/{trip}/edit", _web_edit_form(stored))
        assert response.status_code == 200
        saved = await _row(state, trip)
        assert saved["updated_at"] != stored["updated_at"]
        assert {**saved, "updated_at": None} == {**stored, "updated_at": None}
        assert commits.take() == [f"/trips/{trip}/edit"]
    _run(check)


def test_only_a_category_or_purpose_change_makes_the_tag_human():
    async def check(state):
        van = await _second_vehicle(state)
        trip = await _detected(state, BASE, tag_source="rule", category="business", purpose="Visit")
        for body in ({"notes": "n"}, {"vehicle_id": van}, {"exclusion": "not_my_vehicle"},
                     {"category": "business"}, {"purpose": "Visit"}, {"notes": None, "exclusion": None}):
            assert (await _api(state, "PATCH", f"/api/v1/trips/{trip}", body=body)).status_code == 200
            assert (await _row(state, trip))["tag_source"] == "rule", body
        assert (await _api(state, "PATCH", f"/api/v1/trips/{trip}", body={"purpose": "Other"})).status_code == 200
        assert (await _row(state, trip))["tag_source"] == "human"
        other = await _detected(state, BASE + timedelta(hours=1), tag_source="rule")
        assert (await _api(state, "PATCH", f"/api/v1/trips/{other}",
                           body={"category": "personal"})).status_code == 200
        assert (await _row(state, other))["tag_source"] == "human"
        cleared = await _detected(state, BASE + timedelta(hours=2), tag_source="rule", purpose="P")
        assert (await _api(state, "PATCH", f"/api/v1/trips/{cleared}",
                           body={"purpose": "  "})).status_code == 200
        row = await _row(state, cleared)
        assert (row["purpose"], row["tag_source"]) == (None, "human")
    _run(check)


def test_patch_refuses_what_the_trip_cannot_hold():
    async def check(state):
        ids = await _seed(state)
        await _seed(state, OTHER)
        their_vehicle = (await _vehicles(state, OTHER))[0]
        detected, manual, routed = ids["gapped"], ids["manual"], ids["routed_manual"]
        before = {trip_id: await _row(state, trip_id) for trip_id in (detected, manual, routed)}
        cases = [
            (detected, {"category": None}, {"category": "This value is not valid."}),
            (detected, {"category": "bogus"}, {"category": "This value is not valid."}),
            (detected, {"exclusion": ""}, {"exclusion": "This value is not valid."}),
            (detected, {"unknown": 1}, {"unknown": "This value is not valid."}),
            (detected, {"vehicle_id": "1"}, {"vehicle_id": "This value is not valid."}),
            (detected, {"vehicle_id": True}, {"vehicle_id": "This value is not valid."}),
            (detected, {"vehicle_id": their_vehicle}, {"vehicle_id": "Choose a vehicle that still exists."}),
            (detected, {"vehicle_id": 2**70}, {"vehicle_id": "Choose a vehicle that still exists."}),
            (detected, {"notes": "a\x00b"}, {"notes": "This value is not valid."}),
            (detected, {"started_at": "2026-04-14T15:00:00Z"},
             {"started_at": "This can only be changed on a manual trip."}),
            (detected, {"distance_m": 5}, {"distance_m": "This can only be changed on a manual trip."}),
            (detected, {"start_label": None}, {"start_label": "This can only be changed on a manual trip."}),
            (detected, {"end_label": "Home"},
             {"end_label": "Location names are only available for a trip with no route."}),
            (routed, {"start_label": "Home"},
             {"start_label": "Location names are only available for a trip with no route."}),
            (manual, {"started_at": None, "ended_at": None, "distance_m": None},
             {"started_at": "This value is not valid.", "ended_at": "This value is not valid.",
              "distance_m": "This value is not valid."}),
            (manual, {"ended_at": "2026-04-14T19:59:00Z"}, {"ended_at": "The end must be after the start."}),
            (manual, {"started_at": "2026-04-14T20:20:00Z"}, {"ended_at": "The end must be after the start."}),
            (manual, {"started_at": "2026-04-14T20:00:00"}, {"started_at": "Enter a time with its offset."}),
            (manual, {"started_at": "2026-04-14"}, {"started_at": "Enter a time with its offset."}),
            (manual, {"started_at": 1776193200}, {"started_at": "This value is not valid."}),
            (manual, {"distance_m": 0}, {"distance_m": "Distance must be a positive finite number."}),
            (manual, {"distance_m": "5"}, {"distance_m": "This value is not valid."}),
            (manual, {"start_label": "x" * 101}, {"start_label": "Keep it to 100 characters or fewer."}),
        ]
        for trip_id, body, fields in cases:
            response = await _api(state, "PATCH", f"/api/v1/trips/{trip_id}", body=body)
            assert response.status_code == 422, (body, response.text)
            assert response.json() == {"error": {"code": "validation_failed",
                                                 "message": ERRORS["validation_failed"][1],
                                                 "fields": fields}}, body
        for content in ("[]", "not json", '{"distance_m": NaN}'):
            response = await _api(state, "PATCH", f"/api/v1/trips/{manual}", content=content,
                                  headers={"Content-Type": "application/json"})
            assert response.status_code == 422, content
        assert {trip_id: await _row(state, trip_id) for trip_id in before} == before
    _run(check)


def test_patch_and_delete_never_reach_another_account(monkeypatch):
    async def check(state):
        commits = _Commits(monkeypatch)
        theirs = await _seed(state, OTHER)
        rows = {trip_id: await _row(state, trip_id) for trip_id in theirs.values()}
        for trip_id in theirs.values():
            response = await _api(state, "PATCH", f"/api/v1/trips/{trip_id}", body={"notes": "mine"})
            assert (response.status_code, response.json()) == (404, _envelope("not_found"))
            response = await _api(state, "DELETE", f"/api/v1/trips/{trip_id}")
            assert (response.status_code, response.json()) == (404, _envelope("not_found"))
        for path in ("/api/v1/trips/0", f"/api/v1/trips/{2**63}", "/api/v1/trips/999999"):
            assert (await _api(state, "PATCH", path, body={"notes": "x"})).status_code == 404
            assert (await _api(state, "DELETE", path)).status_code == 404
        assert {trip_id: await _row(state, trip_id) for trip_id in rows} == rows
        async with state.raw.connection() as conn:
            assert await (await conn.execute("SELECT count(*) FROM trip_boundary_overrides")).fetchone() == (0,)
        assert commits.take() == []
        # The owner of those trips can still edit them.
        trip_id = theirs["manual"]
        response = await _api(state, "PATCH", f"/api/v1/trips/{trip_id}", OTHER, body={"notes": "theirs"})
        assert response.status_code == 200 and response.json()["notes"] == "theirs"
    _run(check)


def test_delete_is_source_aware_like_the_web(monkeypatch):
    async def check(state):
        commits = _Commits(monkeypatch)
        web_detected = await _detected(state, BASE)
        api_detected = await _detected(state, BASE + timedelta(hours=1))
        web_manual = await _insert(state, BASE + timedelta(hours=2), notes="n")
        api_manual = await _insert(state, BASE + timedelta(hours=3), notes="n")
        usage = await _usage(state)
        response = await _web(state, f"/trips/{web_detected}/delete", {"fragment": "true"})
        assert response.status_code == 200
        web_detected_cost = usage - await _usage(state)
        usage = await _usage(state)
        response = await _api(state, "DELETE", f"/api/v1/trips/{api_detected}")
        assert (response.status_code, response.content) == (204, b"")
        assert usage - await _usage(state) == web_detected_cost
        async with state.raw.connection() as conn:
            overrides = await (await conn.execute(
                "SELECT kind::text, device, range_start, range_end FROM trip_boundary_overrides "
                "ORDER BY range_start")).fetchall()
        assert [(kind, device) for kind, device, *_ in overrides] == [("discard", "phone")] * 2
        assert [(start, end) for *_, start, end in overrides] == [
            (BASE, BASE + timedelta(minutes=20)),
            (BASE + timedelta(hours=1), BASE + timedelta(hours=1, minutes=20))]
        usage = await _usage(state)
        assert (await _web(state, f"/trips/{web_manual}/delete", {})).status_code == 204
        web_manual_cost = usage - await _usage(state)
        usage = await _usage(state)
        assert (await _api(state, "DELETE", f"/api/v1/trips/{api_manual}")).status_code == 204
        assert usage - await _usage(state) == web_manual_cost
        for trip_id in (web_detected, api_detected, web_manual, api_manual):
            assert await _row(state, trip_id) is None
        async with state.raw.connection() as conn:
            assert await (await conn.execute("SELECT count(*) FROM trip_boundary_overrides")).fetchone() == (2,)
        assert commits.take() == [f"/trips/{web_detected}/delete", f"/api/v1/trips/{api_detected}",
                                  f"/trips/{web_manual}/delete", f"/api/v1/trips/{api_manual}"]
        assert (await _api(state, "DELETE", f"/api/v1/trips/{api_manual}")).status_code == 404
    _run(check)


def test_delete_refuses_a_trip_still_in_progress(monkeypatch):
    async def check(state):
        commits = _Commits(monkeypatch)
        async with state.pools[state.owner].connection() as conn:
            device = await seed_tracking_device(conn, "car")
            await conn.execute(
                "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, received_at, geom) "
                "SELECT %s, %s, 'car', now() - n * interval '1 minute', now(), "
                "ST_SetSRID(ST_MakePoint(-122.3, 47.6), 4326)::geography FROM generate_series(1, 20) n",
                (account_id(conn), device))
            live = (await (await conn.execute(
                "INSERT INTO trips (account_id, tracking_device_id, device, source, started_at, ended_at, "
                "distance_m) VALUES (%s, %s, 'car', 'detected', now() - interval '10 minutes', "
                "now() - interval '1 minute', 700) RETURNING id", (account_id(conn), device))).fetchone())[0]
        before = await _row(state, live)
        response = await _api(state, "DELETE", f"/api/v1/trips/{live}")
        assert (response.status_code, response.json()) == (409, _envelope("trip_in_progress"))
        assert await _row(state, live) == before
        assert commits.take() == []
    _run(check)


def _create_body(**overrides) -> dict:
    body = {"started_at": "2026-04-20T16:00:00Z", "ended_at": "2026-04-20T16:30:00Z",
            "distance_m": 4.2 * 1609.344, "category": "business", "purpose": " Visit ",
            "notes": " note ", "exclusion": "not_my_vehicle", "start_label": " A ", "end_label": "B"}
    body.update(overrides)
    return body


async def _create(state, body, key=None, account=None):
    headers = {} if key is False else {"Idempotency-Key": _key() if key is None else key}
    return await _api(state, "POST", "/api/v1/trips", account, body=body, headers=headers)


def test_create_leaves_the_same_row_and_charge_as_the_web_form(monkeypatch):
    async def check(state):
        commits = _Commits(monkeypatch)
        vehicle = (await _vehicles(state))[0]
        usage = await _usage(state)
        response = await _web(state, "/trips/manual", {
            "date": "2026-04-20", "start_time": "09:00", "end_time": "09:30", "distance": "4.2",
            "category": "business", "purpose": " Visit ", "notes": " note ", "vehicle_id": str(vehicle),
            "exclusion": "not_my_vehicle", "start_label": " A ", "end_label": "B"})
        assert response.status_code == 204
        web_cost = await _usage(state) - usage
        async with state.raw.connection() as conn:
            web_id = (await (await conn.execute("SELECT max(id) FROM trips")).fetchone())[0]
        usage = await _usage(state)
        key = _key()
        response = await _create(state, _create_body(vehicle_id=vehicle), key)
        assert response.status_code == 201, response.text
        body = response.json()
        assert response.headers["location"] == f"/api/v1/trips/{body['id']}"
        assert "idempotent-replayed" not in response.headers
        assert await _usage(state) - usage == web_cost
        web_row, api_row = await _row(state, web_id), await _row(state, body["id"])
        assert _durable(api_row) == _durable(web_row)
        assert (api_row["started_at"], api_row["ended_at"]) == (web_row["started_at"], web_row["ended_at"])
        assert web_row["idempotency_key"] is None and web_row["idempotency_digest"] is None
        assert api_row["idempotency_key"] == key and api_row["idempotency_digest"] is not None
        assert api_row["tag_source"] is None and api_row["source"] == "manual"
        assert commits.take() == ["/trips/manual", "/api/v1/trips"]
        detail = (await _api(state, "GET", f"/api/v1/trips/{body['id']}")).json()
        assert body == detail
        assert (body["purpose"], body["notes"], body["start"]["label"], body["route_raw"]) == (
            "Visit", "note", "A", None)
    _run(check)


def test_create_replays_a_repeat_and_refuses_a_reused_key(monkeypatch):
    async def check(state):
        commits = _Commits(monkeypatch)
        key = _key()
        first = await _create(state, _create_body(), key)
        assert first.status_code == 201
        commits.take()
        rows = await _count(state)
        # The same request, spelled differently, is a replay of the current record.
        await _sql(state, "UPDATE trips SET notes = 'edited later' WHERE id = %s", (first.json()["id"],))
        usage = await _usage(state)
        for body in (_create_body(), _create_body(started_at="2026-04-20T09:00:00-07:00",
                                                  purpose="Visit", end_label=" B ")):
            replay = await _create(state, body, key.upper())
            assert replay.status_code == 200, replay.text
            assert replay.headers["idempotent-replayed"] == "true"
            assert "location" not in replay.headers
            assert replay.json() == {**first.json(), "notes": "edited later"}
        assert await _count(state) == rows
        assert await _usage(state) == usage
        assert commits.take() == []
        stored = await _row(state, first.json()["id"])
        for body in (_create_body(distance_m=1.0), _create_body(purpose=None), {
                k: v for k, v in _create_body().items() if k != "category"}):
            response = await _create(state, body, key)
            assert (response.status_code, response.json()) == (409, _envelope("idempotency_key_reused"))
        assert await _row(state, first.json()["id"]) == stored
        assert await _count(state) == rows
        # Keys belong to the account: another account may use the same one.
        assert (await _create(state, _create_body(), key, OTHER)).status_code == 201
        # Deleting the record frees its key.
        assert (await _api(state, "DELETE", f"/api/v1/trips/{first.json()['id']}")).status_code == 204
        again = await _create(state, _create_body(), key)
        assert again.status_code == 201 and again.json()["id"] != first.json()["id"]
    _run(check)


async def _count(state) -> int:
    async with state.raw.connection() as conn:
        return (await (await conn.execute("SELECT count(*) FROM trips")).fetchone())[0]


def test_create_without_a_valid_key_or_body_claims_nothing(monkeypatch):
    async def check(state):
        commits = _Commits(monkeypatch)
        response = await _create(state, _create_body(), False)
        assert response.status_code == 422
        assert response.json()["error"]["fields"] == {"Idempotency-Key": "This field is required."}
        for bad in ("not-a-uuid", "{" + _key() + "}", _key().replace("-", ""), "urn:uuid:" + _key(), ""):
            response = await _create(state, _create_body(), bad)
            assert response.status_code == 422, bad
            assert response.json()["error"]["fields"] == {"Idempotency-Key": "This value is not valid."}
        key = _key()
        their_vehicle = (await _vehicles(state, OTHER))[0]
        for body, fields in (
            (_create_body(distance_m=0), {"distance_m": "Distance must be a positive finite number."}),
            (_create_body(ended_at="2026-04-20T16:00:00Z"), {"ended_at": "The end must be after the start."}),
            (_create_body(vehicle_id=their_vehicle), {"vehicle_id": "Choose a vehicle that still exists."}),
            (_create_body(start_label="x" * 101), {"start_label": "Keep it to 100 characters or fewer."}),
            (_create_body(category=None), {"category": "This value is not valid."}),
            (_create_body(path=[]), {"path": "This value is not valid."}),
            ({"started_at": "2026-04-20T16:00:00Z"}, {"ended_at": "This field is required.",
                                                      "distance_m": "This field is required."}),
        ):
            response = await _create(state, body, key)
            assert response.status_code == 422, body
            assert response.json()["error"]["fields"] == fields
        assert await _count(state) == 0
        assert commits.take() == []
        assert (await _create(state, _create_body(), key)).status_code == 201
        # A plain create needs no optional fields.
        minimal = await _create(state, {"started_at": "2026-04-21T16:00:00.5Z",
                                        "ended_at": "2026-04-21T16:30:00Z", "distance_m": 10})
        assert minimal.status_code == 201
        assert (minimal.json()["category"], minimal.json()["started_at"]) == (
            "unclassified", "2026-04-21T16:00:00.500000Z")
    _run(check)


def test_concurrent_creates_with_one_key_make_one_trip():
    async def check(state):
        # Through the application, admission runs one foreground request per
        # account at a time, so the second of two simultaneous creates is
        # either a replay or busy, and a retry of a busy one replays.
        key = _key()
        responses = await asyncio.gather(*(_create(state, _create_body(), key) for _ in range(2)))
        statuses = sorted(r.status_code for r in responses)
        assert statuses in ([200, 201], [201, 503]), statuses
        if statuses == [201, 503]:
            busy = next(r for r in responses if r.status_code == 503)
            assert busy.json()["error"]["code"] == "busy"
            assert (await _create(state, _create_body(), key)).status_code == 200
        assert await _count(state) == 1

        # In the database, two transactions claiming one key leave one trip.
        key = uuid.uuid4()
        trip = ManualTrip(started_at=BASE, ended_at=BASE + timedelta(minutes=5), distance_m=10.0)

        async with application_role_pools(TEST_DB) as roles:
            pool = AccountPool(roles.runtime, AccountPrincipal(state.owner, True, 1))

            async def claim():
                async with pool.connection() as conn:
                    outcome = await create_manual_trip(conn, trip, key, manual_trip_digest(trip))
                    await asyncio.sleep(0.2)
                    return outcome
            outcomes = await asyncio.gather(claim(), claim())
        assert sorted(created for _, created in outcomes) == [False, True]
        assert len({trip_id for trip_id, _ in outcomes}) == 1
        assert await _count(state) == 2

        # A create racing an uncommitted claim waits for it, then replays it
        # once it commits, or inserts once it rolls back.
        for commit, expected in ((True, 200), (False, 201)):
            key = _key()
            trip = ManualTrip(
                started_at=BASE, ended_at=BASE + timedelta(minutes=30), distance_m=4.2 * 1609.344,
                category="business", exclusion="not_my_vehicle", purpose="Visit", notes="note",
                start_label="A", end_label="B")
            holder = await psycopg.AsyncConnection.connect(TEST_DB)
            try:
                await holder.execute(
                    "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m, "
                    "category, exclusion, purpose, notes, start_label, end_label, idempotency_key, "
                    "idempotency_digest) VALUES (%s, 'manual', 'manual', %s, %s, %s, 'business', "
                    "'not_my_vehicle', 'Visit', 'note', 'A', 'B', %s, %s)",
                    (state.owner, trip.started_at, trip.ended_at, trip.distance_m, key,
                     manual_trip_digest(trip)))
                task = asyncio.create_task(_create(state, _create_body(
                    started_at=format_instant(BASE), ended_at=format_instant(BASE + timedelta(minutes=30))), key))
                await asyncio.sleep(0.5)
                assert not task.done()
                await (holder.commit() if commit else holder.rollback())
                response = await task
            finally:
                await holder.close()
            assert response.status_code == expected, response.text
            async with state.raw.connection() as conn:
                assert await (await conn.execute(
                    "SELECT count(*) FROM trips WHERE idempotency_key = %s", (key,))).fetchone() == (1,)
    _run(check)


def test_a_full_account_gets_storage_full_and_claims_nothing(monkeypatch):
    async def check(state):
        commits = _Commits(monkeypatch)
        trip = await _insert(state, BASE, OTHER)
        before = await _row(state, trip)
        async with state.raw.connection() as conn:
            used = (await (await conn.execute(
                "SELECT actual_bytes + reserved_bytes FROM account_usage WHERE account_id = %s",
                (OTHER,))).fetchone())[0]
            await conn.execute(
                "UPDATE storage_grants SET limit_mode = 'custom', account_limit_bytes = %s, "
                "raw_limit_bytes = NULL, enhancement_limit_bytes = NULL WHERE account_id = %s", (used, OTHER))
        key = _key()
        response = await _create(state, _create_body(), key, OTHER)
        assert (response.status_code, response.json()) == (409, _envelope("storage_full"))
        response = await _api(state, "PATCH", f"/api/v1/trips/{trip}", OTHER, body={"notes": "x" * 500})
        assert (response.status_code, response.json()) == (409, _envelope("storage_full"))
        assert await _row(state, trip) == before
        assert commits.take() == []
        async with state.raw.connection() as conn:
            await conn.execute("UPDATE storage_grants SET limit_mode = 'default', account_limit_bytes = NULL "
                               "WHERE account_id = %s", (OTHER,))
        assert (await _create(state, _create_body(), key, OTHER)).status_code == 201
    _run(check)


def test_writes_authenticate_before_reading_the_body():
    async def check(state):
        trip = await _insert(state, BASE)
        large = json.dumps({"notes": "x" * (2 * 1024 * 1024)})
        bad = {"Authorization": "Bearer not-a-token", "Content-Type": "application/json"}
        for method, path, extra in (("PATCH", f"/api/v1/trips/{trip}", {}),
                                    ("POST", "/api/v1/trips", {"Idempotency-Key": _key()}),
                                    ("DELETE", f"/api/v1/trips/{trip}", {})):
            response = await state.client.request(method, path, content=large, headers={**bad, **extra})
            assert (response.status_code, response.json()["error"]["code"]) == (401, "invalid_token"), method
            # The browser session never authenticates an API write.
            response = await state.client.request(method, path, content="{}", headers={
                "Content-Type": "application/json", **extra, **await _web_headers(state)})
            assert response.status_code == 401, method
        for method, path, extra in (("PATCH", f"/api/v1/trips/{trip}", {}),
                                    ("POST", "/api/v1/trips", {"Idempotency-Key": _key()})):
            response = await _api(state, method, path, content=large,
                                  headers={"Content-Type": "application/json", **extra})
            assert (response.status_code, response.json()["error"]["code"]) == (413, "payload_too_large")
        assert await _row(state, trip) is not None
    _run(check)


def test_web_tag_purpose_and_review_buttons_still_set_human_unconditionally(monkeypatch):
    async def check(state):
        commits = _Commits(monkeypatch)
        tagged, purposed, reviewed = [
            await _detected(state, BASE + timedelta(hours=h), tag_source="rule", category="business",
                            purpose="Same") for h in range(3)]
        stored = {trip_id: await _row(state, trip_id) for trip_id in (tagged, purposed, reviewed)}
        assert (await _web(state, f"/trips/{tagged}/tag", {"category": "business"})).status_code == 200
        assert (await _web(state, f"/trips/{purposed}/purpose", {"purpose": " Same "})).status_code == 200
        response = await _web(state, f"/review/{reviewed}/tag", {
            "category": "business", "purpose": "Same", "notes": "", "vehicle_id": "", "exclusion": ""})
        assert response.status_code == 200
        for trip_id, row in stored.items():
            saved = await _row(state, trip_id)
            assert saved["tag_source"] == "human" and saved["updated_at"] != row["updated_at"]
            assert {**saved, "tag_source": None, "updated_at": None} == {
                **row, "tag_source": None, "updated_at": None}
        # Review tagging does not mark a committed write, as before.
        assert commits.take() == [f"/trips/{tagged}/tag", f"/trips/{purposed}/purpose"]
    _run(check)


def test_portable_export_and_import_leave_idempotency_keys_out():
    async def check(state):
        from app.portable.export import _export_trip, _fetch_export_trips
        response = await _create(state, _create_body())
        assert response.status_code == 201
        async with state.pools[state.owner].connection() as conn:
            trips = [_export_trip(row) for row in await _fetch_export_trips(conn)]
        assert [trip["purpose"] for trip in trips] == ["Visit"]
        assert not {"idempotency_key", "idempotency_digest"} & set(trips[0])
    _run(check)

