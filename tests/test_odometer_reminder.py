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

import app.odometer_reminder as reminder_module
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


def test_publish_reminder_passes_its_exact_message_to_shared_transport(monkeypatch):
    captured = {}

    async def capture(*args):
        captured["message"] = args[-1]

    monkeypatch.setattr(reminder_module, "publish_ntfy", capture)

    async def scenario():
        await publish_reminder(
            None, "https://ntfy.example.com", "alerts", "token", "", "",
            ["Truck"], "https://miles.example.com",
        )

    asyncio.run(scenario())
    assert captured["message"] == (
        "Odograph: log an odometer reading for Truck (vehicle).\n"
        "https://miles.example.com/settings"
    )
