"""API dashboard, Stats and expenses without a database: date and year
bounds, amount parsing, the canonical expense digest, the cursor kind,
numbers-only Stats and the admission lanes."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.api_cursor import decode_cursor, encode_cursor, filter_digest
from app.api_errors import ApiError
from app.api_expenses import _write_values, api_day
from app.api_stats import _stats
from app.expense_ledger import Expense, expense_digest, parse_expense_amount, resolve_treatment
from app.stats_queries import stats_period, summarize_stats

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 25, 12, tzinfo=TZ)


@pytest.mark.parametrize("text,expected", [
    ("1900-01-01", date(1900, 1, 1)), ("9998-12-31", date(9998, 12, 31)), ("2026-04-15", date(2026, 4, 15)),
])
def test_api_days_accept_years_1900_through_9998(text, expected):
    assert api_day("date", text) == expected


@pytest.mark.parametrize("text", [
    "0001-01-01", "1899-12-31", "9999-01-01", "9999-12-31", "2026-02-30", "20260415", "2026-4-15",
    "2026W163", " 2026-04-15", "2026-04-15\n", "",
])
def test_api_days_refuse_anything_else(text):
    with pytest.raises(ApiError) as refused:
        api_day("date", text)
    assert refused.value.fields == {"date": "This value is not valid."}


def test_write_values_parse_dates_and_amounts_by_field():
    parsed = _write_values({"incurred_on": "2026-04-15", "amount": "12.30", "notes": "n", "trip_id": None})
    assert parsed == {"incurred_on": date(2026, 4, 15), "amount": Decimal("12.30"), "notes": "n", "trip_id": None}
    with pytest.raises(ApiError) as refused:
        _write_values({"incurred_on": "0001-01-01", "amount": "1e3", "notes": "a\x00", "vehicle_id": 2**63,
                       "trip_id": 0})
    assert refused.value.fields == {
        "incurred_on": "Invalid expense date", "amount": "Invalid expense amount",
        "notes": "This value is not valid.", "vehicle_id": "No such vehicle", "trip_id": "No such trip"}


@pytest.mark.parametrize("text,expected", [
    ("12.34", Decimal("12.34")), ("12.340", Decimal("12.34")), ("0.01", Decimal("0.01")),
    ("9999999999.99", Decimal("9999999999.99")), ("7", Decimal("7.00")),
    ("0", None), ("-1", None), ("1.234", None), ("10000000000.00", None), ("NaN", None), ("Infinity", None),
    ("sNaN", None), ("abc", None), ("", None), ("9" * 40, None),
])
def test_amounts_follow_the_web_rule(text, expected):
    assert parse_expense_amount(text) == expected


def test_treatments_default_by_category_and_other_has_none():
    assert resolve_treatment("parking", "") == "fully_business"
    assert resolve_treatment("fuel", None) == "business_use_allocated"
    assert resolve_treatment("other", "") is None
    assert resolve_treatment("other", "fully_business") == "fully_business"


def _expense(**values):
    return Expense(**{"vehicle_id": 1, "incurred_on": date(2026, 4, 15), "category": "fuel",
                      "amount": Decimal("12.30"), "treatment": "business_use_allocated", **values})


def test_the_expense_digest_covers_the_normalized_request():
    base = expense_digest(_expense())
    assert len(base) == 32
    assert expense_digest(_expense(amount=parse_expense_amount("12.3"))) == base
    for changed in (_expense(amount=Decimal("12.31")), _expense(vehicle_id=2), _expense(category="tolls"),
                    _expense(treatment="fully_business"), _expense(notes="n"), _expense(trip_id=3),
                    _expense(incurred_on=date(2026, 4, 16))):
        assert expense_digest(changed) != base


def test_expense_cursors_carry_a_date_and_their_own_kind():
    digest = filter_digest("expenses", {"year": "2026"})
    position = (datetime.combine(date(2026, 4, 15), time(), timezone.utc), 42)
    text = encode_cursor("expenses", position, digest)
    assert decode_cursor("expenses", text, digest) == position
    for kind, other_digest in (("trips", digest), ("expenses", filter_digest("expenses", {"year": "2025"}))):
        with pytest.raises(ApiError):
            decode_cursor(kind, text, other_digest)


def test_stats_responses_hold_numbers_and_never_chart_geometry():
    period = stats_period(2026, TZ, NOW)
    category_rows = [("business", 3, 3000.0), ("personal", 1, 1000.0), ("nondeductible", 1, 500.0)]
    cross = [(2025, 3, "business", 1, 1200.0), (2026, 1, "business", 2, 2000.0),
             (2026, 9, "personal", 1, 1000.0), (2026, 10, "nondeductible", 1, 500.0)]
    mileage = [(1, "Car", 1, "business", 2, 2000.0), (None, "", 9, "personal", 1, 1000.0)]
    expenses = [(1, "Car", Decimal("10.10")), (2, "Van", Decimal("0.20"))]
    summary = summarize_stats(period, NOW, category_rows, cross, mileage, expenses, {})
    body = _stats(summary).model_dump(mode="json")
    text = str(body)
    for marker in ("<svg", "viewBox", "var(--", "fill", "rect", "Q1 ", "Jan", "Unassigned", "mi"):
        assert marker not in text, marker
    assert body["totals"]["trip_count"] == 5 and body["totals"]["distance_m"] == 4500.0
    assert body["totals"]["business_share"] == 3000.0 / 4500.0
    assert [c["category"] for c in body["totals"]["categories"]] == [
        "business", "personal", "unclassified", "nondeductible"]
    assert body["multiyear"]["through_month"] == 9
    assert [year["year"] for year in body["multiyear"]["years"]] == [2025, 2026]
    assert [len(year["months"]) for year in body["multiyear"]["years"]] == [9, 9]
    assert [(q["year"], q["quarter"]) for q in body["share_trend"]["quarters"]] == [
        (2025, 1), (2025, 2), (2025, 3), (2025, 4), (2026, 1), (2026, 2), (2026, 3), (2026, 4)]
    assert body["share_trend"]["future_quarters_excluded"] is False
    vehicles = {v["vehicle_id"]: v for v in body["vehicles"]["items"]}
    assert vehicles[1]["expense_total"] == "10.10" and vehicles[2]["expense_total"] == "0.20"
    assert vehicles[None]["vehicle_name"] is None and vehicles[None]["expense_total"] == "0.00"
    assert vehicles[1]["deduction"] is None and body["vehicles"]["unassigned_trip_count"] == 1


def test_bearer_routes_use_the_web_lanes():
    from app import api_expenses, api_stats, ui
    lanes = {(method, route.path): route.capacity_lane
             for make in (ui.make_router, api_expenses.make_router, api_stats.make_router)
             for route in make().routes for method in route.methods}
    assert {key: lanes[key] for key in (
        ("GET", "/api/v1/expenses"), ("GET", "/api/v1/expenses/comparison"), ("POST", "/api/v1/expenses"),
        ("PATCH", "/api/v1/expenses/{expense_id}"), ("DELETE", "/api/v1/expenses/{expense_id}"),
        ("GET", "/api/v1/dashboard/week"), ("GET", "/api/v1/stats"),
    )} == {
        ("GET", "/api/v1/expenses"): "foreground", ("GET", "/api/v1/expenses/comparison"): "foreground",
        ("POST", "/api/v1/expenses"): "form", ("PATCH", "/api/v1/expenses/{expense_id}"): "form",
        ("DELETE", "/api/v1/expenses/{expense_id}"): None,
        ("GET", "/api/v1/dashboard/week"): "navigation", ("GET", "/api/v1/stats"): "foreground",
    }
    # The web routes they mirror.
    assert lanes[("GET", "/expenses")] == "foreground" and lanes[("POST", "/expenses")] == "form"
    assert lanes[("POST", "/expenses/{expense_id}/update")] == "form"
    assert lanes[("POST", "/expenses/{expense_id}/delete")] is None
    assert lanes[("GET", "/")] == "navigation" and lanes[("GET", "/stats")] == "foreground"
    assert lanes[("GET", "/report/{year}")] == "foreground"
