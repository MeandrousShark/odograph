"""Real portal, exact delivery and failed-preparation rollback contracts."""
from __future__ import annotations

import asyncio
import os
import threading
from datetime import date, datetime, timedelta

import psycopg
import pytest

from app.account_context import account_id
from app.digest_summary import DigestSummary, fetch_digest_summary
from app.email_digest import _render
from app.formatting import format_miles, format_usd
from app.rates import load_rates
from app.report import build_annual_report, build_range_report
from app.ui.reports import _fetch_range_trips_in
from test_email_digest_db import APP_URL, TZ, _insert_trip, _mailer, _with_pool, _worker

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="requires disposable database")


def test_real_nonheld_portal_batches_stable_ties_and_exact_complete_delivery(monkeypatch):
    observed = []
    original_fetch = psycopg.AsyncServerCursor.fetchmany

    async def fetch(cursor, size=0):
        if cursor.name.startswith("digest_"):
            assert size == 256
            meta = await (await cursor.connection.execute(
                "SELECT is_holdable, is_scrollable FROM pg_cursors WHERE name=%s", (cursor.name,)
            )).fetchone()
            assert meta == (False, False)
            rows = await original_fetch(cursor, size)
            assert len(rows) <= 256
            assert all(len(row) == 4 for row in rows)
            observed.append(len(rows))
            return rows
        return await original_fetch(cursor, size)

    monkeypatch.setattr(psycopg.AsyncServerCursor, "fetchmany", fetch)

    async def scenario(pool):
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM mileage_rates WHERE account_id=%s", (account_id(conn),))
            await conn.execute(
                "INSERT INTO mileage_rates(account_id,year,rate_per_mi,rate_h2_per_mi,h2_start_month) "
                "VALUES(%s,2025,.5,.75,7),(%s,2027,99,NULL,NULL)", (account_id(conn), account_id(conn)))
            for index in range(257):
                await _insert_trip(conn, datetime(2026, 6, 15, 18, tzinfo=TZ),
                                   ["business", "personal", "unclassified"][index % 3],
                                   [1e20, 1.0, 2e-20, 0.0][index % 4])
            await conn.execute("UPDATE trips SET exclusion='not_deductible' WHERE account_id=%s AND id %% 5=0", (account_id(conn),))
            await conn.execute("UPDATE trips SET exclusion='not_my_vehicle' WHERE account_id=%s AND id %% 7=0", (account_id(conn),))
            trips, rates = await _fetch_range_trips_in(conn, TZ, date(2026, 1, 1), date(2026, 12, 31))
            assert [t["id"] for t in trips] == sorted(t["id"] for t in trips)
            monthly = build_range_report(trips, rates, TZ, date(2026, 6, 1), date(2026, 6, 30))
            filing = build_annual_report(trips, rates, TZ, 2026)
            await conn.execute("SET LOCAL statement_timeout='7500ms'")
            summary = await fetch_digest_summary(conn, TZ, date(2026, 1, 1), date(2026, 12, 31))
            assert summary.business_m.hex() == filing.business_m.hex()
            assert summary.nondeductible_m.hex() == filing.nondeductible_m.hex()
            assert summary.total_deduction.hex() == filing.total_deduction.hex()
            assert (await (await conn.execute("SHOW statement_timeout")).fetchone())[0] == "7500ms"
            assert (await (await conn.execute("SELECT count(*) FROM pg_cursors WHERE name LIKE 'digest_%%'")).fetchone())[0] == 0
        calls = []
        await _worker(pool, _mailer(calls), monthly=True, filing=True).run_once(datetime(2027, 1, 15, 10, tzinfo=TZ))
        # Filing covers the populated year. Its complete body, subject and all links
        # match the original pure report; empty monthly mail remains unconditional.
        message = next(m for m in calls if "filing reminder" in m["Subject"])
        expected = _render("filing_reminder.txt", year=2026,
                           business_mi=format_miles(filing.business_m),
                           nondeductible_mi=format_miles(filing.nondeductible_m) if filing.nondeductible_m else "",
                           deduction=format_usd(filing.total_deduction),
                           report_url=f"{APP_URL}/report/2026", export_url=f"{APP_URL}/report/2026/export")
        assert message["Subject"] == "Odograph: 2026 filing reminder"
        assert message.get_content() == expected + ("" if expected.endswith("\n") else "\n")
        monthly_calls = []
        await _worker(pool, _mailer(monthly_calls), monthly=True).run_once(datetime(2026, 7, 1, 10, tzinfo=TZ))
        expected = _render("monthly_summary.txt", month_label="Jun 2026",
                           business_mi=format_miles(monthly.business_m),
                           nondeductible_mi=format_miles(monthly.nondeductible_m) if monthly.nondeductible_m else "",
                           deduction=format_usd(monthly.total_deduction), unclassified=monthly.caveats.unclassified_trips,
                           report_url=f"{APP_URL}/report/range?from=2026-06-01&to=2026-06-30")
        assert monthly_calls[0]["Subject"] == "Odograph: Jun summary"
        assert monthly_calls[0].get_content() == expected + ("" if expected.endswith("\n") else "\n")
        assert observed.count(256) == 3 and observed.count(1) == 3
    asyncio.run(_with_pool(scenario))


