from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from app.main import make_templates


ROOT = Path(__file__).parents[1]
CSS = (ROOT / "static/style.css").read_text()
SPRITE = (ROOT / "static/icons.svg").read_text()


ADMIN = {
    "id": 7, "name": "Test User", "email": "test@example.com", "is_admin": True,
    "has_avatar": False, "avatar_version": 0,
}
MEMBER = {
    "id": 8, "name": "Basic User", "email": "basic@example.com", "is_admin": False,
    "has_avatar": False, "avatar_version": 0,
}


def _render(path: str = "/", user: dict | None = ADMIN, review_count=...):
    templates = make_templates(SimpleNamespace(display_tz=None, app_version="test"))
    context = {
        "request": SimpleNamespace(url=SimpleNamespace(path=path)),
        "user": user,
        "csrf": "test-csrf",
        "csp_nonce": "test-nonce",
        "static_version": "v1",
    }
    if review_count is not ...:
        context["review_count"] = review_count
    return templates.env.get_template("base.html").render(**context)


def _mobile_nav(body: str) -> str:
    return body.split('<nav class="mobile-bottom-nav"', 1)[1].split("</nav>", 1)[0]


def test_authenticated_mobile_shell_has_expected_destinations_and_local_icons():
    nav = _mobile_nav(_render())
    assert [
        href for href in re.findall(r'<a href="([^"]+)" class="mobile-nav-item', nav)
    ] == ["/", "/trips", "/review", "/report"]
    assert '<details class="mobile-more" data-header-disclosure="more">' in nav
    assert "<span>Week</span>" in nav
    assert "<span>Trips</span>" in nav
    assert "<span>Review</span>" in nav
    assert "<span>Report</span>" in nav
    assert "<span>More</span>" in nav
    for name in ("gauge", "list-dashes", "check-circle", "chart-line", "dots-three-circle"):
        assert f"#{name}" in nav
        assert f'id="{name}"' in SPRITE



def test_mobile_text_controls_use_body_sized_font_after_shared_rule():
    shared_selector = 'button, input:not([type="checkbox"]):not([type="radio"]), select, textarea {'
    mobile_selector = 'input:not([type="checkbox"]):not([type="radio"]), select, textarea {'
    mobile_start = CSS.index("@media (max-width: 760px) {")
    shared_start = CSS.index(shared_selector)
    mobile_start = CSS.index(mobile_selector, mobile_start)
    rule = CSS[mobile_start + len(mobile_selector):].split("}", 1)[0]

    # The later rule must override the shared font: inherit declaration while
    # retaining its text-control exclusions.
    assert mobile_start > shared_start
    assert rule.strip() == "font-size: 1rem;"

def test_authenticated_mobile_shell_has_account_fallback_and_post_logout():
    admin = _render()
    assert '<details class="mobile-account-menu" data-header-disclosure="account">' in admin
    assert '<summary class="mobile-account-trigger" aria-label="Account settings">' in admin
    account = admin.split('<details class="mobile-account-menu"', 1)[1].split("</details>", 1)[0]
    assert 'href="/settings" class="mobile-account-item"' in account
    assert 'href="/settings/account" class="mobile-account-item"' in account
    assert "<span>Account Settings</span>" in account
    assert 'class="logout-form mobile-account-logout" hx-post="/logout"' in account
    assert 'href="/logout"' not in admin
    more = _mobile_nav(admin).split('<div class="mobile-more-panel">', 1)[1]
    assert "Settings" not in more
    assert "Account Security" not in more
    assert "Log out" not in more
    member = _render(user=MEMBER)
    member_account = member.split('<details class="mobile-account-menu"', 1)[1].split("</details>", 1)[0]
    assert 'href="/settings" class="mobile-account-item"' in member_account
    assert 'href="/settings/account" class="mobile-account-item"' in member_account


