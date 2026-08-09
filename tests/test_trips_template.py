from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.main import make_templates

TZ = ZoneInfo("America/Los_Angeles")
ROOT = Path(__file__).parents[1]


def _render_index(vehicles=None, **filters):
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    return templates.env.get_template("trips.html").render(
        months=[], vehicles=vehicles or [], recent_purposes=[], user={"sub": "test"},
        csrf="token", filter_category=filters.get("category", ""),
        filter_from=filters.get("from_", ""), filter_to=filters.get("to", ""),
        filter_vehicle=filters.get("vehicle", ""), filter_url=lambda *a, **k: "/",
        export_url=lambda *a, **k: "/", review_url="/review", ytd_year=2026,
        ytd_deduction=None,
    )


def _render(vehicles):
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("trips.html").render(
        months=[], vehicles=vehicles, user={"sub": "test"}, csrf="token",
        filter_category="", filter_from="", filter_to="", filter_vehicle="",
        filter_url=lambda *a, **k: "/", export_url=lambda *a, **k: "/",
        ytd_year=2026, ytd_deduction=None,
    )
    # Isolate the manual-trip form's Vehicle <select>, distinct from the
    # filter-bar's Vehicle <select name="vehicle"> earlier in the page.
    return body.split('name="vehicle_id"')[1].split("</select>")[0]


def test_sole_vehicle_preselected_for_manual_trip():
    picker = _render([{"id": 1, "name": "My Car", "active": True, "is_default": False}])
    assert '<option value="1" selected>My Car</option>' in picker


def test_default_vehicle_preselected_among_several():
    picker = _render([
        {"id": 1, "name": "Car A", "active": True, "is_default": False},
        {"id": 2, "name": "Car B", "active": True, "is_default": True},
    ])
    assert '<option value="2" selected>Car B</option>' in picker
    assert '<option value="1" >Car A</option>' in picker


def test_no_vehicle_preselected_when_several_and_none_default():
    picker = _render([
        {"id": 1, "name": "Car A", "active": True, "is_default": False},
        {"id": 2, "name": "Car B", "active": True, "is_default": False},
    ])
    assert "selected" not in picker


def test_filter_bar_vehicle_select_has_unassigned_sentinel_option():
    body = _render_index(
        [{"id": 1, "name": "Car A", "active": True, "is_default": False}], vehicle="none",
    )
    picker = body.split('name="vehicle"')[1].split("</select>")[0]

    assert '<option value="">All</option>' in picker
    assert '<option value="none" selected>Unassigned</option>' in picker
    assert '<option value="1" >Car A</option>' in picker


def test_selection_action_bar_has_select_all_before_dialog_buttons():
    body = _render_index()
    bar = body.split('id="selection-action-bar"')[1].split("</div>", 1)[0]

    assert 'id="selection-select-all"' in bar and ">Select all</button>" in bar
    assert bar.index("selection-select-all") < bar.index("category-dialog-open")
    assert "selectionSelectAll.addEventListener('click'" in body


def test_merge_dialog_vehicle_picker_defaults_to_keep():
    body = _render_index([
            {"id": 1, "name": "Car A", "active": True, "is_default": False},
            {"id": 2, "name": "Car B", "active": True, "is_default": True},
        ])
    picker = body.split('id="merge-dialog-vehicle"')[1].split("</select>")[0]

    assert '<option value="keep" selected>Keep</option>' in picker
    assert '<option value="">No vehicle</option>' in picker
    assert '<option value="1">Car A</option>' in picker
    assert '<option value="2">Car B</option>' in picker
    assert "body.append('vehicle_id', mergeVehicleSelect.value)" in body


