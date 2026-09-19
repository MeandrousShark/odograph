from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.db import make_pool
from app.account_context import account_id

from app.ui import _fetch_month_page
from conftest import reset_account_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")
TZ = ZoneInfo("America/New_York")


async def _scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            vehicle_id = (await (await conn.execute(
                "INSERT INTO vehicles (account_id, name) VALUES (%s, 'Car') RETURNING id", (account_id(conn),)
            )).fetchone())[0]
            ids = []
            for category, started in [
                ("business", datetime(2026, 3, 31, 12, tzinfo=timezone.utc)),
                ("personal", datetime(2026, 3, 20, 12, tzinfo=timezone.utc)),
                ("business", datetime(2026, 3, 20, 12, tzinfo=timezone.utc)),
                ("business", datetime(2026, 3, 1, 5, tzinfo=timezone.utc)),
            ]:
                row = await conn.execute(
                    "INSERT INTO trips (account_id, device, source, started_at, ended_at, "
                    "distance_m, category, vehicle_id) VALUES (%s, 'manual', 'manual', %s, %s, "
                    "1000, %s, %s) RETURNING id",
                    (
                        account_id(conn),
                        started,
                        started.replace(hour=(started.hour + 1) % 24),
                        category,
                        vehicle_id,
                    ),
                )
                ids.append((await row.fetchone())[0])
            # UTC timestamps immediately outside March's local bounds.
            for started in (
                datetime(2026, 3, 1, 4, 59, tzinfo=timezone.utc),
                datetime(2026, 4, 1, 4, 0, tzinfo=timezone.utc),
            ):
                await conn.execute(
                    "INSERT INTO trips (account_id, device, source, started_at, ended_at, "
                    "distance_m, category) VALUES (%s, 'manual', 'manual', %s, %s, 1000, "
                    "'business')",
                    (account_id(conn), started, started.replace(minute=(started.minute + 1) % 60),),
                )

            page1, more1 = await _fetch_month_page(
                conn, TZ, 2026, 3, 2, 0, "", None, None, None
            )
            page2, more2 = await _fetch_month_page(
                conn, TZ, 2026, 3, 2, 2, "", None, None, None
            )
            business, _ = await _fetch_month_page(
                conn, TZ, 2026, 3, 10, 0, "business", None, None, vehicle_id
            )
        assert more1 is True and more2 is False
        assert [row["id"] for row in page1 + page2] == [ids[0], ids[2], ids[1], ids[3]]
        assert [row["category"] for row in business] == ["business"] * 3
    finally:
        await raw_pool.close()


def test_month_page_boundaries_order_filters_and_final_page():
    asyncio.run(_scenario())
