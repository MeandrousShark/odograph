"""DB coverage for exclusion-aware stats query wiring."""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.db import make_pool
from app.main import make_templates
from app.ui import make_router
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

UTC = ZoneInfo("UTC")
USER = {"sub": "test"}


def _endpoint():
    for route in make_router().routes:
        if getattr(route, "path", None) == "/stats":
            return route.endpoint
    raise AssertionError("stats route missing")


STATS = _endpoint()


def _request(pool):
    config = SimpleNamespace(display_tz=UTC, app_version="test")
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
        )),
        session={"csrf": "test-csrf"},
    )


async def _scenario() -> None:
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            vehicle_id = (await (await conn.execute(
                "SELECT id FROM vehicles WHERE is_default ORDER BY id LIMIT 1"
            )).fetchone())[0]
            await conn.execute(
                "INSERT INTO trips "
                "(device, source, started_at, ended_at, distance_m, category, vehicle_id, exclusion) "
                "VALUES "
                "('stats', 'manual', '2026-06-01T09:00:00Z', '2026-06-01T09:30:00Z', "
                "1000, 'business', %s, NULL), "
                "('stats', 'manual', '2026-06-02T09:00:00Z', '2026-06-02T09:30:00Z', "
                "2000, 'business', %s, 'not_my_vehicle'), "
                "('stats', 'manual', '2026-06-03T09:00:00Z', '2026-06-03T09:30:00Z', "
                "3000, 'business', %s, 'not_deductible')",
                (vehicle_id, vehicle_id, vehicle_id),
            )

        response = await STATS(_request(pool), USER, 2026, "", "", "")
        stats = response.context["stats"]
        assert stats.trip_count == 2
        assert stats.business_m == 1000.0
        assert stats.nondeductible_m == 3000.0
        assert stats.total_m == 4000.0
        assert stats.business_share == pytest.approx(0.25)
        assert stats.unnamed_trip_count == 2

        trend = response.context["trend"]
        assert trend.has_nondeductible is True
        assert "Classified mileage share by quarter" in trend.chart_svg

        vehicle_rows = response.context["vehicle_breakdown"].vehicles
        vehicle = next(row for row in vehicle_rows if row.vehicle_id == vehicle_id)
        assert vehicle.business_m == 1000.0
        assert vehicle.nondeductible_m == 3000.0
        assert vehicle.total_m == 4000.0
    finally:
        await pool.close()


def test_stats_queries_apply_exclusion_before_category():
    asyncio.run(_scenario())
