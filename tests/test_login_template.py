"""Template-level tests for login.html in all three auth configurations,
independent of the routing/DB logic that picks the context (see
tests/test_auth_route_ordering.py and the DB-backed login tests for that).
"""
from __future__ import annotations

from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.main import make_templates

TZ = ZoneInfo("UTC")


def _render(**context):
    templates = make_templates(SimpleNamespace(display_tz=TZ))
    defaults = {
        "user": None, "csrf": "test-csrf-token",
        "local_admin_exists": False, "oidc_available": False,
        "setup_available": False, "error": None,
    }
    defaults.update(context)
    return templates.env.get_template("login.html").render(**defaults)


def test_local_only_shows_password_form_and_no_oidc_button():
    body = _render(local_admin_exists=True, oidc_available=False)
    assert 'action="/login/local"' in body
    assert 'name="email"' in body
    assert 'name="password"' in body
    assert '<input type="hidden" name="csrf_token" value="test-csrf-token">' in body
    assert "Sign in with OIDC" not in body
    assert "/login/oidc" not in body


def test_both_configured_shows_password_form_and_oidc_button():
    body = _render(local_admin_exists=True, oidc_available=True)
    assert 'action="/login/local"' in body
    assert 'href="/login/oidc"' in body
    assert "Sign in with OIDC" in body


def test_oidc_only_shows_oidc_button_and_no_password_form():
    # Production's configuration: OIDC set up, no local admin yet. GET
    # /login no longer auto-redirects to the provider for this case (that
    # was the logout bug), so this rendered combination is now reachable.
    body = _render(local_admin_exists=False, oidc_available=True)
    assert 'href="/login/oidc"' in body
    assert "Sign in with OIDC" in body
    assert 'action="/login/local"' not in body
    assert 'name="password"' not in body


def test_no_admin_and_setup_available_points_at_setup():
    body = _render(local_admin_exists=False, oidc_available=False, setup_available=True)
    assert 'action="/login/local"' not in body
    assert 'href="/setup"' in body
    assert "README" not in body


def test_no_admin_and_no_admin_token_points_at_readme_only():
    body = _render(local_admin_exists=False, oidc_available=False, setup_available=False)
    assert 'action="/login/local"' not in body
    assert 'href="/setup"' not in body
    assert "README" in body


def test_error_message_renders_when_present():
    body = _render(local_admin_exists=True, error="Invalid email or password.")
    assert "Invalid email or password." in body


def test_no_error_block_when_error_is_none():
    body = _render(local_admin_exists=True, error=None)
    assert "form-error-summary" not in body


def test_login_page_never_renders_secrets():
    # Neither a password hash nor the setup token itself is ever part of
    # this context, but this guards the template surface too -- the
    # password input has no `value` attribute, so a submitted password is
    # never echoed back into the page.
    body = _render(local_admin_exists=True, oidc_available=True, setup_available=True)
    assert '<input type="password" name="password" required autocomplete="current-password">' in body
