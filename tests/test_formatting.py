from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.formatting import format_duration, format_miles, format_usd
from app.main import make_templates


def test_format_duration_preserves_minute_and_hour_forms():
    started_at = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)

    assert (
        format_duration(started_at, started_at + timedelta(minutes=59, seconds=59))
        == "59m"
    )
    assert format_duration(started_at, started_at + timedelta(hours=1, minutes=5)) == "1h 05m"


def test_format_miles_uses_one_decimal_place():
    assert format_miles(1609.344) == "1.0"


def test_format_usd_preserves_missing_and_currency_forms():
    assert format_usd(None) == "--"
    assert format_usd(1234.5) == "$1,234.50"


def test_jinja_filters_delegate_to_shared_formatters():
    templates = make_templates(
        SimpleNamespace(display_tz=timezone.utc, app_version="test")
    )
    started_at = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    trip = {"started_at": started_at, "ended_at": started_at + timedelta(minutes=30)}

    assert templates.env.filters["duration"](trip) == "30m"
    assert templates.env.filters["mi"](1609.344) == "1.0"
    assert templates.env.filters["usd"](1234.5) == "$1,234.50"