def test_selection_action_bar_renders_bar_and_no_combined_form():
    body = _render_index()

    assert 'id="selection-action-bar" class="selection-action-bar" hidden' in body
    assert 'id="selection-count" aria-live="polite"' in body
    assert 'id="category-dialog-open"' in body and ">Category</button>" in body
    assert 'id="purpose-dialog-open"' in body and ">Purpose</button>" in body
    assert 'id="vehicle-dialog-open"' in body and ">Vehicle</button>" in body
    assert 'class="selection-more"' in body and "<summary>More</summary>" in body
    assert 'id="merge-dialog-open"' in body and ">Merge selected…</button>" in body
    assert 'id="selection-clear"' in body and ">Clear selection</button>" in body

    assert 'id="merge-bar"' not in body
    assert 'id="batch-submit"' not in body
    assert '>Apply to selected</button>' not in body
    assert 'id="merge-notes"' not in body
    assert 'id="merge-set-purpose"' not in body


def test_four_bulk_action_dialogs_render_expected_fields():
    body = _render_index([
        {"id": 1, "name": "Car A", "active": True, "is_default": False},
    ])

    category_dialog = body.split('id="category-dialog"')[1].split("</dialog>")[0]
    assert 'aria-labelledby="category-dialog-title"' in category_dialog
    assert '<input type="radio" name="category" value="business">' in category_dialog
    assert '<input type="radio" name="category" value="personal">' in category_dialog
    assert '<input type="radio" name="category" value="unclassified">' in category_dialog
    assert 'checked' not in category_dialog
    assert 'id="category-dialog-confirm" disabled' in category_dialog
    assert 'role="alert"' in category_dialog

    purpose_dialog = body.split('id="purpose-dialog"')[1].split("</dialog>")[0]
    assert '<input type="radio" name="purpose-mode" value="set">' in purpose_dialog
    assert '<input type="radio" name="purpose-mode" value="clear">' in purpose_dialog
    assert 'id="purpose-dialog-input"' in purpose_dialog
    assert 'class="purpose-field' in purpose_dialog
    assert 'id="purpose-dialog-confirm" disabled' in purpose_dialog
    assert 'role="alert"' in purpose_dialog

    vehicle_dialog = body.split('id="vehicle-dialog"')[1].split("</dialog>")[0]
    assert '<option value="clear">No vehicle</option>' in vehicle_dialog
    assert '<option value="1">Car A</option>' in vehicle_dialog
    assert 'keep' not in vehicle_dialog
    assert 'id="vehicle-dialog-confirm" disabled' in vehicle_dialog
    assert 'role="alert"' in vehicle_dialog

    merge_dialog = body.split('id="merge-dialog"')[1].split("</dialog>")[0]
    assert "Merging combines the selected detected trips" in merge_dialog
    assert 'id="merge-dialog-category"' in merge_dialog
    assert 'id="merge-dialog-purpose"' in merge_dialog
    assert 'id="merge-dialog-notes"' in merge_dialog
    assert 'id="merge-dialog-vehicle"' in merge_dialog
    assert '>Merge <span data-selection-count>0 trips</span></button>' in merge_dialog
    assert 'role="alert"' in merge_dialog


def test_dialog_submitters_each_send_only_their_own_field():
    body = _render_index()

    category_handler = body.split("categoryConfirm.addEventListener('click'")[1].split(
        "purposeDialog"
    )[0]
    assert "fetch('/trips/batch_update'" in body
    assert "body.append('category', chosen.value)" in category_handler
    assert "set_purpose" not in category_handler
    assert "vehicle_id" not in category_handler

    purpose_handler = body.split("purposeConfirm.addEventListener('click'")[1].split(
        "vehicleDialog"
    )[0]
    assert "body.append('set_purpose', 'true')" in purpose_handler
    assert "mode.value === 'clear' ? '' : purposeInput.value" in purpose_handler
    assert "category" not in purpose_handler
    assert "vehicle_id" not in purpose_handler

    vehicle_handler = body.split("vehicleConfirm.addEventListener('click'")[1].split(
        "mergeDialog"
    )[0]
    assert "vehicleSelect.value === 'clear' ? '' : vehicleSelect.value" in vehicle_handler
    assert "category" not in vehicle_handler
    assert "set_purpose" not in vehicle_handler

    merge_handler = body.split("mergeConfirm.addEventListener('click'")[1]
    assert "fetch('/trips/merge_selected'" in merge_handler
    # "keep" must reach the server as-is: the server's own tri-state
    # handling is what decides whether to preserve or reclassify, so the
    # client rewriting "keep" to a real category here would silently
    # human-lock a merge the user asked to leave alone.
    assert "body.append('category', category);" in merge_handler
    assert "window.location.href = '/trips/' + data.trip_id" in merge_handler
    assert "if (selection.size < 2) return" in merge_handler

    assert "submitBatchUpdate(categoryDialog, body)" in body
    assert "submitBatchUpdate(purposeDialog, body)" in body
    assert "submitBatchUpdate(vehicleDialog, body)" in body
    assert "window.location.reload()" in body


