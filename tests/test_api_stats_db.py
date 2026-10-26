"""API v1 weekly dashboard and Stats through the real application: totals
equal to the web pages' for the same account and period, numbers only,
account isolation and strict dates."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from api_expense_support import seed_ledger
from test_api_trips_db import OTHER, TEST_DB, TZ_NAME, _get, _run

pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")
FIXTURES = Path(__file__).parent / "fixtures" / "api_v1"
TZ = ZoneInfo(TZ_NAME)
GEOMETRY = ("<svg", "viewBox", "var(--", "fill", "<rect", "path")


def _without_ids(node):
    if isinstance(node, dict):
        return {key: _without_ids(value) for key, value in node.items() if key != "vehicle_id"}
    if isinstance(node, list):
        return [_without_ids(value) for value in node]
    return node


async def _seed(state, account=None):
    async with state.pools[account or state.owner].connection() as conn:
        return await seed_ledger(conn)


class _Pages:
    """Template context of the web pages rendered during a test."""

    def __init__(self, monkeypatch):
        import app.ui.stats
        self.contexts: list[dict] = []
        original = app.ui.stats.render_page

        async def recording(request, template, context, *args, **kwargs):
            self.contexts.append(context)
            return await original(request, template, context, *args, **kwargs)
        monkeypatch.setattr(app.ui.stats, "render_page", recording)

    async def context(self, client, path) -> dict:
        response = await client.get(path)
        assert response.status_code == 200, response.text
        return self.contexts[-1]


def _money(value) -> str | None:
    return None if value is None else format(value, ".2f")


def _distance(source) -> dict:
    return {"total_m": source.total_m, "business_m": source.business_m, "personal_m": source.personal_m,
            "unclassified_m": source.unclassified_m, "nondeductible_m": source.nondeductible_m}


def test_dashboard_totals_equal_the_web_dashboard(monkeypatch):
    async def check(state):
        pages = _Pages(monkeypatch)
        await _seed(state)
        await _seed(state, OTHER)
        today = datetime.now(TZ).date().isoformat()
        for day in ("2026-04-15", "2026-04-13", "2026-04-19", "2026-04-20", "2025-06-12", today):
            response = await _get(state, "/api/v1/dashboard/week", date=day)
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store, private"
            body = response.json()
            web = (await pages.context(state.client, f"/?week={day}"))["dashboard"]
            assert body["week_start"] == web.nav.week_start.isoformat()
            assert body["week_end"] == web.nav.week_end.isoformat()
            assert (body["previous_week_start"], body["next_week_start"]) == (
                web.nav.prev_week_start.isoformat(), web.nav.next_week_start.isoformat())
            assert body["is_current_week"] == web.nav.is_current_week
            assert body["trip_count"] == web.trip_count
            assert body["distance"] == _distance(web.distance)
            assert body["days"] == [{"date": d.day.isoformat(), **_distance(d)} for d in web.daily_series]
            assert body["expense_total"] == _money(web.expense_total)
            assert body["deduction"] == (_money(web.deduction.amount) if web.deduction.available else None)
            assert body["missing_trip_count"] == (web.attention.missing_trip_count if web.attention else 0)
            assert not any(marker in response.text for marker in GEOMETRY)
        # Without a date it is this week, as the web's default.
        body = (await _get(state, "/api/v1/dashboard/week")).json()
        assert body["is_current_week"] is True
        assert body == (await _get(state, "/api/v1/dashboard/week", date=today)).json()
    _run(check)


def _stats_matches_web(body, context):
    web = context["stats"]
    assert body["year"] == context["filter_year"]
    assert (body["period_start"], body["period_end"]) == (
        context["period_start"].isoformat(), context["period_end"].isoformat())
    categories = {item["category"]: item for item in body["totals"]["categories"]}
    assert body["totals"]["trip_count"] == web.trip_count
    for category in ("business", "personal", "unclassified", "nondeductible"):
        assert categories[category]["distance_m"] == getattr(web, f"{category}_m"), category
    assert categories["unclassified"]["trip_count"] == web.unclassified_trips
    assert body["totals"]["distance_m"] == pytest.approx(web.total_m)
    assert body["totals"]["business_share"] == pytest.approx(web.business_share)
    assert [year["year"] for year in body["multiyear"]["years"]] == context["multiyear"].years
    through = body["multiyear"]["through_month"]
    assert (context["multiyear"].coverage_note is not None) == (0 < through < 12)
    trend = context["trend"]
    quarters = body["share_trend"]["quarters"]
    assert sum(1 for q in quarters if q["business_share"] is not None) == trend.plottable_quarters
    assert any(q["nondeductible_m"] for q in quarters) == trend.has_nondeductible
    assert body["share_trend"]["future_quarters_excluded"] == (trend.coverage_note is not None)
    breakdown = context["vehicle_breakdown"]
    assert body["vehicles"]["unassigned_trip_count"] == breakdown.unassigned_trip_count
    assert [
        (v["vehicle_id"], v["total_m"], v["business_m"], v["personal_m"], v["unclassified_m"],
         v["nondeductible_m"], v["expense_total"], v["deduction"])
        for v in body["vehicles"]["items"]
    ] == [
        (v.vehicle_id, v.total_m, v.business_m, v.personal_m, v.unclassified_m, v.nondeductible_m,
         _money(v.expense_total), _money(v.deduction))
        for v in breakdown.vehicles
    ]


def test_stats_totals_equal_the_web_stats_page(monkeypatch):
    async def check(state):
        pages = _Pages(monkeypatch)
        await _seed(state)
        await _seed(state, OTHER)
        this_year = datetime.now(TZ).year
        for year in (2024, 2025, 2026, this_year):
            response = await _get(state, "/api/v1/stats", year=year)
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store, private"
            _stats_matches_web(response.json(), await pages.context(state.client, f"/stats?year={year}"))
            assert not any(marker in response.text for marker in GEOMETRY)
        assert (await _get(state, "/api/v1/stats")).json() == (await _get(state, "/api/v1/stats", year=this_year)).json()
    _run(check)


def test_another_accounts_rows_never_count():
    async def check(state):
        await _seed(state, OTHER)
        week = (await _get(state, "/api/v1/dashboard/week", date="2026-04-15")).json()
        assert week["trip_count"] == 0 and week["expense_total"] == "0.00" and week["distance"]["total_m"] == 0
        for year in (2025, 2026):
            stats = (await _get(state, "/api/v1/stats", year=year)).json()
            assert stats["totals"]["trip_count"] == 0 and stats["multiyear"]["years"] == []
            assert stats["vehicles"]["items"] == [] and stats["share_trend"]["quarters"] == []
        theirs = (await _get(state, "/api/v1/stats", OTHER, year=2025)).json()
        assert theirs["totals"]["trip_count"] == 5
    _run(check)


def test_responses_match_the_synthetic_fixtures():
    async def check(state):
        await _seed(state)
        await _seed(state, OTHER)
        for name, path, params in (("dashboard_week_response.json", "/api/v1/dashboard/week", {"date": "2026-04-15"}),
                                   ("stats_response.json", "/api/v1/stats", {"year": 2025})):
            body = (await _get(state, path, **params)).json()
            assert _without_ids(body) == _without_ids(json.loads((FIXTURES / name).read_text())), name
    _run(check)


def test_dates_and_years_outside_1900_to_9998_are_refused():
    async def check(state):
        for day in ("0001-01-01", "1899-12-31", "9999-01-01", "9999-12-31", "2026-02-30", "20260415", "junk", ""):
            response = await _get(state, "/api/v1/dashboard/week", date=day)
            assert response.status_code == 422, (day, response.text)
            assert response.json()["error"]["fields"] == {"date": "This value is not valid."}, day
        for day in ("1900-01-01", "9998-12-31"):
            assert (await _get(state, "/api/v1/dashboard/week", date=day)).status_code == 200, day
        for year in ("1", "1899", "9999", "abc"):
            response = await _get(state, "/api/v1/stats", year=year)
            assert response.status_code == 422, year
            assert response.json()["error"]["fields"] == {"year": "This value is not valid."}, year
        for year in (1900, 9998):
            assert (await _get(state, "/api/v1/stats", year=year)).status_code == 200, year
    _run(check)


def test_dashboard_and_stats_authenticate_by_bearer_only():
    async def check(state):
        for path in ("/api/v1/dashboard/week", "/api/v1/stats"):
            response = await state.client.get(path)
            assert (response.status_code, response.json()["error"]["code"]) == (401, "invalid_token")
            response = await state.client.get(path, headers={"Authorization": "Bearer nope"})
            assert response.status_code == 401
    _run(check)
