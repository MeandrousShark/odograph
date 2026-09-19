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
from app.geocode import GEOCODE_PRECISION
from app.trip_queries import DISPLAY_DISTANCE_SQL

# Place names/addresses are correlated subselects (not JOINs) so every query
# built on TRIP_COLUMNS picks them up without touching its FROM clause.
# Factored into module-level constants (rather than left inline) so
# `_trip_filter_sql`'s search predicate can reuse the exact same subselects
# instead of duplicating them in the WHERE clause.
_START_PLACE_NAME_SQL = "(SELECT name FROM places WHERE account_id = trips.account_id AND id = trips.start_place_id)"
_END_PLACE_NAME_SQL = "(SELECT name FROM places WHERE account_id = trips.account_id AND id = trips.end_place_id)"
_START_ADDRESS_SQL = (
    "(SELECT address FROM geocode_cache\n"
    f"     WHERE account_id = trips.account_id AND lat = ROUND(ST_Y(trips.start_geom::geometry)::numeric, {GEOCODE_PRECISION})\n"
    f"       AND lon = ROUND(ST_X(trips.start_geom::geometry)::numeric, {GEOCODE_PRECISION}))"
)
_END_ADDRESS_SQL = (
    "(SELECT address FROM geocode_cache\n"
    f"     WHERE account_id = trips.account_id AND lat = ROUND(ST_Y(trips.end_geom::geometry)::numeric, {GEOCODE_PRECISION})\n"
    f"       AND lon = ROUND(ST_X(trips.end_geom::geometry)::numeric, {GEOCODE_PRECISION}))"
)

