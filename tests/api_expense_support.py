"""Synthetic ledger data for dashboard, Stats and expense tests: two
vehicles, trips of every category and exclusion across two years, linked and
unlinked expenses (one with every conflict), odometer readings and rates.

Writes go through an account connection, so row-level security applies.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.account_context import account_id

UTC = timezone.utc
WEEK_ANCHOR = date(2026, 4, 15)


async def _vehicle(conn, name: str) -> int:
    row = await (await conn.execute(
        "SELECT id FROM vehicles WHERE account_id = %s AND name = %s", (account_id(conn), name))).fetchone()
    if row:
        return row[0]
    return (await (await conn.execute(
        "INSERT INTO vehicles (account_id, name) VALUES (%s, %s) RETURNING id",
        (account_id(conn), name))).fetchone())[0]


async def _trip(conn, started_at: datetime, distance_m: float, category: str, **values) -> int:
    columns = {"device": "manual", "source": "manual", "started_at": started_at,
               "ended_at": started_at + timedelta(minutes=30), "distance_m": distance_m,
               "category": category, **values}
    cur = await conn.execute(
        f"INSERT INTO trips (account_id, {', '.join(columns)}) "
        f"VALUES (%s, {', '.join(['%s'] * len(columns))}) RETURNING id",
        [account_id(conn), *columns.values()])
    return (await cur.fetchone())[0]


async def _expense(conn, vehicle: int, incurred_on: date, category: str, amount: str, treatment: str,
                   notes: str | None = None, trip_id: int | None = None) -> int:
    cur = await conn.execute(
        "INSERT INTO expenses (account_id, vehicle_id, incurred_on, category, amount, treatment, notes, trip_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (account_id(conn), vehicle, incurred_on, category, amount, treatment, notes, trip_id))
    return (await cur.fetchone())[0]


async def seed_ledger(conn) -> dict[str, int]:
    """Seed the ledger and return ids by name. Times are UTC; the tests show
    them in America/Los_Angeles, so the late-evening trips fall on the
    previous local day."""
    owner = account_id(conn)
    car = await _vehicle(conn, "My Car")
    van = await _vehicle(conn, "Van")
    for year, rate, h2 in ((2025, "0.7000", None), (2026, "0.7250", "0.7500")):
        await conn.execute(
            "INSERT INTO mileage_rates (account_id, year, rate_per_mi, rate_h2_per_mi, h2_start_month) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (account_id, year) DO UPDATE SET "
            "rate_per_mi = EXCLUDED.rate_per_mi, rate_h2_per_mi = EXCLUDED.rate_h2_per_mi, "
            "h2_start_month = EXCLUDED.h2_start_month",
            (owner, year, rate, h2, 7 if h2 else None))
    ids = {"car": car, "van": van}
    week = datetime(2026, 4, 13, 16, tzinfo=UTC)
    ids["business"] = await _trip(conn, week, 16093.44, "business", vehicle_id=car, purpose="Client")
    ids["personal"] = await _trip(conn, week + timedelta(days=1), 8046.72, "personal", vehicle_id=car)
    ids["unclassified"] = await _trip(conn, week + timedelta(days=2), 3218.69, "unclassified", vehicle_id=van)
    ids["nondeductible"] = await _trip(conn, week + timedelta(days=2, hours=2), 4828.03, "business",
                                       vehicle_id=van, exclusion="not_deductible")
    ids["not_mine"] = await _trip(conn, week + timedelta(days=3), 2000.0, "business",
                                  exclusion="not_my_vehicle")
    ids["unassigned"] = await _trip(conn, week + timedelta(days=4), 1500.5, "business")
    # Late evening local time, the day before in UTC terms.
    ids["late"] = await _trip(conn, datetime(2026, 4, 19, 6, 30, tzinfo=UTC), 1234.5, "business",
                              vehicle_id=car)
    for month, meters, category in ((1, 20000.0, "business"), (2, 12000.0, "personal"),
                                    (5, 30000.0, "business"), (8, 9000.0, "business"),
                                    (8, 4000.0, "unclassified"), (11, 7000.0, "business")):
        await _trip(conn, datetime(2026, month, 10, 17, tzinfo=UTC), meters, category, vehicle_id=car)
    for month, meters, category in ((1, 15000.0, "business"), (3, 8000.0, "personal"),
                                    (6, 22000.0, "business"), (9, 5000.0, "personal"),
                                    (12, 11000.0, "business")):
        await _trip(conn, datetime(2025, month, 12, 18, tzinfo=UTC), meters, category, vehicle_id=van)
    ids["fuel"] = await _expense(conn, car, date(2026, 4, 13), "fuel", "45.67", "business_use_allocated",
                                 "Fill up", ids["business"])
    ids["parking"] = await _expense(conn, car, date(2026, 4, 14), "parking", "12.34", "fully_business")
    # Every conflict: another vehicle, another date, a not-my-vehicle trip.
    ids["conflicted"] = await _expense(conn, van, date(2026, 4, 20), "tolls", "3.10", "fully_business",
                                       None, ids["not_mine"])
    ids["insurance"] = await _expense(conn, van, date(2026, 2, 1), "insurance", "1200.00",
                                      "business_use_allocated", "Annual")
    ids["old"] = await _expense(conn, van, date(2025, 6, 1), "maintenance_repairs", "310.99",
                                "business_use_allocated")
    for vehicle, recorded_at, odometer_m in (
            (car, datetime(2026, 1, 1, 8, tzinfo=UTC), 1_000_000.0),
            (car, datetime(2026, 7, 1, tzinfo=UTC), 1_100_000.0),
            (car, datetime(2027, 1, 1, 9, tzinfo=UTC), 1_250_000.0)):
        await conn.execute(
            "INSERT INTO odometer_readings (account_id, vehicle_id, recorded_at, odometer_m) "
            "VALUES (%s, %s, %s, %s)", (owner, vehicle, recorded_at, odometer_m))
    return ids
