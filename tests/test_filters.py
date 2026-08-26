"""Tests for tag/date-range filtering helpers."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.ui import (
    VEHICLE_FILTER_UNASSIGNED,
    _escape_ilike_term,
    _month_bounds,
    _month_page_url,
    _multiyear_window,
    _parse_vehicle_id,
    _trip_filter_sql,
    _url_with_filters,
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


def test_filter_sql_search_term_is_no_filter_by_default():
    where, params = _trip_filter_sql("", None, None)
    assert where == ""
    assert params == []


def test_filter_sql_empty_and_whitespace_search_term_is_byte_identical_to_no_filter():
    no_q_where, no_q_params = _trip_filter_sql("", None, None)
    for term in ("", "   ", "\t\n"):
        where, params = _trip_filter_sql("", None, None, q=term)
        assert where == no_q_where
        assert params == no_q_params


def test_filter_sql_search_term_matches_notes_purpose_places_and_addresses():
    where, params = _trip_filter_sql("", None, None, q="zephyr")
    assert where == (
        "WHERE (notes ILIKE %s ESCAPE '\\' OR purpose ILIKE %s ESCAPE '\\' OR "
        "(SELECT name FROM places WHERE id = trips.start_place_id) ILIKE %s ESCAPE '\\' OR "
        "(SELECT name FROM places WHERE id = trips.end_place_id) ILIKE %s ESCAPE '\\' OR "
        "(SELECT address FROM geocode_cache\n"
        "     WHERE lat = ROUND(ST_Y(trips.start_geom::geometry)::numeric, 4)\n"
        "       AND lon = ROUND(ST_X(trips.start_geom::geometry)::numeric, 4)) "
        "ILIKE %s ESCAPE '\\' OR "
        "(SELECT address FROM geocode_cache\n"
        "     WHERE lat = ROUND(ST_Y(trips.end_geom::geometry)::numeric, 4)\n"
        "       AND lon = ROUND(ST_X(trips.end_geom::geometry)::numeric, 4)) "
        "ILIKE %s ESCAPE '\\')"
    )
    assert params == ["%zephyr%"] * 6


def test_filter_sql_search_term_combines_with_category_vehicle_and_range():
    from_dt, to_dt = parse_date_range("2026-06-01", "2026-06-30", TZ)
    where, params = _trip_filter_sql(
        "business", from_dt, to_dt, vehicle_id=3, q="zephyr",
    )
    assert where.startswith(
        "WHERE category = %s AND vehicle_id = %s AND started_at >= %s "
        "AND started_at < %s AND (notes ILIKE %s ESCAPE '\\'"
    )
    assert params[:4] == ["business", 3, from_dt, to_dt]
    assert params[4:] == ["%zephyr%"] * 6


def test_escape_ilike_term_escapes_percent_underscore_and_backslash():
    assert _escape_ilike_term("50% off") == "50\\% off"
    assert _escape_ilike_term("a1_b2") == "a1\\_b2"
    assert _escape_ilike_term(r"C:\route") == r"C:\\route"
    assert _escape_ilike_term("100%_done\\") == "100\\%\\_done\\\\"


def test_url_with_filters_includes_search_term():
    assert _url_with_filters("/trips", "", "", "", "zephyr") == "/trips?q=zephyr"


def test_url_with_filters_drops_empty_or_whitespace_search_term():
    assert _url_with_filters("/trips", "", "", "", "") == "/trips"
    assert _url_with_filters("/trips", "", "", "", "   ") == "/trips"


def test_url_with_filters_stores_the_search_term_stripped():
    """The predicate strips before matching, so a padded term must not build
    a different link than the same term unpadded.
    """
    assert (
        _url_with_filters("/trips", "", "", "", "  zephyr  ")
        == _url_with_filters("/trips", "", "", "", "zephyr")
    )


def test_month_page_url_preserves_search_term():
    assert _month_page_url(
        2026, 7, 25, "business", "2026-07-01", "2026-07-31", "3", "zephyr",
    ) == (
        "/trips/month/2026/7?offset=25&category=business&from=2026-07-01"
        "&to=2026-07-31&vehicle=3&q=zephyr"
    )


NOW = datetime(2026, 8, 24, tzinfo=TZ)
YEARS_PRESENT = [2020, 2021, 2022, 2023, 2024, 2025, 2026]


def test_multiyear_window_current_year_clamps_to_today():
    years, cutoff_month, cutoff_day = _multiyear_window(YEARS_PRESENT, 2026, NOW)
    assert years == [2022, 2023, 2024, 2025, 2026]
    assert (cutoff_month, cutoff_day) == (8, 24)


def test_multiyear_window_past_year_ends_at_selected_year_and_is_fully_elapsed():
    years, cutoff_month, cutoff_day = _multiyear_window(YEARS_PRESENT, 2024, NOW)
    assert years == [2020, 2021, 2022, 2023, 2024]
    assert (cutoff_month, cutoff_day) == (12, 31)


def test_multiyear_window_future_year_degrades_to_current_year_cutoff():
    # A hand-crafted ?year=2030 shouldn't claim an unelapsed year is
    # complete, so >= now.year still uses today's same-elapsed-period cutoff.
    years, cutoff_month, cutoff_day = _multiyear_window(YEARS_PRESENT, 2030, NOW)
    assert years == [2022, 2023, 2024, 2025, 2026]
    assert (cutoff_month, cutoff_day) == (8, 24)


def test_multiyear_window_excludes_years_after_selected_year():
    years, _, _ = _multiyear_window([2024, 2025, 2026], 2024, NOW)
    assert years == [2024]


def test_multiyear_window_fewer_than_five_years_available():
    years, _, _ = _multiyear_window([2025, 2026], 2026, NOW)
    assert years == [2025, 2026]
