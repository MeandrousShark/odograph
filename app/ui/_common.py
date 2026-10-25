from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from psycopg.rows import dict_row
from starlette.responses import Response

from app.account_context import account_id
from app.detector.core import haversine_m
from app.trip_queries import (  # noqa: F401
    CATEGORIES,
    DISPLAY_DISTANCE_SQL,
    EXCLUSION_FILTER_NONE,
    EXCLUSIONS,
    REPORT_TRIP_COLUMNS,
    TRIP_COLUMNS,
    VEHICLE_FILTER_UNASSIGNED,
    _END_ADDRESS_SQL,
    _END_PLACE_NAME_SQL,
    _START_ADDRESS_SQL,
    _START_PLACE_NAME_SQL,
    _escape_ilike_term,
    _trip_filter_sql,
)

RULE_CATEGORIES = ("business", "personal")
# One definition of these exact user-facing strings, so trip detail, review,
# manual entry, batch edit, and every report/stats surface that names an
# exclusion render identical wording rather than drifting template by
# template.
EXCLUSION_LABELS = {
    "not_my_vehicle": "Not one of my vehicles",
    "not_deductible": "My vehicle, someone else drove",
}
EXPORT_MEDIA_TYPES = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class ManualTripValidationError(ValueError):
    """Lives here (not app/ui/manual.py, which defines it conceptually)
    because the trip-edit path in app/ui/trips.py needs to raise/catch the
    same error class as manual-trip create, and app/ui/manual.py already
    imports from this module -- defining it there instead would make this
    module import back from manual.py, a cycle. app/ui/manual.py imports
    this name from here so every existing `from app.ui.manual import
    ManualTripValidationError` (including app/ui/__init__.py's re-export)
    keeps working unchanged.
    """
    def __init__(self, errors: dict[str, str]):
        super().__init__(next(iter(errors.values())))
        self.errors = errors


# Matches the database's own char_length cap on trips.start_label/end_label
# (migrations/025_manual_trip_labels.sql) so a value this function accepts
# can never be rejected by the constraint, and vice versa.
TRIP_LABEL_MAX_LENGTH = 100


def normalize_trip_label(value: str, field: str) -> str | None:
    """Shared trim/blank/length handling for a manual trip's optional start
    or end display label, used identically by manual create
    (app/ui/manual.py) and trip edit (app/ui/trips.py) so a direct request
    to either route sees the same behavior instead of two normalizers
    silently drifting apart. Length is counted in characters, not bytes, so
    a name written in a multi-byte script isn't penalized for its encoded
    size -- the same reasoning the database constraint's `char_length`
    (rather than `octet_length`) follows.
    """
    trimmed = value.strip()
    if not trimmed:
        return None
    if len(trimmed) > TRIP_LABEL_MAX_LENGTH:
        raise ManualTripValidationError(
            {field: f"Keep it to {TRIP_LABEL_MAX_LENGTH} characters or fewer."}
        )
    return trimmed


def parse_date_range(
    from_str: str, to_str: str, tz: ZoneInfo
) -> tuple[datetime | None, datetime | None]:
    """Parse `YYYY-MM-DD` `from`/`to` query params (local to `tz`) into a
    half-open UTC-comparable range: `[from_dt, to_dt)`. `to` is inclusive of
    that calendar day, so its exclusive upper bound is local midnight of the
    next day. Malformed or empty strings are ignored (None = open-ended).
    """
    def _parse(s: str) -> datetime | None:
        if not s:
            return None
        try:
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=tz)
        except ValueError:
            return None

    from_dt = _parse(from_str)
    to_dt = _parse(to_str)
    if to_dt is not None:
        to_dt += timedelta(days=1)
    return from_dt, to_dt


def _parse_range_query_dates(from_str: str, to_str: str) -> tuple[date, date]:
    """Strict `from`/`to` (`YYYY-MM-DD`) parsing for the range-report
    routes. Unlike `parse_date_range`'s open-ended-on-junk trip-list filter
    (a bad date there just means "no filter"), there's no sensible default
    range for a report to fall back to. Malformed or missing `from`/`to`
    is a plain 400, never a silently empty or year-wide report.
    """
    try:
        return date.fromisoformat(from_str), date.fromisoformat(to_str)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid date: 'from' and 'to' must both be YYYY-MM-DD"
        )




async def _fetch_recent_purposes(conn, limit: int = 10) -> list[str]:
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


def _parse_vehicle_id(vehicle: str) -> int | None | Literal["none"]:
    """A malformed/empty `vehicle` query param means "no vehicle filter",
    same open-ended-on-junk-input treatment `parse_date_range` gives a bad
    date, rather than raising 400 for what's normally just an unset `<select>`.
    `VEHICLE_FILTER_UNASSIGNED` is the one non-numeric value that isn't junk.
    """
    if vehicle == VEHICLE_FILTER_UNASSIGNED:
        return VEHICLE_FILTER_UNASSIGNED
    try:
        return int(vehicle) if vehicle else None
    except ValueError:
        return None


