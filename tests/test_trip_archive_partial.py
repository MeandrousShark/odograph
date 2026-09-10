from __future__ import annotations

import pytest
from fastapi import HTTPException
from datetime import datetime
from zoneinfo import ZoneInfo

from app.ui.trips import (
    _archive_date_preset_ranges,
    _infer_archive_date_preset,
    _parse_archive_loaded_depth,
    _resolve_archive_date_filter,
)


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        "[]",
        '"2026-07"',
        '{"2026-7": 1}',
        '{"2026-13": 1}',
        '{"0000-01": 1}',
        '{"10000-01": 1}',
        '{"2026-07": true}',
        '{"2026-07": "1"}',
        '{"2026-07": -1}',
        '{"2026-07": 100001}',
    ],
)
def test_archive_loaded_depth_rejects_invalid_transient_metadata(raw):
    with pytest.raises(HTTPException) as exc_info:
        _parse_archive_loaded_depth(raw, 25)

    assert exc_info.value.status_code == 400


def test_archive_loaded_depth_accepts_short_and_zero_rendered_depths():
    assert _parse_archive_loaded_depth(
        '{"2026-07": 0, "2026-08": 3}', 25
    ) == {(2026, 7): 0, (2026, 8): 3}


TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 3, 8, 1, 30, tzinfo=TZ)


def test_archive_date_presets_use_display_timezone_and_calendar_boundaries():
    assert _archive_date_preset_ranges(TZ, NOW) == {
        "all": ("", ""),
        "this_month": ("2026-03-01", "2026-03-31"),
        "last_month": ("2026-02-01", "2026-02-28"),
        "this_year": ("2026-01-01", "2026-12-31"),
        "custom": ("", ""),
    }


def test_archive_date_presets_convert_request_time_into_display_timezone():
    # 2026-03-01 00:30 UTC is still February 28 in Los Angeles.
    utc_instant = datetime(2026, 3, 1, 0, 30, tzinfo=ZoneInfo("UTC"))
    assert _archive_date_preset_ranges(TZ, utc_instant)["this_month"] == (
        "2026-02-01", "2026-02-28"
    )


def test_archive_date_presets_handle_january_and_leap_years():
    january = datetime(2026, 1, 15, tzinfo=TZ)
    leap_day = datetime(2028, 2, 29, 23, 59, tzinfo=TZ)
    assert _archive_date_preset_ranges(TZ, january)["last_month"] == (
        "2025-12-01", "2025-12-31"
    )
    assert _archive_date_preset_ranges(TZ, leap_day)["this_month"] == (
        "2028-02-01", "2028-02-29"
    )


def test_archive_date_preset_inference_keeps_unknown_ranges_custom():
    assert _infer_archive_date_preset("", "", TZ, NOW) == "all"
    assert _infer_archive_date_preset("2026-03-01", "2026-03-31", TZ, NOW) == "this_month"
    assert _infer_archive_date_preset("2026-03-03", "", TZ, NOW) == "custom"
    assert _infer_archive_date_preset("not-a-date", "", TZ, NOW) == "custom"


def test_archive_date_filter_resolves_transient_presets_to_concrete_bounds():
    assert _resolve_archive_date_filter("", "", "last_month", TZ, NOW) == (
        "2026-02-01", "2026-02-28", "last_month"
    )
    assert _resolve_archive_date_filter("2026-03-03", "", "custom", TZ, NOW) == (
        "2026-03-03", "", "custom"
    )
    assert _resolve_archive_date_filter("2026-03-03", "", "unknown", TZ, NOW) == (
        "2026-03-03", "", "custom"
    )


def test_archive_date_filter_keeps_explicit_custom_sticky_with_open_or_empty_bounds():
    # With concrete bounds, "custom" happens to be what inference would
    # already return. The case that actually needs the explicit branch is
    # empty bounds: _infer_archive_date_preset would otherwise call that
    # "all", collapsing an intentionally opened Custom control back to All
    # the next time some other control (e.g. Vehicle) re-resolves this filter.
    assert _resolve_archive_date_filter("", "", "custom", TZ, NOW) == ("", "", "custom")
    assert _resolve_archive_date_filter("2026-03-03", "", "custom", TZ, NOW) == (
        "2026-03-03", "", "custom"
    )
    assert _resolve_archive_date_filter("", "2026-03-20", "custom", TZ, NOW) == (
        "", "2026-03-20", "custom"
    )


def test_archive_date_presets_span_dst_spring_forward_and_fall_back_transitions():
    ny = ZoneInfo("America/New_York")
    # 2026-03-08 is the US spring-forward transition (2 a.m. skips to 3 a.m.
    # local time); the resolved calendar month must still run the full 1st
    # through 31st even though that day is one hour short.
    spring_forward_month = datetime(2026, 3, 15, 12, 0, tzinfo=ny)
    assert _archive_date_preset_ranges(ny, spring_forward_month)["this_month"] == (
        "2026-03-01", "2026-03-31"
    )
    # 2026-11-01 is the US fall-back transition (2 a.m. repeats); the
    # resolved calendar month must still run the full 1st through 30th even
    # though that day is one hour long.
    fall_back_month = datetime(2026, 11, 15, 12, 0, tzinfo=ny)
    assert _archive_date_preset_ranges(ny, fall_back_month)["this_month"] == (
        "2026-11-01", "2026-11-30"
    )
