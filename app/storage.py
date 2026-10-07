"""Account-scoped logical storage status and capacity checks."""
from __future__ import annotations

from app.account_context import account_id


CAPACITY_PREFIX = "storage capacity exceeded:"


def is_storage_capacity_error(exc: BaseException) -> bool:
    """Recognize only the protected quota/grant refusal, not other SQL errors."""
    diag = getattr(exc, "diag", None)
    message = getattr(diag, "message_primary", None) or str(exc)
    return getattr(exc, "sqlstate", None) == "P0001" and message.startswith(CAPACITY_PREFIX)


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
    account_blocked = total >= account_limit
    raw_blocked = raw >= raw_limit
    enhancement_paused = (enhancement >= enhancement_limit or account_blocked
                          or discovery_paused or retry_paused or snap_paused)
    warning = (total * 5 >= account_limit * 4 or raw * 5 >= raw_limit * 4
               or enhancement * 5 >= enhancement_limit * 4)
    if account_blocked or raw_blocked or enhancement_paused:
        recovery = "Storage is at its allowance. Delete unneeded data or ask the operator to raise the limits."
    elif warning:
        recovery = "Storage is approaching its allowance. Review retained data or ask the operator to raise the limits."
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
        "SELECT u.actual_bytes+u.reserved_bytes+%s-%s<=g.account_limit_bytes "
        "AND u.enhancement_bytes+%s<=g.enhancement_limit_bytes "
        "FROM public.account_usage u JOIN public.storage_grants g USING(account_id) "
        "WHERE u.account_id=%s",
        (needed_bytes, account_credit_bytes, needed_bytes, owner),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("storage allowance unavailable")
    return bool(row[0])
