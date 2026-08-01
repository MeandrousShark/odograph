"""Template tests for the quarterly/custom-range report UI:
the Q1-Q4 preset block in `report.html`, and `report_range.html`'s
deliberately narrower scope (no odometer/expense sections).
Same `make_templates`/`.render()` convention as `tests/test_expenses.py`.
"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.main import make_templates
from app.rates import YearRate
from app.report import build_annual_report, build_range_report

TZ = ZoneInfo("America/Los_Angeles")
MILE = 1609.344
RATES = {2026: YearRate(0.7250)}
USER = {"sub": "test"}


def _trip(month: int, category: str = "business", miles: float = 1) -> dict:
    return {
        "started_at": datetime(2026, month, 15, 12, tzinfo=TZ),
        "display_distance_m": miles * MILE,
        "category": category,
        "purpose": "Client visit",
        "has_gap": False,
        "snap_status": "ok",
        "source": "detected",
        "vehicle_name": "Truck",
    }


def _render(name: str, **context) -> str:
    templates = make_templates(SimpleNamespace(display_tz=TZ))
    return templates.env.get_template(name).render(**context)


def test_report_html_has_q1_through_q4_preset_links_for_displayed_year():
    report = build_annual_report([], RATES, TZ, 2026)
    body = _render(
        "report.html", report=report, odometer_coverage=[], expenses=[],
        expense_report=SimpleNamespace(comparisons=[]), user=USER, csrf="token",
    )
    assert '/report/range?from=2026-01-01&to=2026-03-31' in body
    assert '/report/range?from=2026-04-01&to=2026-06-30' in body
    assert '/report/range?from=2026-07-01&to=2026-09-30' in body
    assert '/report/range?from=2026-10-01&to=2026-12-31' in body


def test_report_range_html_has_no_odometer_or_expense_sections():
    report = build_range_report(
        [_trip(4), _trip(5)], RATES, TZ, date(2026, 4, 1), date(2026, 6, 30)
    )
    body = _render("report_range.html", report=report, user=USER, csrf="token")
    assert "2026 Q2" in body
    assert "Odometer coverage" not in body
    assert "Odometer reconciliation" not in body
    assert "Standard vs. actual expense estimate" not in body


def test_report_range_html_empty_state_has_no_tables():
    report = build_range_report([], RATES, TZ, date(2026, 4, 1), date(2026, 6, 30))
    body = _render("report_range.html", report=report, user=USER, csrf="token")
    assert "No trips recorded" in body
    assert "<table>" not in body
