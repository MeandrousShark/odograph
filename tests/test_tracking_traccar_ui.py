"""Traccar Client setup values on the Tracking page: issued-only and encoded."""
from __future__ import annotations

import asyncio
import html
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

from app.capacity import CapacityBusy
from app.main import make_templates
from app.tracking import IssuedCredential
from app.ui.tracking import _render_tracking, _traccar_urls

SECRET = "a b&c=d/+?#é"
ISSUED = IssuedCredential("public", "work phone-7k3q", SECRET, 1)


def _request(app_url="https://odo.example/"):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        config=SimpleNamespace(app_url=app_url))))


def _render(**extra):
    templates = make_templates(SimpleNamespace(display_tz=None, app_version="test"))
    context = {
        "request": SimpleNamespace(url=SimpleNamespace(path="/settings/tracking")),
        "user": {"id": 1, "name": "T", "is_admin": False}, "csrf": "c",
        "devices": [], "credentials": [], "ingest_url": "https://odo.example/ingest",
        "issued": None, "traccar_url": None, "traccar_config_link": None,
    }
    return templates.env.get_template("tracking.html").render(**{**context, **extra})


def test_urls_are_percent_encoded_and_round_trip():
    server_url, link = _traccar_urls(_request(), ISSUED)
    assert server_url.startswith("https://odo.example/ingest/traccar?username=work%20phone-7k3q&key=")
    query = parse_qs(urlsplit(server_url).query)
    assert query == {"username": [ISSUED.username], "key": [SECRET]}
    assert link.startswith("org.traccar.client://config?url=https%3A%2F%2Fodo.example%2Fingest%2Ftraccar%3F")
    assert "&" not in link and "?" in link and link.count("?") == 1
    assert unquote(link.split("url=", 1)[1]) == server_url


def test_issued_page_shows_both_values_with_copy_buttons():
    server_url, link = _traccar_urls(_request(), ISSUED)
    body = _render(issued=ISSUED, traccar_url=server_url, traccar_config_link=link)
    values = [html.unescape(v) for v in re.findall(r'<input readonly value="([^"]*)"', body)]
    assert server_url in values and link in values
    for target in ("traccar-setup-url", "traccar-setup-config"):
        assert re.search(rf'<button type="button"[^>]*data-copy-target="{target}"[^>]*>Copy</button>', body)


def test_plain_page_shows_neither_value():
    body = _render()
    assert "traccar" not in body.lower()
    assert "org.traccar.client" not in body


def test_busy_fallback_includes_both_values():
    @asynccontextmanager
    async def busy_connection():
        raise CapacityBusy("busy")
        yield

    async def run():
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(
                config=SimpleNamespace(app_url="https://odo.example"))),
            state=SimpleNamespace(account_pool=SimpleNamespace(connection=busy_connection)),
        )
        issued = IssuedCredential("public", "user", "<s&cret>", 1)
        response = await _render_tracking(request, {"id": 1}, issued)
        assert response.headers["cache-control"] == "no-store"
        text = response.body.decode()
        server_url, link = _traccar_urls(request, issued)
        assert "<s&cret>" not in text
        assert html.escape(server_url) in text and html.escape(link) in text
        assert "key=%3Cs%26cret%3E" in text
    asyncio.run(run())
