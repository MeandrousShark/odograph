"""Real configuration and account bootstrap for authentication DB fixtures."""
from __future__ import annotations

import os
from dataclasses import replace
from unittest.mock import patch

from psycopg import errors

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


async def bind_auth_test_roles(admin_pool):
    """Attach the real restricted application pools to a privileged test pool."""
    from conftest import restricted_role_pools

    pools = await restricted_role_pools(admin_pool)
    async with pools.control.connection() as conn:
        role = (await (await conn.execute("SELECT session_user")).fetchone())[0]
        if role != "odograph_control":
            raise AssertionError("auth fixture did not receive the restricted control role")
        try:
            async with conn.transaction():
                await conn.execute("SELECT * FROM trips")
        except errors.InsufficientPrivilege:
            pass
        else:
            raise AssertionError("restricted control role can read personal trip rows")
    admin_pool.control_pool = pools.control
    admin_pool.runtime_pool = pools.runtime
    return pools