def test_real_statement_timeout_rolls_back_without_send_then_next_kind_and_hourly_retry(monkeypatch):
    original_fetch = psycopg.AsyncServerCursor.fetchmany
    fail = True

    async def fetch(cursor, size=0):
        nonlocal fail
        if cursor.name.startswith("digest_") and fail:
            fail = False
            await cursor.connection.execute("SET LOCAL statement_timeout='20ms'")
            await cursor.connection.execute("SELECT pg_sleep(2)")
        return await original_fetch(cursor, size)

    monkeypatch.setattr(psycopg.AsyncServerCursor, "fetchmany", fetch)

    async def scenario(pool):
        calls = []
        worker = _worker(pool, _mailer(calls), monthly=True, filing=True)
        now = datetime(2027, 1, 15, 10, tzinfo=TZ)
        first = await worker.run_turn(None, now)
        assert first.batch.failure_type == "QueryCanceled"
        assert calls == []
        async with pool.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM email_deliveries")).fetchone())[0] == 0
        second = await worker.run_turn(first.cursor, now)
        assert second.batch.failure_type is None and len(calls) == 1
        await worker.run_once(now + timedelta(hours=1))
        assert len(calls) == 2
        async with pool.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM email_deliveries")).fetchone())[0] == 2
    asyncio.run(_with_pool(scenario))


def test_repeated_cancel_during_real_fold_and_portal_close_retains_transaction_until_drain(monkeypatch):
    started, release, completed = threading.Event(), threading.Event(), threading.Event()
    original_fold = DigestSummary.fold
    original_close = psycopg.AsyncServerCursor.close
    close_started = None
    release_close = None

    def fold(summary, *args):
        started.set()
        assert release.wait(5)
        original_fold(summary, *args)
        completed.set()

    async def close(cursor):
        if cursor.name.startswith("digest_"):
            close_started.set()
            await release_close.wait()
        await original_close(cursor)

    monkeypatch.setattr(DigestSummary, "fold", fold)
    monkeypatch.setattr(psycopg.AsyncServerCursor, "close", close)

    async def scenario(pool):
        nonlocal close_started, release_close
        close_started, release_close = asyncio.Event(), asyncio.Event()
        async with pool.connection() as conn:
            await _insert_trip(conn, datetime(2026, 6, 15, 18, tzinfo=TZ), "business")
        calls = []
        waiter = asyncio.create_task(_worker(pool, _mailer(calls), monthly=True).run_once(datetime(2026, 7, 1, 10, tzinfo=TZ)))
        while not started.is_set():
            await asyncio.sleep(.001)
        waiter.cancel()
        for _ in range(3):
            waiter.cancel()
            await asyncio.sleep(.01)
        assert not waiter.done() and not completed.is_set() and not close_started.is_set()
        release.set()
        await close_started.wait()
        assert completed.is_set() and not waiter.done()
        waiter.cancel()
        await asyncio.sleep(.01)
        assert not waiter.done() and calls == []
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        async with pool.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM email_deliveries")).fetchone())[0] == 0
            assert (await (await conn.execute("SELECT count(*) FROM pg_cursors WHERE name LIKE 'digest_%%'")).fetchone())[0] == 0
    asyncio.run(_with_pool(scenario))


def test_completed_batch_after_aggregate_budget_expires_never_sends(monkeypatch):
    import app.digest_summary as module
    monkeypatch.setattr(module, "PREPARATION_SECONDS", .02)
    original_fold = DigestSummary.fold

    def fold(summary, *args):
        import time
        time.sleep(.05)
        original_fold(summary, *args)

    monkeypatch.setattr(DigestSummary, "fold", fold)

    async def scenario(pool):
        async with pool.connection() as conn:
            await _insert_trip(conn, datetime(2026, 6, 15, 18, tzinfo=TZ), "business")
        calls = []
        worker = _worker(pool, _mailer(calls), monthly=True)
        await worker.run_once(datetime(2026, 7, 1, 10, tzinfo=TZ))
        assert worker.status.last_failure_type == "DigestPreparationTimeout"
        assert calls == []
        async with pool.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM email_deliveries")).fetchone())[0] == 0
    asyncio.run(_with_pool(scenario))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e-16383", "maximum"])
