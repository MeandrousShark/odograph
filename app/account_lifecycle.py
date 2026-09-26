"""Restricted administrator account lifecycle operations."""
from __future__ import annotations

from collections.abc import Mapping

from psycopg import Error


class AccountLifecycleUnavailable(ValueError):
    def __init__(self):
        super().__init__("Account lifecycle operation unavailable.")


def _admin_identity(admin_user: Mapping) -> tuple[int, int]:
    if (not isinstance(admin_user, Mapping)
        or admin_user.get("is_admin") is not True
        or admin_user.get("is_enabled") is not True
        or type(admin_user.get("id")) is not int
        or not 1 <= admin_user["id"] <= 2**63 - 1
        or type(admin_user.get("auth_version")) is not int
        or not 1 <= admin_user["auth_version"] <= 2**63 - 1):
        raise AccountLifecycleUnavailable()
    return admin_user["id"], admin_user["auth_version"]


async def set_account_enabled(
    conn, admin_user: Mapping, target_account_id: int, *, enable: bool,
) -> str:
    """Return the protected transition's changed or idempotent outcome."""
    actor_id, auth_version = _admin_identity(admin_user)
    if (type(target_account_id) is not int
        or not 1 <= target_account_id <= 2**63 - 1
        or type(enable) is not bool):
        raise AccountLifecycleUnavailable()
    try:
        async with conn.transaction():
            await conn.execute("SET LOCAL lock_timeout = '5s'")
            await conn.execute("SET LOCAL statement_timeout = '15s'")
            cur = await conn.execute(
                "SELECT public.admin_set_account_enabled(%s,%s,%s,%s)",
                (actor_id, auth_version, target_account_id, enable),
            )
            outcome = (await cur.fetchone())[0]
    except Error:
        raise AccountLifecycleUnavailable() from None
    if outcome not in {"enabled", "disabled", "already_enabled", "already_disabled"}:
        raise AccountLifecycleUnavailable()
    return outcome


async def list_account_security_audit(conn, admin_user: Mapping) -> list[dict]:
    """Read bounded audit metadata through the protected administrator view."""
    actor_id, auth_version = _admin_identity(admin_user)
    try:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT * FROM public.list_account_security_audit(%s,%s)",
                (actor_id, auth_version),
            )
            columns = [column.name for column in cur.description]
            return [dict(zip(columns, row, strict=True)) for row in await cur.fetchall()]
    except Error:
        raise AccountLifecycleUnavailable() from None
