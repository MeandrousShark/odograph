"""Traccar Client ingest: form parsing, normalization and pre-body auth."""
from __future__ import annotations

import asyncio
import base64
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.routing import Match

from app.account_context import AccountPrincipal
from app.capacity import AdmissionManager
from app.capacity_routes import INGEST_ROUTES
from app.ingest import (
    BASIC_CREDENTIALS, INGEST_CREDENTIAL_SOURCES, QUERY_CREDENTIAL_MAX_CHARS,
    QUERY_CREDENTIALS, TRACCAR_DEVICE_MAX_CHARS, TRACCAR_MAX_FIELDS, FailedAuthLimiter,
    _int, _num, _query_credentials, _validate_location, make_router, parse_traccar_form,
    traccar_device, traccar_fix, traccar_number,
)
from app.tracking import IngestPrincipal, TrackingUnavailable

DEVICE = IngestPrincipal(AccountPrincipal(1, True, 1), "device-public", 1, "device", 7, 1)
LEGACY = IngestPrincipal(AccountPrincipal(1, True, 1), "legacy-public", 1, "legacy", None, None)


# Form parsing


def test_form_fields_keep_their_original_strings():
    fields, reason = parse_traccar_form(
        b"id=12345678&lat=47.6&lon=-122.3&timestamp=1730000000&speed=0.0"
        b"&batt=85&charge=false&alarm=sos%20now&bearing="
    )
    assert reason is None
    assert fields == {
        "id": "12345678", "lat": "47.6", "lon": "-122.3", "timestamp": "1730000000",
        "speed": "0.0", "batt": "85", "charge": "false", "alarm": "sos now", "bearing": "",
    }


def test_form_decodes_plus_and_utf8_percent_escapes():
    assert parse_traccar_form(b"alarm=a+b%C3%A9") == ({"alarm": "a bé"}, None)


def test_empty_form_is_a_message_without_fields():
    assert parse_traccar_form(b"") == ({}, None)


@pytest.mark.parametrize("body", [
    b"lat=1&lat=2",
    b"id=1&lat=1&lon=2&id=1",
    b"_type=location&lat=1&lon=2",
    b"lat=1&lon=2&_source=other",
    b'{"lat": 47.6, "lon": -122.3}',
    b"lat=1&&lon=2",
    b"lat",
    b"alarm=\xff",
    b"alarm=%FF",
    b"alarm=%ED%A0%80",
])
def test_poison_forms_are_dropped(body):
    fields, reason = parse_traccar_form(body)
    assert fields == {} and reason


def test_field_count_cap():
    at_cap = "&".join(f"f{i}=1" for i in range(TRACCAR_MAX_FIELDS)).encode()
    assert len(parse_traccar_form(at_cap)[0]) == TRACCAR_MAX_FIELDS
    over = at_cap + b"&extra=1"
    assert parse_traccar_form(over) == ({}, "unparseable or too many form fields")


# String-to-number normalization


@pytest.mark.parametrize(("text", "expected"), [
    ("85", 85.0),
    ("47.6", 47.6),
    ("-122.3", -122.3),
    ("+3", 3.0),
    (".5", 0.5),
    ("5.", 5.0),
    ("0", 0.0),
    ("5.0E-4", 0.0005),
    ("-5.0e-4", -0.0005),
    ("1.0E7", 1e7),
    ("1730000000.5", 1730000000.5),
])
def test_decimal_strings_parse(text, expected):
    assert traccar_number(text) == expected


@pytest.mark.parametrize("text", [
    None, "", " ", " 1", "1 ", "\t1", "NaN", "nan", "inf", "-inf", "Infinity",
    "1e999", "-1e999", "9" * 400, "1_000", "0x10", "1,5", "abc", "1.2.3", "e5",
    "1e", "--1", "+-1", "١", "1" * 65, "true",
])
def test_non_decimal_or_non_finite_strings_are_absent(text):
    assert traccar_number(text) is None


def test_fix_maps_fields_and_converts_knots():
    fix = traccar_fix({
        "lat": "47.6", "lon": "-122.3", "timestamp": "1730000000", "accuracy": "4.5",
        "altitude": "12.0", "speed": "10.0", "bearing": "90", "batt": "85.7",
        "charge": "true", "id": "1",
    })
    assert fix == {
        "lat": 47.6, "lon": -122.3, "tst": 1730000000.0, "acc": 4.5, "alt": 12.0,
        "batt": 85.7, "vel": pytest.approx(18.52),
    }
    assert _int(fix["batt"]) == 85


