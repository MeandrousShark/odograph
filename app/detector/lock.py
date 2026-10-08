"""Per-account exclusion for detector, import and structural trip mutations."""
from __future__ import annotations

from app.account_context import account_id
from app.db import DETECTOR_ACCOUNT_LOCK_CLASS_ID, DETECTOR_ADVISORY_LOCK_KEY


async def lock_detector(conn) -> None:
    """Wait for this account's detector exclusion until the transaction ends.

    The deployed global key is held shared, so an exclusive holder (an older
    process during upgrade, scripts/sql/cleanup_test_device.sql) still
    excludes every account, while accounts no longer exclude each other. The
    global key is always taken first so waits cannot form a cycle.
    """
    await conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (DETECTOR_ADVISORY_LOCK_KEY,))
    await conn.execute(
        "SELECT pg_advisory_xact_lock(%s, hashtext(%s::text))",
        (DETECTOR_ACCOUNT_LOCK_CLASS_ID, account_id(conn)),
    )


async def try_lock_detector(conn) -> bool:
    """Non-blocking `lock_detector`; a held shared global key is harmless on failure."""
    cur = await conn.execute(
        "SELECT pg_try_advisory_xact_lock_shared(%s)", (DETECTOR_ADVISORY_LOCK_KEY,)
    )
    if not (await cur.fetchone())[0]:
        return False
    cur = await conn.execute(
        "SELECT pg_try_advisory_xact_lock(%s, hashtext(%s::text))",
        (DETECTOR_ACCOUNT_LOCK_CLASS_ID, account_id(conn)),
    )
    return (await cur.fetchone())[0]
