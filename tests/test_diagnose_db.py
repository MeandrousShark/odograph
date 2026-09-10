"""DB-backed integration test for the diagnostics report wired into the
Settings page and its on-demand connectivity route (app/ui/settings.py +
app/diagnose.py): a real, migrated database plus a real worker's
`WorkerStatus` history, exercised through the actual HTTP routes.
Report-content logic itself (and the no-secrets property) is covered
without a database in tests/test_diagnose.py; this only proves the wiring.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.db import make_pool
from app.main import make_templates
from app.retention import RetentionWorker
from app.ui import make_router
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

REPO_ROOT = Path(__file__).resolve().parents[1]

CSRF_RE = re.compile(r'X-CSRF-Token": "([^"]+)"')


def _bare_app(pool, retention_worker=None) -> FastAPI:
    app = FastAPI()
    app.state.pool = pool
    # Every field config_presence()/worker gating could touch, all empty/off
    # -- OSRM, the geocoder, ntfy, and SMTP all read as "not configured".
    app.state.config = SimpleNamespace(
        dev_no_auth=True, display_tz=timezone.utc,
        geocode_provider=None, app_version="test", app_git_revision="test",
        osrm_url="", ntfy_url="", ntfy_topic="", ntfy_token="",
        ntfy_username="", ntfy_password="", email_enabled=False,
        smtp_username="", smtp_password="", smtp_host="", smtp_port=587,
        smtp_security="starttls", smtp_tls_insecure=False,
        oidc_configured=False, initial_admin_signup=False, app_url="",
    )
    app.state.templates = make_templates(app.state.config)
    if retention_worker is not None:
        app.state.retention_worker = retention_worker
    app.add_middleware(SessionMiddleware, secret_key="test-secret", same_site="lax", https_only=False)
    app.include_router(make_router())
    return app


async def _scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)

        retention_worker = RetentionWorker(pool, retention_days=365)
        # Runs the same guarded path the real background loop uses
        # (app/worker.py's `_run_guarded`), just without starting the loop
        # -- this is what actually populates `WorkerStatus`.
        await retention_worker._run_guarded()

        transport = httpx.ASGITransport(app=_bare_app(pool, retention_worker))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
        ) as client:
            settings_page = await client.get("/settings")
            assert settings_page.status_code == 200
            body = settings_page.text

            assert "up_to_date" in body  # every shipped migration just ran
            assert ">retention<" in body
            assert ">detector<" in body  # absent from app.state -- reports disabled
            diagnostics = body.split("<h2>Diagnostics</h2>", 1)[1]
            workers_section = diagnostics.split("<h3>Workers</h3>", 1)[1].split(
                "<h3>Configuration presence</h3>", 1
            )[0]
            assert "detector" in workers_section
            detector_row = workers_section.split("<td>detector</td>", 1)[1].split("</tr>", 1)[0]
            assert "disabled" in detector_row
            retention_row = workers_section.split("<td>retention</td>", 1)[1].split("</tr>", 1)[0]
            assert "disabled" not in retention_row

            csrf = CSRF_RE.search(body).group(1)
            check_now = await client.post(
                "/settings/diagnostics/check", headers={"X-CSRF-Token": csrf},
            )
            assert check_now.status_code == 200
            assert check_now.text.count("not configured") == 4

            no_csrf = await client.post("/settings/diagnostics/check")
            assert no_csrf.status_code == 403
    finally:
        await pool.close()


def test_settings_page_and_check_now_route_report_real_worker_and_db_state():
    asyncio.run(_scenario())


def test_python_dash_m_app_diagnose_runs_end_to_end_against_a_real_database():
    """The in-container fallback (D6/item 5): no page to click, so this has
    to work standalone. No OSRM/geocoder/ntfy/SMTP env vars are set here, so
    `run_connectivity_checks` reports all four "not configured" without any
    outbound network call -- this test only needs the database.
    """
    env = {
        **os.environ,
        "DATABASE_URL": TEST_DB,
        "INGEST_PASSWORD": "ingest-password",
        "SESSION_SECRET": "session-secret",
        "APP_VERSION": "test-version",
        "APP_GIT_REVISION": "test-revision",
    }
    for key in ("OSRM_URL", "GEOCODE_API_KEY", "GEOCODE_PROVIDER", "NTFY_URL", "NTFY_TOPIC", "SMTP_HOST"):
        env.pop(key, None)

    result = subprocess.run(
        [sys.executable, "-m", "app.diagnose"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "app_version: test-version" in result.stdout
    assert "database: ok" in result.stdout
    assert "config presence:" in result.stdout
    assert "connectivity (checked now):" in result.stdout
    assert result.stdout.count("not configured") == 4
