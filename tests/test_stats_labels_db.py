"""DB regression coverage pinning Top routes and Most visited places to
saved-place identity only, even though trips can now carry arbitrary
start_label/end_label endpoint names (migrations/025_manual_trip_labels.sql).

migrations/025_manual_trip_labels.sql already forbids a label on an endpoint
that has a place id, so a labeled trip is structurally excluded from both
`app/ui/stats.py` ranking queries without any query change. This test pins
that exclusion so a later rewrite of those queries can't silently start
folding labels in.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.db import make_pool
from app.account_context import account_id
from personal_support import personal_request
from app.main import make_templates
from app.ui import make_router
from conftest import reset_account_db

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
    return personal_request(SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
        )),
        session={"csrf": "test-csrf"},
    ))


async def _insert_place(conn, name: str, lat: float, lon: float) -> int:
    row = await conn.execute(
        "INSERT INTO places (account_id, name, geom) VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, "
        "%s), 4326)::geography) RETURNING id",
        (account_id(conn), name, lon, lat,),
    )
    return (await row.fetchone())[0]


async def _scenario() -> None:
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            home_id = await _insert_place(conn, "Home", 47.60, -122.30)
            work_id = await _insert_place(conn, "Work", 47.61, -122.31)
            gym_id = await _insert_place(conn, "Gym", 47.62, -122.32)

            # Two saved-place trips on Home <-> Work, one on Home <-> Gym.
            await conn.execute(
                "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m, "
                "category, start_place_id, end_place_id) VALUES (%s, 'stats', 'manual', "
                "'2026-06-01T09:00:00Z', '2026-06-01T09:30:00Z', 10000, 'business', %s, %s), (41, "
                "'stats', 'manual', '2026-06-02T09:00:00Z', '2026-06-02T09:30:00Z', 5000, "
                "'business', %s, %s), (41, 'stats', 'manual', '2026-06-03T09:00:00Z', "
                "'2026-06-03T09:30:00Z', 2000, 'business', %s, %s)",
                (account_id(conn), home_id, work_id, home_id, work_id, home_id, gym_id,),
            )
            # A No route manual trip with only endpoint labels, no place ids
            # or geometry. Its distance is deliberately far larger than
            # every saved-place trip combined, so if either ranking query
            # ever admitted it, it would dominate the ranking rather than
            # blend in unnoticed.
            await conn.execute(
                "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m, "
                "category, start_label, end_label) VALUES (%s, 'stats', 'manual', "
                "'2026-06-04T09:00:00Z', '2026-06-04T09:30:00Z', 999999, 'business', %s, %s)",
                (account_id(conn), "My Custom Start", "My Custom End",),
            )

        response = await STATS(_request(pool), USER, 2026, "", "", "")
        stats = response.context["stats"]

        assert stats.trip_count == 4
        # Both endpoint place ids are null on the labeled trip, so it is the
        # one trip counted here regardless of its labels.
        assert stats.unnamed_trip_count == 1

        assert len(stats.routes) == 2
        route_pairs = [{r["start_name"], r["end_name"]} for r in stats.routes]
        assert {"Home", "Work"} in route_pairs
        assert {"Home", "Gym"} in route_pairs
        home_work = next(r for r in stats.routes if {r["start_name"], r["end_name"]} == {"Home", "Work"})
        assert home_work["trip_count"] == 2
        assert home_work["total_m"] == 15000.0
        for route in stats.routes:
            assert "Custom" not in route["start_name"]
            assert "Custom" not in route["end_name"]

        places_by_name = {p["name"]: p["visit_count"] for p in stats.places}
        assert places_by_name == {"Home": 3, "Work": 2, "Gym": 1}
        assert "My Custom Start" not in places_by_name
        assert "My Custom End" not in places_by_name
    finally:
        await raw_pool.close()


def test_labeled_endpoints_are_excluded_from_saved_place_rankings():
    asyncio.run(_scenario())
