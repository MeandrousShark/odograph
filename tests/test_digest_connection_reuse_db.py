"""DB-backed regression tests for the monthly-summary/filing-reminder
connection reuse fix.

Before this fix, `_run_monthly_summary` and `_run_filing_reminder` held a
pool connection across their transaction *and* called `_fetch_range_trips`,
which acquired a second connection from the same pool. With a pool of
`max_size=1` that second acquisition can never be satisfied while the first
is still held, so it blocks until the pool's own acquisition timeout fires.
These tests run both kinds against a `max_size=1` pool to prove the digest
run now does all of its DB work on the single connection it already holds.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import pytest
from psycopg_pool import AsyncConnectionPool

from app.email_digest import EmailDigestWorker
from app.mailer import Mailer
from app.account_context import account_id
from conftest import reset_account_db, seed_tracking_device

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

TZ = ZoneInfo("America/Los_Angeles")
APP_URL = "https://miles.example.com"

# A pool acquisition that can't be satisfied should fail fast rather than
# hang for the library's 30s default, since the whole point of these tests
# is to fail loudly (PoolTimeout) rather than time out the test run itself
# if the connection-reuse fix regresses.
POOL_TIMEOUT_S = 3.0


async def _reset_schema(raw_pool):
    pool = await reset_account_db(raw_pool)
    async with pool.connection() as conn:
        await conn.execute("UPDATE vehicles SET active = false WHERE account_id=%s AND name = 'My Car'", (account_id(conn),))
        await seed_tracking_device(conn, "phone", device_id=1)
        await conn.execute(
            "UPDATE account_settings SET display_tz=%s,email_to='you@example.com',"
            "email_monthly_summary=true,email_filing_reminder=true WHERE account_id=%s",
            (str(TZ), account_id(conn)),
        )
    return pool


async def _insert_trip(
    conn, started_at: datetime, category: str = "business",
    distance_m: float = 1000.0, vehicle_id: int | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO trips (account_id, tracking_device_id, device, started_at, ended_at, distance_m, category, vehicle_id) "
        "VALUES (%s, 1, 'phone', %s, %s, %s, %s, %s)",
        (account_id(conn), started_at, started_at + timedelta(minutes=15), distance_m, category, vehicle_id),
    )


def _mailer(calls: list[EmailMessage]) -> Mailer:
    def transport(mailer: Mailer, message: EmailMessage) -> None:
        calls.append(message)

    return Mailer(
        host="smtp.example.com", port=587, username="", password="",
        security="none", tls_insecure=False,
        from_addr="odograph@example.com", to_addr="you@example.com",
        transport=transport,
    )


def _worker(pool, mailer, *, monthly=False, filing=False, filing_mmdd="01-15") -> EmailDigestWorker:
    return EmailDigestWorker(
        pool, mailer, APP_URL, TZ,
        nudge_weekly_hour=18, odometer_reminder_hour=9, digest_hour=9,
        filing_reminder_mmdd=filing_mmdd,
        email_weekly_nudge=False, email_monthly_summary=monthly,
        email_filing_reminder=filing, email_odometer_reminder=False,
    )


async def _with_single_connection_pool(scenario):
    """`min_size=1, max_size=1`: the same pool a digest run would deadlock
    or time out against if it ever tried to hold two connections at once.
    """
    pool = AsyncConnectionPool(
        TEST_DB, min_size=1, max_size=1, timeout=POOL_TIMEOUT_S, open=False,
    )
    await pool.open(wait=True)
    try:
        account_pool = await _reset_schema(pool)
        await scenario(account_pool)
    finally:
        await pool.close()


MONTHLY_NOW = datetime(2026, 7, 1, 10, tzinfo=TZ)  # covers June 2026
FILING_NOW = datetime(2027, 1, 15, 10, tzinfo=TZ)  # on/after the 01-15 boundary


async def _monthly_summary_pool_size_one_scenario(pool):
    calls: list[EmailMessage] = []
    async with pool.connection() as conn:
        await _insert_trip(conn, datetime(2026, 6, 15, 18, tzinfo=TZ), "business", 20 * 1609.344)

    worker = _worker(pool, _mailer(calls), monthly=True)
    await asyncio.wait_for(worker.run_once(MONTHLY_NOW), timeout=10)

    assert worker.status.last_failure_type is None
    assert len(calls) == 1
    assert "Jun 2026" in calls[0].get_content()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT sent FROM email_deliveries WHERE account_id=%s AND kind = 'monthly_summary'",
            (account_id(conn),),
        )
        assert await cur.fetchone() == (True,)


def test_monthly_summary_completes_on_a_pool_of_size_one():
    """Before the fix, `_run_monthly_summary` holding a connection while
    `_fetch_range_trips` tries to borrow a second one from a `max_size=1`
    pool would block until the pool's acquisition timeout raised
    `PoolTimeout`, caught by `_guarded` and recorded as a failure with no
    email sent and no ledger row. This asserts the run instead completes
    normally, on the single connection it already holds.
    """
    asyncio.run(_with_single_connection_pool(_monthly_summary_pool_size_one_scenario))


async def _filing_reminder_pool_size_one_scenario(pool):
    calls: list[EmailMessage] = []
    async with pool.connection() as conn:
        await _insert_trip(conn, datetime(2026, 6, 15, 18, tzinfo=TZ), "business", 100 * 1609.344)

    worker = _worker(pool, _mailer(calls), filing=True)
    await asyncio.wait_for(worker.run_once(FILING_NOW), timeout=10)

    assert worker.status.last_failure_type is None
    assert len(calls) == 1
    assert "2026" in calls[0]["Subject"]
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT sent FROM email_deliveries WHERE account_id=%s AND kind = 'filing_reminder'",
            (account_id(conn),),
        )
        assert await cur.fetchone() == (True,)


def test_filing_reminder_completes_on_a_pool_of_size_one():
    """Same regression as the monthly-summary test above, for
    `_run_filing_reminder`'s own `_fetch_range_trips` call.
    """
    asyncio.run(_with_single_connection_pool(_filing_reminder_pool_size_one_scenario))


async def _both_kinds_pool_size_one_scenario(pool):
    calls: list[EmailMessage] = []
    async with pool.connection() as conn:
        # Only needs to give `_fetch_range_trips_in` a row to return;
        # `monthly_summary` sends even for a zero-business-mile month (see
        # tests/test_email_digest_db.py), so this doesn't need to land in
        # either kind's specific covered period.
        await _insert_trip(conn, datetime(2026, 6, 15, 18, tzinfo=TZ), "business", 20 * 1609.344)

    worker = _worker(pool, _mailer(calls), monthly=True, filing=True)
    # Both `_run_monthly_summary` and `_run_filing_reminder` fire in this
    # single `run_once` -- FILING_NOW satisfies both boundary checks.
    await asyncio.wait_for(worker.run_once(FILING_NOW), timeout=10)

    assert worker.status.last_failure_type is None
    assert len(calls) == 2
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT kind, sent FROM email_deliveries WHERE account_id=%s ORDER BY kind",
            (account_id(conn),),
        )
        rows = await cur.fetchall()
        assert rows == [("filing_reminder", True), ("monthly_summary", True)]


def test_monthly_summary_and_filing_reminder_both_complete_on_a_pool_of_size_one():
    """Both digest kinds that used to double-acquire a pool connection run
    back to back in the same `run_once`, still against a `max_size=1` pool.
    """
    asyncio.run(_with_single_connection_pool(_both_kinds_pool_size_one_scenario))
