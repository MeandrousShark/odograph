"""Holds the redaction claims docs/privacy.md makes, for the paths that
need no database: httpx's logger level, the generic unhandled-error
response, and the worker/route log lines that talk to Geoapify or OSRM --
both of which build request URLs out of exactly the things that must never
reach a log (an API key query parameter, precise coordinates, a user's
search text). DB-backed claims (ingest's no-payload logging, a geocode
cache miss) live in tests/test_redaction_db.py.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import httpx
from starlette.responses import PlainTextResponse

from app.config import Config
from app.main import create_app
from app.ui import make_router

REQUIRED_ENV = {
    "DATABASE_URL": "postgresql://unused/unused",
    "INGEST_PASSWORD": "ingest-password",
    "SESSION_SECRET": "session-secret",
}


def test_httpx_logger_pinned_to_warning():
    # app/main.py raises httpx's logger above INFO specifically so a
    # Geoapify request's full URL (GEOCODE_API_KEY rides along as a query
    # parameter) is never emitted by httpx's own request logging.
    import app.main  # noqa: F401  (import triggers the module-level setLevel call)

    assert logging.getLogger("httpx").level == logging.WARNING


# --- generic 500 response never leaks a traceback, SQL, or bound values ---


class _RaisingConnCtx:
    async def __aenter__(self):
        raise ValueError(
            "duplicate key value violates unique constraint \"geocode_cache_pkey\"\n"
            "DETAIL:  Key (lat, lon)=(37.1235, -122.1235) already exists. "
            "apiKey=SECRET_GEOCODE_KEY"
        )

    async def __aexit__(self, *exc_info):
        return False


class _RaisingPool:
    def connection(self, timeout=None):
        return _RaisingConnCtx()


def _build_app(monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    for key in ("DEV_NO_AUTH", "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    app = create_app(Config.from_env())
    app.state.pool = _RaisingPool()
    return app


async def _get(app, path):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.get(path)


def test_unhandled_error_returns_generic_page_with_no_traceback_or_sql(monkeypatch):
    app = _build_app(monkeypatch)
    response = asyncio.run(_get(app, "/healthz"))

    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    for leak in (
        "Traceback", "DETAIL", "geocode_cache_pkey", "37.1235", "-122.1235",
        "SECRET_GEOCODE_KEY", "ValueError",
    ):
        assert leak not in response.text


# --- geocode worker: a failed/miss lookup never logs the coordinate or key ---


def test_geocode_worker_lookup_failure_never_logs_coordinate_or_api_key(caplog):
    from app.geocode import GeoapifyProvider, GeocodeWorker

    def handler(request: httpx.Request) -> httpx.Response:
        # A real Geoapify 401 response, with the key visible in the request
        # URL exactly the way httpx.HTTPStatusError.__str__ would echo it.
        return httpx.Response(401, json={"error": "invalid api key"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = GeoapifyProvider(
                api_key="SECRET_GEOCODE_KEY", omit_country="United States of America"
            )
            worker = GeocodeWorker(
                pool=None, http_client=client, provider=provider,
                min_interval_s=0, debounce_s=1, sweep_s=1,
            )
            with caplog.at_level(logging.WARNING, logger="app.geocode"):
                await worker._geocode_one(37.123456, -122.123456)

    asyncio.run(run())
    assert "SECRET_GEOCODE_KEY" not in caplog.text
    assert "37.123456" not in caplog.text
    assert "-122.123456" not in caplog.text
    assert "HTTPStatusError" in caplog.text


class _FakeConn:
    async def execute(self, *args, **kwargs):
        return None


class _FakeConnCtx:
    async def __aenter__(self):
        return _FakeConn()

    async def __aexit__(self, *exc_info):
        return False


class _FakePool:
    def connection(self, timeout=None):
        return _FakeConnCtx()


def test_geocode_worker_cache_miss_never_logs_coordinate(caplog):
    from app.geocode import GeoapifyProvider, GeocodeWorker

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = GeoapifyProvider(
                api_key="SECRET_GEOCODE_KEY", omit_country="United States of America"
            )
            worker = GeocodeWorker(
                pool=_FakePool(), http_client=client, provider=provider,
                min_interval_s=0, debounce_s=1, sweep_s=1,
            )
            with caplog.at_level(logging.INFO, logger="app.geocode"):
                await worker._geocode_one(37.123456, -122.123456)

    asyncio.run(run())
    assert "37.123456" not in caplog.text
    assert "-122.123456" not in caplog.text
    assert "no address found" in caplog.text


# --- /places/search: a failed autocomplete call never logs the API key or query ---


def _search_places_endpoint():
    for route in make_router().routes:
        if getattr(route, "path", None) == "/places/search":
            return route.endpoint
    raise AssertionError("search route missing")


SEARCH_PLACES = _search_places_endpoint()


def test_address_search_failure_never_logs_api_key_or_query_text(caplog):
    from app.geocode import GeoapifyProvider

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid api key"})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GeoapifyProvider(
            api_key="SECRET_GEOCODE_KEY", omit_country="United States of America"
        )
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            config=SimpleNamespace(geocode_provider=provider),
            geocode_http_client=client,
            templates=SimpleNamespace(
                TemplateResponse=lambda request, name, context, status_code=200: (
                    PlainTextResponse("ok")
                )
            ),
        )))
        with caplog.at_level(logging.WARNING, logger="app.ui"):
            await SEARCH_PLACES(request, "1600 my secret street address", {"sub": "test"})
        await client.aclose()

    asyncio.run(run())
    assert "SECRET_GEOCODE_KEY" not in caplog.text
    assert "1600 my secret street address" not in caplog.text
    assert "HTTPStatusError" in caplog.text


# --- missing-trip OSRM route suggestion: a failed call never logs coordinates ---


def test_missing_trip_osrm_suggestion_failure_never_logs_coordinates(monkeypatch, caplog):
    import app.ui as ui_module

    async def fake_fetch_trip(pool, trip_id):
        return {
            "prev_trip_end_lat": 47.612345, "prev_trip_end_lon": -122.312345,
            "start_lat": 47.698765, "start_lon": -122.298765,
        }

    monkeypatch.setattr(ui_module, "_fetch_trip", fake_fetch_trip)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"code": "Error"})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            config=SimpleNamespace(osrm_url="http://osrm"),
            osrm_http_client=client,
            pool=None,
        )))
        with caplog.at_level(logging.WARNING, logger="app.ui"):
            result = await ui_module._resolve_missing_trip_osrm_hint(request, "42")
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result is None
    for leak in ("47.612345", "-122.312345", "47.698765", "-122.298765"):
        assert leak not in caplog.text
    assert "HTTPStatusError" in caplog.text
