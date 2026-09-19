"""Real configuration and account bootstrap for authentication DB fixtures."""
from __future__ import annotations

import os
from dataclasses import replace
from unittest.mock import patch

from app.accounts import create_admin
from app.config import Config


def auth_config(database_url, **overrides):
    with patch.dict(os.environ, {
        "DATABASE_URL": database_url, "SESSION_SECRET": "test-auth-secret",
    }, clear=True):
        config = Config.from_env()
    return replace(config, **overrides)


async def seed_auth_account(conn, *, owner_id=1, email="admin@example.com", password_hash="hash"):
    """Full owned bootstrap on current schema; explicit identity on old fixtures."""
    cur = await conn.execute("SELECT to_regclass('public.account_settings')")
    if (await cur.fetchone())[0] is None:
        cur = await conn.execute(
            "INSERT INTO accounts (id,email,password_hash) VALUES (%s,%s,%s) RETURNING id",
            (owner_id,email,password_hash),
        )
        return (await cur.fetchone())[0]
    await conn.execute("SELECT setval(pg_get_serial_sequence('accounts','id'),%s,false)",(owner_id,))
    account = await create_admin(conn,email,password_hash)
    return account["id"]
