"""Page-level button rules: every button is a .control with a variant and
nothing uses hx-confirm.

Every content page (a template using the shared .page wrapper) is registered
in exactly one of FOLLOWS_RULES or NOT_YET_MIGRATED. Pages are checked on their
template source plus the partials they include, since most need
database-shaped context to render; Account Settings and Settings are also
checked rendered.
A page listed in NOT_YET_MIGRATED must still break the rules; once it is clean
the guard below fails until the entry moves, so the list can only shrink.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.main import make_templates

TEMPLATES = Path(__file__).parents[1] / "app" / "templates"
TZ = ZoneInfo("UTC")

FOLLOWS_RULES = {
    "account_security.html",
    "admin_accounts.html",
    "expenses.html",
    "report.html",
    "report_range.html",
    "review.html",
    "settings.html",
    "stats.html",
    "tracking.html",
    "trip.html",
}

# Page -> the plan 0074 slice that converts its buttons, or "unassigned".
NOT_YET_MIGRATED: dict[str, str] = {}

VARIANTS = {"control-primary", "control-secondary", "control-destructive"}

# Buttons that are legitimately not .control. Both live in the shared header
# shell (base.html) that every rendered page includes.
COMPONENT_BUTTON_CLASSES = {
    "header-action",  # header log-out icon button
    "mobile-account-logout-button",  # log-out row in the mobile account menu
}


class _ButtonParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.buttons: list[dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "button":
            self.buttons.append({k: v or "" for k, v in attrs})


def _bad_buttons(buttons: list[dict[str, str]]) -> list[dict[str, str]]:
    bad = []
    for attrs in buttons:
        classes = set(attrs.get("class", "").split())
        if classes & COMPONENT_BUTTON_CLASSES:
            continue
        if "control" in classes and classes & VARIANTS:
            continue
        bad.append(attrs)
    return bad


def _templates():
    return make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))


def _render_account_security(**overrides):
    context = dict(
        user={"name": "admin", "is_admin": True, "has_avatar": True, "avatar_version": 1},
        csrf="token",
        account_email="admin@example.com",
        account_email_verified=False,
        email_challenge_available=True,
        oidc_configured=False,
        linked_identity=None,
        avatar_max_label="500 KB",
        has_password=True,
        can_sign_out_everywhere=True,
        method_notice=None,
        error=None,
        success=None,
    )
    context.update(overrides)
    return _templates().env.get_template("account_security.html").render(**context)


_SIGNED_IN_APPS = [
    SimpleNamespace(public_id="A" * 22, device_label="Phone",
                    created_at=datetime(2026, 10, 1, tzinfo=TZ), last_used_at=None),
    SimpleNamespace(public_id="B" * 22, device_label="Tablet",
                    created_at=datetime(2026, 10, 2, tzinfo=TZ),
                    last_used_at=datetime(2026, 10, 3, tzinfo=TZ)),
]


def _account_security_variants():
    linked = {"email": "a@example.com", "display_name": "A"}
    return {
        "avatar, password, no provider": _render_account_security(),
        "no avatar": _render_account_security(
            user={"name": "admin", "is_admin": True, "has_avatar": False, "avatar_version": 0}
        ),
        "provider linked": _render_account_security(oidc_configured=True, linked_identity=linked),
        "provider available": _render_account_security(oidc_configured=True),
        "no password": _render_account_security(has_password=False, oidc_configured=True),
        "verified email": _render_account_security(account_email_verified=True),
        "signed-in apps": _render_account_security(signed_in_apps=_SIGNED_IN_APPS),
    }


# Rendered pages that follow the rules today.
RENDERED_PAGES = {
    "account_security.html": _account_security_variants,
    "settings.html": lambda: {"every row kind": _render_settings()},
}


@pytest.mark.parametrize("page", sorted(RENDERED_PAGES))
def test_rendered_page_buttons_are_controls_and_do_not_use_hx_confirm(page):
    for label, body in RENDERED_PAGES[page]().items():
        parser = _ButtonParser()
        parser.feed(body)
        assert parser.buttons, f"{page} ({label}) rendered no buttons"
        assert _bad_buttons(parser.buttons) == [], f"{page} ({label})"
        assert "hx-confirm" not in body, f"{page} ({label})"


def test_account_security_covers_the_avatar_remove_dialog_buttons():
    parser = _ButtonParser()
    parser.feed(_render_account_security())
    cancel = [b for b in parser.buttons if "data-avatar-remove-cancel" in b]
    assert len(cancel) == 1
    assert "control" in cancel[0]["class"].split()


def test_signed_in_app_revoke_controls_are_destructive_dialog_buttons():
    parser = _ButtonParser()
    parser.feed(_render_account_security(signed_in_apps=_SIGNED_IN_APPS))
    openers = [b for b in parser.buttons if b.get("data-dialog-open", "").startswith("signed-in-app-revoke-")]
    cancels = [b for b in parser.buttons if "data-dialog-cancel" in b]
    confirms = [b for b in parser.buttons if b.get("type") == "submit"
                and "control-destructive" in b.get("class", "").split()]
    assert len(openers) == 2 and all("control-destructive" in b["class"].split() for b in openers)
    assert len(cancels) == 2 and all("control-secondary" in b["class"].split() for b in cancels)
    assert len(confirms) >= 2


def _source_with_includes(name: str, seen: set[str] | None = None) -> str:
    seen = set() if seen is None else seen
    if name in seen:
        return ""
    seen.add(name)
    source = (TEMPLATES / name).read_text()
    parts = [source]
    for included in re.findall(r"""\{%-?\s*(?:include|from|import)\s+["']([^"']+)["']""", source):
        if (TEMPLATES / included).is_file():
            parts.append(_source_with_includes(included, seen))
    return "\n".join(parts)


def _source_violations(name: str) -> list[str]:
    source = _source_with_includes(name)
    parser = _ButtonParser()
    parser.feed(source)
    found = [f"button without control: {attrs}" for attrs in _bad_buttons(parser.buttons)]
    if "hx-confirm" in source:
        found.append("hx-confirm")
    return found


def test_every_content_page_is_registered_once():
    pages = {
        path.name for path in TEMPLATES.glob("*.html")
        if re.search(r'class="page[ "]', path.read_text())
    }
    assert not FOLLOWS_RULES & set(NOT_YET_MIGRATED)
    assert pages == FOLLOWS_RULES | set(NOT_YET_MIGRATED)


@pytest.mark.parametrize("page", sorted(FOLLOWS_RULES))
def test_page_source_follows_the_button_rules(page):
    assert _source_violations(page) == []


@pytest.mark.parametrize("page", sorted(NOT_YET_MIGRATED))
def test_not_yet_migrated_pages_still_break_the_rules(page):
    assert (TEMPLATES / page).is_file()
    assert _source_violations(page), (
        f"{page} follows the button rules now; move it from NOT_YET_MIGRATED "
        "to FOLLOWS_RULES"
    )


@pytest.mark.parametrize("page, label", [("report.html", "View range"), ("stats.html", "Filter")])
def test_report_and_stats_filter_submit_is_the_primary_control(page, label):
    source = _source_with_includes(page)
    parser = _ButtonParser()
    parser.feed(source)
    assert parser.buttons, f"{page} has no buttons to check"
    assert f'<button class="control control-primary" type="submit">{label}</button>' in source


def test_expenses_commits_with_primary_controls_and_deletes_with_a_destructive_one():
    source = _source_with_includes("expenses.html")
    parser = _ButtonParser()
    parser.feed(source)
    by_label = {}
    for label in ("Apply", "Add", "Save"):
        match = re.search(rf'<button class="([^"]*)" type="submit">{label}</button>', source)
        assert match, f"expenses.html has no {label} submit button"
        by_label[label] = set(match.group(1).split())
    assert all(classes == {"control", "control-primary"} for classes in by_label.values()), by_label

    destructive = [b for b in parser.buttons if "control-destructive" in b.get("class", "").split()]
    triggers = [b for b in destructive if "data-expense-delete-open" in b]
    confirms = [b for b in destructive if b.get("hx-post") == "/expenses/{{ expense.id }}/delete"]
    assert len(triggers) == 1 and triggers[0]["aria-haspopup"] == "dialog"
    assert len(confirms) == 1
    cancels = [b for b in parser.buttons if "data-expense-delete-cancel" in b]
    assert len(cancels) == 1 and "control-secondary" in cancels[0]["class"].split()


def _buttons(page: str) -> list[dict[str, str]]:
    parser = _ButtonParser()
    parser.feed(_source_with_includes(page))
    return parser.buttons


def _labelled(page: str) -> dict[str, set[str]]:
    source = _source_with_includes(page)
    return {
        label.strip(): set(classes.split())
        for classes, label in re.findall(r'<button[^>]*class="([^"]*)"[^>]*>([^<{]+)</button>', source)
    }


def test_settings_saves_with_primary_controls_and_confirms_destructive_actions_in_dialogs():
    labels = _labelled("settings.html")
    for label in ("Save time zone", "Save notifications"):
        assert labels[label] == {"control", "control-primary"}, label

    parser = _ButtonParser()
    parser.feed(_render_settings())
    triggers = [b for b in parser.buttons if "data-dialog-open" in b]
    cancels = [b for b in parser.buttons if "data-dialog-cancel" in b]
    # Place, rule, restore, undo, vehicle deactivate and odometer delete.
    assert len(triggers) == 6
    assert len(cancels) == 6
    for trigger in triggers:
        assert set(trigger["class"].split()) == {"control", "control-destructive"}
        assert trigger["aria-haspopup"] == "dialog"
        assert trigger["aria-controls"] == trigger["data-dialog-open"]
        assert "hx-post" not in trigger
    confirms = [b for b in parser.buttons if "hx-post" in b and "control-destructive" in b["class"].split()]
    assert sorted(b["hx-post"] for b in confirms) == [
        "/places/3/delete", "/rules/4/delete",
        "/settings/boundary_overrides/5/delete", "/settings/boundary_overrides/6/delete",
        "/settings/odometer/8/delete", "/settings/vehicles/7/deactivate",
    ]


def _render_settings():
    when = datetime(2026, 7, 1, 9, tzinfo=TZ)
    vehicle = {"id": 7, "name": "Truck", "make": None, "model": None, "plate": None,
               "active": True, "is_default": False}
    return _templates().env.get_template("settings.html").render(
        user={"id": 1, "name": "admin", "is_admin": True}, csrf="token",
        preferences=SimpleNamespace(
            display_tz="UTC", ntfy_topic="", email_to="", odometer_reminder_requested=False,
            email_weekly_nudge=False, email_monthly_summary=False, email_filing_reminder=False,
            email_odometer_reminder=False, nudge_weekly_hour=18, odometer_reminder_hour=9,
            email_digest_hour=9, email_filing_reminder_mmdd="01-15",
        ),
        rates=[], vehicles=[vehicle],
        odometer=[{"vehicle": vehicle, "intervals": [],
                   "readings": [{"id": 8, "recorded_at": when, "odometer_m": 1609.344, "note": None}]}],
        places=[{"id": 3, "name": "Office", "kind": "work", "lat": 1.0, "lon": 2.0, "radius_m": 150}],
        rules=[{"id": 4, "a_desc": "Home", "b_desc": "Work", "category": "personal"}],
        boundary_overrides=[
            {"id": 5, "device": "phone", "kind": "discard", "range_start": when,
             "range_end": when, "point_id": None, "created_at": when},
            {"id": 6, "device": "phone", "kind": "suppress", "range_start": when,
             "range_end": when, "point_id": None, "created_at": when},
        ],
        device_fixes=[], geocode_enabled=False,
    )


def test_tracking_buttons_use_shared_controls():
    labels = _labelled("tracking.html")
    assert labels["Create device"] == {"control", "control-primary"}
    for label in ("Revoke access", "Revoke shared login"):
        assert labels[label] == {"control", "control-destructive"}, label
    for label in ("Copy", "Replace password", "Create replacement password",
                  "Give this device its own password"):
        assert labels[label] == {"control", "control-secondary"}, label
    assert len(_buttons("tracking.html")) == 11


def test_admin_accounts_buttons_use_shared_controls():
    labels = _labelled("admin_accounts.html")
    for label in ("Create invitation", "Save storage limit"):
        assert labels[label] == {"control", "control-primary"}, label
    for label in ("Disable", "Revoke", "Permanently purge account",
                  "Disable account and start the 30-day deletion window"):
        assert labels[label] == {"control", "control-destructive"}, label
    for label in ("Copy link", "Copy token", "Re-enable", "Queue password recovery",
                  "Resend with a new token", "Cancel deletion and re-enable",
                  "Reauthenticate with OIDC for this purge"):
        assert labels[label] == {"control", "control-secondary"}, label
    assert len(_buttons("admin_accounts.html")) == 13
