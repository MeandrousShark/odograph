"""The archive `place` filter outside the database: its SQL, the links and
URL state that carry it, the chip, and the export's filter preparation."""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.export_filters import TextStore, prepare_filters
from app.export_preparation import filter_sql
from app.main import make_templates
from app.preparation_resources import ResourceBudget, SpoolReservation
from app.review_queue import review_filter
from app.trip_queries import TripFilter, _trip_filter_sql
from app.ui._common import _month_page_url, _parse_place_id, _place_param, _url_with_filters
from app.ui.review import _review_url
from app.ui.trips import _archive_history_directive, _parse_archive_current_url
from tests.test_export_filter_preparation import text
from tests.test_review_template import _render as _render_review_card
from tests.test_review_template import _trip as _review_trip

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[1]

TZ = ZoneInfo("America/Los_Angeles")
PLACE_CLAUSE = "(start_place_id = %s OR end_place_id = %s)"


def test_no_place_builds_the_exact_sql_and_params_as_before():
    assert _trip_filter_sql("business", None, None, 3, q="x", exclusion="none", owner_id=7) == (
        _trip_filter_sql("business", None, None, 3, q="x", exclusion="none", place_id=None, owner_id=7)
    )
    where, _ = _trip_filter_sql("", None, None, owner_id=7)
    assert "place_id" not in where


def test_place_matches_either_endpoint_in_one_predicate():
    where, params = _trip_filter_sql("", None, None, place_id=42, owner_id=7)
    assert where == f"WHERE trips.account_id = %s AND {PLACE_CLAUSE}"
    assert params == [7, 42, 42]


def test_trip_filter_and_review_filter_pass_place_through():
    assert TripFilter(place_id=5).where(7) == _trip_filter_sql("", None, None, place_id=5, owner_id=7)
    where, params = review_filter(None, None, None, "", 5).where(7)
    assert PLACE_CLAUSE in where and "category = %s" in where
    assert params == [7, "unclassified", 5, 5]
    # Existing positional callers still build the filter without a place.
    assert TripFilter("", None, None, None, "", "").place_id is None


@pytest.mark.parametrize("raw,expected", [
    ("", None), ("12", 12), ("abc", None), ("1.5", None), (None, None), (object(), None),
])
def test_place_param_is_forgiving_like_vehicle(raw, expected):
    assert _parse_place_id(raw) == expected
    assert _place_param(raw) == ("" if expected is None else str(expected))


def test_links_carry_place_and_stay_byte_identical_without_it():
    assert _url_with_filters("/trips", "", "", "", "") == "/trips"
    assert _url_with_filters("/trips", "", "", "", "", "") == "/trips"
    assert _url_with_filters("/trips", "2026-01-01", "", "3", "x", "9", category="business") == (
        "/trips?category=business&from=2026-01-01&vehicle=3&q=x&place=9"
    )
    assert _month_page_url(2026, 5, 25, "", "", "", "", "", "", "9") == (
        "/trips/month/2026/5?offset=25&place=9"
    )
    assert _month_page_url(2026, 5, 25, "", "", "", "") == "/trips/month/2026/5?offset=25"
    assert _review_url("", "", "", "", "9") == "/review?place=9"
    assert _review_url("", "", "", "") == "/review"


def test_current_url_parse_and_history_directive_compare_place():
    parsed = _parse_archive_current_url("https://x.example/trips?place=4&vehicle=2")
    assert parsed["place"] == "4" and parsed["vehicle"] == "2"
    state = {"category": "", "from": "", "to": "", "vehicle": "2", "q": "",
             "exclusion": "", "place": "4", "url": "/trips?vehicle=2&place=4"}
    assert _archive_history_directive("https://x.example/trips?vehicle=2&place=4", state) == (
        "HX-Replace-Url", state["url"],
    )
    assert _archive_history_directive("https://x.example/trips?vehicle=2", state)[0] == "HX-Push-Url"
    assert _archive_history_directive(
        "https://x.example/trips?vehicle=2&place=4", {**state, "place": ""},
    )[0] == "HX-Push-Url"


