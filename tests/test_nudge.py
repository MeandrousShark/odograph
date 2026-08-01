"""Pure and HTTP-boundary tests for the weekly ntfy nudge."""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.nudge import latest_window_end, nudge_date_range, nudge_message, publish_nudge

TZ = ZoneInfo("America/Los_Angeles")


def test_latest_window_end_uses_current_sunday_after_send_hour():
    now = datetime(2026, 7, 12, 18, 1, tzinfo=TZ)
    assert latest_window_end(now, 18) == datetime(2026, 7, 12, 18, tzinfo=TZ)


def test_latest_window_end_uses_prior_sunday_before_send_hour():
    now = datetime(2026, 7, 12, 17, 59, tzinfo=TZ)
    assert latest_window_end(now, 18) == datetime(2026, 7, 5, 18, tzinfo=TZ)


def test_latest_window_end_makes_monday_retry_describe_same_window():
    now = datetime(2026, 7, 13, 9, tzinfo=TZ)
    assert latest_window_end(now, 18) == datetime(2026, 7, 12, 18, tzinfo=TZ)


def test_latest_window_end_requires_timezone_aware_time():
    with pytest.raises(ValueError, match="timezone-aware"):
        latest_window_end(datetime(2026, 7, 12, 18), 18)


def test_nudge_message_is_count_only_with_optional_review_link():
    window_end = datetime(2026, 7, 12, 18, tzinfo=TZ)
    assert nudge_message(2, window_end, "") == "Odograph: 2 unclassified trips in the past week."
    assert nudge_message(1, window_end, "https://miles.example.com/") == (
        "Odograph: 1 unclassified trip in the past week.\n"
        "https://miles.example.com/review"
    )


def test_nudge_date_range_is_the_seven_days_ending_at_window_boundary():
    start, end = nudge_date_range(datetime(2026, 7, 12, 18, tzinfo=TZ))
    assert start.isoformat() == "2026-07-05"
    assert end.isoformat() == "2026-07-12"


async def _publish_scenario():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["content"] = request.content.decode()
        return httpx.Response(200, json={"id": "test"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await publish_nudge(
            client, "https://ntfy.example.com/", "mileage-reminders", "secret-token", "", "", 3,
            datetime(2026, 7, 12, 18, tzinfo=TZ), "https://miles.example.com",
        )
    assert captured["url"] == "https://ntfy.example.com/mileage-reminders"
    assert captured["headers"]["authorization"] == "Bearer secret-token"
    assert captured["headers"]["title"] == "Odograph"
    assert captured["content"].startswith("Odograph: 3 unclassified trips")


def test_publish_nudge_posts_count_only_notification_with_auth():
    asyncio.run(_publish_scenario())


async def _basic_auth_publish_scenario():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200, json={"id": "test"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await publish_nudge(
            client, "https://ntfy.example.com", "alerts", "ignored-token", "testuser", "password", 1,
            datetime(2026, 7, 12, 18, tzinfo=TZ), "",
        )
    assert captured["headers"]["authorization"] == "Basic dGVzdHVzZXI6cGFzc3dvcmQ="


def test_publish_nudge_uses_basic_auth_when_configured():
    asyncio.run(_basic_auth_publish_scenario())
