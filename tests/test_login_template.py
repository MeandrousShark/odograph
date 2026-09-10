from __future__ import annotations

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.main import make_templates

TZ = ZoneInfo("UTC")


def _render(**context):
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    defaults = {
        "user": None,
        "csrf": "test-csrf-token",
        "account_exists": False,
        "signup_available": False,
        "legacy_oidc_available": False,
        "oidc_login_available": False,
        "error": None,
    }
    defaults.update(context)
    return templates.env.get_template("login.html").render(**defaults)


def test_account_without_link_shows_only_local_login():
    body = _render(account_exists=True)
    assert 'action="/login/local"' in body
    assert '<input type="hidden" name="csrf_token" value="test-csrf-token">' in body
    assert "/login/oidc" not in body
    assert "/signup" not in body


def test_account_shows_local_and_linked_oidc_login_choices():
    body = _render(account_exists=True, oidc_login_available=True)
    assert 'action="/login/local"' in body
    assert 'href="/login/oidc"' in body
    assert ">or</p>" in body


def test_fresh_install_links_first_account_signup():
    body = _render(signup_available=True)
    assert 'href="/signup"' in body
    assert 'action="/login/local"' not in body
    assert "/login/oidc" not in body


def test_legacy_oidc_upgrade_shows_only_oidc_entry():
    body = _render(legacy_oidc_available=True)
    assert 'href="/login/oidc"' in body
    assert 'action="/login/local"' not in body
    assert "/signup" not in body


def test_closed_empty_install_points_to_operator_recovery():
    body = _render()
    assert "operator recovery command" in body
    assert "/setup" not in body


def test_login_error_is_generic_and_password_is_not_echoed():
    body = _render(account_exists=True, error="Invalid email or password.")
    assert "Invalid email or password." in body
    assert '<input type="password" name="password" required autocomplete="current-password">' in body
    assert 'name="password" value=' not in body


@pytest.mark.parametrize(
    ("context", "expected_action"),
    [
        ({"account_exists": True}, 'action="/login/local"'),
        ({"account_exists": True, "oidc_login_available": True}, 'href="/login/oidc"'),
        ({"signup_available": True}, 'href="/signup"'),
        ({"legacy_oidc_available": True}, 'href="/login/oidc"'),
        ({}, "operator recovery command"),
    ],
)
def test_login_availability_states_share_the_auth_page_structure(context, expected_action):
    body = _render(**context)

    assert '<div class="card auth-card">' in body
    assert '<div class="page-title auth-page-title">' in body
    assert '<h2 class="page-title-heading">Sign in</h2>' in body
    assert expected_action in body


def test_login_error_uses_the_shared_danger_notice_and_alert_role():
    body = _render(error="Invalid email or password.")

    assert 'class="notice notice-danger form-error-summary" role="alert"' in body
