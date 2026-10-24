"""Settings saves time zone and notifications as two independent forms.

Each route must write only its own columns: a save from one form never
resets the other form's stored values to route defaults.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.account_settings import load_account_settings
from app.db import make_pool
from app.main import make_templates
from app.ui import make_router
from app.vehicles import get_auto_assign_default_vehicle, set_auto_assign_default_vehicle
from conftest import reset_account_db
from personal_support import configure_personal_app

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

CSRF_RE = re.compile(r'X-CSRF-Token": "([^"]+)"')

# Every notification column away from its route default, so a save that
# wrongly rewrote them from defaults would show.
SEEDED_NOTIFICATIONS = {
    "ntfy_topic": "alerts_1", "email_to": "alpha@example.test",
    "nudge_weekly_hour": "5", "odometer_reminder_requested": "1",
    "odometer_reminder_hour": "6", "email_weekly_nudge": "1",
    "email_monthly_summary": "1", "email_filing_reminder": "1",
    "email_odometer_reminder": "1", "email_digest_hour": "7",
    "email_filing_reminder_mmdd": "03-04",
}
SEEDED_VALUES = {
    "ntfy_topic": "alerts_1", "email_to": "alpha@example.test",
    "nudge_weekly_hour": 5, "odometer_reminder_requested": True,
    "odometer_reminder_hour": 6, "email_weekly_nudge": True,
    "email_monthly_summary": True, "email_filing_reminder": True,
    "email_odometer_reminder": True, "email_digest_hour": 7,
    "email_filing_reminder_mmdd": "03-04",
}


def _bare_app(pool) -> FastAPI:
    app = FastAPI()
    configure_personal_app(app, pool)
    app.state.config = SimpleNamespace(
        dev_no_auth=True, display_tz=timezone.utc,
        geocode_provider=None, app_version="test", app_git_revision="test",
    )
    app.state.templates = make_templates(app.state.config)
    app.add_middleware(SessionMiddleware, secret_key="test-secret", same_site="lax", https_only=False)
    app.include_router(make_router())
    return app


async def _settings(pool):
    async with pool.connection() as conn:
        return await load_account_settings(conn), await get_auto_assign_default_vehicle(conn)


def _notifications(settings) -> dict:
    return {name: getattr(settings, name) for name in SEEDED_VALUES}


async def _scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            await set_auto_assign_default_vehicle(conn, True)
        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            page = await client.get("/settings")
            headers = {"X-CSRF-Token": CSRF_RE.search(page.text).group(1)}

            # The combined route the old single form posted to is gone.
            combined = await client.post(
                "/settings/preferences", headers=headers,
                data={"display_timezone": "UTC", **SEEDED_NOTIFICATIONS},
            )
            assert combined.status_code in (404, 405)

            for url, data in (
                ("/settings/preferences/timezone", {"display_timezone": "Pacific/Auckland"}),
                ("/settings/preferences/notifications", SEEDED_NOTIFICATIONS),
            ):
                seeded = await client.post(url, headers=headers, data=data)
                assert seeded.status_code == 204, url
            settings, auto_assign = await _settings(pool)
            assert str(settings.display_tz) == "Pacific/Auckland"
            assert _notifications(settings) == SEEDED_VALUES
            assert auto_assign is True

            # Time zone form: only display_timezone changes.
            saved = await client.post(
                "/settings/preferences/timezone", headers=headers,
                data={"display_timezone": " America/Chicago "},
            )
            assert saved.status_code == 204
            assert saved.headers["HX-Redirect"] == "/settings"
            settings, auto_assign = await _settings(pool)
            assert str(settings.display_tz) == "America/Chicago"
            assert _notifications(settings) == SEEDED_VALUES
            assert auto_assign is True

            rejected = await client.post(
                "/settings/preferences/timezone", headers=headers,
                data={"display_timezone": "Not/AZone"},
            )
            assert rejected.status_code == 400
            assert rejected.json()["detail"] == "Invalid time zone"
            missing = await client.post("/settings/preferences/timezone", headers=headers, data={})
            assert missing.status_code == 422
            settings, _ = await _settings(pool)
            assert str(settings.display_tz) == "America/Chicago"
            assert _notifications(settings) == SEEDED_VALUES

            # Notifications form: unchecked boxes post nothing and turn off,
            # and a stray display_timezone is ignored.
            saved = await client.post(
                "/settings/preferences/notifications", headers=headers,
                data={
                    "display_timezone": "UTC", "ntfy_topic": " ", "email_to": "beta@example.test",
                    "nudge_weekly_hour": "20", "odometer_reminder_hour": "8",
                    "email_weekly_nudge": "1", "email_digest_hour": "10",
                    "email_filing_reminder_mmdd": "12-31",
                },
            )
            assert saved.status_code == 204
            settings, auto_assign = await _settings(pool)
            assert str(settings.display_tz) == "America/Chicago"
            assert _notifications(settings) == {
                "ntfy_topic": "", "email_to": "beta@example.test",
                "nudge_weekly_hour": 20, "odometer_reminder_requested": False,
                "odometer_reminder_hour": 8, "email_weekly_nudge": True,
                "email_monthly_summary": False, "email_filing_reminder": False,
                "email_odometer_reminder": False, "email_digest_hour": 10,
                "email_filing_reminder_mmdd": "12-31",
            }
            assert auto_assign is True

            for data, detail in (
                ({"ntfy_topic": "a b"}, "Invalid ntfy topic:"),
                ({"email_filing_reminder_mmdd": "02-30"}, "Invalid reminder month and day"),
                ({"email_to": "a@example.test\r\nBcc: b@example.test"}, "Invalid notification destination"),
            ):
                rejected = await client.post(
                    "/settings/preferences/notifications", headers=headers, data=data,
                )
                assert rejected.status_code == 400, data
                assert rejected.json()["detail"].startswith(detail)
            settings, _ = await _settings(pool)
            assert settings.email_to == "beta@example.test"
            assert settings.email_weekly_nudge is True

            # Both routes keep CSRF.
            for url in ("/settings/preferences/timezone", "/settings/preferences/notifications"):
                forged = await client.post(url, data={"display_timezone": "UTC"})
                assert forged.status_code == 403, url
            settings, _ = await _settings(pool)
            assert str(settings.display_tz) == "America/Chicago"
    finally:
        await raw_pool.close()


def test_time_zone_and_notification_saves_leave_each_other_unchanged():
    asyncio.run(_scenario())
