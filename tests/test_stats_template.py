"""Template test for stats.html's ranking tables.

Same make_templates()/.render() convention as tests/test_expenses.py.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.main import make_templates
from app.stats import Dashboard

TZ = ZoneInfo("America/Los_Angeles")
USER = {"sub": "test"}


def _dashboard(**overrides) -> Dashboard:
    defaults = dict(
        year=2026, trip_count=1, business_m=1.0, personal_m=0.0,
        unclassified_m=0.0, unclassified_trips=0, weekly_chart="", monthly_chart="",
        routes=[{"start_name": "Home", "end_name": "Work", "trip_count": 3, "total_m": 10.0}],
        places=[{"name": "Home", "visit_count": 2}],
        unnamed_trip_count=0,
    )
    defaults.update(overrides)
    return Dashboard(**defaults)


def _render(stats: Dashboard) -> str:
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    return templates.env.get_template("stats.html").render(stats=stats, user=USER, csrf="token")


def test_ranking_headings_sit_directly_before_their_tables():
    # "Top named routes" and "Most-used places" are each immediately followed
    # by a bare <table> -- the same adjacency "By vehicle" has on the report
    # page, and the reason the global first-column padding fix covers this
    # page too, not just the report.
    body = _render(_dashboard())
    assert "<h2>Top named routes</h2>\n    \n    <table>" in body
    assert "<h2>Most-used places</h2>\n    \n    <table>" in body

    css = (Path(__file__).parents[1] / "static/style.css").read_text()
    assert "th:first-child, td:first-child { padding-left: 0; }" in css
