"""DB-backed delivery-ledger tests for the quarterly odometer reminder
worker. Mirrors tests/test_nudge_db.py's structure for the weekly nudge,
against odometer_reminder_windows (migration 010) instead of
nudge_delivery_windows (migration 009) -- the two ledgers never interact.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.db import make_pool
from app.odometer_reminder import OdometerReminderWorker
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

TZ = ZoneInfo("America/Los_Angeles")
QUARTER_START = datetime(2026, 7, 1, 9, tzinfo=TZ)


async def _reset_schema(pool) -> None:
    await reset_db(pool)
    # migrations/008_vehicles.sql seeds an active default vehicle ("My Car")
    # that every scenario below would otherwise see as permanently due (it
    # never gets a reading) -- deactivate it so each scenario's assertions
    # are only about the vehicles it explicitly creates.
    async with pool.connection() as conn:
        await conn.execute("UPDATE vehicles SET active = false WHERE name = 'My Car'")


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


async def _due_vehicle_scenario():
    calls = 0
    captured_content = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls, captured_content
        calls += 1
        captured_content = request.content.decode()
        return httpx.Response(200)

    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            truck_id = await _create_vehicle(conn, "Truck")
            # An inactive vehicle with no reading at all must never trigger
            # a reminder -- it's retired, not neglected.
            await _create_vehicle(conn, "Retired Truck", active=False)
            # Last quarter's reading doesn't count toward this quarter.
            await _insert_reading(conn, truck_id, QUARTER_START - timedelta(days=5), 1000)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            worker = OdometerReminderWorker(
                pool, client, "https://ntfy.example.com", "mileage", "", "", "", "", TZ, 9,
            )
            await worker.run_once(QUARTER_START + timedelta(hours=1))
            # A second pass in the same quarter must be a no-op.
            await worker.run_once(QUARTER_START + timedelta(days=1))

        assert calls == 1
        assert "Truck" in captured_content
        assert "Retired Truck" not in captured_content
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT reminded FROM odometer_reminder_windows WHERE quarter_starts_at = %s",
                (QUARTER_START,),
            )
            assert await cur.fetchone() == (True,)
    finally:
        await pool.close()


def test_reminder_sends_once_per_quarter_for_vehicle_with_no_new_reading():
    asyncio.run(_due_vehicle_scenario())


async def _already_logged_scenario():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            truck_id = await _create_vehicle(conn, "Truck")
            # Logged after the quarter boundary: already satisfied this quarter.
            await _insert_reading(conn, truck_id, QUARTER_START + timedelta(days=1), 1000)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            worker = OdometerReminderWorker(
                pool, client, "https://ntfy.example.com", "mileage", "", "", "", "", TZ, 9,
            )
            await worker.run_once(QUARTER_START + timedelta(days=2))

        assert calls == 0
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT reminded FROM odometer_reminder_windows WHERE quarter_starts_at = %s",
                (QUARTER_START,),
            )
            assert await cur.fetchone() == (False,)
    finally:
        await pool.close()


def test_reminder_skips_vehicle_already_logged_this_quarter_and_sends_nothing():
    asyncio.run(_already_logged_scenario())


async def _retry_scenario():
    responses = [500, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(responses.pop(0))

    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            await _create_vehicle(conn, "Truck")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            worker = OdometerReminderWorker(
                pool, client, "https://ntfy.example.com", "mileage", "", "", "", "", TZ, 9,
            )
            with pytest.raises(httpx.HTTPStatusError):
                await worker.run_once(QUARTER_START + timedelta(hours=1))
            async with pool.connection() as conn:
                cur = await conn.execute("SELECT count(*) FROM odometer_reminder_windows")
                assert await cur.fetchone() == (0,)
            await worker.run_once(QUARTER_START + timedelta(days=1))

        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM odometer_reminder_windows")
            assert await cur.fetchone() == (1,)
    finally:
        await pool.close()


def test_reminder_retries_after_failed_ntfy_response_without_marking_window_done():
    asyncio.run(_retry_scenario())


async def _all_logged_no_send_scenario():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            truck_id = await _create_vehicle(conn, "Truck")
            sedan_id = await _create_vehicle(conn, "Sedan")
            await _insert_reading(conn, truck_id, QUARTER_START + timedelta(hours=1), 1000)
            await _insert_reading(conn, sedan_id, QUARTER_START + timedelta(hours=2), 500)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            worker = OdometerReminderWorker(
                pool, client, "https://ntfy.example.com", "mileage", "", "", "", "", TZ, 9,
            )
            await worker.run_once(QUARTER_START + timedelta(days=1))

        assert calls == 0
    finally:
        await pool.close()


def test_reminder_sends_nothing_when_every_active_vehicle_already_logged():
    asyncio.run(_all_logged_no_send_scenario())
