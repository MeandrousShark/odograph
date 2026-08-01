"""Pure and HTTP-boundary tests for the quarterly odometer reminder
(app/odometer_reminder.py). Mirrors tests/test_nudge.py's split for the
weekly nudge; the quarter-boundary and due-vehicle pure decisions
themselves live in tests/test_odometer.py alongside app/odometer.py, where
that logic is actually defined.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.odometer_reminder import publish_reminder, reminder_message

TZ = ZoneInfo("America/Los_Angeles")


def test_reminder_message_names_single_vehicle():
    assert reminder_message(["Truck"], "") == (
        "Odograph: log an odometer reading for Truck (vehicle)."
    )


def test_reminder_message_names_multiple_vehicles_with_plural_noun():
    assert reminder_message(["Truck", "Sedan"], "") == (
        "Odograph: log an odometer reading for Truck, Sedan (vehicles)."
    )


def test_reminder_message_appends_settings_link_when_app_url_set():
    message = reminder_message(["Truck"], "https://miles.example.com/")
    assert message == (
        "Odograph: log an odometer reading for Truck (vehicle).\n"
        "https://miles.example.com/settings"
    )


async def _publish_scenario():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["content"] = request.content.decode()
        return httpx.Response(200, json={"id": "test"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await publish_reminder(
            client, "https://ntfy.example.com/", "mileage-reminders", "secret-token", "", "",
            ["Truck"], "https://miles.example.com",
        )
    assert captured["url"] == "https://ntfy.example.com/mileage-reminders"
    assert captured["headers"]["authorization"] == "Bearer secret-token"
    assert captured["headers"]["title"] == "Odograph"
    assert captured["content"] == (
        "Odograph: log an odometer reading for Truck (vehicle).\n"
        "https://miles.example.com/settings"
    )


def test_publish_reminder_posts_vehicle_names_with_bearer_auth():
    asyncio.run(_publish_scenario())


async def _basic_auth_publish_scenario():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200, json={"id": "test"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await publish_reminder(
            client, "https://ntfy.example.com", "alerts", "ignored-token", "testuser", "password",
            ["Truck"], "",
        )
    assert captured["headers"]["authorization"] == "Basic dGVzdHVzZXI6cGFzc3dvcmQ="


def test_publish_reminder_uses_basic_auth_when_configured():
    asyncio.run(_basic_auth_publish_scenario())
