"""GET /places/search: the address autocomplete shared by Settings' Add Place
and the manual trip form's Search mode. It reads no account data, so these
tests drive the endpoint function directly with a fake provider and the real
results template.
"""
from __future__ import annotations

import asyncio
import functools
import logging
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx

from app.main import make_templates
from app.provider_pacing import ProviderPacer
from app.ui import make_router

TZ = ZoneInfo("America/Los_Angeles")
ROOT = Path(__file__).parents[1]
USER = {"sub": "test"}


@functools.cache
def _routes():
    return tuple(make_router().routes)


def _search_endpoint():
    for route in _routes():
        if getattr(route, "path", None) == "/places/search":
            return route.endpoint
    raise AssertionError("search route missing")


SEARCH = _search_endpoint()


class FakeProvider:
    def __init__(self, results=None, error=None):
        self.results = results or []
        self.error = error
        self.queries: list[str] = []

    async def autocomplete(self, client, query, limit=5):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.results


def _search(provider, q):
    async def run():
        client = httpx.AsyncClient()
        templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
        config = SimpleNamespace(geocode_provider=provider, display_tz=TZ)
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(
                templates=templates, geocode_http_client=client,
                geocode_pacer=ProviderPacer(0),
            )),
            state=SimpleNamespace(config=config),
        )
        try:
            return await SEARCH(request, q, USER)
        finally:
            await client.aclose()

    return asyncio.run(run())


def test_search_renders_escaped_labels_and_coordinates():
    provider = FakeProvider([
        {"label": "1 <b>Main</b> St & Co", "lat": 47.6, "lon": -122.3},
        {"label": "Second Ave", "lat": 47.7, "lon": -122.25},
    ])

    response = _search(provider, "  main  ")

    body = response.body.decode()
    assert response.status_code == 200
    assert provider.queries == ["main"]
    assert "<b>Main</b>" not in body
    assert "1 &lt;b&gt;Main&lt;/b&gt; St &amp; Co" in body
    assert 'data-lat="47.6" data-lon="-122.3"' in body
    assert 'data-lat="47.7" data-lon="-122.25"' in body
    assert body.count("data-fill-place-latlon") == 2


def test_search_without_a_provider_is_an_empty_200():
    response = _search(None, "main street")

    assert response.status_code == 200
    assert "<button" not in response.body.decode()


def test_search_with_a_blank_query_never_calls_the_provider():
    provider = FakeProvider([{"label": "x", "lat": 1.0, "lon": 2.0}])

    for q in ("", "   "):
        response = _search(provider, q)
        assert response.status_code == 200
        assert "<button" not in response.body.decode()

    assert provider.queries == []


def test_search_provider_failure_is_an_empty_200_and_logs_no_query(caplog):
    provider = FakeProvider(error=httpx.ConnectError("https://x.test/?text=my secret street"))

    with caplog.at_level(logging.WARNING, logger="app.ui"):
        response = _search(provider, "my secret street")

    assert response.status_code == 200
    assert "<button" not in response.body.decode()
    assert "my secret street" not in caplog.text
    assert "x.test" not in caplog.text
    assert "ConnectError" in caplog.text


def test_results_partial_keeps_the_add_place_markup():
    # Settings' click handler reads these attributes; the manual form adds its
    # endpoint through the results container instead, so the buttons stay put.
    partial = (ROOT / "app/templates/_address_results.html").read_text()

    assert (
        '<li><button type="button" data-fill-place-latlon\n'
        '              data-lat="{{ r.lat }}" data-lon="{{ r.lon }}">{{ r.label }}</button></li>'
    ) in partial
    form = (ROOT / "app/templates/_add_place_form.html").read_text()
    assert 'hx-target="#address-results"' in form
    assert '<div id="address-results"></div>' in form
    settings = (ROOT / "app/templates/settings.html").read_text()
    assert "input[name=\"lat\"]" in settings and "input[name=\"lon\"]" in settings