def test_fix_omits_unusable_fields():
    assert traccar_fix({"lat": "", "lon": "NaN", "timestamp": "soon", "speed": "inf"}) == {}


def test_huge_speed_overflows_to_an_absent_reading():
    fix = traccar_fix({"speed": "1e308"})
    assert _num(fix["vel"]) is None


@pytest.mark.parametrize(("name", "value", "key"), [
    ("accuracy", "1e10", "acc"), ("altitude", "-1e10", "alt"), ("speed", "1e9", "vel"),
])
def test_reading_bounds_store_null(name, value, key):
    assert _num(traccar_fix({name: value})[key]) is None


@pytest.mark.parametrize(("batt", "expected"), [
    ("85", 85), ("0", 0), ("100", 100), ("100.5", None), ("-1", None), ("", None),
])
def test_battery_bounds(batt, expected):
    assert _int(traccar_fix({"batt": batt}).get("batt")) == expected


def _fields(**changes):
    fields = {"lat": "47.6", "lon": "-122.3", "timestamp": str(int(time.time()) - 60)}
    fields.update(changes)
    return {key: value for key, value in fields.items() if value is not None}


def test_valid_fix_passes_location_rules():
    assert _validate_location(traccar_fix(_fields())) is None
    assert _validate_location(traccar_fix(_fields(lat="5.0E-4", lon="-5.0E-4"))) is None


@pytest.mark.parametrize("changes", [
    {"lat": None},
    {"lon": None},
    {"lat": ""},
    {"lon": "west"},
    {"timestamp": None},
    {"lat": "90.5"},
    {"lon": "-180.5"},
    {"lat": "0", "lon": "0"},
    {"timestamp": "0"},
    {"timestamp": "-5"},
    # Milliseconds and ISO text are not accepted timestamp forms.
    {"timestamp": str(int(time.time()) * 1000)},
    {"timestamp": "2026-10-27T12:00:00Z"},
    {"timestamp": str(int(time.time()) + 3600)},
])
def test_invalid_fixes_fail_location_rules(changes):
    assert _validate_location(traccar_fix(_fields(**changes))) is not None


def test_device_text_is_capped_with_a_default():
    assert traccar_device({"id": "12345678"}) == "12345678"
    assert traccar_device({"id": "x" * 500}) == "x" * TRACCAR_DEVICE_MAX_CHARS
    assert traccar_device({"id": ""}) == "default"
    assert traccar_device({}) == "default"


# Query credential parsing


def _query_request(query: bytes, **settings):
    manager = SimpleNamespace(config=SimpleNamespace(**settings))
    return SimpleNamespace(scope={"query_string": query}, app=SimpleNamespace(state=SimpleNamespace(capacity=manager)))


def test_query_credential_reads_exactly_one_of_each():
    assert _query_credentials(_query_request(b"username=phone-ab12&key=s3cr-t_x")) == ("phone-ab12", "s3cr-t_x")
    assert _query_credentials(_query_request(b"key=k&other=1&username=u")) == ("u", "k")
    assert _query_credentials(_query_request(b"username=u&key=a%2Bb")) == ("u", "a+b")


@pytest.mark.parametrize("query", [
    b"",
    b"username=u",
    b"key=k",
    b"username=&key=k",
    b"username=u&key=",
    b"username=u&username=u&key=k",
    b"username=u&key=k&key=k",
    b"user=u&key=k",
    b"username=u&key=%FF",
    b"username=\xc3\xa9&key=k",
    b"username=" + b"u" * (QUERY_CREDENTIAL_MAX_CHARS + 1) + b"&key=k",
    b"username=u&key=" + b"k" * (QUERY_CREDENTIAL_MAX_CHARS + 1),
])
def test_bad_query_credentials_are_absent(query):
    assert _query_credentials(_query_request(query)) is None


