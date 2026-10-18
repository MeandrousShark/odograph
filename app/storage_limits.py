"""Administrator storage limit editor over protected control-role functions."""
from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import ROUND_HALF_EVEN, Decimal

from psycopg import Error

from app.storage import is_storage_capacity_error


GIB = 2 ** 30
LIMIT_MODES = ("default", "custom", "unlimited")
MIN_CUSTOM_LIMIT_GIB = Decimal("0.01")
# Whole bytes, rounded down. migrations/047_storage_limit_editor.sql refuses
# anything smaller, so keep the two in step.
MIN_CUSTOM_LIMIT_BYTES = int(MIN_CUSTOM_LIMIT_GIB * GIB)
# At most nine whole digits keeps the byte count inside bigint.
_GIB_RE = re.compile(r"[0-9]{1,9}(\.[0-9]{1,2})?")


class StorageLimitUnavailable(ValueError):
    def __init__(self):
        super().__init__("Storage limit operation unavailable.")


class StorageLimitBusy(ValueError):
    def __init__(self):
        super().__init__("Storage limits are busy.")


# lock_not_available (from lock_timeout) and deadlock_detected.
_BUSY_SQLSTATES = frozenset({"55P03", "40P01"})


class StorageLimitUnfunded(ValueError):
    def __init__(self):
        super().__init__("The instance storage budget cannot fund this limit.")


def _admin_identity(admin_user: Mapping) -> tuple[int, int]:
    if (not isinstance(admin_user, Mapping)
        or admin_user.get("is_admin") is not True
        or admin_user.get("is_enabled") is not True
        or type(admin_user.get("id")) is not int
        or not 1 <= admin_user["id"] <= 2**63 - 1
        or type(admin_user.get("auth_version")) is not int
        or not 1 <= admin_user["auth_version"] <= 2**63 - 1):
        raise StorageLimitUnavailable()
    return admin_user["id"], admin_user["auth_version"]


def parse_custom_gib(value: str) -> int | None:
    """Return the byte count for a GiB entry, or None if it is not a valid size.

    Accepts plain decimals with at most two places. Bytes are rounded down to
    a whole byte, so 0.01 GiB is MIN_CUSTOM_LIMIT_BYTES.
    """
    if type(value) is not str or not _GIB_RE.fullmatch(value):
        return None
    gib = Decimal(value)
    if gib < MIN_CUSTOM_LIMIT_GIB:
        return None
    return int(gib * GIB)


def max_custom_gib(fundable_bytes: int) -> Decimal:
    """The largest two-place GiB value whose byte count fits `fundable_bytes`."""
    # Largest k with int(k / 100 * GIB) <= fundable_bytes.
    hundredths = max(((fundable_bytes + 1) * 100 - 1) // GIB, 0)
    return Decimal(hundredths).scaleb(-2)


def limit_gib(limit_bytes: int) -> Decimal:
    """A byte limit as two-place GiB, nearest, so a saved entry reads back unchanged."""
    return (Decimal(limit_bytes) / GIB).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


async def list_account_storage(conn, admin_user: Mapping) -> dict[int, dict]:
    """Per-account grant, usage and fundable room, keyed by account ID."""
    actor_id, auth_version = _admin_identity(admin_user)
    try:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT * FROM public.storage_list_account_grants(%s,%s)",
                (actor_id, auth_version),
            )
            rows = await cur.fetchall()
    except Error as exc:
        if exc.sqlstate in _BUSY_SQLSTATES:
            raise StorageLimitBusy() from None
        raise StorageLimitUnavailable() from None
    return {
        row[0]: {
            "is_admin": row[1], "limit_mode": row[2], "account_limit_bytes": row[3],
            "default_limit_bytes": row[4], "used_bytes": row[5], "fundable_bytes": row[6],
        }
        for row in rows
    }


async def set_account_storage_limit(
    conn, admin_user: Mapping, target_account_id: int, mode: str,
    account_bytes: int | None = None,
) -> str:
    """Return 'changed' or 'unchanged'. The SQL function validates again."""
    actor_id, auth_version = _admin_identity(admin_user)
    if (type(target_account_id) is not int
        or not 1 <= target_account_id <= 2**63 - 1
        or mode not in LIMIT_MODES
        or (mode == "custom") != (account_bytes is not None)
        or (account_bytes is not None and (
            type(account_bytes) is not int
            or not MIN_CUSTOM_LIMIT_BYTES <= account_bytes <= 2**63 - 1))):
        raise StorageLimitUnavailable()
    try:
        async with conn.transaction():
            await conn.execute("SET LOCAL lock_timeout = '5s'")
            await conn.execute("SET LOCAL statement_timeout = '15s'")
            cur = await conn.execute(
                "SELECT public.storage_set_account_grant(%s,%s,%s,%s,%s)",
                (actor_id, auth_version, target_account_id, mode, account_bytes),
            )
            outcome = (await cur.fetchone())[0]
    except Error as exc:
        if exc.sqlstate in _BUSY_SQLSTATES:
            raise StorageLimitBusy() from None
        if is_storage_capacity_error(exc):
            raise StorageLimitUnfunded() from None
        raise StorageLimitUnavailable() from None
    if outcome not in {"changed", "unchanged"}:
        raise StorageLimitUnavailable()
    return outcome