def test_mobile_account_trigger_avatar_image_replaces_initials_when_uploaded():
    with_avatar = _render(user={**ADMIN, "has_avatar": True, "avatar_version": 42})
    trigger = with_avatar.split('aria-label="Account settings">', 1)[1].split("</summary>", 1)[0]
    assert '<img src="/account/avatar?v=42" alt="" class="account-avatar">' in trigger
    assert 'class="account-avatar" aria-hidden="true">' not in trigger

    other_version = _render(user={**ADMIN, "has_avatar": True, "avatar_version": 7})
    other_trigger = other_version.split('aria-label="Account settings">', 1)[1].split("</summary>", 1)[0]
    assert '<img src="/account/avatar?v=7" alt="" class="account-avatar">' in other_trigger
    assert '<img src="/account/avatar?v=42"' not in other_trigger

    fallback = _render(user=ADMIN)
    fallback_trigger = fallback.split('aria-label="Account settings">', 1)[1].split("</summary>", 1)[0]
    assert '<img src="/account/avatar' not in fallback_trigger
    assert 'class="account-avatar" aria-hidden="true">TU</span>' in fallback_trigger


def test_account_avatar_css_keeps_shared_box_metrics_and_adds_object_fit_cover():
    assert (
        ".account-avatar {\n"
        "  display: inline-flex; width: 1.75rem; height: 1.75rem; flex: none; align-items: center; justify-content: center;\n"
        "  border: 1px solid var(--accent-primary); border-radius: 50%; color: var(--accent-primary);\n"
        "  background: var(--accent-soft); font-size: .7rem; font-weight: 700; letter-spacing: .02em;\n"
        "  object-fit: cover;\n"
        "}"
    ) in CSS
    assert (
        ".mobile-account-trigger {\n"
        "    display: flex; min-height: var(--control-height); align-items: center; justify-content: center;\n"
    ) in CSS


def test_mobile_active_states_cover_primary_and_more_routes():
    primary = {
        "/": "/",
        "/trips/42": "/trips",
        "/review/card": "/review",
        "/report/2026": "/report",
    }
    for path, href in primary.items():
        nav = _mobile_nav(_render(path))
        active = re.findall(r'<a href="([^"]+)" class="mobile-nav-item is-active"[^>]*aria-current="page"', nav)
        assert active == [href]
        assert '<details class="mobile-more is-active">' not in nav

    for path, href in (("/expenses", "/expenses"), ("/stats/coverage", "/stats")):
        nav = _mobile_nav(_render(path))
        assert '<details class="mobile-more is-active" data-header-disclosure="more">' in nav
        assert f'<a href="{href}" class="mobile-more-item is-active" aria-current="page">' in nav

    settings = _render("/settings")
    assert '<details class="mobile-more is-active">' not in _mobile_nav(settings)
    assert '<details class="mobile-account-menu is-active" data-header-disclosure="account">' in settings
    assert '<a href="/settings" class="mobile-account-item is-active" aria-current="page">' in settings
    account = _render("/settings/account")
    assert '<details class="mobile-more is-active">' not in _mobile_nav(account)
    assert '<details class="mobile-account-menu is-active" data-header-disclosure="account">' in account
    assert '<a href="/settings/account" class="mobile-account-item is-active" aria-current="page">' in account


def test_mobile_disclosures_use_one_delegated_mutual_dismissal_script():
    body = _render()
    assert 'data-header-disclosure="account"' in body
    assert 'data-header-disclosure="more"' in body
    assert 'var selector = "[data-header-disclosure]";' in body
    toggle_listener = body.index('document.addEventListener("toggle"')
    assert body.index('      }, true);', toggle_listener) > toggle_listener
    assert 'document.addEventListener("click"' in body
    assert 'document.addEventListener("keydown"' in body
    assert 'event.key !== "Escape"' in body
    assert 'document.querySelector(selector + "[open]")' in body
    assert 'if (event.target.closest(selector)) return;' in body
    assert 'if (summary) summary.focus();' in body


