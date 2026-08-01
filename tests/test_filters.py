"""Tests for tag/date-range filtering helpers."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.ui import (
    VEHICLE_FILTER_UNASSIGNED,
    _month_bounds,
    _month_page_url,
    _parse_vehicle_id,
    _trip_filter_sql,
    parse_date_range,
)

TZ = ZoneInfo("America/New_York")


def test_open_ended_both_sides():
    from_dt, to_dt = parse_date_range("", "", TZ)
    assert from_dt is None
    assert to_dt is None


def test_from_only():
    from_dt, to_dt = parse_date_range("2026-06-01", "", TZ)
    assert from_dt == datetime(2026, 6, 1, tzinfo=TZ)
    assert to_dt is None


def test_to_is_inclusive_of_the_local_day():
    # `to=2026-06-30` should include all of June 30 local time, i.e. the
    # exclusive upper bound is local midnight of July 1.
    from_dt, to_dt = parse_date_range("", "2026-06-30", TZ)
    assert from_dt is None
    assert to_dt == datetime(2026, 7, 1, tzinfo=TZ)


def test_malformed_dates_are_ignored():
    from_dt, to_dt = parse_date_range("not-a-date", "2026-13-40", TZ)
    assert from_dt is None
    assert to_dt is None


def test_malformed_from_does_not_affect_valid_to():
    from_dt, to_dt = parse_date_range("garbage", "2026-06-30", TZ)
    assert from_dt is None
    assert to_dt == datetime(2026, 7, 1, tzinfo=TZ)


def test_filter_sql_no_filters():
    where, params = _trip_filter_sql("", None, None)
    assert where == ""
    assert params == []


def test_filter_sql_category_only():
    where, params = _trip_filter_sql("business", None, None)
    assert where == "WHERE category = %s"
    assert params == ["business"]


def test_filter_sql_unknown_category_ignored():
    where, params = _trip_filter_sql("bogus", None, None)
    assert where == ""
    assert params == []


def test_filter_sql_full_range():
    from_dt, to_dt = parse_date_range("2026-06-01", "2026-06-30", TZ)
    where, params = _trip_filter_sql("personal", from_dt, to_dt)
    assert where == "WHERE category = %s AND started_at >= %s AND started_at < %s"
    assert params == ["personal", from_dt, to_dt]


def test_filter_sql_no_vehicle_by_default():
    where, params = _trip_filter_sql("", None, None)
    assert where == ""
    assert params == []


def test_filter_sql_vehicle_only():
    where, params = _trip_filter_sql("", None, None, vehicle_id=3)
    assert where == "WHERE vehicle_id = %s"
    assert params == [3]


def test_filter_sql_vehicle_combines_with_category_and_range():
    from_dt, to_dt = parse_date_range("2026-06-01", "2026-06-30", TZ)
    where, params = _trip_filter_sql("business", from_dt, to_dt, vehicle_id=3)
    assert where == "WHERE category = %s AND vehicle_id = %s AND started_at >= %s AND started_at < %s"
    assert params == ["business", 3, from_dt, to_dt]


def test_parse_vehicle_id_empty_and_malformed_are_none():
    assert _parse_vehicle_id("") is None
    assert _parse_vehicle_id("not-a-number") is None


def test_month_bounds_use_local_dst_offsets():
    start, end = _month_bounds(2026, 3, TZ)
    assert start.isoformat() == "2026-03-01T00:00:00-05:00"
    assert end.isoformat() == "2026-04-01T00:00:00-04:00"


def test_month_page_url_preserves_active_filters():
    assert _month_page_url(
        2026, 7, 25, "business", "2026-07-01", "2026-07-31", "3"
    ) == (
        "/trips/month/2026/7?offset=25&category=business&from=2026-07-01"
        "&to=2026-07-31&vehicle=3"
    )


def test_parse_vehicle_id_valid():
    assert _parse_vehicle_id("3") == 3


def test_parse_vehicle_id_recognizes_unassigned_sentinel():
    assert _parse_vehicle_id("none") == VEHICLE_FILTER_UNASSIGNED


def test_filter_sql_unassigned_vehicle_is_is_null_with_no_param():
    where, params = _trip_filter_sql("", None, None, vehicle_id=VEHICLE_FILTER_UNASSIGNED)
    assert where == "WHERE vehicle_id IS NULL"
    assert params == []


def test_filter_sql_unassigned_vehicle_combines_with_category_and_range():
    from_dt, to_dt = parse_date_range("2026-06-01", "2026-06-30", TZ)
    where, params = _trip_filter_sql(
        "business", from_dt, to_dt, vehicle_id=VEHICLE_FILTER_UNASSIGNED,
    )
    assert where == (
        "WHERE category = %s AND vehicle_id IS NULL AND started_at >= %s AND started_at < %s"
    )
    assert params == ["business", from_dt, to_dt]
