"""DB-backed delivery-ledger tests for EmailDigestWorker. Mirrors
tests/test_nudge_db.py's and tests/test_odometer_reminder_db.py's structure
against the shared `email_deliveries` ledger (migration 016) instead of
each kind's own table.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import pytest

from app.db import make_pool
from app.email_digest import EmailDigestWorker
from app.mailer import Mailer
from app.rates import load_rates
from app.report import build_range_report
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

TZ = ZoneInfo("America/Los_Angeles")
APP_URL = "https://miles.example.com"


async def _reset_schema(pool) -> None:
    await reset_db(pool)
    # migrations/008_vehicles.sql seeds an active default vehicle ("My Car")
    # that would otherwise always look "due" to the quarterly-odometer
    # kind's scenarios below -- deactivate it so each scenario's assertions
    # are only about the vehicles it explicitly creates (same fix
    # tests/test_odometer_reminder_db.py applies for the ntfy worker).
    async with pool.connection() as conn:
        await conn.execute("UPDATE vehicles SET active = false WHERE name = 'My Car'")


async def _insert_trip(
    conn, started_at: datetime, category: str = "unclassified",
    distance_m: float = 1000.0, vehicle_id: int | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO trips (device, started_at, ended_at, distance_m, category, vehicle_id) "
        "VALUES ('phone', %s, %s, %s, %s, %s)",
        (started_at, started_at + timedelta(minutes=15), distance_m, category, vehicle_id),
    )


async def _create_vehicle(conn, name: str, active: bool = True) -> int:
    cur = await conn.execute(
        "INSERT INTO vehicles (name, active) VALUES (%s, %s) RETURNING id", (name, active)
    )
    return (await cur.fetchone())[0]


async def _insert_reading(conn, vehicle_id: int, recorded_at: datetime, mi: float) -> None:
    await conn.execute(
        "INSERT INTO odometer_readings (vehicle_id, recorded_at, odometer_m) VALUES (%s, %s, %s)",
        (vehicle_id, recorded_at, mi * 1609.344),
    )


def _mailer(calls: list[EmailMessage], raise_for: str | None = None) -> Mailer:
    def transport(mailer: Mailer, message: EmailMessage) -> None:
        calls.append(message)
        if raise_for is not None and raise_for in str(message["Subject"]):
            raise RuntimeError("smtp relay unreachable")

    return Mailer(
        host="smtp.example.com", port=587, username="", password="",
        security="none", tls_insecure=False,
        from_addr="odograph@example.com", to_addr="you@example.com",
        transport=transport,
    )


def _worker(
    pool, mailer, *,
    weekly=False, monthly=False, filing=False, odometer=False,
    filing_mmdd="01-15",
) -> EmailDigestWorker:
    return EmailDigestWorker(
        pool, mailer, APP_URL, TZ,
        nudge_weekly_hour=18, odometer_reminder_hour=9, digest_hour=9,
        filing_reminder_mmdd=filing_mmdd,
        email_weekly_nudge=weekly, email_monthly_summary=monthly,
        email_filing_reminder=filing, email_odometer_reminder=odometer,
    )


async def _with_pool(scenario):
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        await scenario(pool)
    finally:
        await pool.close()


# --- weekly_nudge --------------------------------------------------------

WINDOW_END = datetime(2026, 7, 12, 18, tzinfo=TZ)


async def _weekly_nudge_dedup_scenario(pool):
    calls: list[EmailMessage] = []
    async with pool.connection() as conn:
        await _insert_trip(conn, WINDOW_END - timedelta(days=1))
        await _insert_trip(conn, WINDOW_END - timedelta(days=8))  # outside window
        await _insert_trip(conn, WINDOW_END - timedelta(days=1), "business")

    worker = _worker(pool, _mailer(calls), weekly=True)
    await worker.run_once(WINDOW_END + timedelta(hours=1))
    await worker.run_once(WINDOW_END + timedelta(days=1))

    assert len(calls) == 1
    assert "unclassified" in calls[0]["Subject"].lower()
    assert f"{APP_URL}/review" in calls[0].get_content()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT sent FROM email_deliveries WHERE kind = 'weekly_nudge' AND period_end = %s",
            (WINDOW_END,),
        )
        assert await cur.fetchone() == (True,)


def test_weekly_nudge_sends_once_per_window_and_dedups_on_retry():
    asyncio.run(_with_pool(_weekly_nudge_dedup_scenario))


async def _weekly_nudge_zero_count_scenario(pool):
    calls: list[EmailMessage] = []
    worker = _worker(pool, _mailer(calls), weekly=True)
    await worker.run_once(WINDOW_END + timedelta(hours=1))

    assert calls == []
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT sent FROM email_deliveries WHERE kind = 'weekly_nudge' AND period_end = %s",
            (WINDOW_END,),
        )
        assert await cur.fetchone() == (False,)


def test_weekly_nudge_writes_ledger_row_without_sending_when_nothing_unclassified():
    asyncio.run(_with_pool(_weekly_nudge_zero_count_scenario))


# --- monthly_summary -------------------------------------------------------

MONTHLY_NOW = datetime(2026, 7, 1, 10, tzinfo=TZ)  # covers June 2026


async def _monthly_summary_figures_scenario(pool):
    calls: list[EmailMessage] = []
    async with pool.connection() as conn:
        truck_id = await _create_vehicle(conn, "Truck")
        await _insert_trip(
            conn, datetime(2026, 6, 15, 18, tzinfo=TZ), "business",
            20 * 1609.344, truck_id,
        )
        await _insert_trip(conn, datetime(2026, 6, 20, 18, tzinfo=TZ), "unclassified")
        # Outside June -- must not affect June's figures.
        await _insert_trip(
            conn, datetime(2026, 7, 5, 18, tzinfo=TZ), "business", 99 * 1609.344, truck_id,
        )

    worker = _worker(pool, _mailer(calls), monthly=True)
    await worker.run_once(MONTHLY_NOW)

    assert len(calls) == 1
    body = calls[0].get_content()
    assert "Jun 2026" in body
    assert "Business miles: 20.0" in body
    assert "Unclassified trips: 1" in body
    assert f"{APP_URL}/report/range?from=2026-06-01&to=2026-06-30" in body

    from datetime import date

    from psycopg.rows import dict_row

    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT id, device, source::text AS source, started_at, ended_at, "
            "distance_m AS display_distance_m, category::text AS category, "
            "vehicle_id, false AS has_gap, 'ok'::text AS snap_status "
            "FROM trips ORDER BY started_at"
        )
        trips = await cur.fetchall()
        rates = await load_rates(conn)
    expected = build_range_report(trips, rates, TZ, date(2026, 6, 1), date(2026, 6, 30))
    assert f"Business miles: {expected.business_m / 1609.344:.1f}" in body
    assert f"Deduction: ${expected.total_deduction:,.2f}" in body


def test_monthly_summary_figures_match_build_range_report():
    asyncio.run(_with_pool(_monthly_summary_figures_scenario))


async def _monthly_summary_always_sends_scenario(pool):
    calls: list[EmailMessage] = []
    worker = _worker(pool, _mailer(calls), monthly=True)
    await worker.run_once(MONTHLY_NOW)

    assert len(calls) == 1
    assert "Business miles: 0.0" in calls[0].get_content()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT sent FROM email_deliveries WHERE kind = 'monthly_summary'"
        )
        assert await cur.fetchone() == (True,)


def test_monthly_summary_sends_even_for_a_zero_business_mile_month():
    asyncio.run(_with_pool(_monthly_summary_always_sends_scenario))


# --- filing_reminder ---------------------------------------------------

FILING_NOW = datetime(2027, 1, 15, 10, tzinfo=TZ)  # on/after the 01-15 boundary


async def _filing_reminder_scenario(pool):
    calls: list[EmailMessage] = []
    async with pool.connection() as conn:
        truck_id = await _create_vehicle(conn, "Truck")
        await _insert_trip(
            conn, datetime(2026, 6, 15, 18, tzinfo=TZ), "business", 100 * 1609.344, truck_id,
        )

    worker = _worker(pool, _mailer(calls), filing=True)
    await worker.run_once(FILING_NOW)
    await worker.run_once(FILING_NOW + timedelta(days=1))  # same year: no-op

    assert len(calls) == 1
    message = calls[0]
    assert not message.is_multipart()  # decision 4: link, never attach
    body = message.get_content()
    assert "2026" in message["Subject"]
    assert f"{APP_URL}/report/2026" in body
    assert f"{APP_URL}/report/2026/export" in body
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM email_deliveries WHERE kind = 'filing_reminder'"
        )
        assert await cur.fetchone() == (1,)


def test_filing_reminder_sends_once_per_year_with_both_links_and_no_attachment():
    asyncio.run(_with_pool(_filing_reminder_scenario))


# --- quarterly_odometer -------------------------------------------------

QUARTER_START = datetime(2026, 7, 1, 9, tzinfo=TZ)


async def _quarterly_odometer_due_scenario(pool):
    calls: list[EmailMessage] = []
    async with pool.connection() as conn:
        truck_id = await _create_vehicle(conn, "Truck")
        await _create_vehicle(conn, "Retired Truck", active=False)
        await _insert_reading(conn, truck_id, QUARTER_START - timedelta(days=5), 1000)

    worker = _worker(pool, _mailer(calls), odometer=True)
    await worker.run_once(QUARTER_START + timedelta(hours=1))
    await worker.run_once(QUARTER_START + timedelta(days=1))

    assert len(calls) == 1
    body = calls[0].get_content()
    assert "Truck" in body
    assert "Retired Truck" not in body
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT sent FROM email_deliveries WHERE kind = 'quarterly_odometer' AND period_end = %s",
            (QUARTER_START,),
        )
        assert await cur.fetchone() == (True,)


def test_quarterly_odometer_names_due_vehicles_and_dedups_on_retry():
    asyncio.run(_with_pool(_quarterly_odometer_due_scenario))


async def _quarterly_odometer_none_due_scenario(pool):
    calls: list[EmailMessage] = []
    async with pool.connection() as conn:
        truck_id = await _create_vehicle(conn, "Truck")
        await _insert_reading(conn, truck_id, QUARTER_START + timedelta(hours=1), 1000)

    worker = _worker(pool, _mailer(calls), odometer=True)
    await worker.run_once(QUARTER_START + timedelta(days=1))

    assert calls == []
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT sent FROM email_deliveries WHERE kind = 'quarterly_odometer' AND period_end = %s",
            (QUARTER_START,),
        )
        assert await cur.fetchone() == (False,)


def test_quarterly_odometer_writes_ledger_row_without_sending_when_none_due():
    asyncio.run(_with_pool(_quarterly_odometer_none_due_scenario))


# --- failure semantics / kind independence -------------------------------

async def _one_kind_failure_scenario(pool):
    calls: list[EmailMessage] = []
    async with pool.connection() as conn:
        # Both weekly_nudge and quarterly_odometer have something to send
        # in this run so each attempts a Mailer.send() -- the transport
        # below only fails the weekly-nudge subject.
        await _insert_trip(conn, WINDOW_END - timedelta(days=1))
        truck_id = await _create_vehicle(conn, "Truck")

    mailer = _mailer(calls, raise_for="unclassified")
    worker = _worker(pool, mailer, weekly=True, odometer=True)
    # WINDOW_END (Sun 2026-07-12 18:00) and the nearest quarter start
    # (2026-07-01 09:00) are both <= this `now`.
    now = WINDOW_END + timedelta(hours=1)
    await worker.run_once(now)

    async with pool.connection() as conn:
        weekly_row = await (await conn.execute(
            "SELECT 1 FROM email_deliveries WHERE kind = 'weekly_nudge'"
        )).fetchone()
        odometer_row = await (await conn.execute(
            "SELECT sent FROM email_deliveries WHERE kind = 'quarterly_odometer'"
        )).fetchone()
    assert weekly_row is None  # rolled back; no ledger row for the failing kind
    assert odometer_row == (True,)  # unaffected by the other kind's failure

    # Retried next hour with a healthy transport: weekly_nudge now succeeds;
    # quarterly_odometer is untouched (already has a ledger row).
    calls.clear()
    healthy_mailer = _mailer(calls)
    worker = _worker(pool, healthy_mailer, weekly=True, odometer=True)
    await worker.run_once(now + timedelta(hours=1))

    assert len(calls) == 1
    assert "unclassified" in calls[0]["Subject"].lower()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT sent FROM email_deliveries WHERE kind = 'weekly_nudge'"
        )
        assert await cur.fetchone() == (True,)


def test_one_kinds_failure_does_not_block_the_other_and_is_retried():
    asyncio.run(_with_pool(_one_kind_failure_scenario))


# --- disabled kinds -------------------------------------------------------

async def _disabled_kinds_scenario(pool):
    calls: list[EmailMessage] = []
    async with pool.connection() as conn:
        # Data that would trigger a send for every kind if it were enabled.
        await _insert_trip(conn, WINDOW_END - timedelta(days=1))
        truck_id = await _create_vehicle(conn, "Truck")
        await _insert_trip(
            conn, datetime(2026, 6, 15, 18, tzinfo=TZ), "business", 20 * 1609.344, truck_id,
        )

    # Only weekly_nudge enabled -- the other three must never be evaluated.
    worker = _worker(pool, _mailer(calls), weekly=True)
    await worker.run_once(WINDOW_END + timedelta(hours=1))

    assert len(calls) == 1
    assert "unclassified" in calls[0]["Subject"].lower()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT kind FROM email_deliveries WHERE kind != 'weekly_nudge'"
        )
        assert await cur.fetchall() == []


def test_disabled_kinds_are_never_evaluated():
    asyncio.run(_with_pool(_disabled_kinds_scenario))
