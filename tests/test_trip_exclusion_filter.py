"""Tests for the trip filter's exclusion parameter and shared constants."""
from __future__ import annotations

from app.ui import EXCLUSION_LABELS, EXCLUSIONS, _trip_filter_sql


def test_default_exclusion_reproduces_pre_parameter_sql_and_params():
    """Calling with no `exclusion` argument at all must build exactly the
    SQL and params `_trip_filter_sql` produced before this parameter
    existed, so every pre-existing caller and every URL built from one
    keeps working unchanged.
    """
    where, params = _trip_filter_sql("business", None, None, vehicle_id=3, q="zephyr")
    assert where == (
        "WHERE category = %s AND vehicle_id = %s AND (notes ILIKE %s ESCAPE '\\' OR "
        "purpose ILIKE %s ESCAPE '\\' OR "
        "(SELECT name FROM places WHERE id = trips.start_place_id) ILIKE %s ESCAPE '\\' OR "
        "(SELECT name FROM places WHERE id = trips.end_place_id) ILIKE %s ESCAPE '\\' OR "
        "start_label ILIKE %s ESCAPE '\\' OR end_label ILIKE %s ESCAPE '\\' OR "
        "(SELECT address FROM geocode_cache\n"
        "     WHERE lat = ROUND(ST_Y(trips.start_geom::geometry)::numeric, 4)\n"
        "       AND lon = ROUND(ST_X(trips.start_geom::geometry)::numeric, 4)) "
        "ILIKE %s ESCAPE '\\' OR "
        "(SELECT address FROM geocode_cache\n"
        "     WHERE lat = ROUND(ST_Y(trips.end_geom::geometry)::numeric, 4)\n"
        "       AND lon = ROUND(ST_X(trips.end_geom::geometry)::numeric, 4)) "
        "ILIKE %s ESCAPE '\\')"
    )
    assert params == ["business", 3] + ["%zephyr%"] * 8


def test_no_filters_at_all_is_still_an_empty_where_clause():
    where, params = _trip_filter_sql("", None, None)
    assert where == ""
    assert params == []


def test_empty_exclusion_is_no_filter():
    where, params = _trip_filter_sql("", None, None, exclusion="")
    assert where == ""
    assert params == []


def test_unrecognized_exclusion_is_ignored_same_as_empty():
    where, params = _trip_filter_sql("", None, None, exclusion="bogus")
    assert where == ""
    assert params == []


def test_each_enum_value_produces_equality_predicate_with_bound_param():
    for value in EXCLUSIONS:
        where, params = _trip_filter_sql("", None, None, exclusion=value)
        assert where == "WHERE exclusion = %s"
        assert params == [value]


def test_none_sentinel_means_normal_trips_only_with_no_param():
    where, params = _trip_filter_sql("", None, None, exclusion="none")
    assert where == "WHERE exclusion IS NULL"
    assert params == []


def test_exclusion_combines_with_category_and_vehicle_in_declaration_order():
    where, params = _trip_filter_sql(
        "business", None, None, vehicle_id=3, exclusion="not_my_vehicle",
    )
    assert where == "WHERE category = %s AND vehicle_id = %s AND exclusion = %s"
    assert params == ["business", 3, "not_my_vehicle"]


def test_none_sentinel_combines_with_category_and_date_range():
    from datetime import datetime, timezone
    from_dt = datetime(2026, 6, 1, tzinfo=timezone.utc)
    to_dt = datetime(2026, 7, 1, tzinfo=timezone.utc)
    where, params = _trip_filter_sql(
        "personal", from_dt, to_dt, exclusion="none",
    )
    assert where == (
        "WHERE category = %s AND exclusion IS NULL AND started_at >= %s AND started_at < %s"
    )
    assert params == ["personal", from_dt, to_dt]


def test_exclusions_and_labels_agree_on_keys():
    assert set(EXCLUSIONS) == set(EXCLUSION_LABELS)
