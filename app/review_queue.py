"""The review queue: unclassified trips, oldest first, one at a time.

The queue keeps no server state. A caller passes the `(started_at, id)` of
the last trip it showed, and the next trip is the oldest match after it,
with `id` breaking ties.
"""
from __future__ import annotations

from datetime import datetime

from psycopg.rows import dict_row

from app.account_context import account_id
from app.trip_queries import TRIP_COLUMNS, TripFilter

REVIEW_CATEGORY = "unclassified"


def review_filter(
    from_dt: datetime | None, to_dt: datetime | None,
    vehicle_id: int | None | str = None, q: str = "",
) -> TripFilter:
    """The review filter is always pinned to unclassified trips."""
    return TripFilter(REVIEW_CATEGORY, from_dt, to_dt, vehicle_id, q)


async def next_review_trip(
    conn, filters: TripFilter, after: tuple[datetime, int] | None,
    *, inclusive: bool = False,
) -> tuple[dict | None, int]:
    """The oldest trip matching `filters` after `after` (None starts from
    the oldest), and how many matches remain at or after it, counting the
    returned trip. The count is a point-in-time count with no snapshot.

    `inclusive=True` also accepts the trip at `after` itself.
    """
    where, params = filters.where(account_id(conn))
    position_where, position_params = "", []
    if after is not None:
        op = ">=" if inclusive else ">"
        position_where = f" AND (started_at, id) {op} (%s, %s)"
        position_params = list(after)
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        f"SELECT {TRIP_COLUMNS} FROM trips {where}{position_where} "
        "ORDER BY started_at, id LIMIT 1",
        [*params, *position_params],
    )
    trip = await cur.fetchone()
    if trip is None:
        return None, 0
    remaining_cur = await conn.execute(
        f"SELECT count(*) FROM trips {where} AND (started_at, id) >= (%s, %s)",
        [*params, trip["started_at"], trip["id"]],
    )
    return trip, (await remaining_cur.fetchone())[0]
