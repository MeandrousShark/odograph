"""Template-level tests for setup.html in create and reset modes."""
from __future__ import annotations

from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.main import make_templates

TZ = ZoneInfo("UTC")


def _render(**context):
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    defaults = {
        "user": None, "csrf": "test-csrf-token",
        "mode": "create", "admin_email": None, "error": None,
    }
    defaults.update(context)
    return templates.env.get_template("setup.html").render(**defaults)


def test_create_mode_has_email_field_and_create_copy():
    body = _render(mode="create", admin_email=None)
    assert "Create administrator" in body
    assert 'name="email"' in body
    assert "No administrator exists yet" in body
    assert "resets that administrator" not in body


def test_reset_mode_has_no_email_field_and_states_it_resets_existing_admin():
    body = _render(mode="reset", admin_email="admin@example.com")
    assert "Reset administrator password" in body
    assert 'name="email"' not in body
    assert "admin@example.com" in body
    assert "resets that administrator's password" in body
    assert "can never create a second one" in body


def test_setup_form_posts_token_and_csrf():
    body = _render(mode="create")
    assert 'action="/setup"' in body
    assert 'name="token"' in body
    assert '<input type="hidden" name="csrf_token" value="test-csrf-token">' in body
    assert 'name="password_confirm"' in body


def test_error_message_renders_when_present():
    body = _render(mode="reset", admin_email="admin@example.com", error="Invalid setup token.")
    assert "Invalid setup token." in body


def test_setup_page_never_renders_admin_token_or_password_values():
    # admin_email is the only admin-derived value in context; the token and
    # password inputs must never carry a `value` attribute.
    body = _render(mode="reset", admin_email="admin@example.com")
    assert '<input type="password" name="token" required autocomplete="off" autofocus>' in body
    assert '<input type="password" name="password" required autocomplete="new-password" minlength="8">' in body
