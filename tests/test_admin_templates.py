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
                "multiuser_available": True,
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
    assert 'colspan="8"' in schedule_row
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
