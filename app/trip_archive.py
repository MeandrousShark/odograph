"""Archive rows: the web's per-month pages and the API's keyset pages.

Both page over the same `TripFilter` SQL, newest first with `id` breaking
ties, so neither can match a different set of trips than the other.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from app.account_context import account_id
from app.trip_queries import DISPLAY_DISTANCE_SQL, TRIP_COLUMNS, TripFilter

MONTH_AGGREGATE_COLUMNS_SQL = (
    "count(*) AS trip_count, "
    f"COALESCE(SUM({DISPLAY_DISTANCE_SQL}) FILTER "
    "(WHERE exclusion IS DISTINCT FROM 'not_my_vehicle'), 0) AS total_m, "
    f"COALESCE(SUM({DISPLAY_DISTANCE_SQL}) "
    "FILTER (WHERE category = 'business' AND exclusion IS NULL), 0) AS business_m"
)


def month_bounds(year: int, month: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=tz)
    end = (
        datetime(year + 1, 1, 1, tzinfo=tz)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=tz)
    )
    return start, end


async def fetch_month_totals(conn, filters: TripFilter, tz: ZoneInfo) -> list[dict]:
    """Per local month aggregates, newest month first."""
    where, params = filters.where(account_id(conn))
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT date_trunc('month', started_at AT TIME ZONE %s)::date AS local_month, "
        f"{MONTH_AGGREGATE_COLUMNS_SQL} "
        f"FROM trips {where} GROUP BY local_month ORDER BY local_month DESC",
        [tz.key, *params],
    )
    return await cur.fetchall()


async def fetch_month_rows(conn, filters: TripFilter, tz: ZoneInfo, per_month: int) -> list[dict]:
    """At most `per_month` newest rows of each local month, each carrying
    its `local_month`, newest month first."""
    where, params = filters.where(account_id(conn))
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "WITH ranked AS (SELECT "
        f"{TRIP_COLUMNS}, "
        "date_trunc('month', started_at AT TIME ZONE %s)::date AS local_month, "
        "row_number() OVER (PARTITION BY date_trunc('month', started_at AT TIME ZONE %s) "
        "ORDER BY started_at DESC, id DESC) AS month_row "
        f"FROM trips {where}) "
        "SELECT * FROM ranked WHERE month_row <= %s "
        "ORDER BY local_month DESC, started_at DESC, id DESC",
        [tz.key, tz.key, *params, per_month],
    )
    return await cur.fetchall()


async def fetch_month_page(
    conn, filters: TripFilter, tz: ZoneInfo, year: int, month: int,
    page_size: int, offset: int,
) -> tuple[list[dict], bool]:
    """Fetch one stable local-month page plus a one-row `has_more` sentinel."""
    month_start, month_end = month_bounds(year, month, tz)
    where, params = filters.where(account_id(conn))
    where += " AND" if where else "WHERE"
    where += " started_at >= %s AND started_at < %s"
    params.extend((month_start, month_end))
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        f"SELECT {TRIP_COLUMNS} FROM trips {where} "
        "ORDER BY started_at DESC, id DESC LIMIT %s OFFSET %s",
        [*params, page_size + 1, offset],
    )
    rows = await cur.fetchall()
    return rows[:page_size], len(rows) > page_size


async def fetch_trip_page(
    conn, filters: TripFilter, after: tuple[datetime, int] | None, limit: int,
) -> tuple[list[dict], bool]:
    """Keyset page on `(started_at DESC, id DESC)` strictly after `after`.

    Each page is its own query, with no snapshot: a trip unchanged for the
    whole traversal appears exactly once, while an edit that moves a trip
    across the cursor can repeat or skip it.
    """
    where, params = filters.where(account_id(conn))
    if after is not None:
        where += " AND (started_at, id) < (%s, %s)"
        params.extend(after)
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        f"SELECT {TRIP_COLUMNS} FROM trips {where} ORDER BY started_at DESC, id DESC LIMIT %s",
        [*params, limit + 1],
    )
    rows = await cur.fetchall()
    return rows[:limit], len(rows) > limit
