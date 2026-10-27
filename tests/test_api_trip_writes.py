"""API trip writes without a database: instant and key parsing, the
canonical request digest, the error registry and the admission lane."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel
from starlette.responses import Response

from app.account_context import AccountPrincipal
from app.api_errors import ERRORS, ApiError, register_api_errors
from app.api_v1 import require_api_user
from app.capacity import AdmissionManager
from app.capacity_routes import AdmissionRoute, capacity_policy
from app.api_trips import _idempotency_key, _instant, _write_values
from app.trip_writes import ManualTrip, manual_trip_digest

UTC = timezone.utc


@pytest.mark.parametrize("text,expected", [
    ("2026-04-14T15:00:00Z", datetime(2026, 4, 14, 15, tzinfo=UTC)),
    ("2026-04-14t15:00:00z", datetime(2026, 4, 14, 15, tzinfo=UTC)),
    ("2026-04-14T08:00:00-07:00", datetime(2026, 4, 14, 15, tzinfo=UTC)),
    ("2026-04-14T15:00:00.5Z", datetime(2026, 4, 14, 15, 0, 0, 500000, tzinfo=UTC)),
    ("2026-04-14T15:00:00.123456789Z", datetime(2026, 4, 14, 15, 0, 0, 123456, tzinfo=UTC)),
])
def test_instants_accept_rfc3339_with_an_offset(text, expected):
    assert _instant(text) == expected


@pytest.mark.parametrize("text", [
    "2026-04-14T15:00:00", "2026-04-14", "2026-04-14 15:00:00Z", "2026-04-14T15:00Z",
    "2026-13-14T15:00:00Z", "2026-04-14T24:00:00Z", "2026-04-14T15:00:60Z", "1776193200",
    "2026-04-14T15:00:00+0700", " 2026-04-14T15:00:00Z", "2026-04-14T15:00:00Z\n",
])
def test_instants_refuse_anything_else(text):
    assert _instant(text) is None


def test_write_values_refuse_nul_and_bad_instants_by_field():
    with pytest.raises(ApiError) as refused:
        _write_values({"notes": "a\x00b", "started_at": "yesterday", "purpose": "ok"})
    assert refused.value.code == "validation_failed"
    assert refused.value.fields == {"notes": "This value is not valid.",
                                    "started_at": "Enter a time with its offset."}
    parsed = _write_values({"ended_at": "2026-04-14T15:00:00Z", "category": "business"})
    assert parsed == {"ended_at": datetime(2026, 4, 14, 15, tzinfo=UTC), "category": "business"}


def test_idempotency_keys_are_canonical_uuids():
    key = uuid.uuid4()
    assert _idempotency_key(str(key)) == key
    assert _idempotency_key(str(key).upper()) == key
    for bad in ("", "x", key.hex, "{" + str(key) + "}", "urn:uuid:" + str(key), str(key) + " "):
        with pytest.raises(ApiError) as refused:
            _idempotency_key(bad)
        assert refused.value.fields == {"Idempotency-Key": "This value is not valid."}


def _trip(**values):
    start = datetime(2026, 4, 14, 15, tzinfo=UTC)
    return ManualTrip(**{"started_at": start, "ended_at": start + timedelta(minutes=30),
                         "distance_m": 1000.0, **values})


def test_the_digest_covers_the_normalized_request():
    base = manual_trip_digest(_trip())
    assert len(base) == 32
    shifted = datetime(2026, 4, 14, 8, tzinfo=timezone(timedelta(hours=-7)))
    assert manual_trip_digest(_trip(started_at=shifted)) == base
    for changed in (_trip(distance_m=1000.5), _trip(category="business"), _trip(purpose="p"),
                    _trip(notes="n"), _trip(vehicle_id=1), _trip(exclusion="not_deductible"),
                    _trip(start_label="a"), _trip(end_label="b"),
                    _trip(ended_at=datetime(2026, 4, 14, 15, 31, tzinfo=UTC))):
        assert manual_trip_digest(changed) != base
    with pytest.raises(ValueError):
        manual_trip_digest(_trip(path_geojson={"type": "LineString", "coordinates": []},
                                 start_point=(0, 0), end_point=(1, 1)))


def test_write_error_codes_are_registered():
    assert ERRORS["idempotency_key_reused"][0] == 409
    assert ERRORS["storage_full"][0] == 409
    assert ERRORS["trip_in_progress"][0] == 409


class _NotesBody(BaseModel):
    notes: str


def test_api_edits_and_deletes_authenticate_first_and_free_the_form_slot():
    async def run():
        app = FastAPI()
        register_api_errors(app)
        manager = app.state.capacity = AdmissionManager(SimpleNamespace())
        router = APIRouter(route_class=AdmissionRoute)
        authenticated = False
        calls = []

        async def identity(request: Request):
            if not authenticated:
                raise ApiError("invalid_token")
            request.state.principal = AccountPrincipal(1, True, 1)
            return {"id": 1}
        app.dependency_overrides[require_api_user] = identity

        @router.patch("/api/v1/things/{thing_id}")
        @capacity_policy("form")
        async def edit(thing_id: int, body: _NotesBody, user=Depends(require_api_user)):
            assert not manager._active["auth_interactive"]
            calls.append(("patch", body.notes))
            return Response(status_code=204)

        @router.delete("/api/v1/things/{thing_id}")
        @capacity_policy("form")
        async def remove(thing_id: int, user=Depends(require_api_user)):
            assert not manager._active["auth_interactive"]
            calls.append(("delete", thing_id))
            return Response(status_code=204)
        app.include_router(router)
        reads = 0

        async def body():
            nonlocal reads
            reads += 1
            yield b'{"notes": "hello"}'
        headers = {"content-type": "application/json"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            denied = await client.patch("/api/v1/things/1", content=body(), headers=headers)
            assert denied.status_code == 401 and reads == 0
            authenticated = True
            saved = await client.patch("/api/v1/things/1", content=body(), headers=headers)
            assert saved.status_code == 204, saved.text
            assert (await client.delete("/api/v1/things/2")).status_code == 204
        assert reads == 1 and calls == [("patch", "hello"), ("delete", 2)]
        assert not any(manager._active.values())
    asyncio.run(run())


@pytest.mark.parametrize("text", [
    "0001-01-01T05:00:00Z", "0001-01-01T00:00:00+01:00", "9999-12-31T20:00:00-05:00",
    "1899-12-31T23:59:59Z", "9999-01-01T00:00:00Z",
])
def test_instants_outside_the_display_safe_years_are_refused(text):
    with pytest.raises(ApiError) as refused:
        _write_values({"started_at": text})
    assert refused.value.fields == {"started_at": "Enter a time from 1900 through 9998."}


def test_instants_at_the_display_safe_bounds_are_kept():
    assert _write_values({"started_at": "1900-01-01T00:00:00+14:00"})["started_at"].year == 1900
    assert _write_values({"ended_at": "9998-12-31T23:59:59-12:00"})["ended_at"].year == 9998


@pytest.mark.parametrize("distance", ["3e35", "1e-50", "1e300"])
def test_the_web_form_refuses_distances_a_real_cannot_hold(distance):
    from zoneinfo import ZoneInfo

    from app.trip_writes import ManualTripValidationError
    from app.ui.manual import parse_manual_trip_input
    with pytest.raises(ManualTripValidationError) as refused:
        parse_manual_trip_input("2026-07-14", "09:00", "10:00", distance, ZoneInfo("America/Los_Angeles"))
    expected = ("Distance must be a positive finite number." if distance == "1e-50"
                else "Distance must be no greater than 10,000 km (about 6,214 miles).")
    assert refused.value.errors == {"distance": expected}


def test_distance_bounds_follow_the_real_column():
    from app.trip_writes import REAL_MIN_NORMAL, distance_problem
    assert distance_problem(REAL_MIN_NORMAL) is None
    for value in (1e-50, REAL_MIN_NORMAL / 2, 0, -1, float("nan"), float("inf")):
        assert distance_problem(value) == "Distance must be a positive finite number.", value


def test_distance_has_a_product_maximum_of_10000_km():
    from app.trip_writes import DISTANCE_TOO_LARGE, distance_problem
    assert "10,000 km" in DISTANCE_TOO_LARGE
    assert distance_problem(10_000_000) is None
    assert distance_problem(10_000_000.001) == DISTANCE_TOO_LARGE
    assert distance_problem(3.4028234663852886e38) == DISTANCE_TOO_LARGE
    assert distance_problem(1e300) == DISTANCE_TOO_LARGE


def test_web_manual_input_applies_the_distance_maximum():
    from app.rates import METERS_PER_MILE
    from app.trip_writes import DISTANCE_TOO_LARGE, ManualTripValidationError
    from app.ui.manual import parse_manual_trip_input
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Los_Angeles")
    miles_at_max = 10_000_000 / METERS_PER_MILE
    assert parse_manual_trip_input("2026-07-14", "09:00", "10:00", str(miles_at_max), tz)[2] == pytest.approx(10_000_000)
    with pytest.raises(ManualTripValidationError) as refused:
        parse_manual_trip_input("2026-07-14", "09:00", "10:00", str(miles_at_max + 0.01), tz)
    assert refused.value.errors == {"distance": DISTANCE_TOO_LARGE}