# display_distance_m is the canonical "distance to show": snapped when
# available, raw as fallback (raw distance_m stays selected as the pre-snap
# baseline).
TRIP_COLUMNS = f"""
    id, device, tracking_device_id, source::text AS source, started_at, ended_at, distance_m,
    {DISPLAY_DISTANCE_SQL} AS display_distance_m,
    snap_status::text AS snap_status,
    point_count, has_gap, imported, category::text AS category,
    exclusion::text AS exclusion, purpose, notes,
    start_label, end_label,
    -- Raw place ids, not just start_place_name/end_place_name below: the
    -- trip-edit card's per-endpoint label eligibility has to mirror
    -- migrations/025_manual_trip_labels.sql's own constraint columns
    -- exactly, and a resolved place *name* is not a sound proxy for
    -- whether a place id is actually set.
    start_place_id, end_place_id,
    ST_Y(start_geom::geometry) AS start_lat, ST_X(start_geom::geometry) AS start_lon,
    ST_Y(end_geom::geometry) AS end_lat, ST_X(end_geom::geometry) AS end_lon,
    -- A custom label and a saved place can never both name the same
    -- endpoint: migrations/025_manual_trip_labels.sql requires a label's
    -- place id to be null. That makes "label, else saved-place name" an
    -- unambiguous precedence to resolve right here, so every display
    -- consumer keeps calling describe_endpoint/describe_compact_endpoint on
    -- start_place_name/end_place_name exactly as before and never has to
    -- learn that labels exist.
    COALESCE(start_label, {_START_PLACE_NAME_SQL}) AS start_place_name,
    COALESCE(end_label, {_END_PLACE_NAME_SQL}) AS end_place_name,
    vehicle_id,
    -- Subselect (not JOIN) so a deactivated vehicle still shows its name on
    -- trips that point at it, despite being absent from the default picker.
    (SELECT name FROM vehicles WHERE account_id = trips.account_id AND id = trips.vehicle_id) AS vehicle_name,
    (SELECT count(*) FROM expenses WHERE expenses.account_id = trips.account_id AND expenses.trip_id = trips.id) AS expense_count,
    (path IS NOT NULL OR path_snapped IS NOT NULL) AS has_route_geometry,
    {_START_ADDRESS_SQL} AS start_address,
    {_END_ADDRESS_SQL} AS end_address,
    -- Missing-trip detection: four near-identical subselects for the
    -- previous included trip, because one SELECT item can't reference
    -- another's alias (and a LATERAL join would mean touching every FROM
    -- clause that embeds TRIP_COLUMNS; deferred until this scales past
    -- "acceptable"). A not_my_vehicle trip remains outside missing-trip
    -- continuity, so it cannot create or suppress a warning indirectly.
    -- No `end_geom IS NOT NULL` filter: skipping an included predecessor that
    -- lacks end_geom would silently pick an even older trip, while ST_Distance
    -- against NULL is NULL, exactly "no badge".
    (SELECT ST_Distance(p.end_geom, trips.start_geom) FROM trips p
     WHERE p.account_id = trips.account_id AND p.tracking_device_id = trips.tracking_device_id AND p.source = 'detected'
       AND p.exclusion IS DISTINCT FROM 'not_my_vehicle'
       AND p.started_at < trips.started_at
     ORDER BY p.started_at DESC LIMIT 1) AS prev_end_gap_m,
    (SELECT p.ended_at FROM trips p
     WHERE p.account_id = trips.account_id AND p.tracking_device_id = trips.tracking_device_id AND p.source = 'detected'
       AND p.exclusion IS DISTINCT FROM 'not_my_vehicle'
       AND p.started_at < trips.started_at
     ORDER BY p.started_at DESC LIMIT 1) AS prev_trip_ended_at,
    (SELECT ST_Y(p.end_geom::geometry) FROM trips p
     WHERE p.account_id = trips.account_id AND p.tracking_device_id = trips.tracking_device_id AND p.source = 'detected'
       AND p.exclusion IS DISTINCT FROM 'not_my_vehicle'
       AND p.started_at < trips.started_at
     ORDER BY p.started_at DESC LIMIT 1) AS prev_trip_end_lat,
    (SELECT ST_X(p.end_geom::geometry) FROM trips p
     WHERE p.account_id = trips.account_id AND p.tracking_device_id = trips.tracking_device_id AND p.source = 'detected'
       AND p.exclusion IS DISTINCT FROM 'not_my_vehicle'
       AND p.started_at < trips.started_at
     ORDER BY p.started_at DESC LIMIT 1) AS prev_trip_end_lon,
    (SELECT name FROM places WHERE account_id = trips.account_id AND id = (
       SELECT p.end_place_id FROM trips p
       WHERE p.account_id = trips.account_id AND p.tracking_device_id = trips.tracking_device_id AND p.source = 'detected'
         AND p.exclusion IS DISTINCT FROM 'not_my_vehicle'
         AND p.started_at < trips.started_at
       ORDER BY p.started_at DESC LIMIT 1
     )) AS prev_trip_end_place_name,
    -- Suppression: a manual trip overlapping the window between the
    -- predecessor's end and this trip's start clears the badge. Re-derives
    -- the predecessor's ended_at once more purely to test the overlap.
    EXISTS (
      SELECT 1 FROM trips m
      WHERE m.account_id = trips.account_id AND m.source = 'manual' AND m.started_at < trips.started_at
        AND m.exclusion IS DISTINCT FROM 'not_my_vehicle'
        AND m.ended_at > (
          SELECT p.ended_at FROM trips p
          WHERE p.account_id = trips.account_id AND p.tracking_device_id = trips.tracking_device_id AND p.source = 'detected'
            AND p.exclusion IS DISTINCT FROM 'not_my_vehicle'
            AND p.started_at < trips.started_at
          ORDER BY p.started_at DESC LIMIT 1
        )
    ) AS missing_trip_covered
"""

CATEGORIES = ("business", "personal", "unclassified")
RULE_CATEGORIES = ("business", "personal")
EXCLUSIONS = ("not_my_vehicle", "not_deductible")
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


# The vehicle filter's `<select>` has three states, not two: no filter, one
# specific vehicle, or "only trips with no vehicle at all". The third can't
# be an int, so it needs its own value distinct from every real vehicle id;
# reusing the query string's own "none" spelling keeps _parse_vehicle_id and
# _trip_filter_sql agreeing on one literal instead of a second constant.
VEHICLE_FILTER_UNASSIGNED = "none"

