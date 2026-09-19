from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from app.account_context import AccountConnection, AccountPrincipal

from app.notifications import (
    count_unclassified_trips,
    odometer_reminder_vehicles,
    publish_ntfy,
)


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def fetchone(self):
        return self.rows[0]

    async def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, result_sets):
        self.result_sets = iter(result_sets)
        self.calls = []

    async def execute(self, query, params=None):
        self.calls.append((query, params))
        return _Cursor(next(self.result_sets))


def test_count_unclassified_trips_uses_category_only_and_half_open_window():
    start = datetime(2026, 7, 5, 18, tzinfo=timezone.utc)
    end = datetime(2026, 7, 12, 18, tzinfo=timezone.utc)
    conn = AccountConnection(_Connection([[(3,)]]), AccountPrincipal(41, True, 1))

    assert asyncio.run(count_unclassified_trips(conn, start, end)) == 3
    assert conn.calls == [(
        "SELECT count(*) FROM trips WHERE account_id = %s AND category = 'unclassified' "
        "AND started_at >= %s AND started_at < %s",
        (41, start, end),
    )]


def test_odometer_reminder_vehicles_preserves_query_and_name_order():
    quarter_start = datetime(2026, 7, 1, 9, tzinfo=timezone.utc)
    conn = AccountConnection(_Connection([
        [(1, "Sedan"), (2, "Truck")],
        [(2,)],
    ]), AccountPrincipal(41, True, 1))

    assert asyncio.run(odometer_reminder_vehicles(conn, quarter_start)) == ["Sedan"]
    assert conn.calls == [
        ("SELECT id, name FROM vehicles WHERE account_id = %s AND active ORDER BY name", (41,)),
        (
            "SELECT DISTINCT vehicle_id FROM odometer_readings WHERE account_id = %s AND recorded_at >= %s",
            (41, quarter_start),
        ),
    ]


async def _capture_publish(*, token="", username="", password=""):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["content"] = request.content.decode()
        return httpx.Response(200, json={"id": "test"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await publish_ntfy(
            client, "https://ntfy.example.com/", "mileage-reminders",
            token, username, password, "Exact notification body",
        )
    return captured


def test_publish_ntfy_preserves_url_headers_body_and_bearer_auth():
    captured = asyncio.run(_capture_publish(token="secret-token"))

    assert captured["url"] == "https://ntfy.example.com/mileage-reminders"
    assert captured["headers"]["authorization"] == "Bearer secret-token"
    assert captured["headers"]["title"] == "Odograph"
    assert captured["headers"]["tags"] == "car"
    assert captured["headers"]["priority"] == "default"
    assert captured["content"] == "Exact notification body"


def test_publish_ntfy_prefers_complete_basic_auth_over_bearer():
    captured = asyncio.run(_capture_publish(
        token="ignored-token", username="testuser", password="password"
    ))

    assert captured["headers"]["authorization"] == "Basic dGVzdHVzZXI6cGFzc3dvcmQ="


@pytest.mark.parametrize(
    ("username", "password"),
    [("testuser", ""), ("", "password")],
)
def test_publish_ntfy_uses_bearer_when_basic_auth_is_incomplete(username, password):
    captured = asyncio.run(_capture_publish(
        token="secret-token", username=username, password=password
    ))

    assert captured["headers"]["authorization"] == "Bearer secret-token"


def test_publish_ntfy_sends_no_auth_when_no_credentials_are_configured():
    captured = asyncio.run(_capture_publish())

    assert "authorization" not in captured["headers"]


def test_publish_ntfy_checks_response_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await publish_ntfy(
                client, "https://ntfy.example.com", "alerts", "", "", "", "x"
            )

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(scenario())
