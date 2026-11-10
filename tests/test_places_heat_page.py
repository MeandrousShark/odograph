"""Template wiring for the Places heatmap on places.html.

The query, weight and option rules are proven in tests/js/places_heat.test.js
against static/places_heat.js; this guards the markup the script binds to.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.main import make_templates

TZ = ZoneInfo("UTC")
USER = {"sub": "test"}
ROOT = Path(__file__).resolve().parents[1]


def _render(**context) -> str:
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    base = {
        "user": USER, "csrf": "token", "csp_nonce": "test-nonce", "static_version": "v1",
        "places": [], "year": 2026,
        "vehicles": [{"id": 4, "name": "Civic"}],
        "heat_from": "2025-11-10", "heat_to": "2026-11-10",
    }
    return templates.env.get_template("places.html").render(**{**base, **context})


def _heat_section(body: str) -> str:
    return body.split('<section class="places-heat"', 1)[1].split("</section>", 1)[0]


def test_scripts_load_leaflet_then_heat_then_page_script():
    body = _render()
    order = [
        '<link rel="stylesheet" href="/static/vendor/leaflet/leaflet.css">',
        '<script src="/static/vendor/leaflet/leaflet.js"></script>',
        '<script src="/static/vendor/leaflet-heat/leaflet-heat-global.js"></script>',
        '<script src="/static/places_heat.js"></script>',
    ]
    positions = [body.index(tag) for tag in order]
    assert positions == sorted(positions)
    for path in ("static/places_heat.js", "static/vendor/leaflet-heat/leaflet-heat-global.js"):
        assert (ROOT / path).is_file()


def test_heat_config_rides_on_data_attributes_not_inline_script():
    body = _render()
    section = _heat_section(body)
    assert 'data-heat-url="/places/heat"' in section
    assert 'data-tile-url="https://tile.openstreetmap.org/{z}/{x}/{y}.png"' in section
    assert "data-tile-attribution=" in section
    assert "data-places-heat-map" in section
    for tag in re.findall(r"<script\b[^>]*>", body):
        assert "src=" in tag or 'nonce="test-nonce"' in tag, tag


def test_tile_attribution_is_escaped_into_its_attribute():
    body = _render(map_tile_attribution='<a href="https://example.test">"Tiles"</a>')
    assert 'data-tile-attribution="&lt;a href=&#34;https://example.test&#34;&gt;' in body


def test_mode_toggle_defaults_to_visits():
    section = _heat_section(_render())
    assert re.search(r'<input [^>]*name="mode" value="visits" checked>', section)
    assert re.search(r'<input [^>]*name="mode" value="time">', section)
    assert ">Visits</label>" in section
    assert ">Time spent</label>" in section


def test_dates_default_from_the_route():
    section = _heat_section(_render())
    assert '<input type="date" name="from" value="2025-11-10">' in section
    assert '<input type="date" name="to" value="2026-11-10">' in section


def test_category_and_vehicle_match_the_archive_values():
    section = _heat_section(_render())
    category = section.split('<select name="category"', 1)[1].split("</select>", 1)[0]
    assert re.findall(r'value="([^"]*)"', category) == ["", "business", "personal", "unclassified"]
    vehicle = section.split('<select name="vehicle"', 1)[1].split("</select>", 1)[0]
    assert re.findall(r'value="([^"]*)"', vehicle) == ["", "none", "4"]
    assert ">Unassigned</option>" in vehicle
    assert ">Civic</option>" in vehicle
    assert section.count("data-places-heat-trip-filter") == 2


def test_time_mode_note_says_it_uses_dates_only():
    section = _heat_section(_render())
    assert ("<p class=\"muted\" data-places-heat-time-note hidden>Time spent uses dates only: "
            "stays carry no vehicle or category.</p>") in section


def test_status_region_announces_empty_and_failed_loads():
    section = _heat_section(_render())
    assert '<p class="places-heat-status" data-places-heat-status role="status" hidden></p>' in section


def test_map_shows_without_saved_places_and_with_them():
    without = _render(places=[])
    assert "data-places-heat-map" in without
    assert "No saved places yet." in without
    with_places = _render(places=[{
        "id": 3, "name": "Home", "kind": "home", "trip_count": 0, "total_m": 0.0, "last_visit": None,
    }])
    assert with_places.index("data-places-heat-map") < with_places.index('<table class="settings-table places-table">')


def test_renders_without_the_heat_context():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("places.html").render(
        user=USER, csrf="token", csp_nonce="n", static_version="v1", places=[], year=2026,
    )
    assert '<input type="date" name="from" value="">' in body


def test_heat_map_keeps_a_height_at_phone_width():
    css = (ROOT / "static" / "style.css").read_text()
    rule = css.split(".places-page .places-heat-map {", 1)[1].split("}", 1)[0]
    assert "height: clamp(16rem," in rule
