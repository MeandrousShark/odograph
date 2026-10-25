"""Synthetic trips of every stored shape, for trip read tests.

Writes go through an account connection, so row-level security applies.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.account_context import account_id
from conftest import seed_tracking_device

BASE = datetime(2026, 4, 14, 15, 0, tzinfo=timezone.utc)
RAW_LINE = "LINESTRING(-122.33 47.60, -122.32 47.61, -122.31 47.62)"
GAPPED_SNAPPED = ("MULTILINESTRING((-122.33 47.60, -122.325 47.605),"
                  "(-122.315 47.615, -122.31 47.62))")
ONE_PART_SNAPPED = "MULTILINESTRING((-122.30 47.63, -122.29 47.64))"


async def _trip(conn, **values) -> int:
    columns = ["account_id", *values]
    placeholders = ["%s"] * len(columns)
    params = [account_id(conn), *values.values()]
    for geometry in ("path", "path_snapped", "start_geom", "end_geom"):
        if geometry in values and values[geometry] is not None:
            index = columns.index(geometry)
            placeholders[index] = "ST_GeomFromText(%s, 4326)" if geometry.startswith("path") else (
                "ST_GeomFromText(%s, 4326)::geography")
    cur = await conn.execute(
        f"INSERT INTO trips ({', '.join(columns)}) VALUES ({', '.join(placeholders)}) RETURNING id",
        params,
    )
    return (await cur.fetchone())[0]


async def _stay(conn, device: int, started_at: datetime, ended_at: datetime, point: str) -> None:
    await conn.execute(
        "INSERT INTO stays (account_id, tracking_device_id, device, started_at, ended_at, centroid, point_count) "
        "VALUES (%s, %s, 'phone', %s, %s, ST_GeomFromText(%s, 4326)::geography, 3)",
        (account_id(conn), device, started_at, ended_at, point),
    )


async def seed_trip_kinds(conn) -> dict[str, int]:
    """One trip per stored shape, an hour apart, oldest first.

    Returns ids by kind: `gapped` (raw LineString, two-part snapped route,
    stays at both ends, a linked expense), `snapped_only`, `raw_only`,
    `geometry_free` (detected, no route), `imported`, `manual` (labels, no
    route) and `routed_manual`.
    """
    device = await seed_tracking_device(conn)
    # Stored detector output is bounded by the device's retained points.
    await conn.execute(
        "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, geom) "
        "SELECT %s, %s, 'phone', %s + n * interval '1 minute', "
        "ST_SetSRID(ST_MakePoint(-122.33, 47.60), 4326)::geography FROM generate_series(1, 60) n",
        (account_id(conn), device, BASE - timedelta(days=1)),
    )
    cur = await conn.execute(
        "SELECT id FROM vehicles WHERE account_id = %s ORDER BY id LIMIT 1", (account_id(conn),))
    row = await cur.fetchone()
    vehicle = row[0] if row else (await (await conn.execute(
        "INSERT INTO vehicles (account_id, name) VALUES (%s, 'Car') RETURNING id",
        (account_id(conn),))).fetchone())[0]
    ids: dict[str, int] = {}

    def at(hours: int) -> tuple[datetime, datetime]:
        start = BASE + timedelta(hours=hours)
        return start, start + timedelta(minutes=20)

    start, end = at(0)
    ids["gapped"] = await _trip(
        conn, tracking_device_id=device, device="phone", source="detected", started_at=start,
        ended_at=end, distance_m=2400.5, distance_snapped_m=2310.25, snap_status="ok",
        point_count=42, has_gap=True, category="business", purpose="Client visit",
        notes="Gap near the tunnel", vehicle_id=vehicle, path=RAW_LINE, path_snapped=GAPPED_SNAPPED,
        start_geom="POINT(-122.33 47.60)", end_geom="POINT(-122.31 47.62)",
    )
    await _stay(conn, device, start - timedelta(hours=1), start, "POINT(-122.331 47.601)")
    await _stay(conn, device, end, end + timedelta(minutes=30), "POINT(-122.309 47.621)")
    await conn.execute(
        "INSERT INTO expenses (account_id, vehicle_id, incurred_on, category, amount, treatment, notes, trip_id) "
        "VALUES (%s, %s, %s, 'parking', 12.34, 'fully_business', 'Garage', %s)",
        (account_id(conn), vehicle, start.date(), ids["gapped"]),
    )
    start, end = at(1)
    ids["snapped_only"] = await _trip(
        conn, tracking_device_id=device, device="phone", source="detected", started_at=start,
        ended_at=end, distance_m=1500.0, distance_snapped_m=1490.75, snap_status="ok",
        point_count=12, category="unclassified", path_snapped=ONE_PART_SNAPPED,
        start_geom="POINT(-122.30 47.63)", end_geom="POINT(-122.29 47.64)",
    )
    await _stay(conn, device, end, end + timedelta(minutes=10), "POINT(-122.29 47.64)")
    start, end = at(2)
    ids["raw_only"] = await _trip(
        conn, tracking_device_id=device, device="phone", source="detected", started_at=start,
        ended_at=end, distance_m=1800.0, snap_status="pending", point_count=20,
        category="unclassified", exclusion="not_deductible", path=RAW_LINE,
        start_geom="POINT(-122.33 47.60)", end_geom="POINT(-122.31 47.62)",
    )
    await _stay(conn, device, end, end + timedelta(minutes=10), "POINT(-122.31 47.62)")
    start, end = at(3)
    ids["geometry_free"] = await _trip(
        conn, tracking_device_id=device, device="phone", source="detected", started_at=start,
        ended_at=end, distance_m=900.0, snap_status="failed", point_count=3, category="personal",
    )
    await _stay(conn, device, end, end + timedelta(minutes=10), "POINT(-122.30 47.60)")
    start, end = at(4)
    ids["imported"] = await _trip(
        conn, device="old-phone", source="detected", imported=True, started_at=start,
        ended_at=end, distance_m=3100.0, point_count=30, category="unclassified", path=RAW_LINE,
        start_geom="POINT(-122.33 47.60)", end_geom="POINT(-122.31 47.62)",
    )
    start, end = at(5)
    ids["manual"] = await _trip(
        conn, device="manual", source="manual", started_at=start, ended_at=end, distance_m=5000.0,
        category="business", purpose="Supplies", start_label="Office", end_label="Hardware store",
        vehicle_id=vehicle,
    )
    start, end = at(6)
    ids["routed_manual"] = await _trip(
        conn, device="manual", source="manual", started_at=start, ended_at=end, distance_m=4200.0,
        category="unclassified", path=RAW_LINE,
        start_geom="POINT(-122.33 47.60)", end_geom="POINT(-122.31 47.62)",
    )
    return ids
