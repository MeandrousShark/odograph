"""The Places heatmap: trip endpoints or stay time, summed per grid cell.

Cells are about CELL_M on a side. Rows are a fixed latitude step; each row's
longitude step is scaled by the cosine of the row's centre latitude, so a
cell is close to square on the ground at any latitude. Only a cell's own
centre leaves the server, never an average or a raw coordinate, so a cell
holding one visit does not reveal where it was.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.account_context import account_id
from app.trip_queries import TripFilter

CELL_M = 50.0
# Metres per degree of latitude, to the precision a 50 m cell needs.
M_PER_DEG_LAT = 111_320.0
LAT_STEP = CELL_M / M_PER_DEG_LAT
# Row indexes stay inside the poles, so a row centre's cosine is positive.
MAX_ROW = math.floor(90.0 / LAT_STEP) - 1
COORD_DECIMALS = 6
# Years whose following midnight still exists, the place page's year bound.
MIN_YEAR, MAX_YEAR = 1, 9998
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

HeatMode = Literal["visits", "time"]
VehicleFilter = int | None | Literal["none"]


def cell_index(lat: float, lon: float) -> tuple[int, int]:
    row = min(max(math.floor(lat / LAT_STEP), -MAX_ROW - 1), MAX_ROW)
    return row, math.floor(lon * math.cos(math.radians((row + 0.5) * LAT_STEP)) / LAT_STEP)


def cell_centre(row: int, col: int) -> tuple[float, float]:
    lat = (row + 0.5) * LAT_STEP
    lon = (col + 0.5) * LAT_STEP / math.cos(math.radians(lat))
    return round(lat, COORD_DECIMALS), round(lon, COORD_DECIMALS)


def _cell_sql(lat: str, lon: str) -> str:
    """`cell_index` in SQL, for a FROM list: yields `cell.lat_idx`, `cell.lon_idx`."""
    return (
        f"CROSS JOIN LATERAL (SELECT LEAST(GREATEST(floor({lat} / {LAT_STEP!r}), {-MAX_ROW - 1}), "
        f"{MAX_ROW})::bigint AS lat_idx) band "
        f"CROSS JOIN LATERAL (SELECT band.lat_idx, floor({lon} * cos(radians((band.lat_idx + 0.5) * "
        f"{LAT_STEP!r})) / {LAT_STEP!r})::bigint AS lon_idx) cell"
    )


@dataclass(frozen=True)
class HeatRange:
    start: date
    end: date

    def bounds(self, tz: ZoneInfo) -> tuple[datetime, datetime]:
        """Local midnight of `start` to local midnight after `end`."""
        following = self.end + timedelta(days=1)
        return (datetime(self.start.year, self.start.month, self.start.day, tzinfo=tz),
                datetime(following.year, following.month, following.day, tzinfo=tz))


def default_range(tz: ZoneInfo) -> HeatRange:
    today = datetime.now(tz).date()
    return HeatRange(date(today.year, 1, 1), today)


def _parse_day(name: str, value: str) -> date:
    if not _DAY.fullmatch(value):
        raise HTTPException(status_code=422, detail=f"'{name}' must be YYYY-MM-DD")
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"'{name}' must be YYYY-MM-DD") from None
    if not MIN_YEAR <= day.year <= MAX_YEAR:
        raise HTTPException(status_code=422, detail=f"'{name}' is out of range")
    return day


def parse_range(from_str: str, to_str: str, tz: ZoneInfo) -> HeatRange:
    """Strict dates; an empty one takes the default (this year to today)."""
    default = default_range(tz)
    start = _parse_day("from", from_str) if from_str else default.start
    end = _parse_day("to", to_str) if to_str else default.end
    if start > end:
        raise HTTPException(status_code=422, detail="'from' must not be after 'to'")
    return HeatRange(start, end)


async def _visit_cells(conn, start: datetime, end: datetime, category: str, vehicle_id: VehicleFilter) -> list[tuple]:
    where, params = TripFilter(category, start, end, vehicle_id).where(account_id(conn))
    cur = await conn.execute(
        "SELECT cell.lat_idx, cell.lon_idx, count(*) FROM trips "
        "CROSS JOIN LATERAL (VALUES (trips.start_geom), (trips.end_geom)) AS ends(geom) "
        "CROSS JOIN LATERAL (SELECT ST_Y(ends.geom::geometry) AS lat, ST_X(ends.geom::geometry) AS lon) pt "
        f"{_cell_sql('pt.lat', 'pt.lon')} "
        f"{where} AND ends.geom IS NOT NULL AND trips.exclusion IS DISTINCT FROM 'not_my_vehicle' "
        "GROUP BY cell.lat_idx, cell.lon_idx",
        params,
    )
    return await cur.fetchall()


async def _time_cells(conn, start: datetime, end: datetime) -> list[tuple]:
    # A cell whose stays round to no whole second is dropped.
    cur = await conn.execute(
        "SELECT cell.lat_idx, cell.lon_idx, round(sum(EXTRACT(EPOCH FROM "
        " LEAST(stays.ended_at, %s) - GREATEST(stays.started_at, %s))))::bigint AS seconds "
        "FROM stays "
        "CROSS JOIN LATERAL (SELECT ST_Y(stays.centroid::geometry) AS lat, "
        " ST_X(stays.centroid::geometry) AS lon) pt "
        f"{_cell_sql('pt.lat', 'pt.lon')} "
        "WHERE stays.account_id = %s AND stays.started_at < %s AND stays.ended_at > %s "
        "GROUP BY cell.lat_idx, cell.lon_idx HAVING round(sum(EXTRACT(EPOCH FROM "
        " LEAST(stays.ended_at, %s) - GREATEST(stays.started_at, %s)))) > 0",
        (end, start, account_id(conn), end, start, end, start),
    )
    return await cur.fetchall()


async def heat_cells(
    conn, mode: HeatMode, span: HeatRange, tz: ZoneInfo,
    category: str = "", vehicle_id: VehicleFilter = None,
) -> dict:
    """The response body. `category` and `vehicle_id` are the archive's
    parsed filters and apply to visits only: stays have neither."""
    start, end = span.bounds(tz)
    if mode == "visits":
        rows = await _visit_cells(conn, start, end, category, vehicle_id)
    else:
        rows = await _time_cells(conn, start, end)
    cells = sorted([*cell_centre(row, col), int(weight)] for row, col, weight in rows)
    return {
        "mode": mode,
        "from": span.start.isoformat(),
        "to": span.end.isoformat(),
        "cells": cells,
        "max": max((cell[2] for cell in cells), default=0),
    }
