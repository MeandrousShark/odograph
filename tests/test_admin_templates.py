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


def _app():
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
                "accounts": [], "invitations": [{
                    "id": 11, "email": "invitee@example.invalid", "status": status,
                    "expires_at": expires_at,
                }] if page in ("expired", "open") else [],
                "audit_events": [],
                "multiuser_available": True,
                "notice": "", "deletion_deadline_label": "Not scheduled",
                "invite_result": {
                    "email": "invitee@example.invalid", "token": "one-time-secret",
                    "link": "https://odograph.example.invalid/invite#token=one-time-secret",
                    "email_status": "Email was not requested.",
                } if result else None,
            },
        )

    return app


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