# Same shape as VEHICLE_FILTER_UNASSIGNED above: the exclusion filter also
# has a state -- "normal trips only" -- that isn't one of EXCLUSIONS' enum
# values, so it needs a sentinel distinct from every real value rather than
# being folded into the enum check in _trip_filter_sql.
EXCLUSION_FILTER_NONE = "none"


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


def _escape_ilike_term(term: str) -> str:
    """Escape `%`, `_`, and backslash so a raw search term matches those
    characters literally instead of acting as ILIKE wildcards/escape
    introducer. Backslash must be escaped first, or escaping % and _ would
    double-escape the backslashes this step just inserted.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _trip_filter_sql(
    category: str, from_dt: datetime | None, to_dt: datetime | None,
    vehicle_id: int | None | Literal["none"] = None,
    q: str = "",
    exclusion: str = "",
    *, owner_id: int,
) -> tuple[str, list]:
    """Build a `WHERE` clause + params list for filtering trips by category,
    vehicle, date range, exclusion state, and/or a free-text search term.
    Shared by the trip list, `/export`, the month pager, and `/review` so
    none of them can drift apart.

    The search term matches notes, purpose, either place name, either custom
    endpoint label, or either cached address, case-insensitively and on
    substrings -- the same four place/address subselects `TRIP_COLUMNS`
    selects, reused here rather than duplicated, plus the raw
    `start_label`/`end_label` columns `TRIP_COLUMNS` also selects directly,
    so the predicate can never see different text than what the archive
    displays. An empty or whitespace-only term is no search at all, so the
    generated SQL (and every URL built from it) is unchanged from before
    this filter existed.

    `exclusion` follows the same forgiving-on-junk posture as `category` and
    `vehicle_id`: an empty string or anything outside `EXCLUSIONS` is no
    filter at all, so a page with no exclusion filter selected still builds
    the exact SQL and params this function produced before the parameter
    existed. `EXCLUSION_FILTER_NONE` is the one non-enum value that isn't
    junk, the same role `VEHICLE_FILTER_UNASSIGNED` plays for `vehicle_id`:
    it means "normal trips only", i.e. `exclusion IS NULL`.
    """
    if type(owner_id) is not int or not 1 <= owner_id <= 2**63 - 1:
        raise ValueError("owner_id must be a positive bigint")
    clauses = ["trips.account_id = %s"]
    params: list = [owner_id]
    if category in CATEGORIES:
        clauses.append("category = %s")
        params.append(category)
    if vehicle_id == VEHICLE_FILTER_UNASSIGNED:
        clauses.append("vehicle_id IS NULL")
    elif vehicle_id is not None:
        clauses.append("vehicle_id = %s")
        params.append(vehicle_id)
    if exclusion == EXCLUSION_FILTER_NONE:
        clauses.append("exclusion IS NULL")
    elif exclusion in EXCLUSIONS:
        clauses.append("exclusion = %s")
        params.append(exclusion)
    if from_dt is not None:
        clauses.append("started_at >= %s")
        params.append(from_dt)
    if to_dt is not None:
        clauses.append("started_at < %s")
        params.append(to_dt)
    term = q.strip()
    if term:
        pattern = f"%{_escape_ilike_term(term)}%"
        clauses.append(
            "(notes ILIKE %s ESCAPE '\\' OR purpose ILIKE %s ESCAPE '\\' OR "
            f"{_START_PLACE_NAME_SQL} ILIKE %s ESCAPE '\\' OR "
            f"{_END_PLACE_NAME_SQL} ILIKE %s ESCAPE '\\' OR "
            "start_label ILIKE %s ESCAPE '\\' OR end_label ILIKE %s ESCAPE '\\' OR "
            f"{_START_ADDRESS_SQL} ILIKE %s ESCAPE '\\' OR "
            f"{_END_ADDRESS_SQL} ILIKE %s ESCAPE '\\')"
        )
        params.extend([pattern] * 8)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


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
