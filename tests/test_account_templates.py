from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.main import make_templates

TZ = ZoneInfo("UTC")
CSS = Path("static/style.css").read_text()


class _ContainerStructureParser(HTMLParser):
    """Track div/section nesting so card closing order is tested as markup."""

    def __init__(self):
        super().__init__()
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag not in {"div", "section"}:
            return
        classes = frozenset(dict(attrs).get("class", "").split())
        self.stack.append((tag, classes))

    def handle_endtag(self, tag):
        if tag not in {"div", "section"}:
            return
        if not self.stack or self.stack[-1][0] != tag:
            self.errors.append((tag, self.stack[-1][0] if self.stack else None))
            return
        self.stack.pop()


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
    assert '<div class="card auth-card">' in body
    assert '<div class="page-title auth-page-title">' in body
    assert '<h2 class="page-title-heading">Create administrator account</h2>' in body
    assert 'class="page-title-subtitle">This creates the sole administrator' in body
    assert 'class="control control-primary auth-submit">Create administrator</button>' in body


def test_signup_error_uses_the_shared_danger_notice_and_alert_role():
    body = _templates().env.get_template("signup.html").render(
        user=None, csrf="test-csrf-token", error="Passwords do not match."
    )

    assert 'class="notice notice-danger form-error-summary" role="alert"' in body


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
    assert '<div class="page-title auth-page-title">' in body
    assert '<h2 class="page-title-heading">Establish administrator account</h2>' in body
    assert 'class="page-title-subtitle">Create local login credentials' in body
    assert 'class="control control-primary auth-submit">Establish administrator account</button>' in body


def test_legacy_establishment_error_uses_the_shared_danger_notice_and_alert_role():
    body = _templates().env.get_template("establish_account.html").render(
        user={"name": "Legacy User", "is_admin": False},
        csrf="test-csrf-token",
        email="provider@example.com",
        error="A local account already exists.",
    )

    assert 'class="notice notice-danger form-error-summary" role="alert"' in body


def _render_account_security(
    *,
    oidc_configured=False,
    linked_identity=None,
    error=None,
    success=None,
    account_email_verified=False,
    email_challenge_available=True,
    has_avatar=False,
    has_password=True,
    method_notice=None,
    avatar_version=0,
    avatar_max_label="500 KB",
    can_sign_out_everywhere=True,
):
    return _templates().env.get_template("account_security.html").render(
        user={
            "name": "admin", "is_admin": True,
            "has_avatar": has_avatar, "avatar_version": avatar_version,
        },
        csrf="test-csrf-token",
        account_email="admin@example.com",
        account_email_verified=account_email_verified,
        email_challenge_available=email_challenge_available,
        oidc_configured=oidc_configured,
        linked_identity=linked_identity,
        avatar_max_label=avatar_max_label,
        has_password=has_password,
        can_sign_out_everywhere=can_sign_out_everywhere,
        method_notice=method_notice,
        error=error,
        success=success,
    )


