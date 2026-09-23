"""Template tests for the Settings page's "Device status" section: rendered
rows for known devices, the quiet empty state, and that no coordinates ever
appear in the rendered output.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.main import make_templates
from types import SimpleNamespace

TZ = timezone.utc


def _render(device_fixes):
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    return templates.env.get_template("settings.html").render(
        boundary_overrides=[], rates=[], vehicles=[], odometer=[], places=[],
        rules=[], geocode_enabled=False, device_fixes=device_fixes,
        user={"name": "Tester"}, csrf_token="test",
    )


def test_device_status_lists_each_device_with_timestamps_and_point_count():
    body = _render([
        {
            "device_label": "Phone", "latest_tid": "phone",
            "newest_received_at": datetime(2026, 7, 18, 9, 30, tzinfo=TZ),
            "newest_recorded_at": datetime(2026, 7, 18, 9, 29, tzinfo=TZ),
            "point_count": 1234,
        },
        {
            "device_label": "Bench tester", "latest_tid": "test",
            "newest_received_at": datetime(2026, 7, 1, 8, 0, tzinfo=TZ),
            "newest_recorded_at": datetime(2026, 7, 1, 8, 0, tzinfo=TZ),
            "point_count": 37,
        },
    ])

    assert "Device status" in body
    assert 'id="device-fixes-table"' in body
    assert "No location fixes received yet" not in body
    assert "Phone" in body
    assert "(phone)" in body
    assert "Bench tester" in body
    assert "(test)" in body
    assert ">1234<" in body
    assert ">37<" in body
    # local_dt formats with the day name and HH:MM, per app/main.py's filter.
    assert "2026-07-18 09:30" in body
    assert "2026-07-18 09:29" in body


def test_device_status_shows_quiet_empty_state_with_no_points():
    body = _render([])

    assert "Device status" in body
    assert "No location fixes received yet." in body
    assert 'id="device-fixes-table"' not in body


def test_device_status_never_renders_coordinates():
    body = _render([
        {
            "device_label": "Phone", "latest_tid": "phone",
            "newest_received_at": datetime(2026, 7, 18, 9, 30, tzinfo=TZ),
            "newest_recorded_at": datetime(2026, 7, 18, 9, 29, tzinfo=TZ),
            "point_count": 5,
        },
    ])
    # Device status now sits at the bottom of the page inside the Diagnostics
    # disclosure, not directly ahead of Mileage rates -- bound the slice by
    # the disclosure's close instead.
    section = body.split("Device status", 1)[1].split("</details>", 1)[0]

    assert "lat" not in section.lower()
    assert "lon" not in section.lower()
