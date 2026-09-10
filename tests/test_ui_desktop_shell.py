from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from app.main import make_templates


ROOT = Path(__file__).parents[1]
BASE = (ROOT / "app/templates/base.html").read_text()
CSS = (ROOT / "static/style.css").read_text()


USER = {
    "id": 7, "name": "Test User", "email": "test@example.com", "is_admin": True,
    "has_avatar": False, "avatar_version": 0,
}


def _render(
    path: str = "/", user: dict | None = USER, request: object | None = None
) -> str:
    templates = make_templates(SimpleNamespace(display_tz=None, app_version="test"))
    if request is None:
        request = SimpleNamespace(url=SimpleNamespace(path=path))
    return templates.env.get_template("base.html").render(
        request=request,
        user=user,
        csrf="test-csrf",
        csp_nonce="test-nonce",
        static_version="v1",
    )


def _nav(body: str) -> str:
    return body.split('<nav aria-label="Primary navigation">', 1)[1].split("</nav>", 1)[0]


def test_authenticated_primary_navigation_has_one_route_aware_active_link():
    expected = {
        "/": "Dashboard",
        "/trips": "Trips",
        "/trips/42": "Trips",
        "/review": "Review",
        "/review/card": "Review",
        "/report": "Report",
        "/report/2026": "Report",
        "/expenses": "Expenses",
        "/expenses/ledger": "Expenses",
        "/stats": "Stats",
        "/stats/coverage": "Stats",
    }
    for path, label in expected.items():
        links = _nav(_render(path))
        active = re.findall(r'<a href="([^"]+)" aria-current="page">([^<]+)</a>', links)
        assert len(active) == 1
        assert active[0][1] == label

    for path in ("/settings", "/settings/account", "/trips-archive"):
        assert 'aria-current="page"' not in _nav(_render(path))


def test_authenticated_shell_tolerates_request_without_url_or_path():
    for request in (SimpleNamespace(), SimpleNamespace(url=SimpleNamespace())):
        body = _render(request=request)
        assert 'aria-current="page"' not in _nav(body)


def test_authenticated_shell_keeps_account_identity_and_conditional_account_security():
    body = _render("/settings")
    account = body.split('class="header-account"', 1)[1].split("</div>", 1)[0]
    assert account.count('href="/settings/account"') == 1
    assert 'class="account-identity account-identity-link header-control"' in account
    assert 'href="/settings/account"' in account
    assert 'aria-label="Account Settings, Test User"' in account
    assert 'class="account-avatar" aria-hidden="true">TU</span>' in account
    assert ">Test User</span>" in account
    assert 'class="header-action account-security"' not in account
    assert 'class="header-action settings-link header-control"' in account
    assert 'title="Settings"' in account
    assert 'href="/static/icons.svg?v=v1#gear"' in account
    assert 'href="/static/icons.svg?v=v1#sign-out"' in account

    non_admin = _render("/", {"id": 7, "name": "Basic User", "is_admin": False})
    assert 'href="/settings/account"' not in non_admin
    assert 'class="account-identity account-identity-link"' not in non_admin
    non_admin_account = non_admin.split('class="header-account"', 1)[1].split("</div>", 1)[0]
    assert '<span class="account-identity header-control">' in non_admin_account

    no_id = _render("/", {"id": None, "name": "Admin Legacy", "is_admin": True})
    assert 'href="/settings/account"' not in no_id
    assert 'class="account-identity account-identity-link"' not in no_id
    no_id_account = no_id.split('class="header-account"', 1)[1].split("</div>", 1)[0]
    assert '<span class="account-identity header-control">' in no_id_account


def test_authenticated_shell_account_identity_link_has_aria_current_on_own_page():
    body = _render("/settings/account")
    account = body.split('class="header-account"', 1)[1].split("</div>", 1)[0]
    assert 'class="account-identity account-identity-link header-control" aria-label="Account Settings, Test User" aria-current="page"' in account
    assert 'aria-current="page"' not in _nav(body)

    body_elsewhere = _render("/settings")
    account_elsewhere = body_elsewhere.split('class="header-account"', 1)[1].split("</div>", 1)[0]
    assert 'aria-current="page"' not in account_elsewhere.split("</a>", 1)[0]
    assert 'class="header-action settings-link header-control" aria-label="Settings" title="Settings" aria-current="page"' in account_elsewhere


def test_settings_and_account_settings_are_the_only_account_destination_states():
    settings = _render("/settings")
    account = settings.split('class="header-account"', 1)[1].split("</div>", 1)[0]
    assert account.count('aria-current="page"') == 1
    assert 'settings-link header-control" aria-label="Settings" title="Settings" aria-current="page"' in account
    assert 'account-identity-link header-control" aria-label="Account Settings, Test User"' in account

    account_settings = _render("/settings/account")
    account = account_settings.split('class="header-account"', 1)[1].split("</div>", 1)[0]
    assert account.count('aria-current="page"') == 1
    assert 'account-identity-link header-control" aria-label="Account Settings, Test User" aria-current="page"' in account
    assert 'settings-link header-control" aria-label="Settings" title="Settings" aria-current="page"' not in account


def test_authenticated_actions_are_named_local_icon_controls_and_logout_is_post_only():
    body = _render()
    account = body.split('class="header-account"', 1)[1].split("</div>", 1)[0]
    assert 'title="Settings"' in account
    assert 'aria-label="Settings"' in account
    assert '>Settings</a>' in account
    assert 'title="Log out"' in account
    assert 'aria-label="Log out"' in account
    assert ">Log out<" in account
    assert 'aria-hidden="true"' in account
    assert '<form class="logout-form" hx-post="/logout" hx-swap="none">' in account
    assert 'class="link-button header-action header-control"' in account
    assert 'href="/logout"' not in account