def test_dialog_error_paths_write_into_their_own_dialog_and_keep_selection():
    body = _render_index()

    assert "function showDialogError(dialog, message)" in body
    assert "err.hidden = false" in body
    assert "showDialogError(dialog, err.detail || 'Batch update failed.')" in body
    assert "showDialogError(mergeDialog, err.detail || 'Merge failed.')" in body
    assert "selection.clear()" not in body.split(
        "async function submitBatchUpdate"
    )[1].split("const categoryDialog")[0]


def test_dialog_forms_guard_against_implicit_enter_submission():
    body = _render_index()

    for form_marker, confirm_id in (
        ('data-selection-dialog-confirm="category-dialog-confirm"', "category-dialog-confirm"),
        ('data-selection-dialog-confirm="purpose-dialog-confirm"', "purpose-dialog-confirm"),
        ('data-selection-dialog-confirm="vehicle-dialog-confirm"', "vehicle-dialog-confirm"),
        ('data-selection-dialog-confirm="merge-dialog-confirm"', "merge-dialog-confirm"),
    ):
        assert form_marker in body
        assert f'id="{confirm_id}"' in body

    submit_guard = body.split("document.addEventListener('submit'")[1]
    assert "e.preventDefault()" in submit_guard
    assert "form.dataset.selectionDialogConfirm" in submit_guard
    assert "confirmButton.click()" in submit_guard
    assert "!confirmButton.disabled" in submit_guard


def test_selection_counts_pluralize_singular_trip():
    body = _render_index()

    assert "function tripCountLabel(n)" in body
    assert "n === 1 ? 'trip' : 'trips'" in body
    assert "el.textContent = tripCountLabel(selection.size)" in body
    assert "selectionCount.textContent = `${tripCountLabel(selection.size)} selected`" in body
    # Headings/confirm labels now wrap the whole "N trips" phrase in the
    # counted span rather than hard-coding a trailing " trips" outside it,
    # so a single-trip batch can read "1 trip" instead of "1 trips".
    assert "for <span data-selection-count>0 trips</span>" in body
    assert ">Merge <span data-selection-count>0 trips</span></button>" in body


def test_reconcile_runs_on_htmx_after_settle_so_highlight_survives_card_swaps():
    body = _render_index()

    assert "document.addEventListener('htmx:afterSettle', reconcileSelection)" in body
    assert "document.addEventListener('htmx:afterSwap'" in body
    assert "document.addEventListener('htmx:afterRequest'" in body