def test_query_over_the_header_byte_cap_is_absent_and_oversized():
    query = b"username=u&key=k&pad=" + b"x" * 64
    request = _query_request(query, capacity_basic_header_max_bytes=len(query) - 1)
    assert QUERY_CREDENTIALS.oversized(request)
    assert _query_credentials(request) is None
    request = _query_request(query, capacity_basic_header_max_bytes=len(query))
    assert not QUERY_CREDENTIALS.oversized(request)
    assert _query_credentials(request) == ("u", "k")


def test_each_ingest_route_has_its_credential_source():
    assert set(INGEST_CREDENTIAL_SOURCES) == INGEST_ROUTES
    assert INGEST_CREDENTIAL_SOURCES["/ingest"] is BASIC_CREDENTIALS
    assert not BASIC_CREDENTIALS.device_only
    assert INGEST_CREDENTIAL_SOURCES["/ingest/traccar"] is QUERY_CREDENTIALS
    assert QUERY_CREDENTIALS.device_only


# Routes


def test_routes_resolve_beside_each_other_in_the_ingest_lane():
    routes = make_router().routes
    for path, name in (("/ingest", "ingest"), ("/ingest/traccar", "ingest_traccar")):
        scope = {"type": "http", "method": "POST", "path": path}
        matched = next(route for route in routes if route.matches(scope)[0] is Match.FULL)
        assert matched.name == name and matched.path == path
        assert matched.capacity_lane == "ingest"
        assert matched.methods == {"POST"}


class _UnusedPool:
    def __getattr__(self, name):
        pytest.fail("a dropped Traccar message reached the database")


def _app(monkeypatch, verify, **settings):
    app = FastAPI()
    app.state.capacity = AdmissionManager(SimpleNamespace(**settings))
    app.state.config = SimpleNamespace(
        ingest_username="", ingest_password="", ingest_max_body_bytes=1000,
    )
    app.state.ingest_limiter = FailedAuthLimiter(10, 900)
    app.state.control_pool = object()
    app.state.runtime_pool = _UnusedPool()
    app.state.detector_scheduler = SimpleNamespace(poke=lambda: pytest.fail("unexpected poke"))
    calls = []

    async def authenticate(control_pool, username, password, **kwargs):
        calls.append((username, password))
        return await verify(username, password)

    monkeypatch.setattr("app.ingest.authenticate_ingest", authenticate)
    app.include_router(make_router())
    return app, calls


def _failures(app):
    return sum(len(entries) for entries in app.state.ingest_limiter._failures.values())


async def _post(app, url, body=b"lat=1&lon=2", headers=None):
    reads = 0
    chunks = [body]

    async def receive():
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": chunks.pop() if chunks else b"", "more_body": False}

    messages = []

    async def send(message):
        messages.append(message)

    path, _, query = url.partition("?")
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
        "headers": [(b"content-type", b"application/x-www-form-urlencoded"), *(headers or ())],
        "client": ("192.0.2.1", 1000), "server": ("test", 80),
    }
    await app(scope, receive, send)
    start = messages[0]
    body_out = b"".join(m.get("body", b"") for m in messages[1:])
    return start["status"], dict(start["headers"]), body_out, reads


async def _device(username, password):
    return DEVICE


@pytest.mark.parametrize("url", [
    "/ingest/traccar",
    "/ingest/traccar?username=u",
    "/ingest/traccar?key=k",
    "/ingest/traccar?username=u&username=u&key=k",
    "/ingest/traccar?username=u&key=" + "k" * (QUERY_CREDENTIAL_MAX_CHARS + 1),
])
def test_missing_or_malformed_query_credential_fails_before_lookup_and_body(monkeypatch, url):
    async def run():
        app, calls = _app(monkeypatch, _device)
        status, headers, _, reads = await _post(app, url)
        assert status == 401 and reads == 0 and calls == []
        assert _failures(app) == 1
        assert not any(app.state.capacity._active.values())
    asyncio.run(run())


def test_query_over_header_cap_fails_in_preflight(monkeypatch):
    async def run():
        app, calls = _app(monkeypatch, _device, capacity_basic_header_max_bytes=64)
        status, _, _, reads = await _post(app, "/ingest/traccar?username=u&key=" + "k" * 64)
        assert status == 401 and reads == 0 and calls == []
        assert _failures(app) == 1
    asyncio.run(run())