def test_unauthenticated_pages_keep_only_the_brand_and_no_account_shell():
    body = _render("/login", None)
    assert '<a href="/" class="brand-link">' in body
    assert ">Odograph</span>" in body
    assert '<nav aria-label="Primary navigation">' not in body
    assert 'class="header-account"' not in body
    assert 'hx-post="/logout"' not in body


def test_account_avatar_image_replaces_initials_when_uploaded():
    body = _render("/settings/account", {**USER, "has_avatar": True, "avatar_version": 42})
    account = body.split('class="header-account"', 1)[1].split("</div>", 1)[0]
    assert '<img src="/account/avatar?v=42" alt="" class="account-avatar">' in account
    assert 'class="account-avatar" aria-hidden="true">' not in account

    other_version = _render("/settings/account", {**USER, "has_avatar": True, "avatar_version": 7})
    other_account = other_version.split('class="header-account"', 1)[1].split("</div>", 1)[0]
    assert '<img src="/account/avatar?v=7" alt="" class="account-avatar">' in other_account
    assert '<img src="/account/avatar?v=42"' not in other_account

    without_avatar = _render("/settings/account", USER)
    fallback_account = without_avatar.split('class="header-account"', 1)[1].split("</div>", 1)[0]
    assert '<img src="/account/avatar' not in fallback_account
    assert 'class="account-avatar" aria-hidden="true">TU</span>' in fallback_account


def test_account_avatar_css_keeps_shared_box_metrics_and_adds_object_fit_cover():
    assert (
        ".account-avatar {\n"
        "  display: inline-flex; width: 1.75rem; height: 1.75rem; flex: none; align-items: center; justify-content: center;\n"
        "  border: 1px solid var(--accent-primary); border-radius: 50%; color: var(--accent-primary);\n"
        "  background: var(--accent-soft); font-size: .7rem; font-weight: 700; letter-spacing: .02em;\n"
        "  object-fit: cover;\n"
        "}"
    ) in CSS


def test_desktop_header_uses_compact_nocturne_rule_and_active_accent_indicator():
    assert "min-height: 58px" in CSS
    header_rule = CSS.split("header {", 1)[1].split("}", 1)[0]
    assert "background-image: var(--rule-fade-soft);" in header_rule
    assert 'header nav a[aria-current="page"]' in CSS
    assert "@media (min-width: 761px)" in CSS
    assert "header { align-items: end; padding-bottom: var(--space-1); }" in CSS
    assert "header h1 { display: flex; min-height: var(--control-height); align-items: center; }" in CSS
    assert "header nav { flex-wrap: nowrap; gap: var(--space-2); }" in CSS
    assert "header nav a { padding-inline: 0; }" in CSS
    assert ".header-account .account-name { display: none; }" in CSS
    assert ".header-account .settings-link, .header-account .logout-form .header-action { font-size: 0; }" in CSS
    assert "bottom: calc(-1 * var(--space-1))" in CSS
    assert 'background: var(--accent-primary)' in CSS
    assert "var(--control-height)" in CSS


def test_account_identity_link_keeps_box_metrics_and_gets_accent_treatment():
    assert (
        ".account-identity { display: inline-flex; min-width: 0; min-height: var(--control-height); "
        "align-items: center; gap: var(--space-1); }"
    ) in CSS
    # The rule must target the .account-name descendant directly: both
    # `.account-name` and `header .who` (which the name span also carries)
    # set their own `color`, and `header .who` (0,1,1) beats a bare
    # `.account-identity-link:hover { color: ... }` (0,1,0 on the ancestor,
    # which does not even reach the child via inheritance-that-loses). A
    # selector scoped to the ancestor alone is therefore dead: it would
    # parse and pass a naive substring check while painting nothing.
    assert (
        ".account-identity-link:hover .account-name,\n"
        ".account-identity-link:focus-visible .account-name { color: var(--accent-hover); }"
    ) in CSS
    assert (
        '.account-identity-link[aria-current="page"] .account-name '
        "{ color: var(--accent-primary); font-weight: 600; }"
    ) in CSS
    # A selector on the ancestor alone would be dead for the reason above;
    # guard against silently reintroducing it.
    assert ".account-identity-link:hover, .account-identity-link:focus-visible { color:" not in CSS
    assert '.account-identity-link[aria-current="page"] { color:' not in CSS


def test_header_account_controls_share_quiet_states_and_tablet_targets():
    assert ".header-control {" in CSS
    assert "min-height: var(--control-height)" in CSS
    assert "padding: 0 var(--space-2)" in CSS
    assert "gap: var(--space-1)" in CSS
    assert "text-decoration: none" in CSS
    assert "a.header-control:hover, button.header-control:hover," in CSS
    assert "a.header-control:focus-visible, button.header-control:focus-visible" in CSS
    assert "a.header-control:active, button.header-control:active" in CSS
    assert ".account-identity.header-control:hover" not in CSS
    assert '.header-control[aria-current="page"]' in CSS
    assert "width: var(--control-height); min-width: var(--control-height); padding-inline: 0;" in CSS
    assert ".header-account .header-action-icon { width: 1.25rem; height: 1.25rem; }" in CSS