def test_selection_mode_enters_clears_and_finishes_without_expanding_m3_scope():
    body = _render_index()

    assert 'id="selection-start"' in body and ">Select trips</button>" in body
    assert 'id="selection-clear"' in body and ">Clear selection</button>" in body
    assert 'id="selection-done"' in body and ">Done</button>" in body
    assert 'id="selection-bar-done"' in body and ">Done</button>" in body
    assert "selectionStart.addEventListener('click', () => setSelectionMode(true))" in body
    assert "selectionClear.addEventListener('click', clearSelection)" in body
    assert "selectionDone.addEventListener('click', () => setSelectionMode(false))" in body
    assert "selectionBarDone.addEventListener('click', () => setSelectionMode(false))" in body
    assert "document.body.classList.toggle('selection-mode', selectionMode)" in body
    assert "selectionDone.hidden = !selectionMode" in body
    # The bar hides only when selection mode itself is off, not merely when
    # the current selection is empty — that's what keeps "Clear selection"
    # (bar stays open, buttons grey out) visibly distinct from "Done" (bar
    # and per-card checkboxes disappear).
    assert "selectionBar.hidden = !selectionMode;" in body
    assert "if (!enabled) selection.clear()" in body
    assert "selector.hidden = !selectionMode" in body
    assert "card.classList.toggle('is-selected', selected)" in body
    assert "categoryOpen.disabled = !hasSelection" in body
    assert "purposeOpen.disabled = !hasSelection" in body
    assert "vehicleOpen.disabled = !hasSelection" in body
    assert "const canMerge = selection.size >= 2" in body
    assert "mergeOpen.disabled = !canMerge" in body
    assert "Merging needs at least two selected trips." in body
    assert "Select at least two trips to apply or merge." not in body
    assert "function reconcileSelection()" in body
    assert "if (!card)" in body and "selection.delete(id)" in body
    assert "checkbox.checked = selected" in body
    assert "document.addEventListener('htmx:afterSwap'" in body
    assert "document.addEventListener('htmx:afterRequest'" in body


def test_manual_trip_form_closed_and_unprefilled_by_default():
    body = _render_index()

    assert '<details class="trip-page-disclosure trip-manual add-manual" id="manual-trip" >' in body
    assert 'class="trip-manual-content"' in body
    assert 'name="start_time" value=""' in body


def test_manual_trip_form_opens_from_manual_open_flag_without_prefill():
    # manual_open, not manual_prefill, drives the disclosure's open attribute
    # -- this is the dashboard's "Add manual trip" link (a bare #manual-trip
    # fragment, since a browser only auto-expands a <details> when a fragment
    # targets something inside it, never the <details> itself).
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("trips.html").render(
        months=[], vehicles=[], recent_purposes=[], user={"sub": "test"},
        csrf="token", filter_category="", filter_from="", filter_to="",
        filter_vehicle="", filter_url=lambda *a, **k: "/",
        export_url=lambda *a, **k: "/", review_url="/review", ytd_year=2026,
        ytd_deduction=None, manual_open=True,
    )

    assert '<details class="trip-page-disclosure trip-manual add-manual" id="manual-trip" open>' in body
    assert 'name="start_time" value=""' in body
    assert "The date, start time, and notes were prefilled" not in body


def test_manual_trip_form_opens_and_prefills_from_missing_trip_badge_link():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("trips.html").render(
        months=[], vehicles=[], recent_purposes=[], user={"sub": "test"},
        csrf="token", filter_category="", filter_from="", filter_to="",
        filter_vehicle="", filter_url=lambda *a, **k: "/",
        export_url=lambda *a, **k: "/", review_url="/review", ytd_year=2026,
        ytd_deduction=None, manual_open=True,
        manual_prefill={
            "date": "2026-07-01", "start_time": "08:10",
            "notes": "bridge: Work → Home", "osrm_hint": None,
        },
    )

    assert '<details class="trip-page-disclosure trip-manual add-manual" id="manual-trip" open>' in body
    assert 'name="date"' in body and 'value="2026-07-01"' in body
    assert 'name="start_time" value="08:10"' in body
    assert 'value="bridge: Work → Home"' in body
    assert "osrm-hint" not in body
    assert "The date, start time, and notes were prefilled" in body
    assert "Enter the end time and distance" in body


def test_manual_trip_form_shows_osrm_hint_when_present():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("trips.html").render(
        months=[], vehicles=[], recent_purposes=[], user={"sub": "test"},
        csrf="token", filter_category="", filter_from="", filter_to="",
        filter_vehicle="", filter_url=lambda *a, **k: "/",
        export_url=lambda *a, **k: "/", review_url="/review", ytd_year=2026,
        ytd_deduction=None, manual_open=True,
        manual_prefill={
            "date": "2026-07-01", "start_time": "08:10", "notes": "",
            "osrm_hint": "~1.4 mi by road",
        },
    )

    assert 'class="osrm-hint"' in body
    assert "~1.4 mi by road" in body


