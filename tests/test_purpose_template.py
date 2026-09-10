from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.main import make_templates

ROOT = Path(__file__).parents[1]


def _templates():
    return make_templates(SimpleNamespace(
        display_tz=ZoneInfo("America/Los_Angeles"), app_version="test",
    ))


def test_purpose_component_keeps_free_text_and_exposes_recent_selector():
    template = _templates().env.from_string(
        '{% from "_purpose_field.html" import purpose_field %}'
        '{{ purpose_field(["Client meeting", "Supply pickup"], "Client meeting", '
        'input_class="notes", hx_post="/trips/42/purpose", hx_swap="none") }}'
    )
    body = template.render()

    assert 'name="purpose" value="Client meeting"' in body
    assert 'autocomplete="off" data-purpose-input' in body
    assert 'data-purpose-selector' in body
    assert 'aria-label="Choose a recent business purpose"' in body
    assert '<option value="Supply pickup">Supply pickup</option>' in body
    assert 'hx-post="/trips/42/purpose" hx-trigger="change"' in body
    assert "datalist" not in body


def test_purpose_component_disables_empty_recent_selector():
    template = _templates().env.from_string(
        '{% from "_purpose_field.html" import purpose_field %}{{ purpose_field([]) }}'
    )
    body = template.render()

    assert 'data-purpose-selector' in body
    assert "disabled" in body
    assert 'class="purpose-field purpose-field-empty"' in body


def test_purpose_selector_uses_delegated_change_for_htmx_swaps():
    source = (ROOT / "app/templates/base.html").read_text()

    assert 'document.addEventListener("change"' in source
    assert 'event.target.matches("[data-purpose-selector]")' in source
    assert 'input.dispatchEvent(new Event("change", { bubbles: true }))' in source
    assert "event.target.selectedIndex = 0" in source


def test_recent_selector_overlays_input_without_widening_purpose_field():
    stylesheet = (ROOT / "static/style.css").read_text()

    assert ".purpose-field { position: relative; display: inline-block;" in stylesheet
    assert ".purpose-field input { box-sizing: border-box; padding-right: 2rem; }" in stylesheet
    assert "appearance: none; position: absolute; inset: 0 0 0 auto; width: 1.75rem;" in stylesheet
    assert ".purpose-recent option { color: var(--fg); background: var(--bg);" in stylesheet
    assert "width: 5.25rem" not in stylesheet
    assert "flex: 0 0 5.25rem" not in stylesheet


def test_trip_edit_uses_shared_recent_purpose_picker():
    source = (ROOT / "app/templates/_trip_edit_card.html").read_text()

    assert "purpose_field(" in source
    assert "recent-purposes" not in source
    assert "<datalist" not in source


def test_trip_card_long_content_wraps_without_forcing_page_width():
    stylesheet = (ROOT / "static/style.css").read_text()

    assert ".trip-route {" in stylesheet
    # .trip-card's own padding/border/radius/background/shadow moved into
    # the shared .card base class (see test_dashboard_template.py's
    # test_detected_and_manual_cards_render_through_shared_partial for the
    # markup side of this); .trip-card itself no longer declares them.
    assert ".card {" in stylesheet
    assert "min-width: 0" in stylesheet
    assert "overflow-wrap: anywhere" in stylesheet


def test_all_purpose_editing_templates_use_component_without_datalist():
    for name in ("trips.html", "_trip_edit_card.html", "_review_card.html", "trip.html"):
        source = (ROOT / "app/templates" / name).read_text()
        assert "purpose_field(" in source
        assert "recent-purposes" not in source
        assert "<datalist" not in source
