"""Trip reads shared by the web pages and the API: one trip, its detail,
its route geometry and recent purpose suggestions."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from app.account_context import account_id
from app.expense_queries import EXPENSE_SELECT_JOIN, annotate_expense
from app.trip_queries import TRIP_COLUMNS


async def fetch_trip(conn, trip_id: int) -> dict | None:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        f"SELECT {TRIP_COLUMNS} FROM trips WHERE id = %s AND account_id = %s",
        (trip_id, account_id(conn)),
    )
    return await cur.fetchone()


async def trip_position(conn, trip_id: int):
    """The `(started_at, id)` of a trip in this account, or None."""
    cur = await conn.execute(
        "SELECT started_at FROM trips WHERE id = %s AND account_id = %s", (trip_id, account_id(conn))
    )
    row = await cur.fetchone()
    return (row[0], trip_id) if row else None


async def fetch_route_geojson(conn, trip_id: int) -> tuple[str | None, str | None]:
    """The raw and snapped routes as `ST_AsGeoJSON` text, each None when absent."""
    cur = await conn.execute(
        "SELECT ST_AsGeoJSON(path), ST_AsGeoJSON(path_snapped) "
        "FROM trips WHERE id = %s AND account_id = %s", (trip_id, account_id(conn))
    )
    row = await cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def route_parts(geojson: str | None) -> list | None:
    """MultiLineString coordinates for a stored route, or None.

    A LineString becomes one part. Parts are never joined, so a recording
    gap stays a break between parts.
    """
    if geojson is None:
        return None
    geometry = json.loads(geojson)
    if geometry["type"] == "LineString":
        return [geometry["coordinates"]]
    if geometry["type"] == "MultiLineString":
        return geometry["coordinates"]
    raise ValueError("unexpected route geometry type")


async def fetch_recent_purposes(conn, limit: int = 10) -> list[str]:
    """Return distinct, nonblank purposes ordered by their latest use.

    Values are trimmed at write time as well as here. Keeping the defensive
    trim in this query prevents older/direct SQL writes with surrounding
    whitespace from creating visually duplicate datalist suggestions.
    """
    cur = await conn.execute(
        "SELECT purpose FROM ("
        " SELECT DISTINCT ON (btrim(purpose)) btrim(purpose) AS purpose, updated_at"
        " FROM trips WHERE account_id = %s AND purpose IS NOT NULL AND btrim(purpose) <> ''"
        " ORDER BY btrim(purpose), updated_at DESC"
        ") recent ORDER BY updated_at DESC, purpose LIMIT %s",
        (account_id(conn), limit),
    )
    return [row[0] for row in await cur.fetchall()]


async def fetch_trip_expenses(conn, trip_id: int, tz: ZoneInfo) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        EXPENSE_SELECT_JOIN
        + "WHERE expenses.trip_id = %s AND expenses.account_id = %s ORDER BY expenses.incurred_on DESC, expenses.id DESC",
        (trip_id, account_id(conn)),
    )
    return [annotate_expense(row, tz) for row in await cur.fetchall()]


@dataclass(frozen=True)
class TripStay:
    """A stay touching the trip: the one it left (`start`) or reached (`end`)."""
    position: Literal["start", "end"]
    centroid: dict


@dataclass(frozen=True)
class TripDetail:
    trip: dict
    path_geojson: str | None
    path_snapped_geojson: str | None
    stays: tuple[TripStay, ...]
    has_next_trip: bool
    has_prev_trip: bool
    expenses: list[dict]


async def fetch_trip_detail(conn, trip_id: int, tz: ZoneInfo) -> TripDetail | None:
    """One trip with its routes, adjoining stays, merge adjacency and
    linked expenses, or None when this account has no such trip."""
    trip = await fetch_trip(conn, trip_id)
    if trip is None:
        return None
    expenses = await fetch_trip_expenses(conn, trip_id, tz)
    path_geojson = path_snapped_geojson = None
    stays: list[TripStay] = []
    has_next_trip = has_prev_trip = False
    # A routed manual trip has a real `path` too (see add_manual_trip),
    # so it needs the same geometry fetch a detected trip gets; the
    # stay-centroid and adjacent-trip queries below stay detected-only,
    # since neither concept exists for a manual trip.
    if trip["source"] == "detected" or trip["has_route_geometry"]:
        path_geojson, path_snapped_geojson = await fetch_route_geojson(conn, trip_id)
    if trip["source"] == "detected":
        cur = await conn.execute(
            "SELECT ST_AsGeoJSON(centroid::geometry), ended_at FROM stays "
            "WHERE account_id = %s AND tracking_device_id = %s AND (ended_at = %s OR started_at = %s)",
            (account_id(conn), trip["tracking_device_id"], trip["started_at"], trip["ended_at"]),
        )
        stays = [
            TripStay("start" if ended_at == trip["started_at"] else "end", json.loads(centroid))
            for centroid, ended_at in await cur.fetchall()
        ]
        # Merge buttons only render when a genuine adjacent
        # detected trip exists for this device.
        cur = await conn.execute(
            "SELECT EXISTS (SELECT 1 FROM trips WHERE account_id = %s AND tracking_device_id = %s "
            " AND source = 'detected' AND NOT imported AND started_at > %s), "
            "EXISTS (SELECT 1 FROM trips WHERE account_id = %s AND tracking_device_id = %s "
            " AND source = 'detected' AND NOT imported AND started_at < %s)",
            (account_id(conn), trip["tracking_device_id"], trip["started_at"],
             account_id(conn), trip["tracking_device_id"], trip["started_at"]),
        )
        has_next_trip, has_prev_trip = await cur.fetchone()
    return TripDetail(
        trip=trip, path_geojson=path_geojson, path_snapped_geojson=path_snapped_geojson,
        stays=tuple(stays), has_next_trip=has_next_trip, has_prev_trip=has_prev_trip,
        expenses=expenses,
    )