def _render_email_challenge_confirm(*, user=True, error=None, success=None):
    return _templates().env.get_template("email_challenge_confirm.html").render(
        user={"id": 1, "name": "admin", "is_admin": True} if user else None,
        csrf="test-csrf-token",
        error=error,
        success=success,
        csp_nonce="test-nonce",
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
    assert '<div class="page-title auth-page-title">' in body
    assert '<title>Account Settings: Odograph</title>' in body
    assert '<h2 class="page-title-heading">Account Settings</h2>' in body
    assert '<section class="card account-settings-card account-settings-profile" aria-labelledby="profile-heading">' in body
    assert '<h3 id="profile-heading">Profile</h3>' in body
    assert '<section class="card account-settings-card account-settings-security" aria-labelledby="security-heading">' in body
    assert '<h3 id="security-heading">Security</h3>' in body
    assert body.index('class="page-title auth-page-title"') < body.index('class="card account-settings-card')
    assert 'aria-labelledby="change-password-heading"' in body
    assert 'id="change-password-heading">Change password</h4>' in body
    assert 'aria-labelledby="operator-recovery-heading"' in body
    assert 'id="operator-recovery-heading">Operator recovery</h4>' in body
    assert 'id="login-email-heading">Login email</h4>' in body
    assert 'action="/settings/account/email/verify/request"' in body
    assert 'action="/settings/account/email/change/request"' in body
    structure = _ContainerStructureParser()
    structure.feed(body)
    assert structure.errors == []
    assert structure.stack == []


def test_account_security_sign_out_everywhere_explains_session_scope_and_dev_boundary():
    body = _render_account_security(has_password=False)
    assert 'hx-post="/settings/account/sign-out-everywhere"' in body
    assert "does not sign you out of your sign-in provider" in body
    assert "or revoke tracking credentials" in body
    assert 'hx-post="/settings/account/sign-out-everywhere"' not in (
        _render_account_security(can_sign_out_everywhere=False)
    )


def test_account_email_forms_require_password_and_collect_twice_entered_new_address():
    body = _render_account_security()

    verify = body.split('action="/settings/account/email/verify/request"', 1)[1].split("</form>", 1)[0]
    change = body.split('action="/settings/account/email/change/request"', 1)[1].split("</form>", 1)[0]
    assert 'name="csrf_token" value="test-csrf-token"' in verify
    assert 'name="current_password"' in verify
    assert 'name="current_password"' in change
    assert 'name="new_email"' in change and 'type="email"' in change
    assert 'name="new_email_confirm"' in change and 'type="email"' in change
    assert "current login email stays active until you confirm it" in body


def test_account_email_verified_state_and_smtp_availability_gate_request_forms():
    verified = _render_account_security(account_email_verified=True)
    assert "Your current login email is verified." in verified
    assert 'action="/settings/account/email/verify/request"' not in verified
    assert 'action="/settings/account/email/change/request"' in verified

    unavailable = _render_account_security(email_challenge_available=False)
    assert "Email challenge delivery isn't available right now." in unavailable
    assert 'action="/settings/account/email/verify/request"' not in unavailable
    assert 'action="/settings/account/email/change/request"' not in unavailable


def test_email_challenge_confirmation_scrubs_fragment_and_keeps_manual_token_entry():
    body = _render_email_challenge_confirm()

    assert 'action="/settings/account/email/confirm"' in body
    assert 'name="csrf_token" value="test-csrf-token"' in body
    assert 'name="purpose"' in body
    assert 'value="verify_current"' in body
    assert 'value="change_email"' in body
    assert 'id="email-challenge-token" type="text" name="token" required' in body
    assert 'fragment.get("purpose")' in body
    assert 'fragment.get("token")' in body
    assert 'window.history.replaceState(' in body
    assert 'window.location.pathname + window.location.search' in body
    assert 'window.location.href' not in body
    assert "paste the token from your email" in body
    assert 'name="token" value=' not in body


def test_email_challenge_confirmation_success_does_not_render_a_token_form():
    body = _render_email_challenge_confirm(success="Email address confirmed.")

    assert 'class="notice notice-success form-success" role="status"' in body
    assert "Email address confirmed." in body
    assert 'action="/settings/account/email/confirm"' not in body
    assert 'name="token"' not in body


def test_signed_out_email_challenge_page_offers_safe_sign_in_path():
    body = _render_email_challenge_confirm(user=False)

    assert "Sign in as the account that requested this challenge before submitting." in body
    assert 'href="/login"' in body
    assert 'href="/settings/account"' not in body
    assert 'href="/login?token=' not in body


def test_account_settings_keeps_notices_above_two_distinct_content_cards():
    body = _render_account_security(error="Something went wrong.", success="Saved.")
    stack = body.index('<div class="account-settings-stack">')
    profile = body.index('class="card account-settings-card account-settings-profile"')
    security = body.index('class="card account-settings-card account-settings-security"')
    assert body.index('class="notice notice-danger form-error-summary"') < stack
    assert body.index('class="notice notice-success form-success"') < stack
    assert profile < security
    profile_body = body[profile:security]
    security_body = body[security:]
    assert 'id="account-avatar-heading">Avatar</h4>' in profile_body
    assert 'action="/settings/account/avatar"' in profile_body
    assert 'action="/settings/account/password"' not in profile_body
    assert 'action="/settings/account/password"' in security_body
    assert 'id="oidc-provider-heading">Optional sign-in provider</h4>' in security_body
    assert 'id="operator-recovery-heading">Operator recovery</h4>' in security_body


def test_account_security_offers_password_reauthenticated_oidc_linking_when_unlinked():
    body = _render_account_security(oidc_configured=True)

    assert "Sign-in provider: available but not linked." in body
    assert '<form method="post" action="/settings/account/oidc/link"' in body
    assert '<input type="hidden" name="csrf_token" value="test-csrf-token">' in body
    assert 'name="current_password"' in body
    assert 'autocomplete="current-password"' in body
    assert 'action="/settings/account/oidc/unlink"' not in body
    assert 'name="confirm_unlink"' not in body
    assert 'class="control control-primary auth-submit">Link sign-in provider</button>' in body


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
    assert 'class="control control-destructive auth-submit">Unlink sign-in provider</button>' in body
    for secret in (
        "secret-issuer",
        "secret-subject-123",
        "secret-access-token",
        "secret-refresh-token",
        "secret-id-token",
        "secret-raw-claims",
    ):
        assert secret not in body


def test_account_security_error_and_success_use_accessible_shared_notices():
    body = _render_account_security(
        error="Current password is incorrect.", success="Password changed."
    )

    assert 'class="notice notice-danger form-error-summary" role="alert"' in body
    assert 'class="notice notice-success form-success" role="status"' in body


def test_method_confirmation_is_visible_beside_password_controls():
    from app.auth import OIDC_REAUTH_NOTICE, PASSWORD_SAVED_NOTICE

    for notice, has_password in (
        (OIDC_REAUTH_NOTICE, False),
        (PASSWORD_SAVED_NOTICE, True),
    ):
        body = _render_account_security(
            has_password=has_password, success=notice, method_notice=notice,
        )
        assert body.count(notice) == 1
        assert (
            body.index('<h3 id="security-heading">')
            < body.index(notice)
            < body.index('<h4 id="change-password-heading"')
        )


def test_account_security_recovery_and_identity_metadata_keep_safe_wrapping_structure():
    body = _render_account_security(
        oidc_configured=True,
        linked_identity={"email": "a" * 200, "display_name": "b" * 200},
    )

    assert '<dl class="account-security-identity">' in body
    assert "python -m app.manage_account reset-password" in body
    assert 'class="auth-checkbox"' in body
    assert 'input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"])' in CSS


def test_account_security_avatar_section_without_an_avatar_shows_initials_and_upload_only():
    body = _render_account_security(has_avatar=False)

    assert 'aria-labelledby="account-avatar-heading"' in body
    assert 'id="account-avatar-heading">Avatar</h4>' in body
    assert '<span class="account-avatar account-security-avatar-placeholder" aria-hidden="true">A</span>' in body
    assert '<img src="/account/avatar' not in body
    assert '<form method="post" action="/settings/account/avatar"' in body
    assert 'enctype="multipart/form-data"' in body
    assert '<input type="hidden" name="csrf_token" value="test-csrf-token">' in body
    assert 'name="file"' in body
    assert 'accept="image/png,image/jpeg,image/webp"' in body
    assert 'class="control control-primary auth-submit">Upload avatar</button>' in body
    assert "PNG, JPEG, or WebP, up to 500 KB." in body
    assert 'action="/settings/account/avatar/remove"' not in body
    assert 'data-avatar-remove-open="' not in body
    assert 'id="avatar-remove-dialog"' not in body


def test_account_security_avatar_section_with_an_avatar_shows_remove_confirmation_dialog():
    body = _render_account_security(has_avatar=True, avatar_version=42)

    assert '<img src="/account/avatar?v=42" alt="" class="account-security-avatar-image">' in body
    assert 'class="account-avatar account-security-avatar-placeholder"' not in body
    assert '<button type="button" class="control control-destructive auth-submit"' in body
    assert 'aria-haspopup="dialog" aria-controls="avatar-remove-dialog"' in body
    assert 'data-avatar-remove-open="avatar-remove-dialog">Remove avatar</button>' in body
    assert '<dialog id="avatar-remove-dialog" class="app-dialog account-avatar-remove-dialog"' in body
    assert 'aria-labelledby="avatar-remove-dialog-title"' in body
    assert 'aria-describedby="avatar-remove-dialog-description"' in body
    assert '<form method="post" action="/settings/account/avatar/remove"' in body
    assert '<input type="hidden" name="csrf_token" value="test-csrf-token">' in body
    assert '<input type="hidden" name="confirm_remove" value="yes">' in body
    assert 'id="avatar-remove-dialog-title">Remove avatar?</h2>' in body
    assert 'id="avatar-remove-dialog-description">This removes your uploaded avatar' in body
    assert 'data-avatar-remove-cancel autofocus>Cancel</button>' in body
    assert 'class="control control-destructive account-avatar-remove-confirm">Remove avatar</button>' in body
    assert 'dialog.showModal()' in body
    assert 'data-avatar-remove-open' in body
    assert 'data-avatar-remove-cancel' in body
    assert "window.confirm" not in body
    # Upload and confirmation are separate forms, each with its own CSRF field.
    assert body.count('name="csrf_token" value="test-csrf-token"') >= 3