def test_effective_numeric_rate_preserves_decimal_decoder_extremes_and_specials(value):
    async def scenario(pool):
        async with pool.connection() as conn:
            await _insert_trip(conn, datetime(2026, 6, 15, 18, tzinfo=TZ), "business", 1.0)
            await conn.execute("DELETE FROM mileage_rates WHERE account_id=%s", (account_id(conn),))
            if value == "maximum":
                await conn.execute(
                    "INSERT INTO mileage_rates(account_id,year,rate_per_mi,rate_h2_per_mi,h2_start_month) "
                    "VALUES(%s,2025,('9'||repeat('0',131071)||'.'||repeat('1',16383))::numeric,"
                    "('9'||repeat('0',131071)||'.'||repeat('1',16383))::numeric,7)", (account_id(conn),))
            else:
                await conn.execute(
                    "INSERT INTO mileage_rates(account_id,year,rate_per_mi) VALUES(%s,2026,%s::numeric)",
                    (account_id(conn), value))
            rates = await load_rates(conn)
            trips, _ = await _fetch_range_trips_in(conn, TZ, date(2026, 1, 1), date(2026, 12, 31))
            expected = build_annual_report(trips, rates, TZ, 2026)
            actual = await fetch_digest_summary(conn, TZ, date(2026, 1, 1), date(2026, 12, 31))
            from test_digest_summary import exact
            assert exact(actual.total_deduction) == exact(expected.total_deduction)
    asyncio.run(_with_pool(scenario))


def test_repeated_cancel_drains_real_query_acknowledgement_and_rollback(monkeypatch):
    query_started = cancel_started = release_cancel = rollback_started = release_rollback = None
    target_pid = None
    fail = True
    original_fetch = psycopg.AsyncServerCursor.fetchmany
    original_cancel = psycopg.AsyncConnection._try_cancel
    original_exit = psycopg.AsyncTransaction.__aexit__

    async def fetch(cursor, size=0):
        nonlocal target_pid, fail
        if cursor.name.startswith("digest_") and fail:
            fail = False
            target_pid = cursor.connection.info.backend_pid
            query_started.set()
            await cursor.connection.execute("SELECT pg_sleep(10)")
        return await original_fetch(cursor, size)

    async def cancel(conn, **kwargs):
        if conn.info.backend_pid == target_pid:
            cancel_started.set()
            await release_cancel.wait()
        await original_cancel(conn, **kwargs)

    async def exit_transaction(transaction, exc_type, exc_value, traceback):
        if transaction.pgconn.backend_pid == target_pid and exc_type is asyncio.CancelledError:
            rollback_started.set()
            await release_rollback.wait()
        return await original_exit(transaction, exc_type, exc_value, traceback)

    monkeypatch.setattr(psycopg.AsyncServerCursor, "fetchmany", fetch)
    monkeypatch.setattr(psycopg.AsyncConnection, "_try_cancel", cancel)
    monkeypatch.setattr(psycopg.AsyncTransaction, "__aexit__", exit_transaction)

    async def scenario(pool):
        nonlocal query_started, cancel_started, release_cancel, rollback_started, release_rollback
        query_started, cancel_started, release_cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()
        rollback_started, release_rollback = asyncio.Event(), asyncio.Event()
        calls = []
        worker = _worker(pool, _mailer(calls), monthly=True, filing=True)
        now = datetime(2027, 1, 15, 10, tzinfo=TZ)
        waiter = asyncio.create_task(worker.run_once(now))
        await query_started.wait()
        # Give execute a turn to send its genuine pg_sleep command.
        await asyncio.sleep(.02)
        waiter.cancel()
        await cancel_started.wait()
        for _ in range(3):
            waiter.cancel()
            await asyncio.sleep(.01)
        assert not waiter.done() and calls == []
        async with pool.admin_pool.connection() as observer:
            active = await (await observer.execute(
                "SELECT state,xact_start IS NOT NULL FROM pg_stat_activity WHERE pid=%s", (target_pid,)
            )).fetchone()
            assert active == ("active", True)
        release_cancel.set()
        await rollback_started.wait()
        waiter.cancel()
        await asyncio.sleep(.01)
        assert not waiter.done() and calls == []
        async with pool.admin_pool.connection() as observer:
            assert (await (await observer.execute(
                "SELECT xact_start IS NOT NULL FROM pg_stat_activity WHERE pid=%s", (target_pid,)
            )).fetchone())[0]
        release_rollback.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        async with pool.admin_pool.connection() as observer:
            assert (await (await observer.execute(
                "SELECT xact_start IS NULL FROM pg_stat_activity WHERE pid=%s", (target_pid,)
            )).fetchone())[0]
        assert (await worker.run_turn(2, now)).batch.failure_type is None
        assert len(calls) == 1 and "filing reminder" in calls[0]["Subject"]
    asyncio.run(_with_pool(scenario))
