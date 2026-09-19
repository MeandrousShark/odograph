"""Explicit account fixtures for personal route regression tests."""
from __future__ import annotations

from fastapi import Request
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.account_context import account_id
from app.account_settings import AccountSettings, load_account_settings
from app.accounts import get_account
from app.auth import _ensure_csrf, require_user
from conftest import seed_tracking_device


async def fixture_device(conn, label: str) -> int:
    """Reuse a named fixture stream; same-label isolation tests create explicit IDs."""
    cur = await conn.execute(
        "SELECT id FROM tracking_devices WHERE account_id = %s AND label = %s ORDER BY id",
        (account_id(conn), label),
    )
    rows = await cur.fetchall()
    if len(rows) > 1:
        raise AssertionError("choose an explicit device ID for an ambiguous fixture label")
    if rows:
        return rows[0][0]
    return await seed_tracking_device(conn, label)


def configure_personal_app(app, pool) -> None:
    """Bind a real synthetic account for tests explicitly configured as DEV_NO_AUTH."""
    app.state.control_pool = pool.runtime_pool
    app.state.runtime_pool = pool.runtime_pool
    app.state.dev_principal = pool.principal

    async def synthetic_account(request: Request):
        if not request.app.state.config.dev_no_auth:
            return await require_user(request)
        async with pool.connection() as conn:
            settings = await load_account_settings(conn)
        async with pool.runtime_pool.connection() as conn:
            account = await get_account(conn, pool.principal.account_id)
        _ensure_csrf(request)
        request.state.principal = pool.principal
        request.state.account_pool = pool
        request.state.config = request.app.state.config
        request.state.account_settings = settings
        request.state.detector_runner = getattr(app.state, "detector_runner", None)
        return {
            "id": account["id"], "name": "test", "email": account["email"],
            "is_admin": account["is_admin"], "has_avatar": False,
            "legacy_oidc": False, "avatar_version": 0,
        }

    app.dependency_overrides[require_user] = synthetic_account


def personal_request(request):
    """Give a direct endpoint fixture the same explicit account state as dispatch."""
    app_state = request.app.state
    pool = app_state.pool
    config = getattr(app_state, "config", SimpleNamespace(display_tz=ZoneInfo("UTC")))
    request.state = SimpleNamespace(
        account_pool=pool, principal=pool.principal, config=config,
        account_settings=AccountSettings(display_tz=getattr(config, "display_tz", ZoneInfo("UTC"))),
        detector_runner=getattr(app_state, "detector_runner", None),
    )
    return request
