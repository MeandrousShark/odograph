from __future__ import annotations

from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.main import make_templates

TZ = ZoneInfo("UTC")


def _templates():
    return make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))


def test_signup_form_collects_local_credentials_and_csrf_without_secrets():
    body = _templates().env.get_template("signup.html").render(
        user=None, csrf="test-csrf-token", error=None
    )

    assert 'action="/signup"' in body
    assert 'name="email"' in body
    assert 'name="password"' in body
    assert 'name="password_confirm"' in body
    assert '<input type="hidden" name="csrf_token" value="test-csrf-token">' in body
    assert 'name="password" value=' not in body


def test_legacy_establishment_collects_editable_local_credentials_without_identity_secrets():
    body = _templates().env.get_template("establish_account.html").render(
        user={"name": "Legacy User", "is_admin": False},
        csrf="test-csrf-token",
        email="provider@example.com",
        error=None,
        issuer="https://identity.example.invalid/secret-issuer",
        subject="secret-subject",
        token="secret-token",
    )

    assert 'action="/account/establish"' in body
    assert 'name="email" value="provider@example.com"' in body
    assert 'name="password"' in body
    assert 'name="password_confirm"' in body
    assert '<input type="hidden" name="csrf_token" value="test-csrf-token">' in body
    assert 'name="password" value=' not in body
    assert "secret-issuer" not in body
    assert "secret-subject" not in body
    assert "secret-token" not in body


def _render_account_security(*, oidc_configured=False, linked_identity=None):
    return _templates().env.get_template("account_security.html").render(
        user={"name": "admin", "is_admin": True},
        csrf="test-csrf-token",
        account_email="admin@example.com",
        oidc_configured=oidc_configured,
        linked_identity=linked_identity,
        error=None,
        success=None,
    )


def test_account_security_shows_safe_email_password_form_and_recovery_command():
    body = _render_account_security()

    assert "admin@example.com" in body
    assert 'action="/settings/account/password"' in body
    assert 'name="current_password"' in body
    assert 'name="password_confirm"' in body
    assert "python -m app.manage_account reset-password" in body
    assert "No sign-in provider is configured." in body
    assert 'action="/settings/account/oidc/link"' not in body
    assert 'action="/settings/account/oidc/unlink"' not in body
    assert "password_hash" not in body
    assert "auth_version" not in body


def test_account_security_offers_password_reauthenticated_oidc_linking_when_unlinked():
    body = _render_account_security(oidc_configured=True)

    assert "Sign-in provider: available but not linked." in body
    assert '<form method="post" action="/settings/account/oidc/link"' in body
    assert '<input type="hidden" name="csrf_token" value="test-csrf-token">' in body
    assert 'name="current_password"' in body
    assert 'autocomplete="current-password"' in body
    assert 'action="/settings/account/oidc/unlink"' not in body
    assert 'name="confirm_unlink"' not in body


def test_account_security_shows_only_safe_linked_identity_metadata_and_unlink_form():
    body = _render_account_security(
        oidc_configured=True,
        linked_identity={
            "email": "provider@example.com",
            "display_name": "Provider User",
            "issuer": "https://identity.example.invalid/secret-issuer",
            "subject": "secret-subject-123",
            "access_token": "secret-access-token",
            "refresh_token": "secret-refresh-token",
            "id_token": "secret-id-token",
            "raw_claims": "secret-raw-claims",
        },
    )

    assert "Sign-in provider: linked." in body
    assert "provider@example.com" in body
    assert "Provider User" in body
    assert '<form method="post" action="/settings/account/oidc/unlink"' in body
    assert '<input type="hidden" name="csrf_token" value="test-csrf-token">' in body
    assert 'name="current_password"' in body
    assert 'name="confirm_unlink" value="yes" required' in body
    assert "removes the linked sign-in provider" in body
    assert 'action="/settings/account/oidc/link"' not in body
    for secret in (
        "secret-issuer",
        "secret-subject-123",
        "secret-access-token",
        "secret-refresh-token",
        "secret-id-token",
        "secret-raw-claims",
    ):
        assert secret not in body
