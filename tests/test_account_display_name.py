"""Display name validation and how the account shell renders it."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from app.accounts import DisplayNameError, normalize_display_name
from app.auth import _account_user
from app.main import make_templates

LABEL_RE = re.compile(r'<span class="who account-name">([^<]*)</span>')
INITIALS_RE = re.compile(r'<span class="account-avatar" aria-hidden="true">([^<]*)</span>')


def _account(**overrides):
    return {
        "id": 7, "email": "jane.doe@example.com", "is_admin": False, "is_enabled": True,
        "avatar_mime": None, "avatar_updated_at": None, "display_name": None, **overrides,
    }


def _templates():
    return make_templates(SimpleNamespace(display_tz=None, app_version="test"))


def _render_base(user, path="/"):
    return _templates().env.get_template("base.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path=path)), user=user,
        csrf="test-csrf", csp_nonce="test-nonce", static_version="v1",
    )


@pytest.mark.parametrize("value,expected", [
    ("Ada", "Ada"),
    ("  Ada Lovelace\t\n", "Ada Lovelace"),
    ("　Ada ", "Ada"),
    ("", None),
    ("   ", None),
    ("x" * 100, "x" * 100),
    (" " + "x" * 100 + " ", "x" * 100),
    ("Zoë 山田", "Zoë 山田"),
    ("ops@home", "ops@home"),
    ("<script>", "<script>"),
])
def test_normalize_display_name_accepts(value, expected):
    assert normalize_display_name(value) == expected


@pytest.mark.parametrize("value", [
    "x" * 101,
    "Ada\x00", "Ada\x07Lovelace", "Ada\nLovelace", "Ada\x7f", "Ada\x85x",
    "Ada\u200bLovelace", "Ada\u200d", "\u202eAda", "Ada\u2066", "\ufeffAda",
    "Ada\ue000", "Ada\U000f0000", "Ada\ud800", "Ada\u2028Lovelace", "Ada\u2029Lovelace",
])
def test_normalize_display_name_rejects(value):
    with pytest.raises(DisplayNameError):
        normalize_display_name(value)


def test_account_user_prefers_display_name_and_keeps_the_email_fallback():
    unset = _account_user(_account())
    assert unset["name"] == "jane.doe"
    assert unset["display_name"] is None
    assert unset["email"] == "jane.doe@example.com"

    named = _account_user(_account(display_name="Jane @ Home"))
    assert named["name"] == "Jane @ Home"
    assert named["display_name"] == "Jane @ Home"
    assert named["email"] == "jane.doe@example.com"


@pytest.mark.parametrize("display_name,label,initials", [
    (None, "jane.doe", "J"),
    ("Jane Q Doe", "Jane Q Doe", "JD"),
    ("Cher", "Cher", "C"),
    ("ops@home", "ops@home", "O"),
    ("Jane @ Home", "Jane @ Home", "JH"),
    ("zoë ångström", "zoë ångström", "ZÅ"),
    ("<b>Bo</b> & Co", "&lt;b&gt;Bo&lt;/b&gt; &amp; Co", "&lt;C"),
])
def test_header_label_and_initials(display_name, label, initials):
    body = _render_base(_account_user(_account(display_name=display_name)))
    assert LABEL_RE.findall(body) == [label]
    assert set(INITIALS_RE.findall(body)) == {initials}
    assert f'aria-label="Account Settings, {label}"' in body
    assert "<b>Bo</b>" not in body


def test_legacy_provider_name_is_unchanged():
    body = _render_base({
        "id": None, "name": "Provider Person", "display_name": None,
        "email": "legacy@example.com", "is_admin": False, "has_avatar": False, "avatar_version": 0,
    })
    assert LABEL_RE.findall(body) == ["Provider Person"]
    assert set(INITIALS_RE.findall(body)) == {"PP"}


def _render_account_security(user, **context):
    return _templates().env.get_template("account_security.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/settings/account")),
        user=user, csrf="test-csrf-token", account_email="jane.doe@example.com",
        avatar_max_label="500 KB", **context,
    )


def test_profile_card_display_name_form():
    body = _render_account_security(
        _account_user(_account(display_name="Jane Doe")), display_name_value="Jane Doe")
    profile = body.split('class="card account-settings-card account-settings-profile"', 1)[1].split(
        'class="card account-settings-card account-settings-security"', 1)[0]
    assert 'id="account-display-name-heading">Display name</h4>' in profile
    assert '<form method="post" action="/settings/account/display-name" class="auth-form">' in profile
    assert '<input type="hidden" name="csrf_token" value="test-csrf-token">' in profile
    assert ('<input type="text" name="display_name" value="Jane Doe" maxlength="100" autocomplete="name"'
            in profile)
    assert 'aria-invalid' not in profile
    assert '<span class="account-avatar account-security-avatar-placeholder" aria-hidden="true">JD</span>' in profile


def test_profile_card_display_name_field_error_keeps_the_escaped_input():
    body = _render_account_security(
        _account_user(_account()), display_name_value='<i>"x"</i>',
        display_name_error="Keep it to 100 characters or fewer.")
    assert 'value="&lt;i&gt;&#34;x&#34;&lt;/i&gt;"' in body
    assert 'aria-describedby="account-display-name-hint account-display-name-error" aria-invalid="true"' in body
    assert '<span class="field-error" id="account-display-name-error">Keep it to 100 characters or fewer.</span>' in body
    assert LABEL_RE.findall(body) == ["jane.doe"]


def test_admin_list_display_name_column():
    from tests.test_admin_templates import _app
    import asyncio
    import httpx

    accounts = [
        {"id": 1, "display_name": None, "email": "admin@example.invalid", "email_verified": True,
         "is_admin": True, "is_enabled": True, "has_password": True, "has_oidc": False,
         "deletion_deadline": None},
        {"id": 2, "display_name": "<b>Member</b>", "email": "member@example.invalid", "email_verified": True,
         "is_admin": False, "is_enabled": True, "has_password": True, "has_oidc": False,
         "deletion_deadline": None},
    ]

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(accounts)), base_url="http://testserver",
        ) as client:
            return await client.get("/accounts")

    body = asyncio.run(check()).text
    head = body.split('<thead role="rowgroup">', 1)[1].split("</thead>", 1)[0]
    assert head.count('role="columnheader"') == 9
    assert '<th scope="col" role="columnheader">Display name</th>' in head
    rows = body.split('<tr role="row" class="admin-account-row')[1:]
    assert '<td role="cell" data-label="Display name"></td>' in rows[0]
    assert '<td role="cell" data-label="Display name">&lt;b&gt;Member&lt;/b&gt;</td>' in rows[1]
    assert "<b>Member</b>" not in body
