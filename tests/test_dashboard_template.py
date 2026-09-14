"""Template tests for the weekly dashboard. `dashboard.html` is rendered
directly against a
hand-built `WeekDashboard` (same "build the pure model, render it, assert on
the HTML" pattern as `tests/test_trips_template.py`) so these stay fast and
DB-free; `tests/test_dashboard_db.py` covers the route/query wiring.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.dashboard import (
    AttentionStrip,
    DayGroup,
    DailyDistanceBreakdown,
    DeductionEstimate,
    DistanceBreakdown,
    WeekDashboard,
    WeekNav,
)
from app.main import make_templates

TZ = ZoneInfo("America/Los_Angeles")
MI = 1609.344  # app.rates.METERS_PER_MILE, kept local so this file has no DB-touching import
ROOT = Path(__file__).resolve().parents[1]


def _daily_series(values=None) -> list[DailyDistanceBreakdown]:
    values = values or {}
    series = []
    for offset in range(7):
        meters = dict(
            business_m=0.0, personal_m=0.0, unclassified_m=0.0,
            nondeductible_m=0.0,
        )
        meters.update(values.get(offset, {}))
        series.append(DailyDistanceBreakdown(
            day=date(2026, 7, 13 + offset), **meters,
        ))
    return series


def _trip(trip_id: int, started_at: datetime, ended_at: datetime, category="business",
          distance_m: float = 10 * MI, source: str = "detected", **overrides) -> dict:
    trip = {
        "id": trip_id, "device": "phone", "source": source,
        "started_at": started_at, "ended_at": ended_at,
        "distance_m": distance_m, "display_distance_m": distance_m,
        "snap_status": "snapped" if source == "detected" else None,
        "point_count": 20, "has_gap": False,
        "category": category, "purpose": "", "notes": "",
        "start_lat": 47.0, "start_lon": -122.0, "end_lat": 47.1, "end_lon": -122.1,
        "start_place_name": "Home", "end_place_name": "Office",
        "vehicle_id": None, "vehicle_name": None,
        "has_route_geometry": source == "detected",
        "start_address": None, "end_address": None,
        "prev_end_gap_m": None, "prev_trip_ended_at": None,
        "prev_trip_end_lat": None, "prev_trip_end_lon": None,
        "prev_trip_end_place_name": None, "missing_trip_covered": False,
    }
    trip.update(overrides)
    return trip


def _nav(week_start=date(2026, 7, 13), week_end=date(2026, 7, 19), is_current_week=True,
          prev=date(2026, 7, 6), nxt=date(2026, 7, 20)) -> WeekNav:
    return WeekNav(
        week_start=week_start, week_end=week_end,
        prev_week_start=prev, next_week_start=nxt, is_current_week=is_current_week,
    )


def _dashboard(**overrides) -> WeekDashboard:
    defaults = dict(
        trip_count=0,
        distance=DistanceBreakdown(0.0, 0.0, 0.0, 0.0),
        daily_series=_daily_series(),
        expense_total=Decimal("0.00"),
        deduction=DeductionEstimate(amount=0.0, available=True),
        day_groups=[],
        attention=None,
        nav=_nav(),
    )
    defaults.update(overrides)
    return WeekDashboard(**defaults)


def _render(dashboard: WeekDashboard, vehicles=None) -> str:
    templates = make_templates(SimpleNamespace(
        display_tz=TZ, missing_trip_gap_m=1000.0, app_version="test",
    ))
    return templates.env.get_template("dashboard.html").render(
        dashboard=dashboard, vehicles=vehicles or [], user={"sub": "test"}, csrf="token",
    )


def _render_dashboard_tag_response(dashboard: WeekDashboard, trip: dict, vehicles=None) -> str:
    templates = make_templates(SimpleNamespace(
        display_tz=TZ, missing_trip_gap_m=1000.0, app_version="test",
    ))
    return templates.env.get_template("_dashboard_tag_response.html").render(
        dashboard=dashboard, dashboard_oob=True, trip=trip,
        vehicles=vehicles or [], recent_purposes=[], user={"sub": "test"}, csrf="token",
    )


def test_metrics_render_trip_count_distance_and_expenses():
    dashboard = _dashboard(
        trip_count=3,
        distance=DistanceBreakdown(total_m=10 * MI, business_m=6 * MI, personal_m=3 * MI, unclassified_m=1 * MI),
        expense_total=Decimal("42.50"),
        deduction=DeductionEstimate(amount=4.02, available=True),
    )
    body = _render(dashboard)
    assert ">3<" in body
    assert "10.0 mi" in body
    assert "Business" in body and "6.0 mi" in body
    assert "Personal" in body and "3.0 mi" in body
    assert "Unclassified" in body and "1.0 mi" in body
    assert "Non-deductible" in body and "0.0 mi" in body
    assert "$42.50" in body
    assert "$4.02" in body


def test_dashboard_breakdown_labels_can_shrink_and_wrap_instead_of_overflow():
    # Each label is a grid item in its own auto/1fr/auto row; without its own
    # min-width: 0, a long label like "Unclassified" refused to shrink below
    # its min-content width and painted over the value beside it once the
    # outer four-up grid narrowed the column below that width.
    body = _render(_dashboard())
    for label in ("Business", "Personal", "Unclassified", "Non-deductible"):
        assert f'class="dashboard-breakdown-label">{label}</span>' in body
    css = (ROOT / "static/style.css").read_text()
    assert ".dashboard-breakdown-label { min-width: 0; }" in css


def test_dashboard_breakdown_list_is_content_driven_not_a_hand_placed_breakpoint():
    css = (ROOT / "static/style.css").read_text()
    assert "grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); gap: var(--space-4);" in css
    narrow_css = css.split("@media (max-width: 420px)", 1)[1]
    assert "dashboard-breakdown-list" not in narrow_css


def test_exact_date_range_heading():
    body = _render(_dashboard())
    assert (
        '<h2 id="dashboard-hero-title" class="dashboard-heading"><a href="/" title="Go to current week">Jul 13, 2026 - Jul 19, 2026</a></h2>'
        in body
    )


def test_dashboard_hero_is_one_stable_partial_boundary():
    body = _render(_dashboard())
    assert body.count('id="dashboard-hero"') == 1
    assert 'aria-labelledby="dashboard-hero-title"' in body
    assert 'aria-label="Daily mileage"' in body
    assert 'id="dashboard-daily-title"' not in body


def test_dashboard_hero_uses_minimal_hierarchy():
    body = _render(_dashboard())
    for redundant_title in (
        "Weekly mileage", "Distance this week", "Distance breakdown",
        "Seven-day view", "Miles by local start date",
    ):
        assert redundant_title not in body
    assert '<h3 id="dashboard-daily-title">Daily mileage</h3>' not in body
    assert body.index('class="dashboard-total"') < body.index('class="dashboard-breakdown-list"')
    assert body.index('class="dashboard-breakdown-list"') < body.index('class="dashboard-daily"')
    assert 'aria-labelledby="dashboard-hero-title"' in body


def test_dashboard_total_deemphasizes_the_unit_without_losing_its_label():
    body = _render(_dashboard(distance=DistanceBreakdown(10 * MI, 0.0, 0.0, 0.0)))
    assert '<span class="dashboard-total-number">10.0</span> <span class="dashboard-total-unit">mi</span>' in body
    assert 'aria-label="Total mileage for this week: 10.0 miles"' in body


def test_dashboard_rows_have_reversible_category_controls_for_all_states():
    # A committed row uses the same joined pair as an unclassified one, so
    # re-tagging it (business <-> personal) is exactly the same interaction
    # as classifying it in the first place; both forms keep dashboard_week
    # and the tag endpoint/target/swap unchanged.
    trip = _trip(42, datetime(2026, 7, 13, 9, tzinfo=TZ), datetime(2026, 7, 13, 10, tzinfo=TZ), category="personal")
    body = _render(_dashboard(
        trip_count=1,
        day_groups=[DayGroup(
            day=date(2026, 7, 13), is_today=False, is_yesterday=False, trips=[trip],
        )],
    ))
    quick = body.split('class="trip-quick-actions"', 1)[1].split('</div>', 1)[0]
    assert quick.count('name="dashboard_week" value="2026-07-13"') == 2
    assert quick.count('hx-post="/trips/42/tag"') == 2
    assert quick.count('hx-target="#trip-42"') == 2
    assert quick.count('hx-swap="outerHTML"') == 2
    assert 'name="category" value="business"' in quick
    assert 'name="category" value="personal"' in quick


def test_normal_dashboard_render_has_no_out_of_band_marker():
    body = _render(_dashboard())
    assert 'hx-swap-oob=' not in body


def test_dashboard_tag_response_has_one_card_and_one_whole_hero_oob():
    trip = _trip(42, datetime(2026, 7, 13, 9, tzinfo=TZ), datetime(2026, 7, 13, 10, tzinfo=TZ))
    body = _render_dashboard_tag_response(_dashboard(trip_count=1), trip)
    assert body.count('id="trip-42"') == 1
    assert body.count('id="dashboard-hero"') == 1
    assert body.count('hx-swap-oob="outerHTML"') == 1
    assert body.index('id="trip-42"') < body.index('id="dashboard-hero"')
    assert 'id="dashboard-hero"' in body and 'class="dashboard-hero"' in body


def test_next_week_disabled_on_current_week():
    body = _render(_dashboard(nav=_nav(is_current_week=True)))
    assert 'aria-disabled="true"' in body
    assert 'href="/?week=2026-07-20"' not in body


def test_next_week_enabled_on_a_past_week():
    body = _render(_dashboard(nav=_nav(
        week_start=date(2026, 6, 29), week_end=date(2026, 7, 5),
        is_current_week=False, prev=date(2026, 6, 22), nxt=date(2026, 7, 6),
    )))
    assert 'href="/?week=2026-07-06"' in body
    assert 'aria-disabled="true"' not in body
    navigation = body.split('<nav class="week-nav"', 1)[1].split('</nav>', 1)[0]
    assert 'href="/">Current week</a>' in navigation
    assert 'href="/?week=2026-06-22"' in navigation


def test_previous_week_and_current_week_links_always_present():
    body = _render(_dashboard())
    assert 'href="/?week=2026-07-06"' in body
    assert 'aria-label="Previous week"' in body
    assert 'href="/" title="Go to current week">Jul 13, 2026 - Jul 19, 2026</a>' in body
    assert '>Current week</a>' not in body


def test_empty_week_state_keeps_secondary_metrics_and_nav():
    body = _render(_dashboard())
    assert "No trips this week." in body
    assert ">0<" in body  # trip count metric still rendered
    assert 'href="/?week=2026-07-06"' in body  # nav still present


def test_daily_chart_has_seven_zero_filled_accessible_stubs():
    body = _render(_dashboard())
    assert body.count('class="dashboard-day"') == 7
    assert body.count('class="dashboard-bar-empty"') == 7
    assert body.count('class="dashboard-day-name"') == 7
    assert 'class="dashboard-day-date"' not in body
    assert 'class="dashboard-day-total"' not in body
    assert 'role="list"' in body
    assert 'aria-label="Daily mileage for Jul 13, 2026 - Jul 19, 2026"' in body
    assert body.count('role="listitem"') == 7
    assert body.count("Business 0.0 miles; Personal 0.0 miles; Unclassified 0.0 miles; Non-deductible 0.0 miles.") == 7


def test_daily_chart_renders_total_magnitude_and_four_state_composition():
    body = _render(_dashboard(daily_series=_daily_series({
        0: dict(business_m=1609.344, personal_m=3218.688),
        1: dict(nondeductible_m=1609.344),
    })))
    assert body.count('class="dashboard-day"') == 7
    assert 'class="dashboard-bar-segment dashboard-bar-business" style="flex-basis: 33.33%;"' in body
    assert 'class="dashboard-bar-segment dashboard-bar-personal" style="flex-basis: 66.67%;"' in body
    assert 'class="dashboard-bar-segment dashboard-bar-nondeductible" style="flex-basis: 100.0%;"' in body
    assert 'aria-label="Monday, July 13: 3.0 miles total.' in body
    assert 'aria-label="Tuesday, July 14: 1.0 miles total.' in body


def test_dashboard_css_keeps_category_colors_and_responsive_hooks():
    css = (ROOT / "static/style.css").read_text()
    for state in ("business", "personal", "unclassified", "nondeductible"):
        assert f".dashboard-bar-{state} {{ background: var(--cat-" in css
    assert ".dashboard-state-business .dashboard-breakdown-swatch" in css
    assert ".dashboard-state-nondeductible .dashboard-breakdown-swatch" in css
    assert ".dashboard-day-heading::after" in css and "order: 2;" in css
    assert ".dashboard-day-heading > span:first-child { order: 1; }" in css
    assert ".dashboard-day-subtotal {\n  order: 3;" in css
    assert 'grid-template-areas: "time content category distance actions";' in css
    assert 'grid-template-areas: "time distance" "content content" "category actions";' in css
    assert ".trip-quick-actions {" in css and "box-shadow: inset 0 0 0 1px var(--border-strong);" in css
    assert ".trip-quick-form:last-child { box-shadow: inset 1px 0 0 var(--border-strong); }" in css
    # The committed-pill popup is gone; a committed row reuses this same
    # pair with the filled category treatment on the selected half.
    assert ".dashboard-row-committed-pill" not in css
    assert ".trip-quick-button-selected.category-business" in css
    assert "color-mix(in srgb, var(--cat-business) 34%, var(--control-bg));" in css
    assert ".trip-quick-button-selected.category-personal" in css
    assert "color-mix(in srgb, var(--cat-personal) 34%, var(--control-bg));" in css
    assert "--rule-fade: linear-gradient(\n    to right, transparent, var(--border) 48px" in css
    assert "background-image: var(--rule-fade);" in css
    assert "@media (min-width: 761px) {\n  .dashboard-trip-row:hover {" in css
    assert "min-width: 3.875rem; text-align: right;" in css
    assert "font-size: .94rem; font-weight: 500;" in css
    assert "@media (max-width: 760px)" in css
    assert "grid-template-columns: repeat(7, minmax(2rem, 1fr));" in css
    assert "width: .8rem; height: 7.5rem; min-height: 0;" in css
    assert "grid-template-rows: 7.5rem auto;" in css
    tablet_css = css.split("@media (max-width: 900px)", 1)[1].split("@media (max-width: 760px)", 1)[0]
    assert ".dashboard-hero-header { grid-template-columns: 1fr; align-items: start; gap: var(--space-4); }" in tablet_css
    assert "@media (max-width: 420px)" in css
    assert ".dashboard-bar-track { height: 7rem; }" in css
    assert ".dashboard-secondary li:first-child" in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_mobile_dashboard_row_and_classify_pair_drop_outline_treatment():
    css = (ROOT / "static/style.css").read_text()
    mobile_css = css.split("@media (max-width: 760px)", 1)[1].split(
        "@media (max-width: 420px)", 1
    )[0]
    # The mobile card is told apart by raised tone (background + shadow),
    # not a border, and the joined classify pair drops its ring and
    # internal divider at this width, replaced by a per-half recessed fill.
    assert "border: 1px solid var(--border); border-radius: var(--radius-md);" not in mobile_css
    assert ":first-child { border-top: 1px solid var(--border); }" not in mobile_css
    assert "background: var(--surface-raised); box-shadow: var(--shadow);" in mobile_css
    assert ".trip-quick-actions { flex: 1; width: auto; min-width: 0; box-shadow: none;" in mobile_css
    assert ".trip-quick-form:last-child { box-shadow: none; }" in mobile_css
    assert ".trip-quick-button { width: 100%; background: var(--control-fill-raised); border-radius: var(--radius-sm); }" in mobile_css
    # Desktop keeps both the ring and the divider.
    assert "box-shadow: inset 0 0 0 1px var(--border-strong);" in css
    assert ".trip-quick-form:last-child { box-shadow: inset 1px 0 0 var(--border-strong); }" in css


def test_dashboard_day_heading_margin_rule_is_not_duplicated():
    # A second, later .dashboard-day-heading rule once silently overrode this
    # margin at the same specificity (--space-5 computed but never applied).
    # Only one declaration of the block should exist.
    css = (ROOT / "static/style.css").read_text()
    assert css.count(".dashboard-day-heading {") == 1
    assert ".dashboard-day-heading {\n  display: flex; min-width: 0; gap: var(--space-3); align-items: center;\n  margin: var(--space-5) 0 var(--space-2); color: var(--fg);\n}" in css
    assert ".dashboard-day-heading:first-of-type { margin-top: 0; }" in css


def test_attention_strip_absent_when_none():
    body = _render(_dashboard(attention=None))
    assert "attention-strip" not in body


def test_attention_strip_present_with_review_and_missing_trip_links():
    dashboard = _dashboard(attention=AttentionStrip(
        unclassified_count=2, review_url="/review?from=2026-07-13&to=2026-07-19",
        missing_trip_count=1, missing_trip_url="/trips/manual?manual_date=2026-07-14",
    ))
    body = _render(dashboard)
    assert "attention-strip" in body
    assert 'class="attention-strip" role="status"' in body
    assert 'href="/review?from=2026-07-13&amp;to=2026-07-19"' in body
    assert "2 unclassified" in body
    assert 'href="/trips/manual?manual_date=2026-07-14"' in body
    assert "1 possible missing trip" in body
    strip = body.split('class="attention-strip"', 1)[1].split('</div>', 1)[0]
    assert 'class="icon icon-lightning' in strip


def test_attention_strip_css_uses_accent_not_warn_tokens():
    css = (ROOT / "static/style.css").read_text()
    strip_rule = css.split(".attention-strip {", 1)[1].split("}", 1)[0]
    assert "--warn" not in strip_rule
    assert "border-left-width" not in strip_rule
    assert "var(--accent-primary)" in strip_rule
    assert "var(--accent-soft-raised)" in strip_rule
    assert "var(--radius-md)" in strip_rule
    assert ".attention-heading { color: var(--accent-primary); }" in css


def test_dark_theme_maps_raised_tokens_to_todays_values():
    # A future edit to either token must not silently shift Dark: both
    # dark-facing blocks are required to map back to the pre-existing value
    # (the plain surface, and the plain soft accent fill) unchanged.
    css = (ROOT / "static/style.css").read_text()
    dark_media_block = css.split("@media (prefers-color-scheme: dark) {\n  :root {", 1)[1].split(
        "\n  }\n}", 1
    )[0]
    assert "--surface-raised: var(--dark-surface-raised);" in dark_media_block
    assert "--accent-soft-raised: var(--accent-soft);" in dark_media_block
    dark_attr_block = css.split(':root[data-theme="dark"] {', 1)[1].split("\n}", 1)[0]
    assert "--surface-raised: var(--dark-surface-raised);" in dark_attr_block
    assert "--accent-soft-raised: var(--accent-soft);" in dark_attr_block
    assert "--dark-surface-raised: var(--dark-surface);" in css


def test_view_all_trips_and_add_manual_trip_links():
    body = _render(_dashboard())
    assert 'class="control control-secondary" href="/trips">View all trips</a>' in body
    assert 'class="control control-secondary" href="/trips/manual">Add manual trip</a>' in body


def test_deduction_unavailable_shows_settings_link_not_a_number():
    body = _render(_dashboard(deduction=DeductionEstimate(amount=None, available=False)))
    assert "Unavailable" in body
    assert 'href="/settings"' in body
    assert "add one" in body


def test_dashboard_row_shows_a_custom_label_as_the_endpoint_name():
    # A custom label and a saved place can never coexist on one endpoint
    # (migrations/025_manual_trip_labels.sql), so TRIP_COLUMNS already
    # resolves start_place_name to the label ahead of any saved-place name
    # (app/ui/_common.py) before this template ever sees the row.
    trip = _trip(42, datetime(2026, 7, 13, 9, tzinfo=TZ), datetime(2026, 7, 13, 10, tzinfo=TZ),
                 source="manual", start_place_name="Grandma's house")
    body = _render(_dashboard(
        trip_count=1,
        day_groups=[DayGroup(day=date(2026, 7, 13), is_today=False, is_yesterday=False, trips=[trip])],
    ))

    assert "Grandma&#39;s house" in body


def test_detected_and_manual_rows_render_through_dashboard_partial():
    detected = _trip(1, datetime(2026, 7, 13, 15, tzinfo=TZ),
                      datetime(2026, 7, 13, 16, tzinfo=TZ), source="detected")
    manual = _trip(2, datetime(2026, 7, 13, 9, tzinfo=TZ), datetime(2026, 7, 13, 10, tzinfo=TZ),
                    source="manual", category="personal")
    dashboard = _dashboard(
        trip_count=2,
        day_groups=[DayGroup(day=date(2026, 7, 13), is_today=False, is_yesterday=False,
                              trips=[manual, detected])],
    )
    body = _render(dashboard)
    assert 'id="trip-1"' in body
    assert 'id="trip-2"' in body
    assert 'class="status-badge manual-badge">Manual</span>' in body
    assert body.count('class="dashboard-trip-row"') == 2
    assert 'class="card trip-card"' not in body


def test_unclassified_dashboard_row_has_equal_quick_actions_without_selection():
    trip = _trip(42, datetime(2026, 7, 13, 9, tzinfo=TZ),
                 datetime(2026, 7, 13, 10, tzinfo=TZ), category="unclassified")
    body = _render(_dashboard(
        trip_count=1,
        day_groups=[DayGroup(day=date(2026, 7, 13), is_today=False, is_yesterday=False, trips=[trip])],
    ))
    quick = body.split('class="trip-quick-actions"', 1)[1].split(
        '</div>', 1
    )[0]
    assert quick.count('class="trip-quick-form"') == 2
    assert 'name="category" value="business"' in quick
    assert 'name="category" value="personal"' in quick
    assert 'name="dashboard_week" value="2026-07-13"' in quick
    assert quick.count('hx-post="/trips/42/tag"') == 2


def test_unclassified_dashboard_row_quick_actions_are_a_joined_pair_with_icons():
    trip = _trip(42, datetime(2026, 7, 13, 9, tzinfo=TZ),
                 datetime(2026, 7, 13, 10, tzinfo=TZ), category="unclassified")
    body = _render(_dashboard(
        trip_count=1,
        day_groups=[DayGroup(day=date(2026, 7, 13), is_today=False, is_yesterday=False, trips=[trip])],
    ))
    quick = body.split('class="trip-quick-actions"', 1)[1].split('</div>', 1)[0]
    assert quick.count('class="trip-quick-button category-business"') == 1
    assert quick.count('class="trip-quick-button category-personal"') == 1
    assert 'class="icon icon-briefcase' in quick
    assert 'class="icon icon-house' in quick
    # Neither half may look pre-selected: no selected class or aria-pressed
    # attribute on either half beyond its own category/icon/label.
    assert 'trip-quick-button-selected' not in quick
    assert 'aria-pressed' not in quick
    assert 'checked' not in quick


def test_dashboard_row_keeps_statuses_and_full_actions_accessible():
    trip = _trip(
        42, datetime(2026, 7, 13, 9, tzinfo=TZ), datetime(2026, 7, 13, 10, tzinfo=TZ),
        category="business", exclusion="not_my_vehicle", source="manual",
        purpose="Client visit", vehicle_name="Work car", expense_count=2, has_gap=True,
        has_route_geometry=True,
        prev_end_gap_m=2000.0,
        prev_trip_ended_at=datetime(2026, 7, 13, 8, tzinfo=TZ),
        prev_trip_end_lat=47.0, prev_trip_end_lon=-122.0,
    )
    body = _render(_dashboard(
        trip_count=1,
        day_groups=[DayGroup(day=date(2026, 7, 13), is_today=False, is_yesterday=False, trips=[trip])],
    ))
    assert 'class="dashboard-trip-row dashboard-trip-row-not-my-vehicle"' in body
    assert "Not one of my vehicles" in body
    assert "Manual" in body and "2 expenses" in body
    assert "Recording gap" in body and "Possible missing trip" in body
    assert "Vehicle: Work car" in body
    assert 'href="/trips/42"' in body
    assert 'hx-get="/trips/42/edit?dashboard_week=2026-07-13"' in body
    assert 'data-trip-delete-open="trip-delete-dashboard-42"' in body
    delete_trigger = body.index('aria-controls="trip-delete-dashboard-42"')
    delete_component = body[body.rfind('<button', 0, delete_trigger):body.index('</dialog>', delete_trigger)]
    assert 'name="dashboard_week" value="2026-07-13"' in delete_component


def test_dashboard_row_omits_default_vehicle_but_keeps_it_reachable():
    trip = _trip(
        42, datetime(2026, 7, 13, 9, tzinfo=TZ), datetime(2026, 7, 13, 10, tzinfo=TZ),
        category="business", purpose="", vehicle_id=1, vehicle_name="My Car",
    )
    body = _render(_dashboard(
        trip_count=1,
        day_groups=[DayGroup(day=date(2026, 7, 13), is_today=False, is_yesterday=False, trips=[trip])],
    ), vehicles=[{"id": 1, "name": "My Car", "is_default": True}])
    row = body.split('<article id="trip-42"', 1)[1].split('</article>', 1)[0]
    # The meta wrapper itself must not render for a row with no purpose and
    # only a default vehicle to show -- neither of its children applies.
    assert 'class="dashboard-row-meta"' not in row
    assert 'class="dashboard-row-statuses"' not in row
    assert '<span class="dashboard-row-vehicle">' not in row
    assert 'title="Vehicle:' not in row
    assert '<span class="dashboard-row-vehicle visually-hidden">Vehicle: My Car</span>' in row


def test_dashboard_row_non_default_vehicle_has_no_hidden_duplicate():
    trip = _trip(
        42, datetime(2026, 7, 13, 9, tzinfo=TZ), datetime(2026, 7, 13, 10, tzinfo=TZ),
        category="business", purpose="", vehicle_id=2, vehicle_name="Work car",
    )
    body = _render(_dashboard(
        trip_count=1,
        day_groups=[DayGroup(day=date(2026, 7, 13), is_today=False, is_yesterday=False, trips=[trip])],
    ), vehicles=[
        {"id": 1, "name": "My Car", "is_default": True},
        {"id": 2, "name": "Work car", "is_default": False},
    ])
    row = body.split('<article id="trip-42"', 1)[1].split('</article>', 1)[0]
    assert row.count("Work car") == 1
    assert "visually-hidden" not in row


def test_dashboard_row_unassigned_vehicle_has_no_hidden_duplicate():
    trip = _trip(42, datetime(2026, 7, 13, 9, tzinfo=TZ), datetime(2026, 7, 13, 10, tzinfo=TZ),
                 category="business", purpose="")
    body = _render(_dashboard(
        trip_count=1,
        day_groups=[DayGroup(day=date(2026, 7, 13), is_today=False, is_yesterday=False, trips=[trip])],
    ), vehicles=[{"id": 1, "name": "My Car", "is_default": True}])
    row = body.split('<article id="trip-42"', 1)[1].split('</article>', 1)[0]
    assert row.count("Not assigned") == 1
    assert "visually-hidden" not in row


def test_dashboard_row_shows_non_default_vehicle_inline():
    trip = _trip(
        42, datetime(2026, 7, 13, 9, tzinfo=TZ), datetime(2026, 7, 13, 10, tzinfo=TZ),
        category="business", purpose="", vehicle_id=2, vehicle_name="Work car",
    )
    body = _render(_dashboard(
        trip_count=1,
        day_groups=[DayGroup(day=date(2026, 7, 13), is_today=False, is_yesterday=False, trips=[trip])],
    ), vehicles=[
        {"id": 1, "name": "My Car", "is_default": True},
        {"id": 2, "name": "Work car", "is_default": False},
    ])
    row = body.split('<article id="trip-42"', 1)[1].split('</article>', 1)[0]
    assert '<span class="dashboard-row-vehicle">Vehicle: Work car</span>' in row


def test_dashboard_row_unassigned_vehicle_keeps_not_assigned_inline():
    trip = _trip(42, datetime(2026, 7, 13, 9, tzinfo=TZ), datetime(2026, 7, 13, 10, tzinfo=TZ),
                 category="business", purpose="")
    body = _render(_dashboard(
        trip_count=1,
        day_groups=[DayGroup(day=date(2026, 7, 13), is_today=False, is_yesterday=False, trips=[trip])],
    ), vehicles=[{"id": 1, "name": "My Car", "is_default": True}])
    row = body.split('<article id="trip-42"', 1)[1].split('</article>', 1)[0]
    assert '<span class="dashboard-row-vehicle">Vehicle: Not assigned</span>' in row


def test_dashboard_row_statuses_wrapper_only_renders_with_a_signal():
    quiet = _trip(1, datetime(2026, 7, 13, 9, tzinfo=TZ), datetime(2026, 7, 13, 10, tzinfo=TZ))
    manual = _trip(2, datetime(2026, 7, 13, 9, tzinfo=TZ), datetime(2026, 7, 13, 10, tzinfo=TZ), source="manual")
    body = _render(_dashboard(
        trip_count=2,
        day_groups=[DayGroup(day=date(2026, 7, 13), is_today=False, is_yesterday=False, trips=[manual, quiet])],
    ))
    quiet_row = body.split('<article id="trip-1"', 1)[1].split('</article>', 1)[0]
    manual_row = body.split('<article id="trip-2"', 1)[1].split('</article>', 1)[0]
    assert 'class="dashboard-row-statuses"' not in quiet_row
    assert 'class="dashboard-row-statuses"' in manual_row


def test_committed_dashboard_row_has_one_selected_classify_half():
    trip = _trip(42, datetime(2026, 7, 13, 9, tzinfo=TZ),
                 datetime(2026, 7, 13, 10, tzinfo=TZ), category="personal")
    body = _render(_dashboard(
        trip_count=1,
        day_groups=[DayGroup(day=date(2026, 7, 13), is_today=False, is_yesterday=False, trips=[trip])],
    ))
    # No committed-pill popup remains at all.
    assert "dashboard-row-committed-pill" not in body
    assert "dashboard-row-category-change" not in body
    assert "dashboard-row-category-form" not in body
    assert 'type="radio"' not in body
    quick = body.split('class="trip-quick-actions"', 1)[1].split('</div>', 1)[0]
    personal_button = quick.split('value="personal"', 1)[1].split('</form>', 1)[0]
    business_button = quick.split('value="business"', 1)[1].split('</form>', 1)[0]
    # Exactly one half -- the trip's current category -- is selected.
    assert 'class="trip-quick-button category-personal trip-quick-button-selected"' in personal_button
    assert 'aria-pressed="true"' in personal_button
    assert 'class="trip-quick-button category-business"' in business_button
    assert 'trip-quick-button-selected' not in business_button
    assert 'aria-pressed' not in business_button
    assert 'Category' in body.split('<article id="trip-42"', 1)[1].split('trip-quick-actions', 1)[0]


def test_dashboard_and_archive_rows_share_the_classify_pair_macro():
    # Both consumers must import the one shared control rather than each
    # carrying its own inline copy that can drift out of sync.
    dashboard_row = (ROOT / "app/templates/_dashboard_trip_row.html").read_text()
    archive_row = (ROOT / "app/templates/_trip_archive_row.html").read_text()
    for source in (dashboard_row, archive_row):
        assert 'from "_trip_category_pair.html" import trip_category_pair' in source
        assert 'trip_category_pair(trip' in source
        assert 'class="trip-quick-actions"' not in source


def test_dashboard_row_more_menu_offers_clear_category_for_a_committed_trip():
    trip = _trip(42, datetime(2026, 7, 13, 9, tzinfo=TZ),
                 datetime(2026, 7, 13, 10, tzinfo=TZ), category="business")
    body = _render(_dashboard(
        trip_count=1,
        day_groups=[DayGroup(day=date(2026, 7, 13), is_today=False, is_yesterday=False, trips=[trip])],
    ))
    panel = body.split('class="dashboard-row-more-panel"', 1)[1].split('</details>', 1)[0]
    assert "Clear category" in panel
    clear_button = panel.split("Clear category", 1)[0].rsplit("<button", 1)[1]
    assert 'hx-post="/trips/42/tag"' in clear_button
    assert '"category": "unclassified"' in clear_button
    assert '"dashboard_week": "2026-07-13"' in clear_button
    assert 'hx-target="#trip-42"' in clear_button
    assert 'hx-swap="outerHTML"' in clear_button


def test_dashboard_row_more_menu_has_no_clear_category_when_already_unclassified():
    trip = _trip(42, datetime(2026, 7, 13, 9, tzinfo=TZ),
                 datetime(2026, 7, 13, 10, tzinfo=TZ), category="unclassified")
    body = _render(_dashboard(
        trip_count=1,
        day_groups=[DayGroup(day=date(2026, 7, 13), is_today=False, is_yesterday=False, trips=[trip])],
    ))
    panel = body.split('class="dashboard-row-more-panel"', 1)[1].split('</details>', 1)[0]
    assert "Clear category" not in panel


def test_dashboard_row_uses_start_time_route_icon_and_more_panel_access():
    trip = _trip(42, datetime(2026, 7, 13, 9, tzinfo=TZ),
                 datetime(2026, 7, 13, 10, tzinfo=TZ), category="business")
    body = _render(_dashboard(
        trip_count=1,
        day_groups=[DayGroup(day=date(2026, 7, 13), is_today=False, is_yesterday=False, trips=[trip])],
    ))
    row = body.split('<article id="trip-42"', 1)[1].split('</article>', 1)[0]
    assert '>09:00<' in row
    assert '>10:00<' not in row
    assert 'aria-label="Trip time: Mon 2026-07-13 09:00 to Mon 2026-07-13 10:00"' in row
    assert 'class="icon icon-arrow-right' in row
    assert '> to <' not in row
    assert 'View details' in row and 'Edit' in row and 'Delete trip' in row


def test_dashboard_row_action_cluster_has_edit_shortcut_and_full_more_menu():
    trip = _trip(42, datetime(2026, 7, 13, 9, tzinfo=TZ),
                 datetime(2026, 7, 13, 10, tzinfo=TZ), category="business")
    body = _render(_dashboard(
        trip_count=1,
        day_groups=[DayGroup(day=date(2026, 7, 13), is_today=False, is_yesterday=False, trips=[trip])],
    ))
    row = body.split('<article id="trip-42"', 1)[1].split('</article>', 1)[0]
    actions = row.split('class="dashboard-row-actions"', 1)[1]
    shortcut = actions.split('<details', 1)[0]
    assert 'class="dashboard-row-icon-button"' in shortcut
    assert 'aria-label="Edit trip"' in shortcut
    assert 'hx-get="/trips/42/edit?dashboard_week=2026-07-13"' in shortcut
    assert 'hx-target="#trip-42"' in shortcut and 'hx-swap="outerHTML"' in shortcut
    # The dots-three menu keeps every action too, so nothing depends solely
    # on the new shortcut for keyboard or touch users.
    assert 'View details' in actions and 'Edit' in actions and 'Delete trip' in actions


def test_day_group_heading_includes_today_and_yesterday_prefixes():
    dashboard = _dashboard(day_groups=[
        DayGroup(day=date(2026, 7, 13), is_today=True, is_yesterday=False,
                  trips=[_trip(1, datetime(2026, 7, 13, 9, tzinfo=TZ), datetime(2026, 7, 13, 10, tzinfo=TZ))]),
        DayGroup(day=date(2026, 7, 12), is_today=False, is_yesterday=True,
                  trips=[_trip(2, datetime(2026, 7, 12, 9, tzinfo=TZ), datetime(2026, 7, 12, 10, tzinfo=TZ))]),
    ])
    body = _render(dashboard)
    assert "Today" in body
    assert "Yesterday" in body
    assert "Mon Jul 13" in body
    assert "Sun Jul 12" in body


def test_last_trip_row_list_is_marked_so_the_final_row_drops_its_rule():
    # The marker belongs to the last .dashboard-trip-row-list, not the last
    # trip: with two day groups it must land on the second (day_group order
    # is caller-controlled, here newest-first) group's list.
    trip_a = _trip(1, datetime(2026, 7, 13, 9, tzinfo=TZ), datetime(2026, 7, 13, 10, tzinfo=TZ))
    trip_b = _trip(2, datetime(2026, 7, 12, 9, tzinfo=TZ), datetime(2026, 7, 12, 10, tzinfo=TZ))
    body = _render(_dashboard(
        trip_count=2,
        day_groups=[
            DayGroup(day=date(2026, 7, 13), is_today=False, is_yesterday=False, trips=[trip_a]),
            DayGroup(day=date(2026, 7, 12), is_today=False, is_yesterday=False, trips=[trip_b]),
        ],
    ))
    assert body.count("dashboard-trip-row-list-last") == 1
    marker_index = body.index("dashboard-trip-row-list-last")
    assert body.index('id="trip-1"') < marker_index < body.index('id="trip-2"')


def test_last_trip_row_list_marker_present_with_a_single_day_group():
    trip = _trip(1, datetime(2026, 7, 13, 9, tzinfo=TZ), datetime(2026, 7, 13, 10, tzinfo=TZ))
    body = _render(_dashboard(
        trip_count=1,
        day_groups=[DayGroup(day=date(2026, 7, 13), is_today=False, is_yesterday=False, trips=[trip])],
    ))
    assert body.count("dashboard-trip-row-list-last") == 1


def test_last_trip_row_list_marker_absent_with_no_day_groups():
    body = _render(_dashboard(day_groups=[]))
    assert "dashboard-trip-row-list-last" not in body


def test_last_trip_row_css_suppresses_its_own_bottom_rule():
    css = (ROOT / "static/style.css").read_text()
    assert ".dashboard-trip-row-list-last > .dashboard-trip-row:last-child {\n  background-image: none;\n}" in css