def test_authorization_header_is_ignored(monkeypatch):
    async def run():
        app, calls = _app(monkeypatch, _device)
        basic = b"Basic " + base64.b64encode(b"u:k")
        status, _, _, reads = await _post(app, "/ingest/traccar", headers=[(b"authorization", basic)])
        assert status == 401 and reads == 0 and calls == []
        # An oversized header does not count against this route either.
        oversized = [(b"authorization", b"Basic " + b"x" * 9000)]
        status, _, body, _ = await _post(app, "/ingest/traccar?username=u&key=k", b"", oversized)
        assert (status, body) == (200, b"")
        assert calls == [("u", "k")] and _failures(app) == 1
    asyncio.run(run())


def test_wrong_secret_counts_as_a_failure(monkeypatch):
    async def wrong(username, password):
        return None

    async def run():
        app, calls = _app(monkeypatch, wrong)
        status, _, _, reads = await _post(app, "/ingest/traccar?username=u&key=k")
        assert status == 401 and reads == 0 and calls == [("u", "k")]
        assert _failures(app) == 1
    asyncio.run(run())


def test_valid_legacy_credential_is_refused_without_counting(monkeypatch):
    async def legacy(username, password):
        return LEGACY

    async def run():
        app, calls = _app(monkeypatch, legacy)
        entered = []
        operation = app.state.capacity.operation

        def tracked(lane, **kwargs):
            entered.append(lane)
            return operation(lane, **kwargs)

        app.state.capacity.operation = tracked
        status, _, _, reads = await _post(app, "/ingest/traccar?username=u&key=k")
        assert status == 401 and reads == 0 and calls == [("u", "k")]
        assert _failures(app) == 0
        assert "ingest" not in entered
    asyncio.run(run())


def test_presetup_environment_login_is_refused_without_counting(monkeypatch):
    async def presetup(username, password):
        raise TrackingUnavailable("Complete account setup before tracking")

    async def run():
        app, _ = _app(monkeypatch, presetup)
        status, _, _, reads = await _post(app, "/ingest/traccar?username=u&key=k")
        assert status == 401 and reads == 0
        assert _failures(app) == 0
    asyncio.run(run())


def test_blocked_client_gets_503_before_lookup(monkeypatch):
    async def run():
        app, calls = _app(monkeypatch, _device)
        for _ in range(10):
            app.state.ingest_limiter.record_failure("192.0.2.1")
        status, headers, _, reads = await _post(app, "/ingest/traccar?username=u&key=k")
        assert status == 503 and b"retry-after" in headers
        assert reads == 0 and calls == []
    asyncio.run(run())


def test_busy_auth_lane_answers_503(monkeypatch):
    async def run():
        app, calls = _app(monkeypatch, _device, capacity_auth_ingest_slots=1, capacity_auth_ingest_pending=0)
        entered, release = asyncio.Event(), asyncio.Event()

        async def occupy():
            async with app.state.capacity.operation("auth_ingest"):
                entered.set()
                await release.wait()

        occupied = asyncio.create_task(occupy())
        await entered.wait()
        status, _, _, reads = await _post(app, "/ingest/traccar?username=u&key=k")
        release.set()
        await occupied
        assert status == 503 and reads == 0 and calls == []
    asyncio.run(run())


@pytest.mark.parametrize("body", [
    b"",
    b"id=1&timestamp=1730000000&batt=50",
    b"lat=1&lat=2&lon=3",
    b"_type=location&lat=1&lon=2",
    b'{"_type":"location","lat":1,"lon":2}',
    b"lat=1&lon=2&alarm=%FF",
    b"lat=1&lon=2&alarm=%00",
    "&".join(f"f{i}=1" for i in range(TRACCAR_MAX_FIELDS + 1)).encode(),
    b"lat=1&lon=2&pad=" + b"x" * 1000,
])
def test_heartbeat_and_poison_bodies_answer_empty_200_without_storage(monkeypatch, body):
    async def run():
        app, _ = _app(monkeypatch, _device)
        status, headers, out, reads = await _post(app, "/ingest/traccar?username=u&key=k", body)
        assert (status, out) == (200, b"") and reads >= 1
        assert _failures(app) == 0
    asyncio.run(run())


def test_get_is_not_accepted():
    app = FastAPI()
    app.state.capacity = AdmissionManager()
    app.include_router(make_router())

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/ingest/traccar?username=u&key=k&lat=1&lon=2")
        assert response.status_code == 405
    asyncio.run(run())
