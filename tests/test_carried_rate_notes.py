"""The carried-rate note on each web page that shows a deduction priced
with an earlier year's rate, and its absence otherwise."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.dashboard import (
    DailyDistanceBreakdown,
    DeductionEstimate,
    DistanceBreakdown,
    WeekDashboard,
    WeekNav,
    build_week_dashboard,
)
from app.main import make_templates
from app.rates import CarriedRate, YearRate, carried_rate
from app.stats_vehicle import VehicleBreakdown, VehicleStats, build_vehicle_breakdown

pytestmark = pytest.mark.unit

TZ = ZoneInfo("America/Los_Angeles")
MI = 1609.344
RATES = {2026: YearRate(0.7250)}
CARRIED = CarriedRate(year=2027, source_year=2026, rate=0.7250)
SENTENCE = (
    "No 2027 mileage rate is saved. 2027 figures use the 2026 rate of "
    "$0.7250/mi until you add one."
)
LINK = '<a href="/settings">Add the 2027 rate</a>.'


def _templates():
    return make_templates(SimpleNamespace(display_tz=TZ, app_version="test", missing_trip_gap_m=1000.0))


def _render(name: str, **context) -> str:
    return _templates().env.get_template(name).render(**context)


# --- dashboard -----------------------------------------------------------

def _trip(trip_id: int, started_at: datetime, distance_m: float = 10 * MI) -> dict:
    return {
        "id": trip_id, "started_at": started_at, "category": "business",
        "display_distance_m": distance_m, "prev_end_gap_m": None,
        "prev_trip_ended_at": None, "prev_trip_end_lat": None, "prev_trip_end_lon": None,
        "prev_trip_end_place_name": None, "missing_trip_covered": False,
        "start_place_name": None, "start_lat": None, "start_lon": None, "start_address": None,
    }


def _week(trips, rates, week_start):
    return build_week_dashboard(
        trips, Decimal("0"), rates, week_start, TZ,
        datetime(2027, 6, 1, tzinfo=timezone.utc), 1000.0,
    )


def test_dashboard_week_spanning_a_rated_and_a_carried_year_names_the_carried_year():
    # Mon 2026-12-28 .. Sun 2027-01-03.
    trips = [
        _trip(1, datetime(2026, 12, 29, 12, tzinfo=TZ)),
        _trip(2, datetime(2027, 1, 2, 12, tzinfo=TZ)),
    ]
    dashboard = _week(trips, RATES, date(2026, 12, 28))
    assert dashboard.deduction.available is True
    assert dashboard.deduction.amount == pytest.approx(2 * 10 * 0.7250)
    assert dashboard.deduction.carried == CARRIED


def test_dashboard_names_the_latest_carried_year():
    trips = [
        _trip(1, datetime(2026, 12, 29, 12, tzinfo=TZ)),
        _trip(2, datetime(2027, 1, 2, 12, tzinfo=TZ)),
    ]
    dashboard = _week(trips, {2025: YearRate(0.70)}, date(2026, 12, 28))
    assert dashboard.deduction.carried == CarriedRate(2027, 2025, 0.70)


def test_dashboard_has_no_carried_note_for_a_rated_week_or_a_week_without_business():
    rated = _week([_trip(1, datetime(2026, 7, 14, 12, tzinfo=TZ))], RATES, date(2026, 7, 13))
    assert rated.deduction.carried is None
    empty = _week([], RATES, date(2027, 1, 4))
    assert empty.deduction.carried is None


def _dashboard(deduction: DeductionEstimate) -> WeekDashboard:
    return WeekDashboard(
        trip_count=1,
        distance=DistanceBreakdown(0.0, 0.0, 0.0, 0.0),
        daily_series=[
            DailyDistanceBreakdown(day=date(2027, 1, 4 + i), business_m=0.0, personal_m=0.0,
                                   unclassified_m=0.0, nondeductible_m=0.0)
            for i in range(7)
        ],
        expense_total=Decimal("0.00"),
        deduction=deduction,
        day_groups=[],
        attention=None,
        nav=WeekNav(week_start=date(2027, 1, 4), week_end=date(2027, 1, 10),
                    prev_week_start=date(2026, 12, 28), next_week_start=date(2027, 1, 11),
                    is_current_week=True),
    )


def test_dashboard_hero_shows_the_note_with_its_link_only_when_carried():
    carried = _render("_dashboard_hero.html", dashboard=_dashboard(
        DeductionEstimate(amount=7.25, available=True, carried=CARRIED)))
    assert "$7.25" in carried
    assert f"{SENTENCE} {LINK}" in carried
    assert carried.index("$7.25") < carried.index(SENTENCE)

    plain = _render("_dashboard_hero.html", dashboard=_dashboard(
        DeductionEstimate(amount=7.25, available=True)))
    assert "mileage rate is saved" not in plain


# --- archive -------------------------------------------------------------

def _archive_context(**overrides):
    context = dict(
        months=[], vehicles=[], recent_purposes=[], user={"sub": "test"}, csrf="token",
        places=[], notice="", filter_category="", filter_from="", filter_to="",
        filter_vehicle="", filter_q="", filter_exclusion="",
        export_url=lambda *a, **k: "/", review_url="/review",
        ytd_year=2027, ytd_deduction=0.0, ytd_carried=CARRIED,
    )
    context.update(overrides)
    return context


@pytest.mark.parametrize("template", ["trips.html", "_trip_archive_response.html", "_trip_archive_ytd.html"])
def test_archive_year_to_date_shows_the_note_when_carried(template):
    body = _render(template, **_archive_context())
    ytd = body[body.index('id="trip-archive-ytd"'):]
    assert "$0.00" in ytd
    assert f"{SENTENCE} {LINK}" in ytd

    plain = _render(template, **_archive_context(ytd_year=2026, ytd_carried=None))
    assert "mileage rate is saved" not in plain


def _month(**overrides):
    month = {
        "label": "January 2027", "year": 2027, "month_num": 1, "trip_count": 2,
        "total_m": 20 * MI, "business_m": 10 * MI, "business_deduction": 7.25,
        "carried_rate": CARRIED,
    }
    month.update(overrides)
    return month


def test_month_summary_shows_the_short_form_after_the_deduction():
    body = _render("_trip_month_summary.html", month=_month())
    assert "$7.25 deduction (2026 rate)" in body
    assert "mileage rate is saved" not in body

    plain = _render("_trip_month_summary.html", month=_month(carried_rate=None))
    assert "rate)" not in plain
    assert "$7.25 deduction\n" in plain


def test_month_summary_short_form_in_the_full_archive_render():
    month = {**_month(), "trips": [], "has_more": False, "loaded_depth": 0, "next_url": "/"}
    body = _render("trips.html", **_archive_context(months=[month]))
    assert "$7.25 deduction (2026 rate)" in body


def test_build_month_summary_records_the_carried_rate():
    from app.ui.trips import _build_month_summary

    row = {"trip_count": 1, "total_m": MI, "business_m": MI}
    assert _build_month_summary(2027, 1, row, RATES)["carried_rate"] == CARRIED
    assert _build_month_summary(2026, 1, row, RATES)["carried_rate"] is None


# --- stats ---------------------------------------------------------------

def _vehicle():
    return VehicleStats(
        vehicle_id=1, vehicle_name="Truck", business_m=MI, personal_m=0.0,
        unclassified_m=0.0, total_m=MI, expense_total=0.0, deduction=0.725,
    )


def test_stats_vehicle_breakdown_records_the_carried_rate_when_it_has_rows():
    rows = [(1, "Truck", 3, "business", 1, MI)]
    assert build_vehicle_breakdown(rows, [], 2027, RATES).carried == CARRIED
    assert build_vehicle_breakdown(rows, [], 2026, RATES).carried is None
    assert build_vehicle_breakdown([], [], 2027, RATES).carried is None


def test_stats_vehicle_table_shows_the_note_under_the_table_when_carried():
    body = _render("_stats_vehicle.html", vehicle_breakdown=VehicleBreakdown(
        vehicles=[_vehicle()], assigned_vehicle_count=1, carried=CARRIED))
    assert f"{SENTENCE} {LINK}" in body
    assert body.index("</table>") < body.index(SENTENCE)

    plain = _render("_stats_vehicle.html", vehicle_breakdown=VehicleBreakdown(
        vehicles=[_vehicle()], assigned_vehicle_count=1))
    assert "mileage rate is saved" not in plain

    empty = _render("_stats_vehicle.html", vehicle_breakdown=VehicleBreakdown(carried=CARRIED))
    assert "mileage rate is saved" not in empty


# --- settings ------------------------------------------------------------

def test_settings_rates_fragment_shows_the_note_without_a_link_when_carried():
    rates = [{"year": 2026, "rate_per_mi": 0.725, "rate_h2_per_mi": None, "h2_start_month": None}]
    body = _render("_rates_table.html", rates=rates, rates_carried=CARRIED)
    assert f'<p class="muted">{SENTENCE}</p>' in body
    assert "Add the 2027 rate" not in body

    plain = _render("_rates_table.html", rates=rates, rates_carried=None)
    assert "mileage rate is saved" not in plain
    assert '</table>\n</div>\n\n<p class="muted">Tick <em>split from</em>' in plain


def test_settings_current_year_carried_reads_the_table_rows(monkeypatch):
    import app.ui.settings as settings

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2027, 3, 1, tzinfo=tz)

    monkeypatch.setattr(settings, "datetime", Frozen)
    rows = [{"year": 2026, "rate_per_mi": 0.725, "rate_h2_per_mi": None, "h2_start_month": None}]
    assert settings._current_year_carried(rows, TZ) == CARRIED
    rows.append({"year": 2027, "rate_per_mi": 0.75, "rate_h2_per_mi": None, "h2_start_month": None})
    assert settings._current_year_carried(rows, TZ) is None
