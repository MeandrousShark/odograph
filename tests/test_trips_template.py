from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.main import make_templates

TZ = ZoneInfo("America/Los_Angeles")
ROOT = Path(__file__).parents[1]
PLACES = [
    {"id": 1, "name": "Home", "kind": "home", "lat": 47.6, "lon": -122.3, "radius_m": 100},
    {"id": 2, "name": "Office", "kind": "work", "lat": 47.7, "lon": -122.4, "radius_m": 100},
]


def _render_index(vehicles=None, places=None, notice="", csp_nonce="", **filters):
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    return templates.env.get_template("trips.html").render(
        months=[], vehicles=vehicles or [], recent_purposes=[], user={"sub": "test"},
        csrf="token", places=places or [], notice=notice, csp_nonce=csp_nonce,
        filter_category=filters.get("category", ""),
        filter_from=filters.get("from_", ""), filter_to=filters.get("to", ""),
        filter_vehicle=filters.get("vehicle", ""), filter_q=filters.get("q", ""),
        filter_exclusion=filters.get("exclusion", ""),
        export_url=lambda *a, **k: "/", review_url="/review", ytd_year=2026,
        ytd_deduction=None,
    )


def _render_manual(vehicles=None, places=None, manual_prefill=None, csp_nonce=""):
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    return templates.env.get_template("manual_trip.html").render(
        vehicles=vehicles or [], places=places or [], recent_purposes=[],
        user={"sub": "test"}, csrf="token", manual_prefill=manual_prefill,
        csp_nonce=csp_nonce,
    )


def _render(vehicles):
    body = _render_manual(vehicles=vehicles)
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


def test_filter_bar_exclusion_select_has_all_states():
    body = _render_index(exclusion="not_my_vehicle")
    picker = body.split('name="exclusion"')[1].split("</select>")[0]

    assert '<option value="">All</option>' in picker
    assert '<option value="none">Normal trips</option>' in picker
    assert '<option value="not_my_vehicle" selected>Not one of my vehicles</option>' in picker
    assert '<option value="not_deductible">My vehicle, someone else drove</option>' in picker


def test_select_all_matching_is_discoverable_before_any_row_is_selected():
    body = _render_index()
    status = body.split('class="trip-archive-status"', 1)[1].split("</div>", 1)[0]

    assert 'id="selection-select-all"' in status
    assert ">Select all matching</button>" in status
    handler = body.split("selectionSelectAll.addEventListener('click'", 1)[1]
    assert "window.archiveController?.selectionQuery?.()" in handler
    assert "fetch(query ? `/trips/selection?${query}` : '/trips/selection'" in handler


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
    assert 'id="merge-dialog-open"' in body and ">Merge selected...</button>" in body
    assert 'id="delete-selected-open"' in body and ">Delete selected</button>" in body
    assert 'id="selection-clear"' in body and ">Clear selection</button>" in body
    assert 'id="selection-actions-open"' in body and ">Actions</button>" in body
    assert '<dialog id="selection-actions-dialog"' in body
    assert 'aria-labelledby="selection-actions-dialog-title"' in body
    assert 'id="selection-actions-mount"' in body
    assert 'data-selection-actions-close autofocus' in body

    # Selection lost its mode: there is no longer a "Done" button that exits
    # it, either in the header or the bar itself.
    assert 'id="selection-bar-done"' not in body
    assert 'id="selection-done"' not in body

    assert 'id="merge-bar"' not in body
    assert 'id="batch-submit"' not in body
    assert '>Apply to selected</button>' not in body
    assert 'id="merge-notes"' not in body
    assert 'id="merge-set-purpose"' not in body


def test_mobile_selection_uses_compact_strip_and_native_action_sheet():
    stylesheet = (ROOT / "static" / "style.css").read_text()
    narrow = stylesheet.split("  .selection-action-bar {", 1)[1]

    bar_rule = narrow.split("}", 1)[0]
    assert "grid-template-columns: minmax(0, 1fr) auto;" in bar_rule
    assert "max-height" not in bar_rule
    assert ".selection-action-bar > .selection-action-controls { display: none; }" in narrow
    assert ".selection-actions-open { display: inline-flex;" in narrow
    assert ".selection-actions-sheet[open]" in narrow
    assert "env(safe-area-inset-bottom)" in narrow
    assert "body.archive-has-selection" in narrow
    assert "display: contents" not in narrow


def test_five_bulk_action_dialogs_render_expected_fields():
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

    exclusion_dialog = body.split('id="exclusion-dialog"')[1].split("</dialog>")[0]
    assert '<input type="radio" name="exclusion" value=""> Normal trip' in exclusion_dialog
    assert 'value="not_my_vehicle"' in exclusion_dialog
    assert 'value="not_deductible"' in exclusion_dialog
    assert 'id="exclusion-dialog-confirm" disabled' in exclusion_dialog

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

    delete_dialog = body.split('id="delete-selected-dialog"')[1].split("</dialog>")[0]
    assert 'aria-describedby="delete-selected-dialog-description"' in delete_dialog
    assert "Stored location data is kept, so detected trips can be restored from Settings" in delete_dialog
    assert "permanently deleted and cannot be restored" in delete_dialog
    assert 'class="control control-destructive"' in delete_dialog
    assert 'data-selection-dialog-cancel' in delete_dialog
    assert 'id="delete-selected-confirm"' in delete_dialog


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
    assert "submitBatchUpdate(vehicleDialog, body)" in body
    delete_handler = body.split("deleteSelectedConfirm.addEventListener('click'")[1]
    assert "fetch('/trips/batch_delete'" in delete_handler
    assert "deletedIds.forEach((id) => selection.delete(id));" in delete_handler
    assert "showDialogError(deleteSelectedDialog" in delete_handler
    assert "submitBatchUpdate(purposeDialog, body)" in body

    # A batch write refreshes the archive in place instead of reloading the
    # document, which is what lets the selection and the action bar survive
    # it. The only reload left on the page is the bfcache guard.
    batch = body.split("async function submitBatchUpdate", 1)[1].split(
        "const categoryDialog", 1
    )[0]
    assert "window.location.reload()" not in batch
    assert "window.archiveController.finishWrite(true)" in batch
    assert "document.getElementById('trip-archive-header')?.focus();" in body


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
        ('data-selection-dialog-confirm="delete-selected-confirm"', "delete-selected-confirm"),
    ):
        assert form_marker in body
        assert f'id="{confirm_id}"' in body

    # The page has two submit listeners now (the archive filter form's Enter
    # handling is the other one), so this isolates the dialog guard by its
    # own first line rather than by the shared listener registration.
    submit_guard = body.split("const form = e.target.closest('.selection-dialog-content');")[1]
    assert "e.preventDefault()" in submit_guard
    assert "form.dataset.selectionDialogConfirm" in submit_guard
    assert "confirmButton.click()" in submit_guard
    assert "!confirmButton.disabled" in submit_guard


def test_selection_counts_pluralize_singular_trip():
    body = _render_index()

    assert "function tripCountLabel(n)" in body
    assert "n === 1 ? 'trip' : 'trips'" in body
    assert "el.textContent = tripCountLabel(selection.size)" in body
    # The bar's own label also has to say when part of the selection is not
    # on screen, so it comes from the shared helper (whose singular, plural,
    # and "outside this view" forms are proven in tests/js/) rather than
    # being assembled here.
    assert "selectionCount.textContent = selectionHelpers.selectionCountLabel(counts)" in body
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


