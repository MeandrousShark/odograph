"""Page-level button rules: every button is a .control with a variant and
nothing uses hx-confirm.

Every content page (a template using the shared .page wrapper) is registered
in exactly one of FOLLOWS_RULES or NOT_YET_MIGRATED. Pages are checked on their
template source plus the partials they include, since most need
database-shaped context to render; Account Settings is also checked rendered.
A page listed in NOT_YET_MIGRATED must still break the rules; once it is clean
the guard below fails until the entry moves, so the list can only shrink.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.main import make_templates

TEMPLATES = Path(__file__).parents[1] / "app" / "templates"
TZ = ZoneInfo("UTC")

FOLLOWS_RULES = {
    "account_security.html",
    "expenses.html",
    "report.html",
    "report_range.html",
    "review.html",
    "stats.html",
}

# Page -> the plan 0074 slice that converts its buttons, or "unassigned".
NOT_YET_MIGRATED = {
    "settings.html": "C5",
    "tracking.html": "unassigned",
    "admin_accounts.html": "unassigned",
    "trip.html": "unassigned",
}

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
    }


# Rendered pages that follow the rules today.
RENDERED_PAGES = {
    "account_security.html": _account_security_variants,
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
