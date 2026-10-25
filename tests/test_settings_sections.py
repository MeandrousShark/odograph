"""Settings topic sections, section nav, split preference forms and the
in-page confirmation dialogs that replaced hx-confirm."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.main import make_templates

ROOT = Path(__file__).parents[1]
TZ = ZoneInfo("UTC")
WHEN = datetime(2026, 7, 1, 9, tzinfo=TZ)
VEHICLE = {"id": 7, "name": "Truck", "make": None, "model": None, "plate": None,
           "active": True, "is_default": False}
ODOMETER = [{"vehicle": VEHICLE, "intervals": [],
             "readings": [{"id": 8, "recorded_at": WHEN, "odometer_m": 1609.344, "note": None}]}]
PREFERENCES = SimpleNamespace(
    display_tz="America/Chicago", ntfy_topic="alerts", email_to="a@example.test",
    odometer_reminder_requested=True, email_weekly_nudge=True, email_monthly_summary=False,
    email_filing_reminder=False, email_odometer_reminder=False, nudge_weekly_hour=18,
    odometer_reminder_hour=9, email_digest_hour=9, email_filing_reminder_mmdd="01-15",
)
STORAGE = {
    "actual_bytes": 512, "reserved_bytes": 256, "total_bytes": 768,
    "raw_bytes": 128, "enhancement_bytes": 64,
    "account_limit_bytes": 1024, "raw_limit_bytes": 256, "enhancement_limit_bytes": 128,
    "warning": False, "account_blocked": False, "raw_blocked": False, "enhancement_paused": False,
}
ALL_SECTIONS = [
    ("account", "Account and storage"),
    ("preferences", "Preferences"),
    ("notifications", "Notifications"),
    ("vehicles-and-rates", "Vehicles and rates"),
    ("places-and-rules", "Places and rules"),
    ("data", "Data"),
    ("system", "System"),
]


def _templates():
    return make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))


def _render(**overrides):
    context = dict(
        user={"id": 1, "name": "admin", "is_admin": True}, csrf="token",
        preferences=PREFERENCES, storage=STORAGE,
        rates=[], vehicles=[VEHICLE], odometer=ODOMETER,
        places=[{"id": 3, "name": "Office", "kind": "work", "lat": 1.0, "lon": 2.0, "radius_m": 150}],
        rules=[{"id": 4, "a_desc": "Home", "b_desc": "Work", "category": "personal"}],
        boundary_overrides=[
            {"id": 5, "device": "phone", "kind": "discard", "range_start": WHEN,
             "range_end": WHEN, "point_id": None, "created_at": WHEN},
            {"id": 6, "device": "phone", "kind": "suppress", "range_start": WHEN,
             "range_end": WHEN, "point_id": None, "created_at": WHEN},
        ],
        device_fixes=[], geocode_enabled=False,
        diagnostics={"app_version": "test", "git_revision": "test",
                     "schema_version": 1, "detector_version": 1},
    )
    context.update(overrides)
    return _templates().env.get_template("settings.html").render(**context)


def _sections(body):
    return re.findall(
        r'<section class="settings-section[^"]*" id="([a-z-]+)" aria-labelledby="([a-z-]+)">\s*'
        r'<h2 id="([a-z-]+)">([^<]+)</h2>',
        body,
    )


def _nav(body):
    nav = body.split('<nav class="settings-nav" aria-label="Settings sections">', 1)[1].split("</nav>", 1)[0]
    menu = nav.split('<details class="settings-nav-menu">', 1)[1].split("</details>", 1)[0]
    side = nav.split('<ul class="settings-nav-list">', 1)[1].split("</ul>", 1)[0]
    link = r'<li><a href="#([a-z-]+)">([^<]+)</a></li>'
    return re.findall(link, menu), re.findall(link, side), menu


def _section(body, section_id):
    start = body.index(f'id="{section_id}" aria-labelledby=')
    end = body.find('<section class="settings-section', start)
    return body[start:end if end != -1 else len(body)]


def _dialog(body, dialog_id):
    return body.split(f'<dialog id="{dialog_id}" class="app-dialog"', 1)[1].split("</dialog>", 1)[0]


def test_settings_topic_sections_render_in_order_with_labelled_h2_anchors():
    sections = _sections(_render())
    assert [(sid, heading) for sid, _, _, heading in sections] == ALL_SECTIONS
    for section_id, labelled_by, heading_id, _ in sections:
        assert labelled_by == heading_id == f"{section_id}-section-heading"


def test_section_nav_links_match_the_rendered_sections_in_every_variant():
    variants = {
        "admin": _render(),
        "member without storage": _render(
            user={"id": 2, "name": "member", "is_admin": False}, storage=None,
        ),
    }
    # `preferences is defined` is the template's guard, so omit it entirely.
    context_without_preferences = dict(
        user={"id": 2, "name": "member", "is_admin": False}, csrf="token",
        rates=[], vehicles=[], odometer=[], places=[], rules=[], boundary_overrides=[],
        device_fixes=[], geocode_enabled=False,
    )
    variants["no preferences"] = _templates().env.get_template("settings.html").render(
        **context_without_preferences
    )
    for label, body in variants.items():
        rendered = [(sid, heading) for sid, _, _, heading in _sections(body)]
        menu, side, _ = _nav(body)
        assert menu == side == rendered, label
        for section_id, _ in rendered:
            assert body.count(f'id="{section_id}"') == 1, (label, section_id)
    assert ("notifications", "Notifications") not in _nav(variants["no preferences"])[0]
    assert len(_nav(variants["no preferences"])[0]) == 6


def test_phone_jump_menu_is_a_closed_disclosure_of_plain_links():
    body = _render()
    _, _, menu = _nav(body)
    assert "<summary>Jump to section</summary>" in menu
    assert "<select" not in menu and "<script" not in menu
    opening = body.split('<details class="settings-nav-menu"', 1)[1].split(">", 1)[0]
    assert "open" not in opening

    css = (ROOT / "static/style.css").read_text()
    # The side list needs the body at its 1100px cap so the editable tables
    # still fit beside it; narrower windows get the jump menu.
    assert "body {" in css and "max-width: 1100px; margin: 0 auto;" in css
    desktop = css.split("@media (min-width: 1101px) {\n  .settings-layout", 1)[1].split("\n}\n", 1)[0]
    assert ".settings-nav { position: sticky;" in desktop
    assert ".settings-nav-menu { display: none; }" in desktop
    narrow = css.split("@media (max-width: 1100px) {\n", 1)[1].split("\n}\n", 1)[0]
    assert narrow.strip() == ".settings-nav-list { display: none; }"


def test_settings_headings_step_down_from_topic_to_item_to_h4():
    css = (ROOT / "static/style.css").read_text()
    assert ".settings-page .settings-section > h2 { margin: 0 0 var(--space-5); font-size: 1.15rem; }" in css
    assert (
        ".settings-page .settings-subsection > h3,\n"
        ".settings-page .section-disclosure > summary > h3 { font-size: 1rem; font-weight: 600; }"
    ) in css
    assert ".settings-page .settings-subsection h4 { margin: var(--space-4) 0 var(--space-2); font-size: .9rem; font-weight: 600; }" in css


def test_editable_settings_tables_fit_the_desktop_column_beside_the_nav():
    # Body 1100px less 2 x 1.5rem padding, a 12rem nav, a 1.25rem gap and the
    # card's 2 x 1.25rem padding and borders leave about 798px (49.8rem).
    css = (ROOT / "static/style.css").read_text()
    for section in ("rates", "vehicles", "odometer-readings", "odometer-intervals",
                    "places", "rules", "overrides"):
        rule = css.split(f".settings-table-scroll-{section} .settings-table {{ min-width: ", 1)[1]
        assert float(rule.split("rem", 1)[0]) <= 49, section


def test_inbound_anchors_keep_their_targets():
    body = _render()
    # base.html and trip.html link to /settings#storage-allowance.
    assert body.count('id="storage-allowance"') == 1
    assert body.count('id="manual-trip-edits"') == 1
    assert body.count('id="diagnostics"') == 1
    assert 'id="storage-allowance"' in _section(body, "account")
    assert 'id="manual-trip-edits"' in _section(body, "places-and-rules")
    assert 'id="diagnostics"' in _section(body, "system")


def test_subsections_sit_inside_their_topic_card():
    body = _render()
    expected = {
        "account": ["settings-account-section", 'id="storage-allowance"'],
        "preferences": ["settings-timezone", "settings-appearance"],
        "vehicles-and-rates": ["settings-rates", "settings-vehicles", "settings-odometer"],
        "places-and-rules": ["settings-places", "settings-rules", 'id="manual-trip-edits"'],
        "system": ["settings-diagnostics", "settings-device-status"],
    }
    for section_id, markers in expected.items():
        section = _section(body, section_id)
        positions = [section.index(marker) for marker in markers]
        assert positions == sorted(positions), section_id
    # Seven topic cards and no other card: subsections use their own class.
    assert body.count('<section class="settings-section') == len(ALL_SECTIONS)
    assert body.count('<section class="settings-subsection') == 11
    assert "<h3>Mileage rates</h3>" not in body
    assert '<h3 id="rates-heading">Mileage rates</h3>' in body
    assert "<h4>Truck</h4>" in _section(body, "vehicles-and-rates")


def test_tracking_link_moved_into_the_account_section():
    tracking = '<p><a href="/settings/tracking">Tracking devices and credentials</a></p>'
    body = _render()
    assert body.count('href="/settings/tracking"') == 1
    account = body.split('<section class="settings-subsection settings-account-section"', 1)[1].split("</section>", 1)[0]
    assert tracking in account
    assert account.index('href="/settings/account"') < account.index(tracking)

    # Without an account id the Account subsection is omitted, but the link
    # stays in the Account and storage section.
    anonymous = _render(user={"name": "dev"})
    assert "settings-account-section" not in anonymous
    assert tracking in _section(anonymous, "account")


def _form(body, action):
    start = body.index(f'hx-post="{action}"')
    start = body.rindex("<form", 0, start)
    return body[start:body.index("</form>", start)]


def test_time_zone_and_notifications_are_separate_forms_posting_only_their_fields():
    body = _render()
    assert 'hx-post="/settings/preferences"' not in body

    timezone = _form(body, "/settings/preferences/timezone")
    assert 'hx-swap="none"' in timezone
    assert re.findall(r'name="([a-z_]+)"', timezone) == ["display_timezone"]
    assert 'value="America/Chicago"' in timezone
    assert timezone.count("<button") == 1
    assert '<button type="submit" class="control control-primary">Save time zone</button>' in timezone
    assert "/settings/preferences/timezone" in _section(body, "preferences")

    notifications = _form(body, "/settings/preferences/notifications")
    assert 'hx-swap="none"' in notifications
    assert set(re.findall(r'name="([a-z_]+)"', notifications)) == {
        "ntfy_topic", "email_to", "nudge_weekly_hour", "odometer_reminder_requested",
        "odometer_reminder_hour", "email_weekly_nudge", "email_monthly_summary",
        "email_filing_reminder", "email_odometer_reminder", "email_digest_hour",
        "email_filing_reminder_mmdd",
    }
    assert notifications.count("<button") == 1
    assert '<button type="submit" class="control control-primary">Save notifications</button>' in notifications
    assert "/settings/preferences/notifications" in _section(body, "notifications")
    assert 'name="ntfy_topic" value="alerts"' in notifications
    assert 'name="odometer_reminder_requested" value="1" checked' in notifications
    assert 'name="email_monthly_summary" value="1">' in notifications


def test_every_settings_confirmation_is_an_in_page_dialog_with_its_original_request():
    body = _render()
    assert "hx-confirm" not in body
    cases = {
        "place-delete-3": ("Delete place &#39;Office&#39;?", "Trips referencing it will show coordinates instead.",
                           "Delete place", "/places/3/delete", None),
        "rule-delete-4": ("Delete this rule?", "Side A: Home. Side B: Work. Category: Personal.",
                          "Delete rule", "/rules/4/delete", None),
        "boundary-override-confirm-5": ("Restore this deleted trip?", "Its stored location data will be reprocessed.",
                                        "Restore trip", "/settings/boundary_overrides/5/delete", None),
        "boundary-override-confirm-6": ("Undo this merge/split?", "The affected trips will be re-detected.",
                                        "Undo edit", "/settings/boundary_overrides/6/delete", None),
        "vehicle-deactivate-7": ("Deactivate &#39;Truck&#39;?",
                                 "It stays on trips already using it, but drops out of the picker for new trips.",
                                 "Deactivate vehicle", "/settings/vehicles/7/deactivate", "#vehicles-table"),
        "odometer-delete-8": ("Delete this reading?", "Add a corrected one instead of editing.",
                              "Delete reading", "/settings/odometer/8/delete", "#odometer-table"),
    }
    for dialog_id, (title, description, confirm, action, target) in cases.items():
        assert body.count(f'data-dialog-open="{dialog_id}"') == 1, dialog_id
        dialog = _dialog(body, dialog_id)
        assert f'aria-labelledby="{dialog_id}-title"' in dialog
        assert f'<h2 id="{dialog_id}-title">{title}</h2>' in dialog
        assert f'<p id="{dialog_id}-description">{description}</p>' in dialog
        assert '<button type="button" class="control control-secondary" data-dialog-cancel autofocus>Cancel</button>' in dialog
        swap = f' hx-target="{target}" hx-swap="outerHTML"' if target else ""
        assert (
            f'<button type="button" class="control control-destructive" hx-post="{action}"{swap}>{confirm}</button>'
        ) in dialog, dialog_id
        if not target:
            assert "hx-target" not in dialog and "hx-swap" not in dialog
        # Only the confirm button sends the request; the trigger just opens.
        assert body.count(f'hx-post="{action}"') == 1, dialog_id
    # Restore and undo keep their distinct trigger titles and labels.
    assert 'title="Restore deleted trip"' in body and 'title="Undo this override"' in body


def test_vehicles_and_odometer_partials_carry_their_dialogs_for_htmx_swaps():
    templates = _templates()
    vehicles = templates.env.get_template("_vehicles_table.html").render(vehicles=[VEHICLE])
    odometer = templates.env.get_template("_odometer_table.html").render(
        vehicles=[VEHICLE], odometer=ODOMETER,
    )
    for body, dialog_id, action, target in (
        (vehicles, "vehicle-deactivate-7", "/settings/vehicles/7/deactivate", "#vehicles-table"),
        (odometer, "odometer-delete-8", "/settings/odometer/8/delete", "#odometer-table"),
    ):
        assert "hx-confirm" not in body
        assert 'class="danger"' not in body
        # The swap target wraps the dialog, so a swap replaces it with the row.
        assert body.lstrip().startswith(f'<div id="{target[1:]}">')
        dialog = _dialog(body, dialog_id)
        assert f'hx-post="{action}" hx-target="{target}" hx-swap="outerHTML"' in dialog
        assert body.count(f'data-dialog-open="{dialog_id}"') == 1


def test_base_dialog_handler_opens_and_cancels_generic_dialogs():
    source = (ROOT / "app/templates/base.html").read_text()
    handler = source.split("// Destructive actions use in-page native dialogs", 1)[1].split("</script>", 1)[0]
    assert "[data-dialog-open]" in handler
    assert "trigger.dataset.dialogOpen" in handler
    assert "[data-dialog-cancel]" in handler
    # The existing trip, avatar and expense attributes keep working.
    for name in ("trip-delete", "avatar-remove", "expense-delete"):
        assert f"[data-{name}-open]" in handler and f"[data-{name}-cancel]" in handler
    css = (ROOT / "static/style.css").read_text()
    assert ".danger {" not in css
    assert ".settings-table-actions .app-dialog { text-align: left; }" in css