def _render_archive(**context):
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    defaults = dict(
        months=[], vehicles=[], recent_purposes=[], user={"sub": "test"}, csrf="token",
        places=[], notice="", csp_nonce="", filter_category="", filter_from="", filter_to="",
        filter_vehicle="", filter_q="", filter_exclusion="",
        export_url=lambda *a, **k: "/", review_url="/review", ytd_year=2026, ytd_deduction=None,
    )
    defaults.update(context)
    return templates.env.get_template("trips.html").render(**defaults)


def _chip(body):
    start = body.index('<a class="control control-secondary control-compact trip-filter-place"')
    return body[start:body.index("</a>", start)]


def test_place_filter_renders_a_hidden_input_and_a_named_removable_chip():
    body = _render_archive(filter_place="7", filter_place_name="Home & Co",
                           place_clear_url="/trips?vehicle=2")
    assert '<input type="hidden" name="place" value="7">' in body
    chip = _chip(body)
    assert 'href="/trips?vehicle=2"' in chip
    assert "data-archive-place-remove>" in chip and " hidden" not in chip
    assert 'Place: <span data-archive-place-name>Home &amp; Co</span>' in chip
    assert '<span class="visually-hidden">Remove filter </span>' in chip
    # A place filter alone counts as active, so Clear is offered.
    assert 'class="control control-secondary filter-clear" href="/trips">Clear</a>' in body


def test_no_place_filter_keeps_the_chip_hidden_and_the_input_empty():
    body = _render_archive()
    assert '<input type="hidden" name="place" value="">' in body
    assert "data-archive-place-remove hidden>" in _chip(body)
    assert 'filter-clear" href="/trips">Clear</a>' not in body


def test_unknown_place_still_shows_a_removable_chip():
    chip = _chip(_render_archive(filter_place="99", filter_place_name=""))
    assert " hidden" not in chip
    assert "<span data-archive-place-name>Unknown place</span>" in chip


def test_archive_controller_reads_writes_and_clears_the_place_field():
    body = _render_archive()
    assert "const FILTER_NAMES = ['q', 'category', 'date_preset', 'from', 'to', 'vehicle', 'exclusion', 'place'];" in body
    assert "setPlaceFilter(form, '', '');" in body
    assert "setPlaceFilter(form, state.place || '', state.place_name || '', state.place_clear_url);" in body
    assert body.count("|| state.place\n") == 2


def test_review_card_carries_place_in_its_form_and_delete_dialog():
    body = _render_review_card(trip=_review_trip("detected"), filter_place="7", filter_q="")
    form = body.split('<form id="review-form"', 1)[1].split("</form>", 1)[0]
    assert '<input type="hidden" name="place" value="7">' in form
    dialog = body.split("trip-delete-dialog-content", 1)[1].split("</form>", 1)[0]
    assert '<input type="hidden" name="place" value="7">' in dialog


@pytest.fixture
def store(tmp_path):
    reservation = SpoolReservation.acquire(tmp_path / "spool", time.monotonic() + 5)
    result = TextStore(ResourceBudget(reservation.directory))
    try:
        yield result
    finally:
        result.close()
        result.budget.close()
        reservation.release()


def _filters(store, **values):
    for key in ("from", "to", "vehicle", "q", "category", "exclusion"):
        text(store, key, values.get(key, ""))
    if "place" in values:
        text(store, "place", values["place"])
    return prepare_filters(store, TZ, "utf8")


@pytest.mark.parametrize("raw,expected", [("12", 12), ("", None), ("junk", None)])
def test_export_preparation_parses_place_like_the_archive(store, raw, expected):
    assert _filters(store, place=raw)["place"] == expected


def test_export_preparation_without_a_place_has_no_place_filter(store):
    assert _filters(store)["place"] is None


def test_export_filter_sql_applies_the_place():
    metadata = dict(category="", exclusion="", vehicle=None, place=8, from_dt=None, to_dt=None, search=False)
    assert filter_sql(metadata, 7) == _trip_filter_sql("", None, None, place_id=8, owner_id=7)


def test_review_undo_replays_the_place_the_action_was_taken_under():
    source = (ROOT / "app/templates/review.html").read_text()
    assert "place: form && form.elements.place ? form.elements.place.value : ''," in source
    assert "q: action.q, place: action.place," in source
