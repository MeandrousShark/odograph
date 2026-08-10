"""Pure and HTTP-boundary tests for the weekly ntfy nudge."""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import app.nudge as nudge_module
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


def test_publish_nudge_passes_its_exact_message_to_shared_transport(monkeypatch):
    captured = {}

    async def capture(*args):
        captured["message"] = args[-1]

    monkeypatch.setattr(nudge_module, "publish_ntfy", capture)

    async def scenario():
        await publish_nudge(
            None, "https://ntfy.example.com", "alerts", "token", "", "", 3,
            datetime(2026, 7, 12, 18, tzinfo=TZ), "https://miles.example.com",
        )

    asyncio.run(scenario())
    assert captured["message"] == (
        "Odograph: 3 unclassified trips in the past week.\n"
        "https://miles.example.com/review"
    )