def test_selection_has_no_mode_bar_follows_selection_size_and_escape_clears():
    body = _render_index()

    # No mode toggle anywhere: no "Select trips" entry point, no "Done"
    # button, no selectionMode state or body class.
    assert 'id="selection-start"' not in body
    assert 'id="selection-done"' not in body
    assert 'id="selection-bar-done"' not in body
    assert "selectionMode" not in body
    assert "selection-mode" not in body
    assert "setSelectionMode" not in body

    assert 'id="selection-clear"' in body and ">Clear selection</button>" in body
    assert "selectionClear.addEventListener('click', clearSelection)" in body

    # The bar's visibility is driven purely by selection size, not a mode:
    # it appears on the first check and leaves once the last one clears.
    assert "selectionBar.hidden = selection.size < 1;" in body
    assert "card.classList.toggle('is-selected', selected)" in body
    assert "categoryOpen.disabled = !hasSelection" in body
    assert "purposeOpen.disabled = !hasSelection" in body
    assert "vehicleOpen.disabled = !hasSelection" in body
    # Merge also needs every selected trip on screen, since eligibility is
    # read off the rendered rows; the rule itself is proven in tests/js/.
    assert "const canMerge = selectionHelpers.canMergeSelection(counts)" in body
    assert "mergeOpen.disabled = !canMerge" in body
    assert "Merging needs at least two selected trips." in body
    assert "Select at least two trips to apply or merge." not in body

    # The checkbox change handler always registers a selection now; there is
    # no mode guard gating it (the "selectionMode" absence check above
    # already covers this handler along with everything else).
    assert "if (!e.target.matches('.merge-select')) return;" in body

    # Escape backs out by clearing the selection (which hides the bar via
    # updateSelectionShell), instead of exiting a mode.
    # Two keydown listeners exist now (the archive filter form flushes a
    # pending search on Enter), so this isolates the Escape handler by its
    # own guard rather than by the shared listener registration.
    escape_handler = body.split("if (e.key !== 'Escape') return;", 1)[1].split("});", 1)[0]
    assert "if (selection.size >= 1) clearSelection();" in escape_handler

    assert "function reconcileSelection()" in body
    # An id whose row is not rendered is no longer dropped on sight: a batch
    # update can move a selected trip outside the current filters without
    # deleting it. Deletion prunes on its own path instead.
    assert "selection.delete(parseInt(deleted[1], 10));" in body
    assert "checkbox.checked = selected" in body
    assert "document.addEventListener('htmx:afterSwap'" in body
    assert "document.addEventListener('htmx:afterRequest'" in body


def test_manual_trip_page_is_unprefilled_by_default():
    body = _render_manual()

    assert '<div class="manual-trip-page" id="manual-trip">' in body
    assert '<h2>Add manual trip</h2>' in body
    assert 'name="start_time" value=""' in body


def test_manual_trip_page_without_prefill_has_no_missing_trip_message():
    body = _render_manual()
    assert '<div class="manual-trip-page" id="manual-trip">' in body
    assert 'name="start_time" value=""' in body
    assert "The date, start time, and notes were prefilled" not in body


def test_manual_trip_page_prefills_from_missing_trip_badge_link():
    body = _render_manual(manual_prefill={
        "date": "2026-07-01", "start_time": "08:10",
        "notes": "bridge: Work → Home", "osrm_hint": None,
    })
    assert '<div class="manual-trip-page" id="manual-trip">' in body
    assert 'name="date"' in body and 'value="2026-07-01"' in body
    assert 'name="start_time" value="08:10"' in body
    assert 'value="bridge: Work → Home"' in body
    assert "osrm-hint" not in body
    assert "The date, start time, and notes were prefilled" in body
    assert "Enter the end time and distance" in body


def test_manual_trip_form_shows_osrm_hint_when_present():
    body = _render_manual(manual_prefill={
        "date": "2026-07-01", "start_time": "08:10", "notes": "",
        "osrm_hint": "~1.4 mi by road",
    })
    assert 'class="osrm-hint"' in body
    assert "~1.4 mi by road" in body


def test_manual_trip_add_action_uses_a_centered_full_width_primary_row():
    body = _render_manual()
    css = (ROOT / "static/style.css").read_text()

    assert (
        '<div class="manual-trip-submit-row">\n'
        '    <button type="submit" class="control control-primary">Add</button>'
        in body
    )
    assert ".manual-trip-submit-row {" in css
    submit_row = _css_rule(css, ".manual-trip-submit-row")
    assert "flex: 1 0 100%;" in submit_row
    assert "justify-content: center;" in submit_row


def test_mobile_archive_filter_disclosure_uses_applied_state_and_preserves_status():
    inactive = _render_index()
    active = _render_index(category="business")

    assert 'data-archive-filter-active="false"' in inactive
    assert 'data-archive-filter-active="true"' in active
    for body in (inactive, active):
        assert 'data-archive-filter-toggle hidden aria-expanded="false"' in body
        assert 'aria-controls="trip-filter-controls"' in body
        controls_start = body.index('id="trip-filter-controls"')
        controls_end = body.index('</form>\n  </div>', controls_start)
        status_start = body.index('class="trip-archive-status"')
        assert controls_end < status_start
        assert 'id="archive-status"' in body
        assert 'id="archive-status-retry"' in body


def test_trip_pager_is_block_markup_with_stable_next_url():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("_trip_page_rows.html").render(
        trips=[], has_more=True, next_url="/trips/month/2026/1?offset=25",
    )
    assert '<div class="trip-pager">' in body
    assert "<tr" not in body and "<td" not in body
    assert 'hx-get="/trips/month/2026/1?offset=25"' in body
    assert 'hx-target="closest .trip-pager"' in body


