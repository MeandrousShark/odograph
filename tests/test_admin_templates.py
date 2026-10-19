"""Account administration shell and one-response invitation output."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, Request

from app.main import make_templates
from tests.auth_db_fixtures import auth_config


def _user(*, admin: bool):
    return {
        "id": 1, "name": "Sample User", "email": "sample@example.invalid",
        "is_admin": admin, "has_avatar": False, "avatar_version": 0,
    }


def _app(accounts=None):
    cfg = auth_config("postgresql://test:test@127.0.0.1/mileage")
    app = FastAPI()
    app.state.templates = make_templates(cfg)

    @app.get("/member")
    async def render_member(request: Request):
        request.state.config = cfg
        return app.state.templates.TemplateResponse(
            request, "base.html", {
                "user": _user(admin=False), "csrf": "csrf", "review_count": 0,
            },
        )

    @app.get("/{page}")
    async def render(request: Request, page: str):
        request.state.config = cfg
        result = page == "result"
        status = "Expired" if page == "expired" else "Open"
        expires_at = datetime.now(timezone.utc) - timedelta(hours=1) if status == "Expired" else datetime.now(timezone.utc) + timedelta(hours=1)
        return app.state.templates.TemplateResponse(
            request,
            "admin_accounts.html",
            {
                "user": _user(admin=True), "csrf": "csrf", "review_count": 0,
                "accounts": accounts or [], "invitations": [{
                    "id": 11, "email": "invitee@example.invalid", "status": status,
                    "expires_at": expires_at,
                }] if page in ("expired", "open") else [],
                "audit_events": [],
                "actor_account": {"has_password": True, "has_oidc": True},
                "oidc_purge_available": True,
                "notice": "", "deletion_deadline_label": "Not scheduled",
                "invite_result": {
                    "email": "invitee@example.invalid", "token": "one-time-secret",
                    "link": "https://odograph.example.invalid/invite#token=one-time-secret",
                    "email_status": "Email was not requested.",
                } if result else None,
            },
        )

    return app


def test_account_deletion_actions_stay_with_their_target_in_full_width_rows():
    now = datetime.now(timezone.utc)
    accounts = [
        {
            "id": 1, "email": "admin@example.invalid", "email_verified": True,
            "is_admin": True, "is_enabled": True, "has_password": True,
            "has_oidc": True, "deletion_deadline": None, "purge_available": False,
        },
        {
            "id": 2, "email": "enabled-member@example.invalid", "email_verified": True,
            "is_admin": False, "is_enabled": True, "has_password": True,
            "has_oidc": False, "deletion_deadline": None, "purge_available": False,
        },
        {
            "id": 3, "email": "grace-member@example.invalid", "email_verified": True,
            "is_admin": False, "is_enabled": False, "has_password": True,
            "has_oidc": True, "deletion_deadline": now + timedelta(days=3),
            "purge_available": False,
        },
        {
            "id": 4, "email": "purge-member@example.invalid", "email_verified": True,
            "is_admin": False, "is_enabled": False, "has_password": True,
            "has_oidc": False, "deletion_deadline": now - timedelta(days=1),
            "purge_available": True,
        },
        {
            "id": 5, "email": "disabled-member@example.invalid", "email_verified": False,
            "is_admin": False, "is_enabled": False, "has_password": False,
            "has_oidc": True, "deletion_deadline": None, "purge_available": False,
        },
    ]

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(accounts)), base_url="http://testserver",
        ) as client:
            return await client.get("/accounts")

    page = asyncio.run(check())
    body = page.text
    assert page.status_code == 200
    deletion_rows = body.split('<tr role="row" class="admin-account-deletion-row">')[1:]
    assert len(deletion_rows) == 4

    schedule_row = deletion_rows[0].split("</tr>", 1)[0]
    assert 'colspan="10"' in schedule_row
    assert "Schedule deletion for enabled-member@example.invalid" in schedule_row
    assert 'action="/admin/accounts/2/deletion"' in schedule_row
    assert 'name="target_email"' in schedule_row
    assert 'name="acknowledge" value="1" required' in schedule_row
    assert "Target account: <strong>enabled-member@example.invalid</strong>" in schedule_row

    grace_row = deletion_rows[1].split("</tr>", 1)[0]
    assert "Target account: <strong>grace-member@example.invalid</strong>" in grace_row
    assert 'action="/admin/accounts/3/deletion/cancel"' in grace_row
    assert "Cancel deletion and re-enable" in grace_row
    assert "<details" not in grace_row

    purge_row = deletion_rows[2].split("</tr>", 1)[0]
    assert "Permanently purge purge-member@example.invalid" in purge_row
    assert 'action="/settings/account/oidc/reauth"' in purge_row
    assert 'name="action" value="purge_account"' in purge_row
    assert 'name="target" value="4"' in purge_row
    assert 'action="/admin/accounts/4/purge"' in purge_row
    assert 'name="confirm_purge" value="1" required' in purge_row
    assert "name=\"current_password\"" in purge_row

    disabled_row = deletion_rows[3].split("</tr>", 1)[0]
    assert "Schedule deletion for disabled-member@example.invalid" in disabled_row
    assert 'action="/admin/accounts/5/deletion"' in disabled_row


def test_admin_navigation_is_only_rendered_for_administrators():
    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()), base_url="http://testserver",
        ) as client:
            admin_page = await client.get("/accounts")
            member_page = await client.get("/member")
            return admin_page, member_page

    admin_page, member_page = asyncio.run(check())
    assert 'href="/admin/accounts"' in admin_page.text
    assert "Accounts</span>" in admin_page.text
    assert 'href="/admin/accounts"' not in member_page.text


def test_invitation_token_is_scoped_to_the_one_time_result_template():
    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()), base_url="http://testserver",
        ) as client:
            result = await client.get("/result")
            later_list = await client.get("/accounts")
            return result, later_list

    result, later_list = asyncio.run(check())
    assert 'value="https://odograph.example.invalid/invite#token=one-time-secret"' in result.text
    assert 'value="one-time-secret"' in result.text
    assert "one-time-secret" not in later_list.text
    assert "token_digest" not in later_list.text


def test_expired_invitations_do_not_offer_the_atomic_open_resend_action():
    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()), base_url="http://testserver",
        ) as client:
            expired = await client.get("/expired")
            opened = await client.get("/open")
            return expired, opened

    expired, opened = asyncio.run(check())
    assert "Expired" in expired.text
    assert '/admin/invitations/11/resend' not in expired.text
    assert '/admin/invitations/11/resend' in opened.text


def _storage_account(account_id, *, admin=False, mode="default", limit=None, default=None,
                     used=0, max_gib="5.00", custom_gib=None):
    storage = {
        "is_admin": admin, "limit_mode": mode, "account_limit_bytes": limit,
        "default_limit_bytes": default, "used_bytes": used, "fundable_bytes": 0,
        "max_gib": max_gib,
    }
    if custom_gib is not None:
        storage["custom_gib"] = custom_gib
    return {
        "id": account_id, "email": f"account-{account_id}@example.invalid", "email_verified": True,
        "is_admin": admin, "is_enabled": True, "has_password": True, "has_oidc": False,
        "deletion_deadline": None, "purge_available": False, "storage": storage,
    }


def _storage_rows(accounts):
    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(accounts)), base_url="http://testserver",
        ) as client:
            return await client.get("/accounts")

    page = asyncio.run(check())
    assert page.status_code == 200
    rows = page.text.split('<tr role="row" class="admin-account-row')[1:]
    return page.text, [row.split("</tr>", 1)[0] for row in rows], rows


def test_storage_column_shows_usage_and_each_limit_mode():
    gib, mib = 1024 ** 3, 1024 ** 2
    body, cells, _ = _storage_rows([
        _storage_account(1, admin=True, mode="unlimited", default=2 * gib, used=3 * mib),
        _storage_account(2, mode="unlimited", default=2 * gib),
        _storage_account(3, mode="default", limit=2 * gib, default=2 * gib, used=512),
        _storage_account(4, mode="custom", limit=int(1.5 * gib), default=2 * gib, custom_gib="1.50"),
        _storage_account(5, mode="custom", limit=10737418, default=2 * gib, used=11 * mib,
                         custom_gib="0.01"),
        _storage_account(6, mode="default", limit=None, default=None),
    ])
    assert "3.0 MiB used" in cells[0] and "No limit (administrator)" in cells[0]
    assert "No limit" in cells[1] and "Instance default" not in cells[1]
    assert "512 bytes used" in cells[2] and "Instance default: 2.00 GiB" in cells[2]
    assert "Custom: 1.50 GiB" in cells[3]
    assert "Over allowance" in cells[4] and "11.0 MiB used" in cells[4]
    assert all("Over allowance" not in cell for index, cell in enumerate(cells) if index != 4)
    assert "Instance default: no limit" in cells[5]
    assert "Unavailable" not in body


def test_storage_limits_read_in_gib_while_usage_keeps_its_own_unit():
    mib = 1024 ** 2
    body, cells, _ = _storage_rows([
        _storage_account(2, mode="custom", limit=50 * mib, default=50 * mib, used=1037968,
                         custom_gib="0.05"),
    ])
    assert "1013.6 KiB used" in cells[0]
    assert "Custom: 0.05 GiB" in cells[0]
    assert '<option value="default">Instance default (0.05 GiB)</option>' in body


def test_storage_editor_is_offered_for_members_only():
    gib = 1024 ** 3
    body, _, rows = _storage_rows([
        _storage_account(1, admin=True, mode="unlimited", default=gib),
        _storage_account(7, admin=True, mode="unlimited", default=gib),
        _storage_account(2, mode="custom", limit=gib, default=gib, max_gib="3.25", custom_gib="1.00"),
        _storage_account(3, mode="default", limit=gib, default=gib, max_gib="0.00"),
    ])
    assert 'action="/admin/accounts/1/storage-limit"' not in body
    assert 'action="/admin/accounts/7/storage-limit"' not in body
    member = rows[2]
    assert 'action="/admin/accounts/2/storage-limit"' in member
    assert '<input type="hidden" name="csrf_token" value="csrf">' in member
    assert '<option value="custom" selected>Custom</option>' in member
    assert '<option value="default">Instance default (1.00 GiB)</option>' in member
    assert '<option value="unlimited">No limit</option>' in member
    assert ('<input type="number" name="account_gib" step="0.01" min="0.01" max="3.25" '
            'inputmode="decimal" value="1.00">') in member
    assert "Up to 3.25 GiB fits" in member
    assert "<label>Storage limit" in member and "<label>Custom limit in GiB" in member
    full = rows[3]
    assert '<option value="default" selected>' in full
    assert 'max="0.00" inputmode="decimal">' in full
    assert "no room for a custom limit" in full


def test_storage_column_tolerates_missing_storage_data():
    body, cells, _ = _storage_rows([{
        "id": 2, "email": "member@example.invalid", "email_verified": True, "is_admin": False,
        "is_enabled": True, "has_password": True, "has_oidc": False, "deletion_deadline": None,
        "purge_available": False,
    }])
    assert '<span class="muted">Unavailable</span>' in cells[0]
    assert "/storage-limit" not in body