def test_trip_pager_is_block_markup_with_stable_next_url():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("_trip_page_rows.html").render(
        trips=[], has_more=True, next_url="/trips/month/2026/1?offset=25",
    )
    assert '<div class="trip-pager">' in body
    assert "<tr" not in body and "<td" not in body
    assert 'hx-get="/trips/month/2026/1?offset=25"' in body
    assert 'hx-target="closest .trip-pager"' in body


def test_trip_list_has_no_thumbnail_attribution():
    first_render = _render_index()
    second_render = _render_index()

    assert "openstreetmap.org/copyright" not in first_render
    assert "openstreetmap.org/copyright" not in second_render


def test_trip_filters_use_native_disclosure_and_open_for_every_active_filter():
    collapsed = _render_index()

    assert '<div class="trip-page-disclosures">' in collapsed
    assert '<details class="trip-page-disclosure trip-tools" >' in collapsed
    assert '<summary>Filters &amp; tools</summary>' in collapsed
    assert '<summary>Add manual trip</summary>' in collapsed
    assert collapsed.count('class="trip-page-disclosure ') == 2
    assert 'class="filter-date-row"' in collapsed
    assert 'class="filter-apply-row"' in collapsed
    assert 'class="filter-link-groups"' in collapsed
    assert 'class="control control-secondary filter-clear"' not in collapsed

    for filters in (
        {"category": "business"}, {"from_": "2026-07-01"},
        {"to": "2026-07-31"}, {"vehicle": "2"},
    ):
        active = _render_index(**filters)
        assert '<details class="trip-page-disclosure trip-tools" open>' in active
        assert '<a class="control control-secondary filter-clear" href="/trips">Clear</a>' in active


def test_trip_page_disclosures_share_scoped_summary_row_and_full_width_content():
    body = _render_index()
    stylesheet = (ROOT / "static/style.css").read_text()
    settings_source = (ROOT / "app/templates/settings.html").read_text()

    filters = body.index('<details class="trip-page-disclosure trip-tools"')
    manual = body.index('<details class="trip-page-disclosure trip-manual add-manual"')
    assert filters < manual
    assert ".trip-page-disclosures {" in stylesheet
    assert "--trip-page-summary-width: min(9rem, calc(50% - var(--space-1)))" in stylesheet
    assert "position: relative; width: 100%; min-width: 0;" in stylesheet
    assert "padding-top: var(--control-height)" in stylesheet
    assert ".trip-page-disclosure > summary {" in stylesheet
    assert "position: absolute; top: 0; width: var(--trip-page-summary-width);" in stylesheet
    assert "padding: var(--space-2); background: var(--control-bg); color: var(--control-fg);" in stylesheet
    assert "font-size: .85rem; font-weight: 500; line-height: 1.2;" in stylesheet
    assert ".trip-tools > summary { left: 0; }" in stylesheet
    assert ".trip-manual > summary { left: calc(var(--trip-page-summary-width) + var(--space-2)); }" in stylesheet
    assert ".trip-tools > .trip-filter-bar {" in stylesheet
    assert ".trip-manual-content {" in stylesheet
    assert "min-width: 0; width: 100%; margin-top: var(--space-3);" in stylesheet
    assert "trip-page-disclosure" not in settings_source


