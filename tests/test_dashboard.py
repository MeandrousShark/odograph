"""Tests for the weekly dashboard's pure presentation model."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.dashboard import (
    DailyDistanceBreakdown,
    build_week_dashboard,
    format_week_range,
    parse_week_anchor,
    week_bounds,
)
from app.rates import YearRate

LA = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc


def _duration_hours(start: datetime, end: datetime) -> float:
    """`end - start` on two aware datetimes that share the same `ZoneInfo`
    instance silently ignores DST (a documented Python quirk: same-`tzinfo`
    subtraction skips `utcoffset()` and just diffs the naive fields) --
    converting both to UTC first is the only way to see the true elapsed
    wall-clock duration across a spring-forward/fall-back boundary.
    """
    return (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / 3600


def _trip(trip_id: int, started_at: datetime, category: str, distance_m: float, **overrides) -> dict:
    trip = {
        "id": trip_id,
        "started_at": started_at,
        "category": category,
        "display_distance_m": distance_m,
        "prev_end_gap_m": None,
        "prev_trip_ended_at": None,
        "prev_trip_end_lat": None,
        "prev_trip_end_lon": None,
        "prev_trip_end_place_name": None,
        "missing_trip_covered": False,
        "start_place_name": None,
        "start_lat": None,
        "start_lon": None,
        "start_address": None,
    }
    trip.update(overrides)
    return trip


# --- format_week_range ------------------------------------------------

def test_format_week_range_uses_friendly_us_dates():
    assert format_week_range(date(2026, 7, 13), date(2026, 7, 19)) == (
        "Jul 13, 2026 - Jul 19, 2026"
    )


# --- parse_week_anchor -------------------------------------------------

def test_parse_week_anchor_absent_falls_back_to_today():
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    assert parse_week_anchor("", LA, now) == now.astimezone(LA).date()


def test_parse_week_anchor_valid_date_used_verbatim():
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    assert parse_week_anchor("2026-03-04", LA, now) == date(2026, 3, 4)


def test_parse_week_anchor_malformed_falls_back_to_today():
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    assert parse_week_anchor("not-a-date", LA, now) == now.astimezone(LA).date()


def test_parse_week_anchor_nonsense_date_falls_back_to_today():
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    # 2026-02-30 doesn't exist -- date.fromisoformat rejects it.
    assert parse_week_anchor("2026-02-30", LA, now) == now.astimezone(LA).date()


@pytest.mark.parametrize("weekday_date", [
    date(2026, 7, 13),  # Monday
    date(2026, 7, 14),  # Tuesday
    date(2026, 7, 16),  # Thursday
    date(2026, 7, 18),  # Saturday
    date(2026, 7, 19),  # Sunday
])
def test_week_bounds_normalizes_any_weekday_to_its_monday(weekday_date):
    assert week_bounds(weekday_date, LA).monday == date(2026, 7, 13)


# --- week_bounds ---------------------------------------------------------

def test_week_bounds_spring_forward_week_is_167_hours():
    bounds = week_bounds(date(2026, 3, 8), LA)
    assert bounds.monday == date(2026, 3, 2)
    assert bounds.start == datetime(2026, 3, 2, tzinfo=LA)
    assert bounds.end == datetime(2026, 3, 9, tzinfo=LA)
    assert _duration_hours(bounds.start, bounds.end) == pytest.approx(167.0)


def test_week_bounds_fall_back_week_is_169_hours():
    bounds = week_bounds(date(2026, 11, 1), LA)
    assert bounds.monday == date(2026, 10, 26)
    assert bounds.start == datetime(2026, 10, 26, tzinfo=LA)
    assert bounds.end == datetime(2026, 11, 2, tzinfo=LA)
    assert _duration_hours(bounds.start, bounds.end) == pytest.approx(169.0)


def test_week_bounds_month_crossing_week():
    bounds = week_bounds(date(2026, 1, 29), LA)
    assert bounds.monday == date(2026, 1, 26)
    assert bounds.start == datetime(2026, 1, 26, tzinfo=LA)
    assert bounds.end == datetime(2026, 2, 2, tzinfo=LA)


def test_week_bounds_new_year_week():
    bounds = week_bounds(date(2027, 1, 1), LA)
    assert bounds.monday == date(2026, 12, 28)
    assert bounds.start == datetime(2026, 12, 28, tzinfo=LA)
    assert bounds.end == datetime(2027, 1, 4, tzinfo=LA)
    assert _duration_hours(bounds.start, bounds.end) == pytest.approx(168.0)


# --- build_week_dashboard: empty week -------------------------------------

def test_empty_week_zeros():
    dashboard = build_week_dashboard(
        [], Decimal("0.00"), {}, date(2026, 7, 13), LA,
        datetime(2026, 7, 15, 12, 0, tzinfo=UTC), 1000.0,
    )
    assert dashboard.trip_count == 0
    assert dashboard.distance.total_m == 0.0
    assert dashboard.distance.business_m == 0.0
    assert dashboard.distance.personal_m == 0.0
    assert dashboard.distance.unclassified_m == 0.0
    assert dashboard.deduction.available is True
    assert dashboard.deduction.amount == 0.0
    assert dashboard.day_groups == []
    assert [entry.day for entry in dashboard.daily_series] == [
        date(2026, 7, day) for day in range(13, 20)
    ]
    assert all(isinstance(entry, DailyDistanceBreakdown) for entry in dashboard.daily_series)
    assert all(
        entry.business_m == entry.personal_m == entry.unclassified_m == entry.nondeductible_m == 0.0
        for entry in dashboard.daily_series
    )
    assert dashboard.attention is None
    assert dashboard.nav.week_start == date(2026, 7, 13)


# --- build_week_dashboard: category breakdown -----------------------------

def test_category_breakdown_keeps_unclassified_separate():
    trips = [
        _trip(1, datetime(2026, 7, 13, 9, tzinfo=UTC), "business", 10_000.0),
        _trip(2, datetime(2026, 7, 14, 9, tzinfo=UTC), "personal", 5_000.0),
        _trip(3, datetime(2026, 7, 15, 9, tzinfo=UTC), "unclassified", 2_000.0),
    ]
    dashboard = build_week_dashboard(
        trips, Decimal("0"), {2026: YearRate(0.70)}, date(2026, 7, 13), LA,
        datetime(2026, 7, 20, tzinfo=UTC), 1000.0,
    )
    assert dashboard.trip_count == 3
    assert dashboard.distance.total_m == 17_000.0
    assert dashboard.distance.business_m == 10_000.0
    assert dashboard.distance.personal_m == 5_000.0
    assert dashboard.distance.unclassified_m == 2_000.0


def test_exclusions_win_before_category_in_week_dashboard():
    trips = [
        _trip(1, datetime(2026, 7, 13, 9, tzinfo=UTC), "business", 1000.0),
        _trip(
            2, datetime(2026, 7, 14, 9, tzinfo=UTC), "business", 2000.0,
            exclusion="not_my_vehicle",
        ),
        _trip(
            3, datetime(2026, 7, 15, 9, tzinfo=UTC), "business", 3000.0,
            exclusion="not_deductible",
        ),
    ]
    dashboard = build_week_dashboard(
        trips, Decimal("0.00"), {2026: YearRate(0.70)}, date(2026, 7, 13), LA,
        datetime(2026, 7, 15, 12, tzinfo=UTC), 1000.0,
    )

    assert dashboard.trip_count == 2
    assert dashboard.distance.total_m == 4000.0
    assert dashboard.distance.business_m == 1000.0
    assert dashboard.distance.nondeductible_m == 3000.0
    assert dashboard.deduction.amount == pytest.approx(1000.0 / 1609.344 * 0.70)
    assert [trip["id"] for group in dashboard.day_groups for trip in group.trips] == [3, 2, 1]
    daily = {entry.day: entry for entry in dashboard.daily_series}
    assert daily[date(2026, 7, 13)].business_m == 1000.0
    assert daily[date(2026, 7, 14)].business_m == 0.0
    assert daily[date(2026, 7, 15)].nondeductible_m == 3000.0
    assert all(
        entry.business_m == entry.personal_m == entry.unclassified_m == entry.nondeductible_m == 0.0
        for day, entry in daily.items()
        if day not in {date(2026, 7, 13), date(2026, 7, 15)}
    )


def test_attention_counts_excluded_unclassified_without_changing_mileage_math():
    trips = [
        _trip(
            1, datetime(2026, 7, 13, 9, tzinfo=UTC), "unclassified", 2000.0,
            exclusion="not_my_vehicle", prev_end_gap_m=5000.0,
            prev_trip_ended_at=datetime(2026, 7, 13, 8, tzinfo=UTC),
            start_lat=47.0, start_lon=-122.0,
        ),
        _trip(
            2, datetime(2026, 7, 14, 9, tzinfo=UTC), "unclassified", 3000.0,
            exclusion="not_deductible",
        ),
    ]
    dashboard = build_week_dashboard(
        trips, Decimal("0.00"), {}, date(2026, 7, 13), LA,
        datetime(2026, 7, 15, 12, tzinfo=UTC), 1000.0,
    )

    assert dashboard.attention is not None
    assert dashboard.attention.unclassified_count == 2
    assert dashboard.attention.missing_trip_count == 0
    assert dashboard.attention.missing_trip_url is None
    assert dashboard.trip_count == 1
    assert dashboard.distance.total_m == 3000.0
    assert dashboard.distance.unclassified_m == 0.0
    assert dashboard.distance.nondeductible_m == 3000.0
    assert [trip["id"] for group in dashboard.day_groups for trip in group.trips] == [2, 1]
    daily = {entry.day: entry for entry in dashboard.daily_series}
    assert daily[date(2026, 7, 13)].business_m == 0.0
    assert daily[date(2026, 7, 14)].nondeductible_m == 3000.0
    assert all(
        entry.business_m == entry.personal_m == entry.unclassified_m == entry.nondeductible_m == 0.0
        for day, entry in daily.items()
        if day not in {date(2026, 7, 14)}
    )


def test_daily_series_maps_each_category_to_its_local_day():
    trips = [
        _trip(1, datetime(2026, 7, 13, 9, tzinfo=UTC), "business", 1000.0),
        _trip(2, datetime(2026, 7, 14, 9, tzinfo=UTC), "personal", 2000.0),
        _trip(3, datetime(2026, 7, 15, 9, tzinfo=UTC), "unclassified", 3000.0),
        _trip(
            4, datetime(2026, 7, 16, 9, tzinfo=UTC), "business", 4000.0,
            exclusion="not_deductible",
        ),
    ]
    dashboard = build_week_dashboard(
        trips, Decimal("0"), {2026: YearRate(0.70)}, date(2026, 7, 13), LA,
        datetime(2026, 7, 20, tzinfo=UTC), 1000.0,
    )

    assert [entry.day for entry in dashboard.daily_series] == [
        date(2026, 7, day) for day in range(13, 20)
    ]
    assert dashboard.daily_series[0].business_m == 1000.0
    assert dashboard.daily_series[1].personal_m == 2000.0
    assert dashboard.daily_series[2].unclassified_m == 3000.0
    assert dashboard.daily_series[3].nondeductible_m == 4000.0
    assert dashboard.daily_series[3].business_m == 0.0
    assert dashboard.daily_series[0].total_m == pytest.approx(1000.0)
    assert dashboard.daily_series[1].total_m == pytest.approx(2000.0)


def test_daily_series_uses_started_at_local_date_for_cross_midnight_trip():
    trip = _trip(
        1, datetime(2026, 7, 14, 6, 30, tzinfo=UTC), "business", 1000.0,
        ended_at=datetime(2026, 7, 14, 8, 30, tzinfo=UTC),
    )
    dashboard = build_week_dashboard(
        [trip], Decimal("0"), {}, date(2026, 7, 13), LA,
        datetime(2026, 7, 20, tzinfo=UTC), 1000.0,
    )

    assert dashboard.daily_series[0].day == date(2026, 7, 13)
    assert dashboard.daily_series[0].business_m == 1000.0
    assert dashboard.daily_series[1].business_m == 0.0
    assert dashboard.day_groups[0].day == date(2026, 7, 13)


def test_daily_series_has_seven_local_days_in_dst_transition_week():
    trip = _trip(1, datetime(2026, 3, 8, 9, tzinfo=UTC), "personal", 1000.0)
    dashboard = build_week_dashboard(
        [trip], Decimal("0"), {}, date(2026, 3, 8), LA,
        datetime(2026, 3, 10, tzinfo=UTC), 1000.0,
    )

    assert [entry.day for entry in dashboard.daily_series] == [
        date(2026, 3, day) for day in range(2, 9)
    ]
    assert dashboard.daily_series[-1].personal_m == 1000.0


# --- build_week_dashboard: deduction bucketing ----------------------------

def test_deduction_across_new_year_week_uses_two_year_rates():
    trips = [
        _trip(1, datetime(2026, 12, 29, 12, tzinfo=UTC), "business", 1609.344),  # Dec 29 local
        _trip(2, datetime(2027, 1, 2, 20, tzinfo=UTC), "business", 1609.344),    # Jan 2 local
    ]
    rates = {2026: YearRate(0.67), 2027: YearRate(0.70)}
    dashboard = build_week_dashboard(
        trips, Decimal("0"), rates, date(2026, 12, 28), LA,
        datetime(2027, 1, 5, tzinfo=UTC), 1000.0,
    )
    assert dashboard.deduction.available is True
    assert dashboard.deduction.amount == pytest.approx(0.67 + 0.70)


def test_deduction_across_midyear_split_rate_boundary():
    trips = [
        _trip(1, datetime(2026, 6, 29, 20, tzinfo=UTC), "business", 1609.344),  # June 29 local
        _trip(2, datetime(2026, 7, 2, 20, tzinfo=UTC), "business", 1609.344),   # July 2 local
    ]
    rates = {2026: YearRate(0.585, rate_h2_per_mi=0.625, h2_start_month=7)}
    dashboard = build_week_dashboard(
        trips, Decimal("0"), rates, date(2026, 6, 29), LA,
        datetime(2026, 7, 6, tzinfo=UTC), 1000.0,
    )
    assert dashboard.deduction.available is True
    assert dashboard.deduction.amount == pytest.approx(0.585 + 0.625)


def test_deduction_unavailable_when_any_nonzero_bucket_lacks_a_rate():
    trips = [
        _trip(1, datetime(2019, 6, 1, 12, tzinfo=UTC), "business", 1609.344),  # no rate, no fallback
        _trip(2, datetime(2026, 1, 6, 12, tzinfo=UTC), "business", 1609.344),  # rate available
    ]
    rates = {2026: YearRate(0.67)}
    dashboard = build_week_dashboard(
        trips, Decimal("0"), rates, date(2026, 1, 5), LA,
        datetime(2026, 1, 10, tzinfo=UTC), 1000.0,
    )
    assert dashboard.deduction.available is False
    assert dashboard.deduction.amount is None


# --- build_week_dashboard: expenses ----------------------------------------

def test_expense_total_passed_through_unchanged():
    dashboard = build_week_dashboard(
        [], Decimal("42.50"), {}, date(2026, 7, 13), LA,
        datetime(2026, 7, 15, tzinfo=UTC), 1000.0,
    )
    assert dashboard.expense_total == Decimal("42.50")


# --- build_week_dashboard: day grouping -------------------------------------

def test_day_groups_ordered_newest_day_and_trip_first():
    trips = [
        _trip(1, datetime(2026, 7, 13, 15, tzinfo=UTC), "business", 1000.0),  # 08:00 local
        _trip(2, datetime(2026, 7, 13, 20, tzinfo=UTC), "business", 1000.0),  # 13:00 local, later
        _trip(3, datetime(2026, 7, 15, 15, tzinfo=UTC), "personal", 1000.0),
    ]
    dashboard = build_week_dashboard(
        trips, Decimal("0"), {}, date(2026, 7, 13), LA,
        datetime(2026, 7, 20, tzinfo=UTC), 1000.0,
    )
    assert [g.day for g in dashboard.day_groups] == [date(2026, 7, 15), date(2026, 7, 13)]
    same_day_group = dashboard.day_groups[1]
    assert [t["id"] for t in same_day_group.trips] == [2, 1]


def test_today_yesterday_flags_only_correct_relative_to_now():
    now = datetime(2026, 7, 16, 18, 0, tzinfo=UTC)  # 11:00 local July 16
    trips = [
        _trip(1, datetime(2026, 7, 16, 15, tzinfo=UTC), "business", 1000.0),  # today
        _trip(2, datetime(2026, 7, 15, 15, tzinfo=UTC), "business", 1000.0),  # yesterday
        _trip(3, datetime(2026, 7, 14, 15, tzinfo=UTC), "business", 1000.0),
    ]
    dashboard = build_week_dashboard(
        trips, Decimal("0"), {}, date(2026, 7, 13), LA, now, 1000.0,
    )
    by_day = {g.day: g for g in dashboard.day_groups}
    assert by_day[date(2026, 7, 16)].is_today is True
    assert by_day[date(2026, 7, 16)].is_yesterday is False
    assert by_day[date(2026, 7, 15)].is_yesterday is True
    assert by_day[date(2026, 7, 15)].is_today is False
    assert by_day[date(2026, 7, 14)].is_today is False
    assert by_day[date(2026, 7, 14)].is_yesterday is False


def test_today_yesterday_flags_false_when_viewing_a_past_week():
    now = datetime(2026, 7, 20, 18, 0, tzinfo=UTC)  # actual now is a later week
    trips = [_trip(1, datetime(2026, 7, 7, 15, tzinfo=UTC), "business", 1000.0)]
    dashboard = build_week_dashboard(
        trips, Decimal("0"), {}, date(2026, 7, 6), LA, now, 1000.0,
    )
    assert dashboard.day_groups[0].is_today is False
    assert dashboard.day_groups[0].is_yesterday is False


# --- build_week_dashboard: attention strip ---------------------------------

def test_attention_strip_absent_when_nothing_to_act_on():
    trips = [_trip(1, datetime(2026, 7, 13, 15, tzinfo=UTC), "business", 1000.0)]
    dashboard = build_week_dashboard(
        trips, Decimal("0"), {}, date(2026, 7, 13), LA,
        datetime(2026, 7, 20, tzinfo=UTC), 1000.0,
    )
    assert dashboard.attention is None


def test_attention_strip_counts_unclassified_trips():
    trips = [
        _trip(1, datetime(2026, 7, 13, 15, tzinfo=UTC), "unclassified", 1000.0),
        _trip(2, datetime(2026, 7, 14, 15, tzinfo=UTC), "unclassified", 1000.0),
    ]
    dashboard = build_week_dashboard(
        trips, Decimal("0"), {}, date(2026, 7, 13), LA,
        datetime(2026, 7, 20, tzinfo=UTC), 1000.0,
    )
    assert dashboard.attention is not None
    assert dashboard.attention.unclassified_count == 2
    assert dashboard.attention.missing_trip_count == 0
    assert dashboard.attention.missing_trip_url is None
    assert "/review" in dashboard.attention.review_url
    assert "from=2026-07-13" in dashboard.attention.review_url
    assert "to=2026-07-19" in dashboard.attention.review_url


def test_attention_strip_counts_missing_trip_badges():
    prev_end = datetime(2026, 7, 13, 8, tzinfo=UTC)
    trip = _trip(
        5, datetime(2026, 7, 13, 15, tzinfo=UTC), "business", 1000.0,
        prev_end_gap_m=5000.0, prev_trip_ended_at=prev_end,
        start_lat=47.0, start_lon=-122.0,
    )
    dashboard = build_week_dashboard(
        [trip], Decimal("0"), {}, date(2026, 7, 13), LA,
        datetime(2026, 7, 20, tzinfo=UTC), 1000.0,
    )
    assert dashboard.attention is not None
    assert dashboard.attention.missing_trip_count == 1
    assert dashboard.attention.unclassified_count == 0
    assert dashboard.attention.missing_trip_url is not None
    assert "bridge_trip=5" in dashboard.attention.missing_trip_url


def test_attention_strip_absent_when_missing_trip_gap_below_threshold():
    prev_end = datetime(2026, 7, 13, 8, tzinfo=UTC)
    trip = _trip(
        5, datetime(2026, 7, 13, 15, tzinfo=UTC), "business", 1000.0,
        prev_end_gap_m=500.0, prev_trip_ended_at=prev_end,
        start_lat=47.0, start_lon=-122.0,
    )
    dashboard = build_week_dashboard(
        [trip], Decimal("0"), {}, date(2026, 7, 13), LA,
        datetime(2026, 7, 20, tzinfo=UTC), 1000.0,
    )
    assert dashboard.attention is None
