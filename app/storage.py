"""Account-scoped logical storage status and capacity checks."""
from __future__ import annotations

from app.account_context import account_id


CAPACITY_PREFIX = "storage capacity exceeded:"
ACCOUNT_GRANT_REFUSAL = CAPACITY_PREFIX + " no funded account grant available"


def _capacity_message(exc: BaseException) -> str | None:
    if getattr(exc, "sqlstate", None) != "P0001":
        return None
    diag = getattr(exc, "diag", None)
    return getattr(diag, "message_primary", None) or str(exc)


def is_storage_capacity_error(exc: BaseException) -> bool:
    """Recognize only the protected quota/grant refusal, not other SQL errors."""
    message = _capacity_message(exc)
    return message is not None and message.startswith(CAPACITY_PREFIX)


def is_account_grant_refusal(exc: BaseException) -> bool:
    """Recognize the refusal of a new account when no storage grant is fundable."""
    return _capacity_message(exc) == ACCOUNT_GRANT_REFUSAL


async def account_grant_fundable(conn, account_default_bytes: int | None) -> bool:
    """Whether the instance budget could fund one more default account grant.

    Advisory only: it mirrors the account insert trigger but reserves nothing,
    so account creation still enforces the limit itself. Without a default
    account limit there is nothing to fund.
    """
    if account_default_bytes is None:
        return True
    cur = await conn.execute(
        "SELECT instance_budget_bytes, instance_reserve_bytes, total_grants_bytes "
        "FROM public.storage_instance_status()"
    )
    row = await cur.fetchone()
    if row is None:
        return False
    budget, reserve, total_grants = row
    return total_grants + account_default_bytes <= budget - reserve


async def storage_status(conn) -> dict[str, int | bool | str]:
    owner = account_id(conn)
    cur = await conn.execute(
        "SELECT u.actual_bytes,u.reserved_bytes,u.raw_bytes,u.enhancement_bytes,"
        "g.account_limit_bytes,g.raw_limit_bytes,g.enhancement_limit_bytes,"
        "d.capacity_paused,"
        "EXISTS(SELECT 1 FROM public.geocode_retry r WHERE r.account_id=u.account_id "
        "AND r.capacity_paused),"
        "EXISTS(SELECT 1 FROM public.trips t WHERE t.account_id=u.account_id "
        "AND t.snap_capacity_needed_bytes>0) "
        "FROM public.account_usage u JOIN public.storage_grants g USING(account_id) "
        "JOIN public.geocode_discovery d USING(account_id) "
        "WHERE u.account_id=%s", (owner,),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("storage status unavailable")
    (actual, reserved, raw, enhancement, account_limit, raw_limit,
     enhancement_limit, discovery_paused, retry_paused, snap_paused) = row
    total = actual + reserved
    # A NULL limit means no limit for that measure.
    def at(used, limit):
        return limit is not None and used >= limit

    def near(used, limit):
        return limit is not None and used * 5 >= limit * 4

    account_blocked = at(total, account_limit)
    raw_blocked = at(raw, raw_limit)
    enhancement_paused = (at(enhancement, enhancement_limit) or account_blocked
                          or discovery_paused or retry_paused or snap_paused)
    warning = (near(total, account_limit) or near(raw, raw_limit)
               or near(enhancement, enhancement_limit))
    if account_blocked or raw_blocked or enhancement_paused:
        recovery = "Storage is at its allowance. Delete unneeded data or ask an administrator to raise the limits."
    elif warning:
        recovery = "Storage is approaching its allowance. Review retained data or ask an administrator to raise the limits."
    else:
        recovery = ""
    return {
        "actual_bytes": actual,
        "reserved_bytes": reserved,
        "total_bytes": total,
        "raw_bytes": raw,
        "enhancement_bytes": enhancement,
        "account_limit_bytes": account_limit,
        "raw_limit_bytes": raw_limit,
        "enhancement_limit_bytes": enhancement_limit,
        "warning": warning,
        "account_blocked": account_blocked,
        "raw_blocked": raw_blocked,
        "enhancement_paused": enhancement_paused,
        "recovery": recovery,
    }


async def enhancement_available(conn, *, needed_bytes: int = 1,
                                account_credit_bytes: int = 0) -> bool:
    """Check whether an optional provider result could fit current allowances."""
    if needed_bytes < 0 or account_credit_bytes < 0:
        raise ValueError("storage capacity check requires nonnegative bytes")
    owner = account_id(conn)
    cur = await conn.execute(
        "SELECT COALESCE(u.actual_bytes+u.reserved_bytes+%s-%s<=g.account_limit_bytes,true) "
        "AND COALESCE(u.enhancement_bytes+%s<=g.enhancement_limit_bytes,true) "
        "FROM public.account_usage u JOIN public.storage_grants g USING(account_id) "
        "WHERE u.account_id=%s",
        (needed_bytes, account_credit_bytes, needed_bytes, owner),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("storage allowance unavailable")
    return bool(row[0])