def test_trip_filter_disclosure_preserves_urls_and_compacts_narrow_layout():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("trips.html").render(
        months=[], vehicles=[], recent_purposes=[], user={"sub": "test"}, csrf="token",
        filter_category="business", filter_from="2026-07-01", filter_to="2026-07-31",
        filter_vehicle="", filter_url=lambda category: f"/category/{category or 'all'}",
        export_url=lambda kind: f"/export/{kind}", review_url="/review?from=2026-07-01",
        ytd_year=2026, ytd_deduction=None,
    )
    stylesheet = (ROOT / "static/style.css").read_text()

    assert 'class="filter-bar trip-filter-bar"' in body
    assert 'href="/category/all"' in body
    assert 'href="/category/business" class="active"' in body
    assert 'name="from" value="2026-07-01"' in body
    assert 'name="to" value="2026-07-31"' in body
    assert 'href="/export/csv"' in body and 'href="/export/xlsx"' in body
    assert 'href="/review?from=2026-07-01"' in body
    assert ".trip-filter-bar { width: 100%; justify-content: space-between; }" in stylesheet
    assert "@media (max-width: 900px)" in stylesheet
    assert ".trip-filter-bar { justify-content: flex-start; }" in stylesheet
    assert ".trip-filter-bar .filter-date-row" in stylesheet
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in stylesheet
    assert ".trip-filter-bar .filter-apply-row" in stylesheet
    assert "grid-template-columns: minmax(0, 1fr) auto auto" in stylesheet


def test_shell_navigation_and_core_actions_use_stable_text_labels():
    body = _render_index()

    assert '<nav aria-label="Primary navigation">' in body
    nav_block = body.split('<nav aria-label="Primary navigation">', 1)[1].split("</nav>", 1)[0]
    for label in ("Trips", "Review", "Report", "Expenses", "Stats"):
        assert f">{label}</a>" in nav_block
    assert ">Settings</a>" not in nav_block

    # Settings moved into the account cluster, next to Log out, per
    # It's grouped with account controls
    # rather than the primary section links.
    account_block = body.split('class="header-account"', 1)[1].split("</div>", 1)[0]
    assert ">Settings</a>" in account_block
    assert account_block.index(">Settings</a>") < account_block.index(">Log out<")

    assert 'id="theme-toggle"' not in body
    assert "paintLabel" not in body
    assert 'localStorage.getItem("theme")' in body
    assert 'document.documentElement.setAttribute("data-theme", stored)' in body
    for glyph in ("🚗", "✅", "📊", "🧾", "📈", "⚙", "🌙", "☀", "⬇", "＋", "🗑", "▾"):
        assert glyph not in body

    core_sources = [
        (ROOT / "app/templates" / name).read_text()
        for name in ("base.html", "trips.html", "_trip_card.html", "_trip_edit_card.html",
                     "_purpose_field.html")
    ]
    core_sources.append((ROOT / "static/style.css").read_text())
    assert all("▾" not in source for source in core_sources)


def test_card_layout_has_tokens_focus_targets_and_reduced_motion_support():
    stylesheet = (ROOT / "static/style.css").read_text()

    for token in ("--surface:", "--surface-elevated:", "--focus-ring:", "--shadow:",
                  "--control-bg:", "--control-fg:", "--control-border:", "--control-shadow:",
                  "--space-1:", "--radius-md:", "--control-height: 2.75rem"):
        assert token in stylesheet
    assert ":focus-visible" in stylesheet
    assert "@media (prefers-reduced-motion: reduce)" in stylesheet
    assert "@media (max-width: 420px)" in stylesheet
    assert "overflow-x: hidden" not in stylesheet
    assert "display: contents" not in stylesheet
    assert "details-marker" not in stylesheet
    assert 'content: "▾"' not in stylesheet
    assert ':root[data-theme="light"] {' in stylesheet and "color-scheme: light" in stylesheet
    assert ':root[data-theme="dark"] {' in stylesheet and "color-scheme: dark" in stylesheet
    assert "button, input, select, textarea {" in stylesheet
    assert "background-color: var(--control-bg)" in stylesheet
    assert "box-shadow: var(--control-shadow)" in stylesheet
    assert ".filter-bar .pills a" in stylesheet
    assert ".trip-card-details > summary" in stylesheet
    assert ".trip-card.is-selected" in stylesheet
    assert ".selected-label { display: none" in stylesheet