def _css_rule(stylesheet: str, selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", stylesheet)
    assert match, f"missing CSS rule {selector}"
    return match.group(1)


def _render_months(months: list) -> str:
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    return templates.env.get_template("trips.html").render(
        months=months, vehicles=[], recent_purposes=[], user={"sub": "test"},
        csrf="token", places=[], notice="", filter_category="", filter_from="",
        filter_to="", filter_vehicle="", filter_q="", filter_exclusion="",
        export_url=lambda *a, **k: "/",
        review_url="/review", ytd_year=2026, ytd_deduction=None,
    )


def _archive_trip(trip_id: int, month: int) -> dict:
    return {
        "id": trip_id,
        "source": "detected",
        "started_at": datetime(2026, month, 5, 9, 0, tzinfo=timezone.utc),
        "ended_at": datetime(2026, month, 5, 9, 20, tzinfo=timezone.utc),
        "display_distance_m": 1609.344,
        "category": "unclassified",
        "exclusion": None,
        "purpose": None,
        "notes": None,
        "vehicle_id": None,
        "vehicle_name": None,
        "has_route_geometry": False,
        "has_gap": False,
        "expense_count": 0,
        "start_place_name": None, "start_lat": None, "start_lon": None,
        "end_place_name": None, "end_lat": None, "end_lon": None,
    }


def _archive_month(year: int, month_num: int, trip_id: int, next_url: str) -> dict:
    return {
        "label": datetime(year, month_num, 1).strftime("%B %Y"),
        "year": year, "month_num": month_num,
        "trip_count": 1, "total_m": 1609.344, "business_m": 0.0,
        "business_deduction": None,
        "trips": [_archive_trip(trip_id, month_num)],
        "has_more": True, "next_url": next_url,
    }


def test_two_months_paginate_independently_with_their_own_pager_and_rows():
    # Each month gets its own row-list wrapper and its own "Load more"
    # pager scoped to it via hx-target="closest .trip-pager"; a regression
    # that hoisted the pager out of the per-month loop or reused one next_url
    # for both months would slip past every other trips.html test, since
    # they all render with months=[].
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    months = [
        _archive_month(2026, 8, 101, "/trips/month/2026/8?offset=25"),
        _archive_month(2026, 7, 102, "/trips/month/2026/7?offset=25"),
    ]
    body = templates.env.get_template("trips.html").render(
        months=months, vehicles=[], recent_purposes=[], user={"sub": "test"},
        csrf="token", places=[], notice="", filter_category="", filter_from="",
        filter_to="", filter_vehicle="", filter_q="", filter_exclusion="",
        export_url=lambda *a, **k: "/",
        review_url="/review", ytd_year=2026, ytd_deduction=None,
    )

    assert body.count('class="trip-archive-row-list"') == 2
    assert body.count('class="trip-pager"') == 2
    assert 'hx-get="/trips/month/2026/8?offset=25"' in body
    assert 'hx-get="/trips/month/2026/7?offset=25"' in body
    assert body.index("August 2026") < body.index('id="trip-101"') < body.index("July 2026")
    assert body.index("July 2026") < body.index('id="trip-102"')
    # Each row-list's pager is scoped to it via hx-target="closest
    # .trip-pager", so the two "Load more" buttons stay independent even
    # though this asserts the shared attribute, not per-instance wiring.
    assert body.count('hx-target="closest .trip-pager"') == 2


def test_archive_row_header_and_rows_share_one_grid_definition():
    # The header used to define its own grid-template-columns and was never
    # guaranteed to match the data rows', which is what let their column
    # widths drift apart. Locking both to one custom property, and asserting
    # neither block still carries a literal desktop track list of its own,
    # is what keeps a future edit from quietly reintroducing that drift.
    stylesheet = (ROOT / "static/style.css").read_text()
    assert stylesheet.count("--trip-archive-columns:") == 1
    # A leading fixed track for the always-visible checkbox column, prepended
    # to the same token both blocks below still consume.
    assert (
        "--trip-archive-columns: 1.75rem 7.5rem minmax(0, 1fr) 13.75rem 5rem 3.75rem;"
        in stylesheet
    )

    header_block = _css_rule(stylesheet, ".trip-archive-row-header")
    row_block = _css_rule(stylesheet, ".trip-archive-row")

    for block in (header_block, row_block):
        assert block.count("grid-template-columns:") == 1
        assert "grid-template-columns: var(--trip-archive-columns);" in block
        assert "max-content" not in block
        assert "fit-content" not in block
        assert "grid-template-columns: auto" not in block
        # The "select" area leads both the header's decorative cell and the
        # row's own checkbox into the same leading track.
        assert 'grid-template-areas: "select time content category distance actions";' in block

    # Same column gap and the same horizontal inset (a padding matched by an
    # equal negative margin) on both, so their content boxes start at the
    # same offset from the row list's edge.
    assert "gap: 1rem;" in header_block and "gap: 1rem;" in row_block
    assert "margin: 0 -.875rem;" in header_block
    assert "margin: 0 -.875rem;" in row_block

    # The 760px card mode is unaffected in the ways that matter, and gains a
    # matching leading select column of its own rather than a shared token.
    # That column is a fixed 2.75rem (44px, the touch-target minimum) rather
    # than an auto track, so the checkbox gets a comfortably wide tap target
    # rather than shrink-wrapping to its own glyph.
    narrow = stylesheet.split("@media (max-width: 760px)", 1)[1]
    assert ".trip-archive-row-header { display: none; }" in narrow
    assert "grid-template-columns: 2.75rem minmax(0, 1fr) auto;" in narrow
    assert (
        'grid-template-areas: "select time distance" "select content content"'
        ' "select category actions";' in narrow
    )
    # The selector zone's base rule already anchors it to the card's own left
    # edge and stretches it the full card height; only its width differs at
    # this breakpoint, reaching across the card's own left padding, the
    # 2.75rem select column, and one grid gap, so the whole left strip -- not
    # just the small checkbox glyph -- responds to a tap.
    assert (
        ".trip-archive-row-selector { width: calc(var(--space-3) + 2.75rem + var(--space-3)); }"
        in narrow
    )


def test_archive_row_selector_covers_the_full_left_gutter_as_its_hit_area():
    # The checkbox glyph itself stays the same size; only the clickable
    # region enlarges, from the row's actual left edge across the select
    # track and one grid gap, so a stray click anywhere in that left band
    # toggles selection instead of falling through to the row's detail-link
    # overlay underneath (that overlay sits at z-index: 0, this zone at 1).
    stylesheet = (ROOT / "static/style.css").read_text()
    selector_block = _css_rule(stylesheet, ".trip-archive-row-selector")

    # Pinned to the row's own left edge (the same containing block
    # .trip-archive-row-link anchors to) rather than laid out as an in-flow
    # grid child, so the zone can extend past the select track's own width
    # without pushing or being constrained by the grid's other columns.
    assert "position: absolute;" in selector_block
    assert "position: relative;" not in selector_block
    assert "grid-area: select;" not in selector_block
    assert "left: 0;" in selector_block
    assert "top: 0;" in selector_block
    assert "bottom: 0;" in selector_block
    # Left padding bleed + the select track + one grid gap: reaches right up
    # to where the time column's own text starts.
    assert "width: calc(.875rem + 1.75rem + 1rem);" in selector_block
    assert "display: flex;" in selector_block
    assert "display: inline-flex;" not in selector_block
    assert "align-items: center;" in selector_block
    assert "justify-content: center;" in selector_block

    # The select grid track itself stays reserved (a fixed length, not
    # max-content/auto), so the now out-of-flow selector's absolute zone
    # can't cause the time column to slide left and get covered.
    row_block = _css_rule(stylesheet, ".trip-archive-row")
    assert "grid-template-columns: var(--trip-archive-columns);" in row_block


def test_archive_row_divider_uses_the_soft_rule_fade():
    stylesheet = (ROOT / "static/style.css").read_text()
    row_block = _css_rule(stylesheet, ".trip-archive-row")
    assert "background-image: var(--rule-fade-soft);" in row_block
    assert "background-image: var(--rule-fade);" not in row_block


def test_archive_row_shows_both_start_and_end_time():
    months = [_archive_month(2026, 8, 201, "/trips/month/2026/8?offset=25")]
    body = _render_months(months)
    row = body.split('id="trip-201"', 1)[1].split("</article>", 1)[0]

    times = re.findall(r'<time datetime="([^"]+)">([^<]+)</time>', row)
    assert len(times) == 2
    assert times[0][0] == "2026-08-05T09:00:00+00:00"
    assert times[1][0] == "2026-08-05T09:20:00+00:00"

    when = row.split('class="trip-archive-row-when-times"', 1)[1].split("</span>", 1)[0]
    assert " to " in when
    assert "\u2013" not in when and "\u2014" not in when
    assert 'aria-label="Trip time: ' in row
    assert " to " in row.split('aria-label="Trip time: ', 1)[1].split('"', 1)[0]


def test_month_heading_takes_its_own_class_and_larger_size():
    months = [_archive_month(2026, 8, 301, "/trips/month/2026/8?offset=25")]
    body = _render_months(months)
    assert '<h2 class="trip-archive-month-heading">August 2026</h2>' in body

    stylesheet = (ROOT / "static/style.css").read_text()
    heading_block = _css_rule(stylesheet, ".trip-archive-month-heading")
    assert "font-size: 1.25rem" in heading_block
    # h2's shared base rule is untouched: other pages still depend on it.
    assert "h2 { font-size: 1.05rem; margin: 1.5rem 0 .25rem; }" in stylesheet


def test_manual_page_owns_tile_attribution_and_archive_does_not():
    assert _render_manual().count("openstreetmap.org/copyright") == 1
    assert _render_index().count("openstreetmap.org/copyright") == 0


def test_trip_filters_are_a_compact_surface_not_a_disclosure():
    # Search, date, vehicle, category, and exclusion filters moved out of the
    # old "Filters & tools" disclosure into a compact surface that is always
    # visible; only the Clear control is gated on an active filter now, and
    # export/triage moved out entirely to the archive header.
    collapsed = _render_index()

    assert 'trip-page-disclosure' not in collapsed
    assert '<summary>Filters &amp; tools</summary>' not in collapsed
    assert '<summary>Add manual trip</summary>' not in collapsed
    assert 'id="manual-trip-form"' not in collapsed
    assert 'class="filter-bar trip-filter-bar trip-filter-surface"' in collapsed
    assert 'data-archive-date-preset' in collapsed
    assert 'value="this_month"' in collapsed
    assert 'value="last_month"' in collapsed
    assert 'value="this_year"' in collapsed
    assert 'value="custom"' in collapsed
    assert 'filter-date-row' in collapsed
    assert 'filter-apply-row' in collapsed
    assert '>Filter</button>' not in collapsed
    assert 'class="filter-link-groups"' not in collapsed
    assert 'class="control control-secondary filter-clear"' not in collapsed

    for filters in (
        {"category": "business"}, {"from_": "2026-07-01"},
        {"to": "2026-07-31"}, {"vehicle": "2"}, {"q": "zephyr"},
        {"exclusion": "not_my_vehicle"},
    ):
        active = _render_index(**filters)
        assert 'class="filter-bar trip-filter-bar trip-filter-surface"' in active
        assert '<a class="control control-secondary filter-clear" href="/trips">Clear</a>' in active


def test_trip_filter_form_has_a_search_box_that_carries_the_term():
    collapsed = _render_index()
    assert 'class="filter-search-row"' in collapsed
    assert 'type="search" name="q" value=""' in collapsed

    active = _render_index(q="zephyr")
    assert 'type="search" name="q" value="zephyr"' in active


def test_only_the_search_field_flexes_in_the_filter_form():
    """Adding search as a fourth competitor for a fixed form width shrank the
    submit button until its label broke one letter per line. Pinning the date
    and submit groups makes them unshrinkable, so the bar wraps instead, and
    letting the form itself grow keeps it to a single line so it does not cost
    the bar an extra row.
    """
    css = (ROOT / "static/style.css").read_text()
    assert ".filter-bar .trip-filter-form { flex: 1 1 34rem; flex-wrap: nowrap; }" in css
    assert ".filter-date-row, .filter-apply-row { flex: 0 0 auto; }" in css
    assert ".filter-search-row { flex: 1 1 10rem; min-width: 0; }" in css

    # Equal specificity with `.filter-bar .date-range { display: flex; }`, so
    # source order is what makes the form rule win; and the narrow-viewport
    # block must still come later to restore the stacked grid.
    form_rule = css.index(".filter-bar .trip-filter-form { flex:")
    assert css.index(".filter-bar .date-range { display: flex;") < form_rule
    assert form_rule < css.index("@media (max-width: 760px)")


def test_shared_trip_page_disclosure_css_survives_for_stats_page():
    # trips.html no longer pairs a "Filters & tools" disclosure with the
    # manual-trip one (filters moved to the always-visible surface below),
    # but stats.html still renders its own single Filters disclosure with
    # these exact classes, so the shared component these rules describe must
    # keep working even though trips.html has stopped using it.
    stylesheet = (ROOT / "static/style.css").read_text()
    settings_source = (ROOT / "app/templates/settings.html").read_text()
    stats_source = (ROOT / "app/templates/stats.html").read_text()

    assert '<details class="trip-page-disclosure trip-tools"' in stats_source
    assert ".trip-page-disclosures {" in stylesheet
    assert "--trip-page-summary-width: min(9rem, calc(50% - var(--space-1)))" in stylesheet
    assert ".trip-page-disclosure > summary {" in stylesheet
    assert "position: absolute; top: 0; width: var(--trip-page-summary-width);" in stylesheet
    assert ".trip-tools > summary { left: 0; }" in stylesheet
    assert ".trip-tools > .trip-filter-bar {" in stylesheet
    assert "trip-page-disclosure" not in settings_source


def test_trip_archive_header_and_filter_surface_reuse_shared_tokens():
    stylesheet = (ROOT / "static/style.css").read_text()

    assert ".trip-archive-header {" in stylesheet
    assert "background: var(--accent-soft-raised);" in stylesheet
    assert ".trip-filter-surface {" in stylesheet
    assert "background: var(--surface-raised);" in stylesheet
    assert "position: sticky; top: 0; }" in stylesheet
    # A generous, deliberately non-computed clearance so a keyboard user
    # tabbing forward can never land a focused row control hidden behind the
    # sticky filter surface. Both a descendant combinator (a focusable
    # element nested inside a later sibling) and a plain sibling combinator
    # (a later sibling that is itself focusable, e.g. a <summary>) are
    # needed to cover every case.
    assert (
        ".trip-filter-surface ~ * :is(a, button, input, select, textarea, summary),\n"
        "  .trip-filter-surface ~ :is(a, button, input, select, textarea, summary) {"
        in stylesheet
    )
    assert "scroll-margin-top: 8rem;" in stylesheet


def test_archive_header_shows_ytd_deduction_when_a_rate_is_on_file():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("trips.html").render(
        months=[], vehicles=[], recent_purposes=[], user={"sub": "test"}, csrf="token",
        places=[], notice="",
        filter_category="", filter_from="", filter_to="", filter_vehicle="", filter_q="",
        filter_exclusion="",
        export_url=lambda *a, **k: "/", review_url="/review",
        ytd_year=2026, ytd_deduction=293.56,
    )

    # The metric restyle echoes the Dashboard hero's label-then-value
    # treatment (see .dashboard-secondary-label/strong) instead of the old
    # flat sentence, so the label and value are now separate elements.
    assert 'class="trip-archive-ytd-metric"' in body
    assert 'class="trip-archive-ytd-label">Year-to-date (2026) business deduction</span>' in body
    assert "$293.56" in body
    assert "No mileage rate on file" not in body


def test_archive_header_shows_missing_rate_branch_when_ytd_deduction_is_none():
    body = _render_index()

    assert 'class="trip-archive-ytd-metric trip-archive-ytd-metric-muted"' in body
    assert "No mileage rate on file for 2026 yet" in body
    assert 'href="/settings">Add one</a>' in body


def test_archive_header_carries_export_and_manual_entry_points_but_no_selection_toggle_or_review_link():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("trips.html").render(
        months=[], vehicles=[], recent_purposes=[], user={"sub": "test"}, csrf="token",
        places=[], notice="",
        filter_category="", filter_from="", filter_to="", filter_vehicle="", filter_q="",
        filter_exclusion="",
        export_url=lambda kind: f"/export/{kind}", review_url="/review?from=2026-07-01",
        ytd_year=2026, ytd_deduction=None,
    )
    header = body.split('class="trip-archive-header"', 1)[1].split('trip-filter-surface', 1)[0]

    # Selection has no mode toggle any more, so the header no longer carries
    # a "Select trips" entry point.
    assert 'id="selection-start"' not in header
    assert 'id="selection-done"' not in header
    assert 'href="/export/csv"' in header and 'href="/export/xlsx"' in header
    # Review unclassified was removed from the header entirely: it duplicated
    # the Review nav tab and the dashboard's own attention surface.
    assert 'Review unclassified' not in header
    assert 'href="/review?from=2026-07-01"' not in header
    assert 'href="/trips/manual"' in header
    assert '>Add manual trip</a>' in header
    # These entry points must not also appear inside the filter surface,
    # which now carries only search/date/vehicle/category/exclusion.
    filter_surface = body.split('trip-filter-surface"', 1)[1]
    assert 'id="selection-start"' not in filter_surface
    assert 'href="/export/csv"' not in filter_surface


def test_archive_header_export_is_a_labelled_disclosure_with_no_field_pills():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("trips.html").render(
        months=[], vehicles=[], recent_purposes=[], user={"sub": "test"}, csrf="token",
        places=[], notice="",
        filter_category="", filter_from="", filter_to="", filter_vehicle="", filter_q="",
        filter_exclusion="",
        export_url=lambda kind: f"/export/{kind}", review_url="/review?from=2026-07-01",
        ytd_year=2026, ytd_deduction=None,
    )
    header = body.split('class="trip-archive-header"', 1)[1].split('trip-filter-surface', 1)[0]

    # field_pills leaves the header entirely: no label-above-pills wrapper
    # remains for Export.
    assert 'class="field' not in header
    assert 'class="pills"' not in header
    # Export is a disclosure trigger styled like the header's other bare
    # controls, carrying the download icon plus a visible "Export" label
    # (not icon-only), opted into the shared delegated disclosure script the
    # Account and More menus already use.
    assert 'class="trip-archive-export" data-header-disclosure="export"' in header
    assert '<summary class="control control-secondary trip-archive-export-trigger">' in header
    assert '#download' in header
    assert '>Export</summary>' in header
    assert '<a class="trip-archive-export-action" href="/export/csv">CSV</a>' in header
    assert '<a class="trip-archive-export-action" href="/export/xlsx">XLSX</a>' in header
    # Review unclassified is gone from the header (redundant with the Review
    # nav tab and the dashboard's own attention surface).
    assert 'Review unclassified' not in header


def test_archive_ytd_partial_keeps_stable_id_and_oob_attribute():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    template = templates.env.get_template("_trip_archive_ytd.html")

    in_band = template.render(ytd_year=2026, ytd_deduction=293.56, ytd_oob=False)
    assert '<div id="trip-archive-ytd">' in in_band
    assert "hx-swap-oob" not in in_band

    out_of_band = template.render(ytd_year=2026, ytd_deduction=293.56, ytd_oob=True)
    assert '<div id="trip-archive-ytd" hx-swap-oob="outerHTML">' in out_of_band
    assert "$293.56" in out_of_band


def test_archive_header_links_to_dedicated_manual_trip_page():
    header = _render_index().split('class="trip-archive-header"', 1)[1].split(
        'trip-filter-surface', 1
    )[0]
    assert 'href="/trips/manual"' in header
    assert 'href="#manual-trip-form"' not in header
    assert 'manual_open' not in header


def test_trip_filter_surface_preserves_urls_and_compacts_narrow_layout():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("trips.html").render(
        months=[], vehicles=[], recent_purposes=[], user={"sub": "test"}, csrf="token",
        places=[], notice="",
        filter_category="business", filter_from="2026-07-01", filter_to="2026-07-31",
        filter_vehicle="",
        export_url=lambda kind: f"/export/{kind}", review_url="/review?from=2026-07-01",
        ytd_year=2026, ytd_deduction=None,
    )
    stylesheet = (ROOT / "static/style.css").read_text()

    assert 'class="filter-bar trip-filter-bar trip-filter-surface"' in body
    assert 'class="category-segmented trip-filter-category"' in body
    assert 'id="trip-filter-category-business" name="category" value="business" checked>' in body
    assert 'id="trip-filter-category-all" name="category" value=""' in body
    assert 'name="date_preset"' in body
    assert 'name="from" value="2026-07-01"' in body
    assert 'name="to" value="2026-07-31"' in body
    assert 'href="/export/csv"' in body and 'href="/export/xlsx"' in body
    assert ".trip-filter-bar { width: 100%; justify-content: space-between; }" in stylesheet
    assert "@media (max-width: 900px)" in stylesheet
    assert ".trip-filter-bar { justify-content: flex-start; }" in stylesheet
    assert ".trip-filter-bar .filter-date-row" in stylesheet
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in stylesheet
    assert ".trip-filter-bar .filter-apply-row" in stylesheet
    assert "grid-template-columns: minmax(0, 1fr) auto auto" in stylesheet


def test_archive_filter_surface_applies_live_with_a_status_region_and_no_filter_button():
    body = _render_index()
    stylesheet = (ROOT / "static/style.css").read_text()

    # The controller's request/debounce/comparison logic lives in a separate
    # same-origin script so it can be exercised without a browser (see
    # tests/js/); CSP's 'self' covers it without a nonce.
    assert '<script src="/static/archive_state.js"></script>' in body

    surface = body.split('trip-filter-surface"', 1)[1]
    # Filters apply in place, so the form has no control left to press. The
    # mobile disclosure toggle is the only control before the status region;
    # Retry remains the only request action.
    assert 'type="submit"' not in surface
    assert ">Filter</button>" not in body
    before_status = surface.split('class="trip-archive-status"')[0]
    assert before_status.count("<button") == 1
    assert 'data-archive-filter-toggle hidden aria-expanded="false"' in before_status
    assert 'aria-controls="trip-filter-controls"' in before_status
    assert 'id="trip-filter-controls" class="trip-filter-controls"' in before_status

    # One general-purpose region: the bulk-action flows announce through it
    # too, so it is not named after filtering.
    assert (
        '<p id="archive-status" class="trip-archive-status-message" '
        'role="status" aria-live="polite">' in surface
    )
    assert 'id="archive-status-retry"' in surface
    assert 'hidden>Retry</button>' in surface
    assert 'data-archive-filter-active="false"' in body
    assert '.trip-filter-toggle { display: none; }' in stylesheet
    assert '.trip-filter-surface.is-js-enhanced .trip-filter-toggle' in stylesheet

    assert '.trip-archive-results[aria-busy="true"] { opacity: .6; }' in stylesheet


def test_archive_controller_is_wired_to_the_shared_list_endpoint():
    """Wiring only: the request, debounce, and comparison behavior itself is
    proven by tests/js/ against static/archive_state.js, and the real DOM and
    htmx history interactions remain a browser gate. This guards the seams
    between the two, which no other test can see.
    """
    body = _render_index()
    controller = body.split("const helpers = window.ArchiveState;", 1)[1].split(
        "</script>", 1
    )[0]

    assert "const SEARCH_DELAY_MS = 350;" in controller
    assert "helpers.createRequestScope()" in controller
    assert "helpers.createDebouncer({" in controller
    assert "htmx.ajax('GET', query ? `/trips/list?${query}` : '/trips/list'" in controller
    assert "swap: 'outerHTML'," in controller
    # A filter change resets each month's pagination by design, so the
    # transient depth metadata rides on the post-write refresh only.
    apply_filters = controller.split("const applyFilters = () => {", 1)[1].split(
        "\n    };", 1
    )[0]
    assert "loaded_depth" not in apply_filters
    refresh = controller.split("const finishWrite = (refresh) => {", 1)[1].split(
        "\n    };", 1
    )[0]
    assert "requestArchive(appendLoadedDepth(query)," in refresh

    # The generation has to travel with the request: htmx does not promise to
    # issue one synchronously, and an unadopted request loses its stale
    # guard, its busy flag, and its status message. The tag is sent as a
    # request header and read back off the request config, and the request is
    # attributed to the filter surface so it cannot collide with htmx's
    # per-element bookkeeping for document.body.
    assert "const REQUEST_TOKEN_HEADER = 'X-Archive-Request';" in controller
    assert "headers[REQUEST_TOKEN_HEADER] = String(token);" in controller
    assert "source: REQUEST_SOURCE_SELECTOR," in controller
    adoption = controller.split("document.addEventListener('htmx:beforeRequest'", 1)[1]
    assert "config.headers[REQUEST_TOKEN_HEADER]" in adoption
    # Months paginate independently, so a pager request joins the generation
    # on screen instead of superseding another month's outstanding request.
    assert "token = scope.currentToken();" in adoption
    assert "scope.start()" not in adoption.split("});", 1)[0]

    # A superseded response must not reach the rows, the out-of-band export
    # links and summaries, or the address bar, and must not read as an error.
    guard = controller.split("document.addEventListener('htmx:beforeSwap'", 1)[1]
    assert "event.detail.shouldSwap = false;" in guard
    assert "event.detail.isError = false;" in guard

    for event_name in ("htmx:beforeRequest", "htmx:afterRequest",
                       "htmx:historyCacheMiss", "htmx:historyRestore"):
        assert f"document.addEventListener('{event_name}'" in controller

    # Selection state stays in one place; the controller only calls the hook.
    assert "window.archiveClearSelection = clearSelection;" in body
    assert "const selection = new Set();" not in controller


def test_archive_controller_restores_depth_without_polluting_canonical_urls():
    body = _render_index()
    controller = _archive_controller(body)

    assert "const ARCHIVE_HISTORY_STATE_KEY = 'odographArchive';" in controller
    assert "helpers.validateLoadedDepth(archive.loadedDepth)" in controller
    assert "helpers.loadedDepthRestore(saved.depth, rendered)" in controller
    assert "window.history.replaceState(next, document.title, window.location.href);" in controller
    assert "next[ARCHIVE_HISTORY_STATE_KEY]" in controller
    assert "requestArchive(appendLoadedDepth(query, restoration.depth)" in controller
    assert "window.addEventListener('DOMContentLoaded', restoreLoadedDepth, { once: true });" in controller
    assert "helpers.filterDisclosureState(isMobile, filterDisclosureOpen)" in controller
    assert "window.matchMedia('(max-width: 760px)')" in controller
    assert "media.addEventListener('change', refreshForViewport);" in controller
    assert "media.addListener(refreshForViewport);" in controller
    assert "window.addEventListener('resize', refreshForViewport);" in controller
    restored = controller.split("const restoreFromArchiveState = () => {", 1)[1].split(
        "const isPagerRequest", 1
    )[0]
    assert "restoreLoadedDepth();" in restored
    assert "depthRestore: true" in controller

    # A failed restore locks writes and leaves a read-only retry path, while a
    # successful canonical response clears the lock and records new depth.
    assert "isWriteUnavailable: () => depthRestorePending || depthRestoreFailed" in controller
    assert "if (depthRestorePending || depthRestoreFailed) return false;" in controller
    assert "depthRestoreFailed = true;" in controller
    assert "depthRestoreFailed = false;" in controller
    retry = controller.split("#archive-status-retry", 1)[1]
    assert "depthRestore: lastAttempt.depthRestore," in retry


def test_archive_controller_canonicalizes_resolved_dates_and_applied_state():
    body = _render_index()
    controller = _archive_controller(body)

    # The form's disabled preset bounds are still read and serialized as the
    # applied canonical query. Explicit preset changes use the transient mode,
    # which omits stale bounds until the response supplies resolved dates.
    pairs = controller.split("const filterPairs = () => {", 1)[1].split(
        "const currentQuery", 1
    )[0]
    assert "const data = new FormData(form);" in pairs
    assert "field ? field.value : data.get(name)" in pairs
    assert "helpers.archiveFilterQuery(filterPairs(), options)" in controller
    assert "const transientPreset = draftPreset" in controller
    assert "currentQuery(transientPreset ? { transientPreset: true } : undefined)" in controller

    # Successful navigation derives the applied comparison from the returned
    # archive state, never from the transient request or draft controls.
    assert "lastApplied = helpers.archiveStateQuery(state);" in controller
    assert "if (!afterWrite || !deferredApply) syncResolvedControls(state);" in controller
    assert "setFieldValue(form, 'date_preset', state.date_preset || 'all');" in controller
    assert "updateCustomDates();" in controller.split(
        "const syncResolvedControls", 1
    )[1].split("const restoreFromArchiveState", 1)[0]
    assert "draftPreset = null;" in controller


def test_archive_controller_serializes_writes_and_deferred_draft_navigation():
    body = _render_index()
    controller = _archive_controller(body)

    assert "const writeCoordinator = helpers.createWriteCoordinator();" in controller
    assert "if (writeInFlight && !settings.afterWrite)" in controller
    assert "writeCoordinator.deferDraft();" in controller
    assert "writeCoordinator.startRefresh();" in controller
    assert "writeCoordinator.failPost();" in controller
    assert "const shouldApply = writeCoordinator.completeRefresh();" in controller
    assert "writeQuery = lastApplied;" in controller
    assert "let deferredHistoryPath = null;" in controller
    assert "scope.start();" in controller.split("const beginWrite", 1)[1]
    assert "abortSuperseded();" in controller.split("const beginWrite", 1)[1]
    assert "invalidateArchiveHistoryCache();" in controller.split("const beginWrite", 1)[1]
    assert "deferredHistoryPath = historyRetry.path();" in controller.split(
        "const beginWrite", 1
    )[1]
    assert "historyRetry.clear();" in controller.split("const beginWrite", 1)[1]
    assert "isWriteBusy: () => writeInFlight," in controller

    # Refresh failures keep afterWrite on the read-only retry path instead of
    # reopening a second POST or ending the write gate early.
    settled = controller.split("document.addEventListener('htmx:afterRequest'", 1)[1]
    assert "if (afterWrite) endWriteWindow();" in settled
    assert "'The update was saved, but the trip list could not be refreshed.'" in settled
    retry = controller.split("#archive-status-retry", 1)[1]
    assert "requestArchive(lastAttempt.query, {" in retry
    assert "afterWrite: lastAttempt.afterWrite," in retry
    assert "batch_update" not in retry
    finish_failure = controller.split("const finishWrite = (refresh) => {", 1)[1].split(
        "const query = writeQuery", 1
    )[0]
    assert "setBusy(false);" in finish_failure
    assert finish_failure.index("setBusy(false);") < finish_failure.index("applyFilters();")


def test_archive_controller_completes_history_cache_miss_retry_lifecycle():
    body = _render_index()
    controller = _archive_controller(body)

    assert "const historyMisses = new Map();" in controller
    assert "const historyRetry = helpers.createHistoryRetry();" in controller
    assert "searchDebounce.cancel();" in controller.split(
        "document.addEventListener('htmx:historyCacheMiss'", 1
    )[1].split("});", 1)[0]
    assert "const xhr = event.detail.xhr;" in controller
    assert "historyMisses.set(xhr, { token, path });" in controller
    assert "xhr.addEventListener('error', fail" in controller
    assert "xhr.addEventListener('timeout', fail" in controller
    assert "xhr.addEventListener('abort', fail" in controller
    assert "document.addEventListener('htmx:historyCacheMissLoadError'" in controller
    history_failure = controller.split("const failHistoryMiss", 1)[1].split(
        "document.addEventListener('htmx:historyCacheMiss'", 1
    )[0]
    assert "scope.release(xhr);" in history_failure
    assert "setBusy(false);" in history_failure
    assert "historyRetry.pending() && lastAttempt.kind === 'history'" in controller
    assert "window.location.href = historyRetry.path();" in controller
    assert "deferredHistoryPath = path;" in controller
    assert "deferredHistoryPath = helpers.historyRestorePath(" in controller
    history_restore = controller.split(
        "document.addEventListener('htmx:historyRestore'", 1
    )[1].split("document.addEventListener('input'", 1)[0]
    assert "event.detail" in history_restore
    assert "helpers.historyRestorePath(" in history_restore
    assert "window.location.href = historyPath;" in controller
    assert "historyMisses.delete(request);" in controller
    assert "historyRetry.clear();" in controller


def _archive_controller(body):
    return body.split("const helpers = window.ArchiveState;", 1)[1].split("</script>", 1)[0]


def test_batch_refresh_reads_the_rendered_depth_and_keeps_it_out_of_shared_urls():
    """Wiring only: the depth map itself is proven in tests/js/. The depth is
    transient refresh metadata, so it must reach the list request without
    reaching the canonical archive URL or the export links.
    """
    body = _render_index()
    controller = _archive_controller(body)

    depth = controller.split("const loadedDepthEntries = () => {", 1)[1].split("\n    };", 1)[0]
    assert "`${RESULTS_SELECTOR} [data-archive-month]`" in depth
    assert "list.dataset.archiveMonth" in depth
    assert "list.querySelectorAll('.trip-archive-item').length" in depth
    assert "helpers.loadedDepthMap(entries)" in depth

    # The refresh separates the request's own query from the applied filter
    # state, which is what the address bar, the exports, and the next filter
    # comparison are built from.
    refresh = controller.split("const finishWrite = (refresh) => {", 1)[1].split(
        "\n    };", 1
    )[0]
    assert "applied: query," in refresh
    assert "afterWrite: true," in refresh
    assert "loaded_depth" not in controller.split("const syncExportLinks", 1)[1]


def test_batch_refresh_keeps_the_selection_and_reports_a_failed_read_as_read_only():
    body = _render_index()
    controller = _archive_controller(body)
    settled = controller.split("document.addEventListener('htmx:afterRequest'", 1)[1]

    # A filter navigation clears the selection; a post-write refresh must not.
    assert "if (!afterWrite) clearArchiveSelection();" in settled
    assert "announce(afterWrite ? 'Trips updated' : 'Trip list updated');" in settled
    # The write already happened, so the failure message must not invite
    # repeating it, and Retry reissues the read alone.
    assert "'The update was saved, but the trip list could not be refreshed.'" in settled
    retry = controller.split("#archive-status-retry", 1)[1]
    assert "requestArchive(lastAttempt.query, {" in retry
    assert "afterWrite: lastAttempt.afterWrite," in retry
    assert "batch_update" not in retry


def test_archive_serializes_filter_and_pager_requests_against_a_batch_write():
    body = _render_index()
    controller = _archive_controller(body)

    # A filter change during the write is deferred, not dropped: the control
    # already holds the new choice.
    apply_filters = controller.split("const applyFilters = () => {", 1)[1].split(
        "\n    };", 1
    )[0]
    assert "if (writeInFlight) {" in apply_filters
    assert "deferredApply = true;" in apply_filters
    assert "deferredApply = false;" in controller.split("const endWriteWindow", 1)[1]

    # The deferred change leaves its control holding the new value, so the
    # refresh uses the filters captured when the write started rather than
    # turning itself into a navigation the selection is meant to survive.
    begin = controller.split("const beginWrite", 1)[1]
    assert "if (!writeCoordinator.begin(lastApplied)) return false;" in begin
    assert "writeQuery = lastApplied;" in begin
    assert "scope.start();" in begin
    assert "abortSuperseded();" in begin
    assert "searchDebounce.cancel();" in begin
    assert "const query = writeQuery === null ? currentQuery() : writeQuery;" in controller

    # A page appended between the write and its refresh would be rows from
    # before the write, so the pager request is suppressed instead.
    pager = controller.split("} else if (isPagerRequest(detail.elt)) {", 1)[1]
    assert "if (writeInFlight || historyMisses.size > 0 || depthRestorePending || depthRestoreFailed) {" in pager
    assert "event.preventDefault();" in pager

    # A successful refresh closes the write window. A failed refresh keeps it
    # open for the read-only retry, even if a newer intent superseded it.
    assert "const afterWrite = writeRefreshTokens.delete(token);" in controller
    assert "if (afterWrite) endWriteWindow();" in controller

    # The selection script gets one named interface, not the internals.
    assert "window.archiveController = {" in controller
    for member in ("beginWrite:", "finishWrite,", "announce,"):
        assert member in controller.split("window.archiveController = {", 1)[1]


def test_batch_dialog_write_holds_its_confirm_button_and_closes_before_refreshing():
    body = _render_index()
    batch = body.split("async function submitBatchUpdate", 1)[1].split(
        "const categoryDialog", 1
    )[0]

    assert "confirmButton.disabled = true;" in batch
    assert batch.count("confirmButton.disabled = false;") == 3
    assert "if (!window.archiveController.beginWrite()) return;" in batch
    assert "setDialogBusy(dialog, true);" in batch
    assert "setDialogBusy(dialog, false);" in batch
    assert "dialog.dataset.archiveBusy = busy ? 'true' : 'false';" in body
    assert "document.addEventListener('cancel'" in body
    # Closing is what returns focus to the triggering button, through the
    # dialog's own close handler.
    assert batch.index("dialog.close();") < batch.index(
        "window.archiveController.finishWrite(true)"
    )
    # A failed write keeps the dialog and the selection, and reads nothing.
    assert "window.archiveController.finishWrite(false);" in batch
    assert "showDialogError(dialog, err.detail || 'Batch update failed.');" in batch
    assert "window.archiveController.announce('The update could not be saved.');" in batch

    # Merge participates in the same global guard and restores interaction
    # when its POST fails instead of bypassing the batch lifecycle.
    merge = body.split("mergeConfirm.addEventListener('click'", 1)[1]
    assert "if (!window.archiveController.beginWrite()) return;" in merge
    assert "setDialogBusy(mergeDialog, true);" in merge
    assert "window.archiveController.finishWrite(false);" in merge
    assert "const data = await resp.json().catch(() => null);" in merge
    assert "data.trip_id === undefined" in merge
    assert "window.archiveController.announce('The merge could not be completed.');" in merge


def test_row_mutations_invalidate_the_archive_history_snapshot_once():
    """A cached snapshot predates the mutation, so a Back navigation could
    restore it over a successful edit. One delegated listener covers every
    row-level htmx write instead of a call site per action.
    """
    body = _render_index()
    controller = _archive_controller(body)
    listener = controller.split("if (!detail.successful || !config || config.verb === 'get')", 1)
    assert len(listener) == 2
    assert "elt.closest(RESULTS_SELECTOR)" in listener[1]
    assert "invalidateArchiveHistoryCache();" in listener[1]


def test_deletion_is_the_only_missing_row_that_leaves_the_selection():
    """The batch endpoint validates every id atomically, so a deleted id left
    behind would fail the whole next batch. A row that is merely no longer
    rendered is not evidence of deletion.
    """
    body = _render_index()
    selection = body.split("const selectionHelpers = window.ArchiveState;", 1)[1]

    assert "const TRIP_DELETE_PATH = /^\\/trips\\/(\\d+)\\/delete$/;" in selection
    prune = selection.split("const deleted = TRIP_DELETE_PATH.exec(config.path || '');", 1)[1]
    assert "selection.delete(parseInt(deleted[1], 10));" in prune

    reconcile = selection.split("function reconcileSelection() {", 1)[1].split("}", 1)[0]
    assert "selection.delete" not in reconcile
    assert "updateSelectionShell();" in reconcile

    # Reconciliation runs through the shared rule, and only a navigation
    # ends the whole selection.
    assert "selectionHelpers.reconcileSelection(" in selection
    assert "{ intent: intent || 'refresh' }," in selection
    assert "updateSelectionShell('navigate');" in selection


def test_archive_results_root_is_the_history_element_and_carries_restorable_state():
    """History restoration replaces this root's contents and nothing else, so
    everything a sibling control needs (the applied filters, both export
    hrefs, and the canonical URL) has to travel inside it. The attribute has
    to survive HTML parsing too: Jinja's tojson escapes single quotes but not
    double ones, so a double-quoted attribute would truncate at the JSON's
    first key.
    """
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    archive_state = {
        "url": "/trips?category=business&q=zephyr",
        "date_preset": "custom", "from": "2026-07-01", "to": "",
        "category": "business", "vehicle": "3", "q": "zephyr", "exclusion": "none",
        "export_csv": "/export?format=csv&category=business&q=zephyr",
        "export_xlsx": "/export?format=xlsx&category=business&q=zephyr",
    }
    body = templates.env.get_template("_trip_archive_results.html").render(
        months=[], archive_state=archive_state,
    )

    assert '<div id="trip-archive-results" class="trip-archive-results" hx-history-elt>' in body
    assert "hx-history-elt" not in body.replace(
        '<div id="trip-archive-results" class="trip-archive-results" hx-history-elt>', "", 1
    )

    raw = body.split("data-archive-state='", 1)[1].split("'", 1)[0]
    restored = json.loads(raw)
    assert restored == archive_state


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
        for name in ("base.html", "trips.html", "_trip_archive_row.html", "_trip_edit_card.html",
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
    # Checkboxes and radios are deliberately excluded from the shared text
    # control surface: a native checkbox given a fill and a rounded border
    # renders as a glyph on top of a painted box on iOS Safari.
    assert 'button, input:not([type="checkbox"]):not([type="radio"]), select, textarea {' in stylesheet
    assert "button, input, select, textarea {" not in stylesheet
    assert "background-color: var(--control-fill)" in stylesheet
    assert "box-shadow: var(--control-shadow)" in stylesheet
    assert ".filter-bar .pills a" in stylesheet
    assert ".trip-archive-row-more-trigger::marker { content: \"\"; }" in stylesheet
    assert ".trip-card.is-selected" in stylesheet
    assert ".selected-label { display: none" in stylesheet


def test_route_mode_radios_render_with_none_checked_by_default():
    body = _render_manual(places=PLACES)
    picker = body.split('class="route-picker"')[1].split("</fieldset>", 1)[0]

    assert '<input type="radio" name="route_mode" value="none" checked>' in picker
    assert '<input type="radio" name="route_mode" value="places">' in picker
    assert '<input type="radio" name="route_mode" value="map">' in picker
    # Only the "none" option carries `checked`.
    assert picker.count("checked") == 1


def test_place_selects_are_populated_from_the_places_context():
    body = _render_manual(places=PLACES)
    picker = body.split('class="route-picker"')[1].split("<label>Distance", 1)[0]

    start_select = picker.split('name="start_place"')[1].split("</select>")[0]
    end_select = picker.split('name="end_place"')[1].split("</select>")[0]
    for select in (start_select, end_select):
        assert '<option value="1">Home</option>' in select
        assert '<option value="2">Office</option>' in select


def test_route_picker_has_hidden_coordinate_fields_map_reset_and_status_region():
    body = _render_manual(places=PLACES)
    picker = body.split('class="route-picker"')[1].split("<label>Distance", 1)[0]

    assert '<input type="hidden" name="start_lat">' in picker
    assert '<input type="hidden" name="start_lon">' in picker
    assert '<input type="hidden" name="end_lat">' in picker
    assert '<input type="hidden" name="end_lon">' in picker
    assert '<input type="hidden" name="routed_distance">' in picker
    assert '<div id="route-picker-map" class="route-picker-map" hidden></div>' in picker
    assert 'data-route-map-reset' in picker
    assert '>Reset points</button>' in picker
    assert 'class="route-picker-status" role="status">' in picker


def test_distance_input_is_cleared_in_lockstep_with_routed_distance_hint():
    """The server (app/ui/manual.py, add_manual_trip) treats a submitted
    `distance` that differs from the hidden `routed_distance` hint as a
    deliberate override and stores it verbatim. If a route selection is invalidated
    (mode change, map reset, or a preview that fails) without also clearing
    a preview-owned `distance`, the leftover value becomes indistinguishable
    from something the user actually typed. This asserts the helper exists,
    only clears a value it recognizes as its own, and is actually wired into
    every site that blanks `routed_distance` -- not just that the string
    "clearDistanceIfPreviewOwned" appears somewhere in the page.
    """
    body = _render_manual(places=PLACES)

    assert "function clearDistanceIfPreviewOwned()" in body
    helper = body.split("function clearDistanceIfPreviewOwned() {")[1].split("\n  }\n", 1)[0]
    # The comparison has to read both fields, and must not unconditionally
    # blank `distance` -- only when it still matches the routed hint.
    assert "routeField('distance')" in helper
    assert "routeField('routed_distance')" in helper
    assert "current === hint" in helper
    assert "distance.value = '';" in helper

    def block(start_marker: str, end_marker: str = "\n  }") -> str:
        return body.split(start_marker, 1)[1].split(end_marker, 1)[0]

    assert "let routePreviewOwnsDistance = false;" in body
    assert "let routeDistanceUserOwned = false;" in body
    assert "routePreviewOwnsDistance = true;" in body
    assert "routePreviewOwnsDistance = false;" in body
    assert "routeDistanceUserOwned = true;" in body
    assert "!routeDistanceUserOwned" in body

    # Every route invalidation must run the helper before routed_distance is
    # blanked, since the comparison needs the pre-clear hint value.
    endpoints_block = block("function clearRouteEndpoints() {")
    assert "invalidateRoutePreview();" in endpoints_block

    invalidation_block = block("function invalidateRoutePreview() {")
    assert "clearDistanceIfPreviewOwned();" in invalidation_block
    assert invalidation_block.index("clearDistanceIfPreviewOwned();") < invalidation_block.index(
        "setRouteFieldValue('routed_distance', '');"
    )

    # The "Reset points" button's click handler.
    reset_block = block(
        "if (!event.target.closest('[data-route-map-reset]')) return;", "\n  });"
    )
    assert "invalidateRoutePreview();" in reset_block

    endpoint_change_block = block(
        "if (!event.target.matches('select[name=\"start_place\"], select[name=\"end_place\"]')) return;",
        "\n  });",
    )
    assert "invalidateRoutePreview();" in endpoint_change_block
    assert endpoint_change_block.index("invalidateRoutePreview();") < endpoint_change_block.index(
        "maybePreviewRoute();"
    )

    # maybePreviewRoute's three failure branches: transport error, a non-OK
    # HTTP response, and a well-formed but `ok: false` JSON body.
    transport_error_block = block("} catch (err) {", "\n      return;\n    }")
    http_error_block = block("if (!resp.ok) {", "\n      return;\n    }")
    ok_false_block = block("if (!data.ok) {", "\n      return;\n    }")
    for failure_block in (transport_error_block, http_error_block, ok_false_block):
        assert "clearDistanceIfPreviewOwned();" in failure_block
        assert failure_block.index("clearDistanceIfPreviewOwned();") < failure_block.index(
            "setRouteFieldValue('routed_distance', '');"
        )

    # Exactly the definition, the shared invalidation call, and the three
    # failure calls -- no orphaned invalidation path was left unwired.
    assert body.count("clearDistanceIfPreviewOwned()") == 5


def test_route_mode_switch_disables_rather_than_clears_label_inputs():
    """Location names only ever apply to route_mode "none". Unlike the
    routing coordinate/place fields, a typed label must survive switching to
    another mode and back, so the mode-switch handler disables (never
    clears) the label inputs while another mode is active -- a disabled
    input is what actually keeps its value from riding along in that mode's
    own submission.
    """
    body = _render_manual(places=PLACES)

    assert "const LABEL_FIELDS = ['start_label', 'end_label'];" in body

    assert "function setLabelFieldsEnabled(enabled) {" in body
    helper = body.split("function setLabelFieldsEnabled(enabled) {", 1)[1].split(
        "\n  }\n", 1
    )[0]
    assert "field.disabled = !enabled;" in helper
    assert "field.value" not in helper

    panels = body.split("function updateRoutePanels(mode) {", 1)[1].split(
        "\n  }\n", 1
    )[0]
    assert "setLabelFieldsEnabled(mode === 'none');" in panels

    # clearRouteEndpoints() is the routing-fields-only reset that runs on
    # every mode change; the label fields must never appear in it, or a
    # typed value would be wiped out by an ordinary mode change instead of
    # merely being disabled.
    endpoints_block = body.split("function clearRouteEndpoints() {", 1)[1].split(
        "\n  }\n", 1
    )[0]
    assert "start_label" not in endpoints_block
    assert "end_label" not in endpoints_block
    assert "LABEL_FIELDS" not in endpoints_block


def test_failed_route_preview_clears_the_stale_drawn_route():
    """A failed preview (transport error, non-OK response, or a well-formed
    `ok: false` body) leaves behind a route line that belonged to a
    selection the server just rejected, so it must not stay on the map.
    Named-places markers came from that same rejected response and go with
    the line; map-picked markers are the user's own click points and must
    survive, since removing them would silently discard input the user
    would otherwise have to redo.
    """
    body = _render_manual(places=PLACES)

    assert "function clearStaleRouteLine() {" in body
    helper = body.split("function clearStaleRouteLine() {", 1)[1].split("\n  }\n", 1)[0]
    assert "if (routeLine) { routeMap.removeLayer(routeLine); routeLine = null; }" in helper
    # The line always goes; markers only go with it, and only in
    # named-places mode.
    line_removal = helper.index("routeLine = null")
    mode_gate = helper.index("currentRouteMode() === 'places'")
    assert line_removal < mode_gate
    marker_removal = helper[mode_gate:]
    assert "routeStartMarker = null" in marker_removal
    assert "routeEndMarker = null" in marker_removal

    fn_body = body.split("async function maybePreviewRoute() {", 1)[1].split(
        "\n    drawRoutePreview(data);\n  }", 1
    )[0]
    # Called from exactly the three failure branches, never from the
    # success path that ends in drawRoutePreview(data).
    assert fn_body.count("clearStaleRouteLine();") == 3

    guard = "if (seq !== routePreviewSeq) return;"
    for segment in fn_body.split(guard)[1:]:
        # Each guard belongs to one failure branch. A superseded response
        # returns immediately on the guard's own `return`, before this
        # branch (and its call to clearStaleRouteLine) is ever reached, so
        # finding the call inside the branch that follows its guard is
        # exactly the "runs only after the sequence check" property.
        next_guard = segment.find("if (seq")
        branch = segment if next_guard == -1 else segment[:next_guard]
        assert "clearStaleRouteLine();" in branch


def test_route_unavailable_notice_renders_only_when_flagged():
    with_notice = _render_index(notice="route_unavailable")
    assert 'role="status"' in with_notice.split('class="route-saved-notice"')[1][:200]
    assert "automatic routing was unavailable" in with_notice
    assert "OSRM" not in with_notice
    assert with_notice.index('class="route-saved-notice"') < with_notice.index(
        'class="trip-archive-header"'
    )

    without_notice = _render_index(notice="")
    assert "route-saved-notice" not in without_notice
    assert "automatic routing was unavailable" not in without_notice


def test_leaflet_assets_are_linked_in_head():
    body = _render_index()

    assert '<link rel="stylesheet" href="/static/vendor/leaflet/leaflet.css">' not in body
    assert '<script src="/static/vendor/leaflet/leaflet.js"></script>' not in body
    manual = _render_manual()
    assert '<link rel="stylesheet" href="/static/vendor/leaflet/leaflet.css">' in manual
    assert '<script src="/static/vendor/leaflet/leaflet.js"></script>' in manual


def test_every_script_element_carries_the_nonce_or_is_a_same_origin_asset():
    # Same discipline test_security_headers.py checks live-end-to-end: an
    # inline <script> without this page's nonce would be blocked by CSP, so
    # no fragment (or careless addition) may introduce a second bare one. A
    # `src="..."` script is exempt because CSP's `'self'` already covers a
    # same-origin vendored asset regardless of nonce.
    body = _render_index(csp_nonce="test-nonce-xyz")

    tags = re.findall(r"<script\b[^>]*>", body)
    assert tags, "expected at least one <script> tag"
    for tag in tags:
        assert "src=" in tag or 'nonce="test-nonce-xyz"' in tag, tag

    # Exactly the known set of inline script blocks: base.html's five plus
    # trips.html's archive-controller and selection blocks.
    assert body.count('<script nonce="test-nonce-xyz">') == 7

    manual = _render_manual(csp_nonce="test-nonce-xyz")
    manual_tags = re.findall(r"<script\b[^>]*>", manual)
    for tag in manual_tags:
        assert "src=" in tag or 'nonce="test-nonce-xyz"' in tag, tag
    assert manual.count('<script nonce="test-nonce-xyz">') == 6


def test_route_picker_map_click_normalizes_longitude_before_use():
    """The route picker map opens on a world view, which Leaflet renders as
    several horizontally repeated copies of the world. A click on a repeated
    copy reports a raw, unnormalized longitude (for example 241.171875)
    outside [-180, 180], which the server's coordinate validation correctly
    rejects. This asserts the click handler normalizes the coordinate with
    Leaflet's own `wrap()` before it is ever formatted, stored in the hidden
    inputs, or used to place a marker -- not merely that "wrap" appears
    somewhere in the page.
    """
    body = _render_manual(places=PLACES)

    assert "function onRoutePickerMapClick(event) {" in body
    handler = body.split("function onRoutePickerMapClick(event) {", 1)[1].split(
        "\n  }\n", 1
    )[0]

    assert "event.latlng.wrap()" in handler
    wrap_index = handler.index("event.latlng.wrap()")

    # Nothing may read the raw event.latlng after the wrapped value is
    # produced; every subsequent use (formatting, markers, hidden fields)
    # must go through the normalized latlng.
    assert "event.latlng" not in handler[wrap_index + len("event.latlng.wrap()"):]

    # The formatted lat/lon fed to the hidden inputs, and the LatLng handed
    # to L.marker for on-map placement, both have to come from the same
    # normalized value, or the marker would sit somewhere other than what
    # the stored coordinates say.
    assert handler.count("latlng.lat.toFixed(6)") == 1
    assert handler.count("latlng.lng.toFixed(6)") == 1
    assert handler.count("L.marker(latlng,") == 2
    assert wrap_index < handler.index("latlng.lat.toFixed(6)")
    assert wrap_index < handler.index("L.marker(latlng,")