def _parse_vehicle_form(vehicle_id: str) -> int | None:
    """Form-field counterpart of `_parse_vehicle_id`: empty means "no
    vehicle" (NULL), but junk in a POSTed field is a 400 rather than being
    silently treated as unset. A write should never guess.
    """
    try:
        return int(vehicle_id) if vehicle_id else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid vehicle")




def _url_with_filters(
    path: str, from_str: str, to_str: str, vehicle_str: str, q_str: str = "", **leading
) -> str:
    """One query-string builder for every link that carries the current
    filter set, so the views can't drift on param names or ordering.
    `leading` params (category, format, offset) come first; empty values are
    dropped (but a genuine 0, e.g. `offset`, is kept). A whitespace-only
    `q_str` is dropped the same way, matching `_trip_filter_sql` treating it
    as no search: a page with no active search term builds byte-identical
    URLs to before this filter existed. The term is stored stripped, so a
    padded term cannot make an otherwise identical link differ.
    """
    params = {k: v for k, v in leading.items() if v != "" and v is not None}
    if from_str:
        params["from"] = from_str
    if to_str:
        params["to"] = to_str
    if vehicle_str:
        params["vehicle"] = vehicle_str
    term = q_str.strip()
    if term:
        params["q"] = term
    return f"{path}?{urlencode(params)}" if params else path


def _month_bounds(year: int, month: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=tz)
    end = (
        datetime(year + 1, 1, 1, tzinfo=tz)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=tz)
    )
    return start, end


def _month_page_url(
    year: int,
    month: int,
    offset: int,
    category: str,
    from_str: str,
    to_str: str,
    vehicle: str,
    q: str = "",
    exclusion: str = "",
) -> str:
    return _url_with_filters(
        f"/trips/month/{year}/{month}", from_str, to_str, vehicle, q,
        offset=offset, category=category, exclusion=exclusion,
    )


IN_PROGRESS_DETAIL = "This trip is still in progress. Try again once it ends."


async def refuse_in_progress_trips(conn, trip_ids: list[int]) -> None:
    """Refuse an edit to a trip whose end the detector has not settled.

    A trip ending at an arrival the detector has only provisionally accepted,
    or kept while the drive goes on, can still grow: a discard override over
    it could later hide the longer real trip, and a merge or split of it has
    no settled trip to produce. Such a trip is its device's latest detected
    trip and has no persisted stay at its end. A device that has sent nothing
    for a day is not waiting on a next fix, so its trip is editable again.
    Callers hold the detector lock.
    """
    cur = await conn.execute(
        "SELECT 1 FROM trips t WHERE t.account_id = %s AND t.id = ANY(%s) "
        "AND t.source = 'detected' AND NOT t.imported "
        "AND NOT EXISTS (SELECT 1 FROM stays s WHERE s.account_id = t.account_id "
        " AND s.tracking_device_id = t.tracking_device_id AND s.started_at = t.ended_at) "
        "AND EXISTS (SELECT 1 FROM points p WHERE p.account_id = t.account_id "
        " AND p.tracking_device_id = t.tracking_device_id "
        " AND p.received_at > now() - interval '24 hours') "
        "AND NOT EXISTS (SELECT 1 FROM trips l WHERE l.account_id = t.account_id "
        " AND l.tracking_device_id = t.tracking_device_id AND l.source = 'detected' "
        " AND NOT l.imported AND l.started_at > t.started_at) "
        "LIMIT 1",
        (account_id(conn), trip_ids),
    )
    if await cur.fetchone():
        raise HTTPException(status_code=409, detail=IN_PROGRESS_DETAIL)


async def _fetch_trip(pool, trip_id: int) -> dict:
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            f"SELECT {TRIP_COLUMNS} FROM trips WHERE id = %s AND account_id = %s",
            (trip_id, account_id(conn)),
        )
        trip = await cur.fetchone()
    if not trip:
        raise HTTPException(status_code=404, detail="No such trip")
    return trip


def _redirect_back(request: Request, default: str = "/settings") -> Response:
    return Response(status_code=204, headers={"HX-Redirect": request.headers.get("referer") or default})


def _poke_snap_worker(request: Request) -> None:
    """Wake snapping only after a direct UI reprocess has committed.

    Callers invoke this after leaving their pool connection context, which is
    the commit boundary. Keeping the optional-worker lookup here makes every
    merge/split/restore path behave the same when OSRM is disabled and
    makes it impossible to accidentally poke during a rolled-back transaction.
    """
    worker = getattr(request.app.state, "snap_worker", None)
    if worker is not None:
        worker.poke()


def _path_distance_m(rows: list[tuple]) -> float:
    """Use the detector's segment yardstick so split validation cannot drift."""
    return sum(
        haversine_m(a[2], a[3], b[2], b[3]) for a, b in zip(rows, rows[1:])
    )