def test_review_badge_uses_only_the_global_count():
    positive = _mobile_nav(_render("/review", review_count=4))
    assert 'class="mobile-nav-badge" aria-hidden="true">4</span>' in positive
    assert 'aria-label="Review, 4 remaining"' in positive
    dashboard_nav = _mobile_nav(_render("/", review_count=3))
    assert 'class="mobile-nav-badge" aria-hidden="true">3</span>' in dashboard_nav
    assert 'aria-label="Review, 3 remaining"' in dashboard_nav
    for review_count in (0, None):
        assert "mobile-nav-badge" not in _mobile_nav(_render("/review", review_count=review_count))
    assert "mobile-nav-badge" not in _mobile_nav(_render("/review"))


def test_positive_global_review_count_renders_on_every_authenticated_destination():
    for path in (
        "/", "/trips", "/trips/42", "/review", "/report/2026",
        "/report/range", "/expenses", "/stats", "/settings", "/settings/account",
    ):
        nav = _mobile_nav(_render(path, review_count=2))
        assert 'class="mobile-nav-badge" aria-hidden="true">2</span>' in nav


def test_unauthenticated_pages_exclude_both_authenticated_mobile_and_desktop_shells():
    body = _render("/login", user=None)
    assert "mobile-bottom-nav" not in body
    assert "mobile-account-menu" not in body
    assert 'aria-label="Primary navigation"' not in body
    assert 'class="header-account"' not in body


def test_mobile_shell_reserves_safe_area_and_selection_bar_space_without_fixed_width():
    assert "env(safe-area-inset-bottom)" in CSS
    assert "position: fixed; right: 0; bottom: 0; left: 0;" in CSS
    assert "position: fixed; right: 0; bottom: 0; left: 0; z-index: 1001;" in CSS
    assert "padding-bottom: calc(var(--control-height) + var(--space-6) + env(safe-area-inset-bottom));" in CSS
    assert "bottom: calc(var(--control-height) + var(--space-3) + env(safe-area-inset-bottom));" in CSS
    assert "grid-template-columns: repeat(5, minmax(0, 1fr));" in CSS
    assert "flex-direction: column; align-items: center; justify-content: center;" in CSS
    assert "header h1 { display: flex; min-height: var(--control-height); align-items: center; }" in CSS
    assert "max-width: calc(100vw - var(--space-4));" in CSS
    assert "@media (min-width: 761px)" in CSS
    assert ".mobile-account-menu, .mobile-bottom-nav { display: none; }" in CSS
    assert "position: absolute; top: calc(100% + var(--space-2)); right: 0; z-index: 1002;" in CSS


def test_mobile_render_does_not_duplicate_ids():
    body = _render()
    ids = re.findall(r'\bid="([^"]+)"', body)
    assert len(ids) == len(set(ids))


def test_safe_area_viewport_contract_covers_content_and_navigation_edges():
    body = _render()
    assert 'content="width=device-width, initial-scale=1, viewport-fit=cover"' in body
    # Cover exposes all edges, including landscape cutouts, not just the bottom.
    desktop_body = CSS.split("body {", 1)[1].split("}", 1)[0]
    mobile_body = CSS.split("  body {", 1)[1].split("}", 1)[0]
    for rule in (desktop_body, mobile_body):
        for edge in ("top", "right", "bottom", "left"):
            assert f"env(safe-area-inset-{edge})" in rule
    nav_rule = CSS.split("  .mobile-bottom-nav {", 1)[1].split("}", 1)[0]
    assert "calc(var(--space-1) + env(safe-area-inset-bottom))" in nav_rule
    for edge in ("left", "right"):
        assert f"max(var(--space-2), env(safe-area-inset-{edge}))" in nav_rule
    assert "bottom: max(1rem, env(safe-area-inset-bottom));" in CSS


def test_mobile_account_current_page_has_visible_indicator_even_with_avatar():
    for path in ("/settings", "/settings/account"):
        body = _render(path, {**ADMIN, "has_avatar": True})
        assert '<details class="mobile-account-menu is-active"' in body
        assert 'class="account-avatar"' in body
    rule = CSS.split(
        ".mobile-account-menu.is-active > .mobile-account-trigger::after {", 1
    )[1].split("}", 1)[0]
    assert "height: 3px;" in rule
    assert "background: var(--accent-primary);" in rule
    assert "position: absolute;" in rule
