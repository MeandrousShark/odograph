from __future__ import annotations

import json
import logging
import math
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Path, Query, Request
from psycopg import errors
from psycopg.rows import dict_row
from starlette.responses import JSONResponse, RedirectResponse, Response

from app.auth import require_csrf, require_user
from app.dashboard import build_week_dashboard, parse_week_anchor, week_bounds
from app.detector.core import haversine_m
from app.detector.runner import (
    ADVISORY_LOCK_KEY,
    DETECTOR_VERSION,
    load_trip_points,
    reprocess_places,
)
from app.diagnose import build_report, run_connectivity_checks
from app.merge import TripSpan, plan_merge_selected
from app.export import to_csv, to_range_report_xlsx, to_report_xlsx, to_xlsx
from app.expenses import (
    CATEGORY_LABELS,
    EXPENSE_CATEGORIES,
    EXPENSE_TREATMENTS,
    TREATMENT_LABELS,
    build_expense_report,
    default_treatment,
)
from app.missing_trip import missing_trip_badge
from app.odometer import OdometerReading, reconcile, vehicle_coverage_for_report
from app.places_desc import PLACE_KINDS
from app.rates import ENV_PREFIX, METERS_PER_MILE, deduction, load_rates
from app.report import (
    build_annual_report,
    build_range_report,
    default_report_year,
    next_year_disabled,
    range_filename_slug,
    sum_month_deductions,
)
from app.snap import route_distance_m
from app.stats import build_dashboard
from app.validation import parse_finite_number
from app.vehicles import (
    create_vehicle,
    deactivate_vehicle,
    get_auto_assign_default_vehicle,
    list_vehicles,
    set_auto_assign_default_vehicle,
    set_default_vehicle,
    update_vehicle,
)

log = logging.getLogger(__name__)

# Place names/addresses are correlated subselects (not JOINs) so every query
# built on TRIP_COLUMNS picks them up without touching its FROM clause.
# display_distance_m is the canonical "distance to show": snapped when
# available, raw as fallback (raw distance_m stays selected as the pre-snap
# baseline).
TRIP_COLUMNS = """
    id, device, source::text AS source, started_at, ended_at, distance_m,
    COALESCE(distance_snapped_m, distance_m) AS display_distance_m,
    snap_status::text AS snap_status,
    point_count, has_gap, imported, category::text AS category, purpose, notes,
    ST_Y(start_geom::geometry) AS start_lat, ST_X(start_geom::geometry) AS start_lon,
    ST_Y(end_geom::geometry) AS end_lat, ST_X(end_geom::geometry) AS end_lon,
    (SELECT name FROM places WHERE id = trips.start_place_id) AS start_place_name,
    (SELECT name FROM places WHERE id = trips.end_place_id) AS end_place_name,
    vehicle_id,
    -- Subselect (not JOIN) so a deactivated vehicle still shows its name on
    -- trips that point at it, despite being absent from the default picker.
    (SELECT name FROM vehicles WHERE id = trips.vehicle_id) AS vehicle_name,
    (path IS NOT NULL OR path_snapped IS NOT NULL) AS has_route_geometry,
    (SELECT address FROM geocode_cache
     WHERE lat = ROUND(ST_Y(trips.start_geom::geometry)::numeric, 4)
       AND lon = ROUND(ST_X(trips.start_geom::geometry)::numeric, 4)) AS start_address,
    (SELECT address FROM geocode_cache
     WHERE lat = ROUND(ST_Y(trips.end_geom::geometry)::numeric, 4)
       AND lon = ROUND(ST_X(trips.end_geom::geometry)::numeric, 4)) AS end_address,
    -- Missing-trip detection: four near-identical subselects for the
    -- predecessor trip, because one SELECT item can't reference another's
    -- alias (and a LATERAL join would mean touching every FROM clause that
    -- embeds TRIP_COLUMNS; deferred until this scales past "acceptable").
    -- No `end_geom IS NOT NULL` filter: skipping a predecessor that lacks
    -- end_geom would silently pick an even older trip, while ST_Distance
    -- against NULL is NULL — exactly "no badge".
    (SELECT ST_Distance(p.end_geom, trips.start_geom) FROM trips p
     WHERE p.device = trips.device AND p.source = 'detected'
       AND p.started_at < trips.started_at
     ORDER BY p.started_at DESC LIMIT 1) AS prev_end_gap_m,
    (SELECT p.ended_at FROM trips p
     WHERE p.device = trips.device AND p.source = 'detected'
       AND p.started_at < trips.started_at
     ORDER BY p.started_at DESC LIMIT 1) AS prev_trip_ended_at,
    (SELECT ST_Y(p.end_geom::geometry) FROM trips p
     WHERE p.device = trips.device AND p.source = 'detected'
       AND p.started_at < trips.started_at
     ORDER BY p.started_at DESC LIMIT 1) AS prev_trip_end_lat,
    (SELECT ST_X(p.end_geom::geometry) FROM trips p
     WHERE p.device = trips.device AND p.source = 'detected'
       AND p.started_at < trips.started_at
     ORDER BY p.started_at DESC LIMIT 1) AS prev_trip_end_lon,
    (SELECT name FROM places WHERE id = (
       SELECT p.end_place_id FROM trips p
       WHERE p.device = trips.device AND p.source = 'detected'
         AND p.started_at < trips.started_at
       ORDER BY p.started_at DESC LIMIT 1
     )) AS prev_trip_end_place_name,
    -- Suppression: a manual trip overlapping the window between the
    -- predecessor's end and this trip's start clears the badge. Re-derives
    -- the predecessor's ended_at once more purely to test the overlap.
    EXISTS (
      SELECT 1 FROM trips m
      WHERE m.source = 'manual' AND m.started_at < trips.started_at
        AND m.ended_at > (
          SELECT p.ended_at FROM trips p
          WHERE p.device = trips.device AND p.source = 'detected'
            AND p.started_at < trips.started_at
          ORDER BY p.started_at DESC LIMIT 1
        )
    ) AS missing_trip_covered
"""

CATEGORIES = ("business", "personal", "unclassified")
RULE_CATEGORIES = ("business", "personal")
EXPORT_MEDIA_TYPES = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


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
    range for a report to fall back to — malformed or missing `from`/`to`
    is a plain 400, never a silently empty or year-wide report.
    """
    try:
        return date.fromisoformat(from_str), date.fromisoformat(to_str)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid date — 'from' and 'to' must both be YYYY-MM-DD"
        )


# The vehicle filter's `<select>` has three states, not two: no filter, one
# specific vehicle, or "only trips with no vehicle at all". The third can't
# be an int, so it needs its own value distinct from every real vehicle id;
# reusing the query string's own "none" spelling keeps _parse_vehicle_id and
# _trip_filter_sql agreeing on one literal instead of a second constant.
VEHICLE_FILTER_UNASSIGNED = "none"


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
    silently treated as unset — a write should never guess.
    """
    try:
        return int(vehicle_id) if vehicle_id else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid vehicle")


class ManualTripValidationError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__(next(iter(errors.values())))
        self.errors = errors


def _local_time_is_real(naive: datetime, tz: ZoneInfo) -> bool:
    """A spring-forward gap wall time (e.g. 02:30 when clocks jump 02:00 to
    03:00) has no corresponding instant, so attaching a timezone to it and
    normalizing through UTC changes the wall clock. A fall-back ambiguous
    time (occurs twice) round-trips unchanged and must stay accepted.
    """
    aware = naive.replace(tzinfo=tz)
    return aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) == naive


def parse_manual_trip_input(
    date_value: str,
    start_time: str,
    end_time: str,
    distance: str,
    tz: ZoneInfo,
) -> tuple[datetime, datetime, float]:
    """Interpret manual trip fields once for create and edit.

    The browser submits wall-clock values without an offset. Attaching the
    configured display timezone, rather than the server or browser timezone,
    keeps a later edit from moving a trip across a local reporting boundary.
    End times at or before the start represent an overnight trip, matching the
    original manual-entry contract. Distance is parsed from text so NaN and
    infinity cannot bypass a simple positive-number comparison.
    """
    errors: dict[str, str] = {}
    try:
        naive_start = datetime.fromisoformat(f"{date_value}T{start_time}")
    except ValueError:
        naive_start = None
        if not date_value:
            errors["date"] = "Enter a date."
        elif not start_time:
            errors["start_time"] = "Enter a start time."
        else:
            errors["date"] = "Enter a valid date and start time."
    else:
        if not _local_time_is_real(naive_start, tz):
            errors["start_time"] = (
                "That time does not exist on this date (clocks skip forward for "
                "daylight saving). Enter a later time."
            )
    started_at = naive_start.replace(tzinfo=tz) if naive_start is not None else None
    try:
        naive_end = datetime.fromisoformat(f"{date_value}T{end_time}")
    except ValueError:
        naive_end = None
        errors["end_time"] = "Enter a valid end time."
    else:
        if not _local_time_is_real(naive_end, tz):
            errors["end_time"] = (
                "That time does not exist on this date (clocks skip forward for "
                "daylight saving). Enter a later time."
            )
    ended_at = naive_end.replace(tzinfo=tz) if naive_end is not None else None
    try:
        distance_miles = float(distance)
    except (TypeError, ValueError):
        distance_miles = math.nan
    if not math.isfinite(distance_miles) or distance_miles <= 0:
        errors["distance"] = "Distance must be a positive finite number."
    if errors:
        raise ManualTripValidationError(errors)
    assert started_at is not None and ended_at is not None
    if ended_at <= started_at:
        ended_at += timedelta(days=1)
    distance_m = distance_miles * METERS_PER_MILE
    if not math.isfinite(distance_m):
        raise ManualTripValidationError(
            {"distance": "Distance must be a positive finite number."}
        )
    return started_at, ended_at, distance_m


def _trip_filter_sql(
    category: str, from_dt: datetime | None, to_dt: datetime | None,
    vehicle_id: int | None | Literal["none"] = None,
) -> tuple[str, list]:
    """Build a `WHERE` clause + params list for filtering trips by category,
    vehicle, and/or `started_at` range. Shared by the trip list, `/export`,
    the month pager, and `/review` so none of them can drift apart.
    """
    clauses = []
    params: list = []
    if category in CATEGORIES:
        clauses.append("category = %s")
        params.append(category)
    if vehicle_id == VEHICLE_FILTER_UNASSIGNED:
        clauses.append("vehicle_id IS NULL")
    elif vehicle_id is not None:
        clauses.append("vehicle_id = %s")
        params.append(vehicle_id)
    if from_dt is not None:
        clauses.append("started_at >= %s")
        params.append(from_dt)
    if to_dt is not None:
        clauses.append("started_at < %s")
        params.append(to_dt)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def _url_with_filters(path: str, from_str: str, to_str: str, vehicle_str: str, **leading) -> str:
    """One query-string builder for every link that carries the current
    filter set, so the views can't drift on param names or ordering.
    `leading` params (category, format, offset) come first; empty values are
    dropped (but a genuine 0, e.g. `offset`, is kept).
    """
    params = {k: v for k, v in leading.items() if v != "" and v is not None}
    if from_str:
        params["from"] = from_str
    if to_str:
        params["to"] = to_str
    if vehicle_str:
        params["vehicle"] = vehicle_str
    return f"{path}?{urlencode(params)}" if params else path


def _filter_url_factory(from_str: str, to_str: str, vehicle_str: str):
    """Template-callable building the category pills' `/trips` links."""
    return lambda category: _url_with_filters(
        "/trips", from_str, to_str, vehicle_str, category=category
    )


def _export_url_factory(category: str, from_str: str, to_str: str, vehicle_str: str):
    """Template-callable building `/export` links carrying the current
    filter, so an export always matches what's on screen.
    """
    return lambda fmt: _url_with_filters(
        "/export", from_str, to_str, vehicle_str, format=fmt, category=category
    )


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
) -> str:
    return _url_with_filters(
        f"/trips/month/{year}/{month}", from_str, to_str, vehicle,
        offset=offset, category=category,
    )


async def _fetch_month_page(
    conn,
    tz: ZoneInfo,
    year: int,
    month: int,
    page_size: int,
    offset: int,
    category: str,
    from_dt: datetime | None,
    to_dt: datetime | None,
    vehicle_id: int | None | Literal["none"],
) -> tuple[list[dict], bool]:
    """Fetch one stable local-month page plus a one-row `has_more` sentinel."""
    month_start, month_end = _month_bounds(year, month, tz)
    where, params = _trip_filter_sql(category, from_dt, to_dt, vehicle_id)
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


async def _fetch_range_trips(pool, tz: ZoneInfo, start: date, end: date) -> tuple[list[dict], dict]:
    """Trips + rates for a `start`..`end` span (both inclusive, local to `tz`),
    shared by the annual/range report pages and their exports so they can't
    drift apart. The DB-side range is only a coarse UTC pre-filter;
    `build_range_report` re-localizes and drops anything outside `[start, end]`,
    so a trip near the boundary can only be excluded, never mis-attributed.
    `end + 1 day` is computed on the plain `date` (not by adding a timedelta
    to an aware datetime) so a DST transition can't shift the boundary's
    local day — same reason `_month_bounds` builds its boundary directly.
    """
    range_start = datetime(start.year, start.month, start.day, tzinfo=tz)
    next_day = end + timedelta(days=1)
    range_end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=tz)
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            f"SELECT {TRIP_COLUMNS} FROM trips WHERE started_at >= %s AND started_at < %s"
            " ORDER BY started_at",
            (range_start, range_end),
        )
        trips = await cur.fetchall()
        rates = await load_rates(conn)
    return trips, rates


async def _fetch_year_odometer_coverage(pool, tz: ZoneInfo, year: int, trips: list[dict]) -> list:
    """Per-vehicle odometer coverage for the report year, computed outside
    `build_annual_report` from the same `trips` the report already fetched —
    only the year's readings need a query. The upper bound is `<=
    next_year_start` (not `<`) so a reading recorded exactly at midnight
    Jan 1 of the following year, the one reading that actually brackets the
    end of the report year, is included; anything after that boundary is
    still excluded so the reconciliation span stays within the report year.
    """
    year_start = datetime(year, 1, 1, tzinfo=tz)
    next_year_start = datetime(year + 1, 1, 1, tzinfo=tz)
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT odometer_readings.vehicle_id, odometer_readings.recorded_at, "
            "odometer_readings.odometer_m, vehicles.name AS vehicle_name "
            "FROM odometer_readings JOIN vehicles ON vehicles.id = odometer_readings.vehicle_id "
            "WHERE recorded_at >= %s AND recorded_at <= %s",
            (year_start, next_year_start),
        )
        rows = await cur.fetchall()

    readings_by_vehicle: dict[tuple[int, str], list[OdometerReading]] = {}
    for row in rows:
        key = (row["vehicle_id"], row["vehicle_name"])
        readings_by_vehicle.setdefault(key, []).append(
            OdometerReading(row["recorded_at"], row["odometer_m"])
        )
    if not readings_by_vehicle:
        return []

    trips_by_vehicle: dict[tuple[int, str], list[tuple]] = {}
    for trip in trips:
        vehicle_id = trip.get("vehicle_id")
        vehicle_name = trip.get("vehicle_name")
        if vehicle_id is not None and vehicle_name:
            trips_by_vehicle.setdefault((vehicle_id, vehicle_name), []).append(
                (trip["started_at"], trip["display_distance_m"])
            )
    return vehicle_coverage_for_report(readings_by_vehicle, trips_by_vehicle, year_start, next_year_start)


async def _fetch_year_expense_report(pool, tz: ZoneInfo, year: int, trips: list[dict], rates: dict):
    """Fetch ledger rows plus the wider odometer boundary set the
    expense comparison needs.

    Unlike the within-year reconciliation, a tax-year denominator must span
    the entire year. Fetching all readings lets the pure layer choose the
    closest reading on each side without annualizing a partial interval.
    """
    async with pool.connection() as conn:
        expense_cur = conn.cursor(row_factory=dict_row)
        await expense_cur.execute(
            "SELECT expenses.id, expenses.vehicle_id, vehicles.name AS vehicle_name, "
            "expenses.incurred_on, expenses.category::text AS category, expenses.amount, "
            "expenses.treatment::text AS treatment, expenses.notes "
            "FROM expenses JOIN vehicles ON vehicles.id = expenses.vehicle_id "
            "WHERE expenses.incurred_on >= %s AND expenses.incurred_on < %s "
            "ORDER BY expenses.incurred_on, expenses.id",
            (date(year, 1, 1), date(year + 1, 1, 1)),
        )
        expenses = await expense_cur.fetchall()
        reading_cur = conn.cursor(row_factory=dict_row)
        await reading_cur.execute(
            "SELECT odometer_readings.vehicle_id, vehicles.name AS vehicle_name, "
            "odometer_readings.recorded_at, odometer_readings.odometer_m "
            "FROM odometer_readings JOIN vehicles ON vehicles.id = odometer_readings.vehicle_id "
            "ORDER BY odometer_readings.recorded_at"
        )
        readings = await reading_cur.fetchall()
    return expenses, build_expense_report(year, trips, expenses, readings, rates, tz)


def _parse_expense_input(
    incurred_on: str, category: str, amount: str, treatment: str,
) -> tuple[date, str, Decimal, str]:
    try:
        parsed_date = date.fromisoformat(incurred_on)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid expense date")
    if category not in EXPENSE_CATEGORIES:
        raise HTTPException(status_code=400, detail="Invalid expense category")
    try:
        raw_amount = Decimal(amount)
        parsed_amount = raw_amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="Invalid expense amount")
    if (
        not parsed_amount.is_finite() or parsed_amount != raw_amount
        or parsed_amount <= 0 or parsed_amount > Decimal("9999999999.99")
    ):
        raise HTTPException(status_code=400, detail="Invalid expense amount")
    if not treatment:
        if category == "other":
            raise HTTPException(status_code=400, detail="Other expenses require a tax treatment")
        treatment = default_treatment(category)
    if treatment not in EXPENSE_TREATMENTS:
        raise HTTPException(status_code=400, detail="Invalid expense treatment")
    return parsed_date, category, parsed_amount, treatment


async def _fetch_trip(pool, trip_id: int) -> dict:
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(f"SELECT {TRIP_COLUMNS} FROM trips WHERE id = %s", (trip_id,))
        trip = await cur.fetchone()
    if not trip:
        raise HTTPException(status_code=404, detail="No such trip")
    return trip


async def _resolve_missing_trip_osrm_hint(request: Request, bridge_trip: str) -> str | None:
    """The missing-trip badge's road-distance suggestion, resolved only when
    a badge's prefill link is followed — never per trip-list row. `bridge_trip`
    carries the later trip's id so this can re-fetch the coordinates itself
    rather than trusting lat/lon round-tripped through the URL. Degrades to
    `None` (no hint, not an error) on a malformed/stale id, missing OSRM
    config, missing coordinates, or any transport failure.
    """
    cfg = request.app.state.config
    http_client = request.app.state.osrm_http_client
    if not cfg.osrm_url or http_client is None:
        return None
    try:
        trip_id = int(bridge_trip)
    except ValueError:
        return None
    try:
        trip = await _fetch_trip(request.app.state.pool, trip_id)
    except HTTPException:
        return None
    from_lat, from_lon = trip.get("prev_trip_end_lat"), trip.get("prev_trip_end_lon")
    to_lat, to_lon = trip.get("start_lat"), trip.get("start_lon")
    if None in (from_lat, from_lon, to_lat, to_lon):
        return None
    try:
        distance_m = await route_distance_m(
            http_client, cfg.osrm_url, from_lat, from_lon, to_lat, to_lon,
        )
    except (httpx.HTTPError, ValueError) as e:
        # Not str(e): route_distance_m builds the request URL from the two
        # trip endpoints' exact coordinates, and a raise_for_status()
        # HTTPStatusError's message embeds the full URL it failed against.
        # The exception type is enough to know the suggestion failed.
        log.warning("missing-trip OSRM route suggestion failed: %s", type(e).__name__)
        return None
    if distance_m is None:
        return None
    return f"~{distance_m / METERS_PER_MILE:.1f} mi by road"


async def _fetch_trip_card_context(pool, trip_id: int) -> dict:
    """Build the one context shape used by every standalone card render.

    Active vehicles keep pickers concise, while TRIP_COLUMNS carries the name
    of an inactive vehicle already assigned to the trip so editing never
    silently makes that valid stored value unrepresentable.
    """
    trip = await _fetch_trip(pool, trip_id)
    async with pool.connection() as conn:
        vehicles = await list_vehicles(conn)
        recent_purposes = await _fetch_recent_purposes(conn)
    return {"trip": trip, "vehicles": vehicles, "recent_purposes": recent_purposes}


def _trip_edit_values(trip: dict, tz: ZoneInfo) -> dict[str, str]:
    started_at = trip["started_at"].astimezone(tz)
    ended_at = trip["ended_at"].astimezone(tz)
    return {
        "category": trip["category"],
        "purpose": trip.get("purpose") or "",
        "notes": trip.get("notes") or "",
        "vehicle_id": str(trip["vehicle_id"]) if trip.get("vehicle_id") is not None else "",
        "date": started_at.strftime("%Y-%m-%d"),
        "start_time": started_at.strftime("%H:%M"),
        "end_time": ended_at.strftime("%H:%M"),
        "distance": f"{float(trip['distance_m']) / METERS_PER_MILE:.1f}",
    }


async def _fetch_recent_purposes(conn, limit: int = 10) -> list[str]:
    """Return distinct, nonblank purposes ordered by their latest use.

    Values are trimmed at write time as well as here. Keeping the defensive
    trim in this query prevents older/direct SQL writes with surrounding
    whitespace from creating visually duplicate datalist suggestions.
    """
    cur = await conn.execute(
        "SELECT purpose FROM ("
        " SELECT DISTINCT ON (btrim(purpose)) btrim(purpose) AS purpose, updated_at"
        " FROM trips WHERE purpose IS NOT NULL AND btrim(purpose) <> ''"
        " ORDER BY btrim(purpose), updated_at DESC"
        ") recent ORDER BY updated_at DESC, purpose LIMIT %s",
        (limit,),
    )
    return [row[0] for row in await cur.fetchall()]


def _env_override_years() -> set[int]:
    years = set()
    for key in os.environ:
        if not key.startswith(ENV_PREFIX):
            continue
        try:
            years.add(int(key[len(ENV_PREFIX):]))
        except ValueError:
            continue
    return years


async def _fetch_rates_rows(conn) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT year, rate_per_mi::float AS rate_per_mi, "
        " rate_h2_per_mi::float AS rate_h2_per_mi, h2_start_month "
        "FROM mileage_rates ORDER BY year DESC"
    )
    rows = await cur.fetchall()
    env_years = _env_override_years()
    for row in rows:
        row["env_override"] = row["year"] in env_years
    return rows


async def _fetch_places_rows(conn) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, name, kind::text AS kind, "
        " ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon, radius_m "
        "FROM places ORDER BY name"
    )
    return await cur.fetchall()


async def _fetch_odometer_context(conn) -> list[dict]:
    """One entry per vehicle (active + inactive, like `_vehicles_table.html`
    itself), each with its readings (newest first) and, once it has >=2,
    the `ReconInterval` table between them. Grouped in Python rather than
    with a per-vehicle query or a window-function join: the vehicle/reading
    counts here are small, and this keeps `app.odometer.reconcile` — the
    part that actually needs to be correct — entirely out of SQL.
    """
    vehicles = await list_vehicles(conn, include_inactive=True)

    reading_cur = conn.cursor(row_factory=dict_row)
    await reading_cur.execute(
        "SELECT id, vehicle_id, recorded_at, odometer_m, note "
        "FROM odometer_readings ORDER BY recorded_at"
    )
    readings_by_vehicle: dict[int, list[dict]] = {}
    for row in await reading_cur.fetchall():
        readings_by_vehicle.setdefault(row["vehicle_id"], []).append(row)

    # Every trip with a vehicle, not just the report year — this view is a
    # running ledger, unlike the annual report's year-scoped coverage.
    trip_cur = conn.cursor(row_factory=dict_row)
    await trip_cur.execute(
        "SELECT vehicle_id, started_at, COALESCE(distance_snapped_m, distance_m) AS display_distance_m "
        "FROM trips WHERE vehicle_id IS NOT NULL"
    )
    trips_by_vehicle: dict[int, list[tuple]] = {}
    for row in await trip_cur.fetchall():
        trips_by_vehicle.setdefault(row["vehicle_id"], []).append(
            (row["started_at"], row["display_distance_m"])
        )

    result = []
    for vehicle in vehicles:
        rows = readings_by_vehicle.get(vehicle["id"], [])
        readings = [OdometerReading(r["recorded_at"], r["odometer_m"]) for r in rows]
        recon = reconcile(readings, trips_by_vehicle.get(vehicle["id"], []))
        result.append({
            "vehicle": vehicle,
            "readings": list(reversed(rows)),
            "intervals": recon.intervals,
        })
    return result


async def _fetch_device_fixes(conn) -> list[dict]:
    """One row per device that has ever posted a point, so a misconfigured
    phone (wrong tid, stale credentials, app killed by the OS) is visible on
    the Settings page instead of only showing up once trips stop appearing.
    Grouped aggregate over the `(device, recorded_at)` index -- no per-device
    query loop needed at this scale.
    """
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT device, max(received_at) AS newest_received_at, "
        "max(recorded_at) AS newest_recorded_at, count(*) AS point_count "
        "FROM points GROUP BY device ORDER BY device"
    )
    return await cur.fetchall()


async def _fetch_schema_version(conn) -> int:
    cur = await conn.execute("SELECT COALESCE(max(version), 0) FROM schema_migrations")
    row = await cur.fetchone()
    return row[0]


def _side_desc(place_id: int | None, kind: str | None, place_names: dict[int, str]) -> str:
    if place_id is not None:
        return place_names.get(place_id, f"place #{place_id}")
    if kind is not None:
        return f"Any {kind}"
    return "Any place"


async def _fetch_rules_rows(conn, places: list[dict]) -> list[dict]:
    place_names = {p["id"]: p["name"] for p in places}
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, a_place, a_kind::text AS a_kind, b_place, b_kind::text AS b_kind, "
        " category::text AS category FROM tag_rules ORDER BY id"
    )
    rows = await cur.fetchall()
    for row in rows:
        row["a_desc"] = _side_desc(row["a_place"], row["a_kind"], place_names)
        row["b_desc"] = _side_desc(row["b_place"], row["b_kind"], place_names)
    return rows


async def _fetch_boundary_overrides_rows(conn) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, device, kind::text AS kind, range_start, range_end, point_id, created_at "
        "FROM trip_boundary_overrides ORDER BY created_at DESC"
    )
    return await cur.fetchall()


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


async def _apply_human_tag(
    conn, trip_id: int, category: str, purpose: str | None = None,
    *, update_purpose: bool = False,
) -> None:
    """Set tag_source='human' unconditionally, even on a clear back to
    'unclassified' — the load-bearing line for human-tag supremacy: the
    auto-tagger (app.autotag) only touches rows with tag_source NULL or
    'rule', so a deliberate human choice can never be overwritten by a rule.
    Shared by the list-view tag buttons and /review's tag-and-advance so the
    guarantee lives in one place; category validation stays with each caller
    (the list view allows clearing to 'unclassified', review does not).
    """
    if update_purpose:
        await conn.execute(
            "UPDATE trips SET category = %s, purpose = %s, tag_source = 'human', "
            "updated_at = now() WHERE id = %s",
            (category, (purpose or "").strip() or None, trip_id),
        )
    else:
        await conn.execute(
            "UPDATE trips SET category = %s, tag_source = 'human', updated_at = now() "
            "WHERE id = %s",
            (category, trip_id),
        )


def _review_url(from_str: str, to_str: str, vehicle_str: str) -> str:
    """`/review` link carrying the current filters. No category param:
    `/review` always pins category to unclassified.
    """
    return _url_with_filters("/review", from_str, to_str, vehicle_str)


async def _trip_position(conn, trip_id: int) -> tuple[datetime, int] | None:
    """The `(started_at, id)` cursor a review pass advances past. Deliberately
    not filtered by category: tag-and-advance calls this on a trip that has
    just left the unclassified set. None means the trip vanished mid-pass —
    callers fall back to no cursor, restarting from the oldest trip rather
    than 404ing.
    """
    cur = await conn.execute("SELECT started_at FROM trips WHERE id = %s", (trip_id,))
    row = await cur.fetchone()
    return (row[0], trip_id) if row else None


async def _delete_trip_in(conn, trip_id: int) -> tuple[datetime, int]:
    """Apply source-appropriate deletion and return the trip's old cursor.

    Every delete entry point shares this helper so review and list/detail
    cannot drift. The detector advisory lock is intentionally acquired before
    reading the boundary timestamps: otherwise a background pass could rewrite
    them between capture and the discard insert. For detected trips, the
    durable discard and targeted row delete share the caller's transaction;
    later detector passes honor the override without rewriting unrelated trips.
    Manual trips remain a direct row DELETE.
    """
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
    cur = await conn.execute(
        "SELECT device, source::text, started_at, ended_at "
        "FROM trips WHERE id = %s FOR UPDATE",
        (trip_id,),
    )
    trip = await cur.fetchone()
    if not trip:
        raise HTTPException(status_code=404, detail="No such trip")
    device, source, started_at, ended_at = trip
    if source == "manual":
        await conn.execute("DELETE FROM trips WHERE id = %s", (trip_id,))
    else:
        await conn.execute(
            "INSERT INTO trip_boundary_overrides "
            "(device, kind, range_start, range_end) "
            "VALUES (%s, 'discard', %s, %s) ON CONFLICT DO NOTHING",
            (device, started_at, ended_at),
        )
        await conn.execute("DELETE FROM trips WHERE id = %s", (trip_id,))
    return started_at, trip_id


async def _fetch_review_card(
    conn, where: str, params: list, cursor: tuple[datetime, int] | None
) -> dict:
    """One review card (or the lack of one): the oldest unclassified trip
    matching the filters, strictly after `cursor` (None = fresh `/review`
    load). `remaining` counts matches at or after the returned trip, so
    "N remaining" includes the trip on screen. `state` distinguishes an
    empty filtered set ("empty": nothing ever matched) from an exhausted
    pass ("done": the cursor ran out but a fresh load would find trips) —
    review.html renders the two differently.
    """
    extra_where, extra_params = "", []
    if cursor is not None:
        extra_where = " AND (started_at, id) > (%s, %s)"
        extra_params = list(cursor)
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        f"SELECT {TRIP_COLUMNS} FROM trips {where}{extra_where} "
        "ORDER BY started_at, id LIMIT 1",
        [*params, *extra_params],
    )
    trip = await cur.fetchone()
    if trip is None:
        return {
            "trip": None, "remaining": 0,
            "state": "empty" if cursor is None else "done",
            "path_geojson": None, "path_snapped_geojson": None,
        }
    remaining_cur = await conn.execute(
        f"SELECT count(*) FROM trips {where} AND (started_at, id) >= (%s, %s)",
        [*params, trip["started_at"], trip["id"]],
    )
    remaining = (await remaining_cur.fetchone())[0]
    path_geojson = path_snapped_geojson = None
    if trip["source"] == "detected":
        path_cur = await conn.execute(
            "SELECT ST_AsGeoJSON(path), ST_AsGeoJSON(path_snapped) FROM trips WHERE id = %s",
            (trip["id"],),
        )
        row = await path_cur.fetchone()
        if row:
            path_geojson, path_snapped_geojson = row
    return {
        "trip": trip, "remaining": remaining, "state": "card",
        "path_geojson": path_geojson, "path_snapped_geojson": path_snapped_geojson,
    }


def _review_filter_sql(request: Request, from_: str, to: str, vehicle: str) -> tuple[str, list]:
    """The unclassified-pinned filter every `/review` route shares."""
    tz = request.app.state.config.display_tz
    from_dt, to_dt = parse_date_range(from_, to, tz)
    return _trip_filter_sql("unclassified", from_dt, to_dt, _parse_vehicle_id(vehicle))


async def _render_review_card(
    request: Request, conn, template: str, card: dict,
    from_: str, to: str, vehicle: str, **extra,
):
    """Render a review card with the shared context (vehicles, purpose
    suggestions, current filters) every `/review` route passes identically.
    """
    vehicles = await list_vehicles(conn)
    recent_purposes = await _fetch_recent_purposes(conn)
    return request.app.state.templates.TemplateResponse(
        request, template,
        {
            **card, "vehicles": vehicles, "recent_purposes": recent_purposes,
            "filter_from": from_, "filter_to": to, "filter_vehicle": vehicle,
            "review_url": _review_url(from_, to, vehicle),
            **extra,
        },
    )


async def _validate_split_distance(
    conn, trip_id: int, point_id: int, min_trip_distance_m: float
) -> tuple[float, float]:
    """Reject split points that the detector would discard as a short half.

    The check must happen before writing an override: otherwise reprocessing
    silently drops the sub-minimum trip and leaves its points unowned.
    """
    trip_points = await load_trip_points(conn, trip_id)
    idx = next((i for i, row in enumerate(trip_points) if row[0] == point_id), None)
    if idx is None:
        raise HTTPException(
            status_code=400,
            detail="Point is not part of this trip's surviving (filtered) point set",
        )
    first_half_m = _path_distance_m(trip_points[: idx + 1])
    second_half_m = _path_distance_m(trip_points[idx:])
    if first_half_m < min_trip_distance_m or second_half_m < min_trip_distance_m:
        raise HTTPException(
            status_code=400,
            detail=(
                "Split point is too close to the start or end of the trip: both "
                f"resulting halves must be at least {min_trip_distance_m:.0f}m "
                f"(this split would produce {first_half_m:.0f}m and "
                f"{second_half_m:.0f}m)"
            ),
        )
    return first_half_m, second_half_m


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/stats")
    async def stats(request: Request, user: dict = Depends(require_user)):
        """Current-year operational view, deliberately separate from the
        filing-oriented annual report: category work and repeated routes are
        most useful while the year is still in progress, while the report
        preserves its tax-specific caveats and rate-period accounting.
        """
        pool = request.app.state.pool
        tz = request.app.state.config.display_tz
        now = datetime.now(tz)
        year_start = datetime(now.year, 1, 1, tzinfo=tz)
        next_year_start = datetime(now.year + 1, 1, 1, tzinfo=tz)
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ) - timedelta(weeks=11)
        weekly_start = max(year_start, week_start)
        async with pool.connection() as conn:
            category_cur = await conn.execute(
                "SELECT category::text, count(*), COALESCE(SUM(COALESCE(distance_snapped_m, distance_m)), 0) "
                "FROM trips WHERE started_at >= %s AND started_at < %s GROUP BY category",
                (year_start, next_year_start),
            )
            weekly_cur = await conn.execute(
                "SELECT date_trunc('week', started_at AT TIME ZONE %s)::date, category::text, count(*), "
                "COALESCE(SUM(COALESCE(distance_snapped_m, distance_m)), 0) "
                "FROM trips WHERE started_at >= %s AND started_at < %s "
                "GROUP BY 1, 2 ORDER BY 1",
                (tz.key, weekly_start, next_year_start),
            )
            monthly_cur = await conn.execute(
                "SELECT date_trunc('month', started_at AT TIME ZONE %s)::date, category::text, count(*), "
                "COALESCE(SUM(COALESCE(distance_snapped_m, distance_m)), 0) "
                "FROM trips WHERE started_at >= %s AND started_at < %s "
                "GROUP BY 1, 2 ORDER BY 1",
                (tz.key, year_start, next_year_start),
            )
            routes_cur = await conn.execute(
                "WITH named_routes AS ("
                " SELECT LEAST(start_place_id, end_place_id) AS a_id, GREATEST(start_place_id, end_place_id) AS b_id, "
                " count(*) AS trip_count, COALESCE(SUM(COALESCE(distance_snapped_m, distance_m)), 0) AS total_m "
                " FROM trips WHERE started_at >= %s AND started_at < %s "
                " AND start_place_id IS NOT NULL AND end_place_id IS NOT NULL "
                " GROUP BY 1, 2) "
                "SELECT a.name, b.name, named_routes.trip_count, named_routes.total_m "
                "FROM named_routes JOIN places a ON a.id = named_routes.a_id "
                "JOIN places b ON b.id = named_routes.b_id "
                "ORDER BY total_m DESC, trip_count DESC, a.name, b.name LIMIT 5",
                (year_start, next_year_start),
            )
            places_cur = await conn.execute(
                "WITH endpoints AS ("
                " SELECT start_place_id AS place_id FROM trips WHERE started_at >= %s AND started_at < %s "
                " UNION ALL SELECT end_place_id FROM trips WHERE started_at >= %s AND started_at < %s) "
                "SELECT places.name, count(*) AS visit_count FROM endpoints "
                "JOIN places ON places.id = endpoints.place_id GROUP BY places.id, places.name "
                "ORDER BY visit_count DESC, places.name LIMIT 5",
                (year_start, next_year_start, year_start, next_year_start),
            )
            unnamed_cur = await conn.execute(
                "SELECT count(*) FROM trips WHERE started_at >= %s AND started_at < %s "
                "AND (start_place_id IS NULL OR end_place_id IS NULL)",
                (year_start, next_year_start),
            )
            dashboard = build_dashboard(
                now.year, now.date(), await category_cur.fetchall(), await weekly_cur.fetchall(),
                await monthly_cur.fetchall(),
                [
                    {"start_name": row[0], "end_name": row[1], "trip_count": row[2], "total_m": float(row[3])}
                    for row in await routes_cur.fetchall()
                ],
                [{"name": row[0], "visit_count": row[1]} for row in await places_cur.fetchall()],
                (await unnamed_cur.fetchone())[0],
            )
        return request.app.state.templates.TemplateResponse(
            request, "stats.html", {"user": user, "csrf": request.session.get("csrf", ""), "stats": dashboard},
        )

    @router.get("/")
    async def weekly_dashboard(
        request: Request,
        user: dict = Depends(require_user),
        week: str = Query(""),
    ):
        """The bounded weekly dashboard. `week` is an anchor, not a trusted
        Monday — `parse_week_anchor`/`week_bounds` normalize it forgivingly
        so a stale `?week=` link never 400s.

        Only two queries: one `TRIP_COLUMNS` fetch for the week and one
        expense sum over the same local week as `incurred_on` *dates* (that
        column has no time component). Every summary figure derives from the
        same fetched `trips` that become the day-grouped cards, so the
        numbers can't drift from what's on screen.
        """
        pool = request.app.state.pool
        config = request.app.state.config
        tz = config.display_tz
        now = datetime.now(tz)
        anchor = parse_week_anchor(week, tz, now)
        bounds = week_bounds(anchor, tz)

        async with pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {TRIP_COLUMNS} FROM trips WHERE started_at >= %s AND started_at < %s "
                "ORDER BY started_at DESC, id DESC",
                (bounds.start, bounds.end),
            )
            trips = await cur.fetchall()
            expense_cur = await conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM expenses "
                "WHERE incurred_on >= %s AND incurred_on < %s",
                (bounds.monday, bounds.monday + timedelta(days=7)),
            )
            expense_total = (await expense_cur.fetchone())[0]
            rates = await load_rates(conn)
            # dashboard.html includes _trip_card.html, whose vehicle/purpose
            # controls need these same two lists every other include site
            # already passes.
            vehicles = await list_vehicles(conn)
            recent_purposes = await _fetch_recent_purposes(conn)

        # Same getattr-with-default as app/main.py's missing_trip_badge Jinja
        # global: one source for this threshold, not a second knob that could
        # drift from what the trip-card badges use.
        threshold_m = getattr(config, "missing_trip_gap_m", 1000.0)
        dashboard = build_week_dashboard(
            trips, expense_total, rates, anchor, tz, now, threshold_m,
        )

        return request.app.state.templates.TemplateResponse(
            request, "dashboard.html",
            {
                "dashboard": dashboard, "user": user, "csrf": request.session.get("csrf", ""),
                "vehicles": vehicles, "recent_purposes": recent_purposes,
            },
        )

    @router.get("/trips")
    async def trips_archive(
        request: Request,
        user: dict = Depends(require_user),
        category: str = Query(""),
        from_: str = Query("", alias="from"),
        to: str = Query(""),
        vehicle: str = Query(""),
        manual_date: str = Query(""),
        manual_start: str = Query(""),
        manual_notes: str = Query(""),
        bridge_trip: str = Query(""),
        manual_open: str = Query(""),
    ):
        pool = request.app.state.pool
        tz = request.app.state.config.display_tz
        from_dt, to_dt = parse_date_range(from_, to, tz)
        vehicle_id = _parse_vehicle_id(vehicle)
        where, params = _trip_filter_sql(category, from_dt, to_dt, vehicle_id)
        page_size = request.app.state.config.trips_page_size
        ytd_year = datetime.now(tz).year
        year_start = datetime(ytd_year, 1, 1, tzinfo=tz)
        next_year_start = datetime(ytd_year + 1, 1, 1, tzinfo=tz)
        async with pool.connection() as conn:
            aggregate_cur = conn.cursor(row_factory=dict_row)
            await aggregate_cur.execute(
                "SELECT date_trunc('month', started_at AT TIME ZONE %s)::date AS local_month, "
                "count(*) AS trip_count, "
                "COALESCE(SUM(COALESCE(distance_snapped_m, distance_m)), 0) AS total_m, "
                "COALESCE(SUM(COALESCE(distance_snapped_m, distance_m)) "
                "FILTER (WHERE category = 'business'), 0) AS business_m "
                f"FROM trips {where} GROUP BY local_month ORDER BY local_month DESC",
                [tz.key, *params],
            )
            month_rows = await aggregate_cur.fetchall()

            page_cur = conn.cursor(row_factory=dict_row)
            await page_cur.execute(
                "WITH ranked AS (SELECT "
                f"{TRIP_COLUMNS}, "
                "date_trunc('month', started_at AT TIME ZONE %s)::date AS local_month, "
                "row_number() OVER (PARTITION BY date_trunc('month', started_at AT TIME ZONE %s) "
                "ORDER BY started_at DESC, id DESC) AS month_row "
                f"FROM trips {where}) "
                "SELECT * FROM ranked WHERE month_row <= %s "
                "ORDER BY local_month DESC, started_at DESC, id DESC",
                [tz.key, tz.key, *params, page_size + 1],
            )
            initial_rows = await page_cur.fetchall()
            rates = await load_rates(conn)
            vehicles = await list_vehicles(conn)
            recent_purposes = await _fetch_recent_purposes(conn)
            # Grouped by local month so a mid-year rate change prices each
            # half at its own rate (see sum_month_deductions).
            ytd_cur = await conn.execute(
                "SELECT EXTRACT(MONTH FROM started_at AT TIME ZONE %s)::int AS m,"
                " COALESCE(SUM(COALESCE(distance_snapped_m, distance_m)), 0) FROM trips"
                " WHERE category = 'business' AND started_at >= %s AND started_at < %s"
                " GROUP BY m",
                (tz.key, year_start, next_year_start),
            )
            ytd_by_month = [(r[0], r[1]) for r in await ytd_cur.fetchall()]

        page_rows_by_month: dict = {}
        for trip in initial_rows:
            page_rows_by_month.setdefault(trip["local_month"], []).append(trip)

        months = []
        for summary in month_rows:
            local_month = summary["local_month"]
            candidates = page_rows_by_month.get(local_month, [])
            trips = candidates[:page_size]
            month = {
                "label": local_month.strftime("%B %Y"),
                "year": local_month.year,
                "month_num": local_month.month,
                "trips": trips,
                "trip_count": summary["trip_count"],
                "total_m": float(summary["total_m"]),
                "business_m": float(summary["business_m"]),
                "has_more": len(candidates) > page_size,
            }
            month["business_deduction"] = deduction(
                month["business_m"], month["year"], rates, month["month_num"]
            )
            month["next_url"] = _month_page_url(
                month["year"], month["month_num"], page_size,
                category, from_, to, vehicle,
            )
            months.append(month)

        # Missing-trip badge prefill:
        # date/start_time/notes are display-only conveniences for this GET
        # render only -- add_manual_trip (the form's POST target) never
        # sees or validates them, so a malformed/tampered query string here
        # can, at worst, prefill the form oddly, never corrupt a save.
        manual_prefill = None
        if manual_date or manual_start or manual_notes:
            manual_prefill = {
                "date": manual_date, "start_time": manual_start, "notes": manual_notes,
                "osrm_hint": await _resolve_missing_trip_osrm_hint(request, bridge_trip)
                if bridge_trip else None,
            }

        return request.app.state.templates.TemplateResponse(
            request, "trips.html",
            {
                "months": months, "vehicles": vehicles, "recent_purposes": recent_purposes,
                "user": user, "csrf": request.session.get("csrf", ""),
                "filter_category": category, "filter_from": from_, "filter_to": to,
                "filter_vehicle": vehicle,
                "filter_url": _filter_url_factory(from_, to, vehicle),
                "export_url": _export_url_factory(category, from_, to, vehicle),
                "review_url": _review_url(from_, to, vehicle),
                "ytd_year": ytd_year,
                "ytd_deduction": sum_month_deductions(ytd_by_month, ytd_year, rates),
                "manual_prefill": manual_prefill,
                # manual_open, not manual_prefill, drives the <details open> attribute:
                # a fragment link (e.g. #manual-trip) only auto-expands a <details> when
                # the target is inside it, never the <details> itself, so the dashboard's
                # "Add manual trip" link needs this dedicated flag to open the form.
                "manual_open": bool(manual_open or manual_prefill),
            },
        )

    @router.get("/review")
    async def review_page(
        request: Request,
        user: dict = Depends(require_user),
        from_: str = Query("", alias="from"),
        to: str = Query(""),
        vehicle: str = Query(""),
    ):
        where, params = _review_filter_sql(request, from_, to, vehicle)
        async with request.app.state.pool.connection() as conn:
            card = await _fetch_review_card(conn, where, params, cursor=None)
            return await _render_review_card(
                request, conn, "review.html", card, from_, to, vehicle,
                user=user, csrf=request.session.get("csrf", ""),
            )

    @router.get("/review/card")
    async def review_card(
        request: Request,
        user: dict = Depends(require_user),
        after: int = Query(...),
        from_: str = Query("", alias="from"),
        to: str = Query(""),
        vehicle: str = Query(""),
    ):
        """Next-card partial, used by Skip. `after` is the currently displayed
        trip's id — its own `(started_at, id)` becomes the cursor, so a
        skipped trip can't reappear within this pass.
        """
        where, params = _review_filter_sql(request, from_, to, vehicle)
        async with request.app.state.pool.connection() as conn:
            cursor = await _trip_position(conn, after)
            card = await _fetch_review_card(conn, where, params, cursor)
            return await _render_review_card(
                request, conn, "_review_card.html", card, from_, to, vehicle,
            )

    @router.post("/review/{trip_id}/skip", dependencies=[Depends(require_csrf)])
    async def review_skip_trip(
        request: Request,
        trip_id: int,
        purpose: str = Form(""),
        from_: str = Form("", alias="from"),
        to: str = Form(""),
        vehicle: str = Form(""),
        user: dict = Depends(require_user),
    ):
        """Save purpose and advance without classifying the current trip.

        Carrying purpose in this request makes Skip authoritative if a
        change-triggered independent save is still in flight.
        """
        where, params = _review_filter_sql(request, from_, to, vehicle)
        async with request.app.state.pool.connection() as conn:
            position = await _trip_position(conn, trip_id)
            if position is None:
                raise HTTPException(status_code=404, detail="No such trip")
            await conn.execute(
                "UPDATE trips SET purpose = %s, updated_at = now() WHERE id = %s",
                (purpose.strip() or None, trip_id),
            )
            card = await _fetch_review_card(conn, where, params, cursor=position)
            return await _render_review_card(
                request, conn, "_review_card.html", card, from_, to, vehicle,
            )

    @router.post("/review/{trip_id}/tag", dependencies=[Depends(require_csrf)])
    async def review_tag_trip(
        request: Request,
        trip_id: int,
        category: str = Form(...),
        purpose: str = Form(""),
        from_: str = Form("", alias="from"),
        to: str = Form(""),
        vehicle: str = Form(""),
        user: dict = Depends(require_user),
    ):
        """Tag-and-advance in one round trip. Unlike
        the list-view `tag_trip`, a review card is by definition unclassified —
        there's nothing to clear, so category is restricted to the two real
        tags. The just-tagged trip's own position becomes the next cursor: it
        has left the unclassified set, so cursor-forward and "next remaining"
        coincide.
        """
        if category not in ("business", "personal"):
            raise HTTPException(status_code=400, detail="Unknown category")
        where, params = _review_filter_sql(request, from_, to, vehicle)
        async with request.app.state.pool.connection() as conn:
            position = await _trip_position(conn, trip_id)
            if position is None:
                raise HTTPException(status_code=404, detail="No such trip")
            await _apply_human_tag(
                conn, trip_id, category, purpose, update_purpose=True,
            )
            card = await _fetch_review_card(conn, where, params, cursor=position)
            return await _render_review_card(
                request, conn, "_review_card.html", card, from_, to, vehicle,
            )

    @router.post("/review/{trip_id}/delete", dependencies=[Depends(require_csrf)])
    async def review_delete_trip(
        request: Request,
        trip_id: int,
        from_: str = Form("", alias="from"),
        to: str = Form(""),
        vehicle: str = Form(""),
        user: dict = Depends(require_user),
    ):
        """Delete the displayed trip and advance beyond its prior position.

        The cursor is captured by `_delete_trip_in` before either deletion
        removes the row, so the review pass continues exactly as tag and
        Skip do, including stable id tie-breaking.
        """
        where, params = _review_filter_sql(request, from_, to, vehicle)
        async with request.app.state.pool.connection() as conn:
            position = await _delete_trip_in(conn, trip_id)
            card = await _fetch_review_card(conn, where, params, cursor=position)
            return await _render_review_card(
                request, conn, "_review_card.html", card, from_, to, vehicle,
            )

    @router.get("/trips/month/{year}/{month}")
    async def trip_month_page(
        request: Request,
        year: int = Path(ge=1, le=9998),
        month: int = Path(ge=1, le=12),
        offset: int = Query(0, ge=0),
        category: str = Query(""),
        from_: str = Query("", alias="from"),
        to: str = Query(""),
        vehicle: str = Query(""),
        user: dict = Depends(require_user),
    ):
        pool = request.app.state.pool
        tz = request.app.state.config.display_tz
        page_size = request.app.state.config.trips_page_size
        from_dt, to_dt = parse_date_range(from_, to, tz)
        async with pool.connection() as conn:
            trips, has_more = await _fetch_month_page(
                conn, tz, year, month, page_size, offset, category,
                from_dt, to_dt, _parse_vehicle_id(vehicle),
            )
            vehicles = await list_vehicles(conn)
            recent_purposes = await _fetch_recent_purposes(conn)
        return request.app.state.templates.TemplateResponse(
            request,
            "_trip_page_rows.html",
            {
                "trips": trips,
                "vehicles": vehicles,
                "recent_purposes": recent_purposes,
                "has_more": has_more,
                "next_url": _month_page_url(
                    year, month, offset + page_size, category, from_, to, vehicle
                ),
            },
        )

    @router.get("/export")
    async def export_trips(
        request: Request,
        user: dict = Depends(require_user),
        format: str = Query("csv"),
        category: str = Query(""),
        from_: str = Query("", alias="from"),
        to: str = Query(""),
        vehicle: str = Query(""),
    ):
        if format not in EXPORT_MEDIA_TYPES:
            raise HTTPException(status_code=400, detail="format must be csv or xlsx")
        pool = request.app.state.pool
        tz = request.app.state.config.display_tz
        from_dt, to_dt = parse_date_range(from_, to, tz)
        vehicle_id = _parse_vehicle_id(vehicle)
        # Reuses the exact same filter SQL as index() so a filtered export
        # can never drift from what's currently on screen.
        where, params = _trip_filter_sql(category, from_dt, to_dt, vehicle_id)
        async with pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {TRIP_COLUMNS} FROM trips {where} ORDER BY started_at DESC", params
            )
            trips = await cur.fetchall()
            rates = await load_rates(conn)

        content = to_csv(trips, rates, tz) if format == "csv" else to_xlsx(trips, rates, tz)
        return Response(
            content=content,
            media_type=EXPORT_MEDIA_TYPES[format],
            headers={"Content-Disposition": f'attachment; filename="trips.{format}"'},
        )

    @router.get("/report")
    async def report_redirect(request: Request, user: dict = Depends(require_user)):
        tz = request.app.state.config.display_tz
        year = default_report_year(datetime.now(tz))
        return RedirectResponse(f"/report/{year}", status_code=302)

    # Registered ahead of "/report/{year}" (and its /export) — FastAPI/
    # Starlette match a bare "{year}" path segment structurally before ever
    # trying to convert it to int, so "/report/range" would otherwise be
    # swallowed by "/report/{year}" and 422 on int-parsing "range" instead of
    # ever reaching these handlers.
    @router.get("/report/range")
    async def report_range_page(
        request: Request,
        from_: str = Query("", alias="from"),
        to: str = Query(""),
        user: dict = Depends(require_user),
    ):
        tz = request.app.state.config.display_tz
        start, end = _parse_range_query_dates(from_, to)
        pool = request.app.state.pool
        trips, rates = await _fetch_range_trips(pool, tz, start, end)
        try:
            report = build_range_report(trips, rates, tz, start, end)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return request.app.state.templates.TemplateResponse(
            request, "report_range.html",
            {
                "report": report,
                "user": user, "csrf": request.session.get("csrf", ""),
            },
        )

    @router.get("/report/range/export")
    async def report_range_export(
        request: Request,
        from_: str = Query("", alias="from"),
        to: str = Query(""),
        user: dict = Depends(require_user),
    ):
        tz = request.app.state.config.display_tz
        start, end = _parse_range_query_dates(from_, to)
        pool = request.app.state.pool
        trips, rates = await _fetch_range_trips(pool, tz, start, end)
        try:
            report = build_range_report(trips, rates, tz, start, end)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        content = to_range_report_xlsx(report, trips, rates, tz)
        filename = f"mileage-report-{range_filename_slug(start, end)}.xlsx"
        return Response(
            content=content,
            media_type=EXPORT_MEDIA_TYPES["xlsx"],
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get("/report/{year}")
    async def report_page(
        request: Request,
        year: int = Path(ge=1, le=9998),  # +1 must also stay in datetime's 1-9999 range
        user: dict = Depends(require_user),
    ):
        pool = request.app.state.pool
        tz = request.app.state.config.display_tz
        trips, rates = await _fetch_range_trips(pool, tz, date(year, 1, 1), date(year, 12, 31))
        report = build_annual_report(trips, rates, tz, year)
        odometer_coverage = await _fetch_year_odometer_coverage(pool, tz, year, trips)
        expenses, expense_report = await _fetch_year_expense_report(pool, tz, year, trips, rates)
        return request.app.state.templates.TemplateResponse(
            request, "report.html",
            {
                "report": report, "odometer_coverage": odometer_coverage,
                "expenses": expenses, "expense_report": expense_report,
                "user": user, "csrf": request.session.get("csrf", ""),
                "next_year_disabled": next_year_disabled(report.year, datetime.now(tz)),
            },
        )

    @router.get("/report/{year}/export")
    async def report_export(
        request: Request,
        year: int = Path(ge=1, le=9998),
        user: dict = Depends(require_user),
    ):
        pool = request.app.state.pool
        tz = request.app.state.config.display_tz
        trips, rates = await _fetch_range_trips(pool, tz, date(year, 1, 1), date(year, 12, 31))
        report = build_annual_report(trips, rates, tz, year)
        odometer_coverage = await _fetch_year_odometer_coverage(pool, tz, year, trips)
        expenses, expense_report = await _fetch_year_expense_report(pool, tz, year, trips, rates)
        content = to_report_xlsx(
            report, trips, rates, tz, odometer_coverage, expense_report, expenses
        )
        return Response(
            content=content,
            media_type=EXPORT_MEDIA_TYPES["xlsx"],
            headers={"Content-Disposition": f'attachment; filename="mileage-report-{year}.xlsx"'},
        )

    @router.get("/expenses")
    async def expense_ledger(
        request: Request,
        year: int | None = Query(None, ge=1, le=9998),
        vehicle: str = Query(""),
        user: dict = Depends(require_user),
    ):
        tz = request.app.state.config.display_tz
        selected_year = year or datetime.now(tz).year
        vehicle_id = _parse_vehicle_id(vehicle)
        async with request.app.state.pool.connection() as conn:
            vehicles = await list_vehicles(conn, include_inactive=True)
            cur = conn.cursor(row_factory=dict_row)
            clauses = ["expenses.incurred_on >= %s", "expenses.incurred_on < %s"]
            params: list = [date(selected_year, 1, 1), date(selected_year + 1, 1, 1)]
            if vehicle_id is not None:
                clauses.append("expenses.vehicle_id = %s")
                params.append(vehicle_id)
            await cur.execute(
                "SELECT expenses.id, expenses.vehicle_id, vehicles.name AS vehicle_name, "
                "expenses.incurred_on, expenses.category::text AS category, expenses.amount, "
                "expenses.treatment::text AS treatment, expenses.notes "
                "FROM expenses JOIN vehicles ON vehicles.id = expenses.vehicle_id WHERE "
                + " AND ".join(clauses)
                + " ORDER BY expenses.incurred_on DESC, expenses.id DESC",
                params,
            )
            expenses = await cur.fetchall()
        category_totals: dict[str, Decimal] = {}
        for expense in expenses:
            category_totals[expense["category"]] = (
                category_totals.get(expense["category"], Decimal("0")) + expense["amount"]
            )
        return request.app.state.templates.TemplateResponse(
            request, "expenses.html",
            {
                "expenses": expenses,
                "vehicles": vehicles,
                "selected_year": selected_year,
                "selected_vehicle": vehicle,
                "category_totals": category_totals,
                "ledger_total": sum(category_totals.values(), Decimal("0")),
                "category_labels": CATEGORY_LABELS,
                "treatment_labels": TREATMENT_LABELS,
                "expense_categories": EXPENSE_CATEGORIES,
                "expense_treatments": EXPENSE_TREATMENTS,
                "user": user,
                "csrf": request.session.get("csrf", ""),
            },
        )

    @router.post("/expenses", dependencies=[Depends(require_csrf)])
    async def add_expense(
        request: Request,
        vehicle_id: int = Form(...),
        incurred_on: str = Form(...),
        category: str = Form(...),
        amount: str = Form(...),
        treatment: str = Form(""),
        notes: str = Form(""),
        user: dict = Depends(require_user),
    ):
        parsed_date, category, parsed_amount, treatment = _parse_expense_input(
            incurred_on, category, amount, treatment
        )
        async with request.app.state.pool.connection() as conn:
            try:
                await conn.execute(
                    "INSERT INTO expenses (vehicle_id, incurred_on, category, amount, treatment, notes) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (vehicle_id, parsed_date, category, parsed_amount, treatment, notes.strip() or None),
                )
            except errors.ForeignKeyViolation:
                raise HTTPException(status_code=400, detail="No such vehicle")
        return Response(status_code=204, headers={"HX-Redirect": f"/expenses?year={parsed_date.year}"})

    @router.post("/expenses/{expense_id}/update", dependencies=[Depends(require_csrf)])
    async def update_expense(
        request: Request,
        expense_id: int,
        vehicle_id: int = Form(...),
        incurred_on: str = Form(...),
        category: str = Form(...),
        amount: str = Form(...),
        treatment: str = Form(""),
        notes: str = Form(""),
        user: dict = Depends(require_user),
    ):
        parsed_date, category, parsed_amount, treatment = _parse_expense_input(
            incurred_on, category, amount, treatment
        )
        async with request.app.state.pool.connection() as conn:
            try:
                cur = await conn.execute(
                    "UPDATE expenses SET vehicle_id = %s, incurred_on = %s, category = %s, "
                    "amount = %s, treatment = %s, notes = %s, updated_at = now() WHERE id = %s",
                    (
                        vehicle_id, parsed_date, category, parsed_amount, treatment,
                        notes.strip() or None, expense_id,
                    ),
                )
            except errors.ForeignKeyViolation:
                raise HTTPException(status_code=400, detail="No such vehicle")
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="No such expense")
        return Response(status_code=204, headers={"HX-Redirect": f"/expenses?year={parsed_date.year}"})

    @router.post("/expenses/{expense_id}/delete", dependencies=[Depends(require_csrf)])
    async def delete_expense(
        request: Request, expense_id: int, user: dict = Depends(require_user)
    ):
        async with request.app.state.pool.connection() as conn:
            cur = await conn.execute("DELETE FROM expenses WHERE id = %s", (expense_id,))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="No such expense")
        return Response(status_code=204, headers={"HX-Redirect": "/expenses"})

    @router.get("/trips/{trip_id}/card")
    async def trip_card(request: Request, trip_id: int, user: dict = Depends(require_user)):
        ctx = await _fetch_trip_card_context(request.app.state.pool, trip_id)
        return request.app.state.templates.TemplateResponse(request, "_trip_card.html", ctx)

    @router.get("/trips/{trip_id}/edit")
    async def edit_trip_card(request: Request, trip_id: int, user: dict = Depends(require_user)):
        ctx = await _fetch_trip_card_context(request.app.state.pool, trip_id)
        ctx.update({
            "values": _trip_edit_values(ctx["trip"], request.app.state.config.display_tz),
            "errors": {},
        })
        return request.app.state.templates.TemplateResponse(request, "_trip_edit_card.html", ctx)

    @router.post("/trips/{trip_id}/edit", dependencies=[Depends(require_csrf)])
    async def save_trip_card(
        request: Request,
        trip_id: int,
        category: str = Form(...),
        purpose: str = Form(""),
        notes: str = Form(""),
        vehicle_id: str = Form(""),
        date: str = Form(""),
        start_time: str = Form(""),
        end_time: str = Form(""),
        distance: str = Form(""),
        user: dict = Depends(require_user),
    ):
        values = {
            "category": category,
            "purpose": purpose,
            "notes": notes,
            "vehicle_id": vehicle_id,
            "date": date,
            "start_time": start_time,
            "end_time": end_time,
            "distance": distance,
        }
        field_errors: dict[str, str] = {}
        if category not in CATEGORIES:
            field_errors["category"] = "Choose a valid category."
        try:
            parsed_vehicle_id = int(vehicle_id) if vehicle_id else None
        except ValueError:
            parsed_vehicle_id = None
            field_errors["vehicle_id"] = "Choose a valid vehicle."

        normalized_purpose = purpose.strip() or None
        normalized_notes = notes.strip() or None
        pool = request.app.state.pool
        try:
            async with pool.connection() as conn:
                async with conn.transaction():
                    cur = conn.cursor(row_factory=dict_row)
                    await cur.execute(
                        "SELECT source::text AS source, category::text AS category, purpose, "
                        "tag_source::text AS tag_source FROM trips WHERE id = %s FOR UPDATE",
                        (trip_id,),
                    )
                    stored = await cur.fetchone()
                    if stored is None:
                        raise HTTPException(status_code=404, detail="No such trip")

                    if parsed_vehicle_id is not None and "vehicle_id" not in field_errors:
                        vehicle_cur = await conn.execute(
                            "SELECT 1 FROM vehicles WHERE id = %s", (parsed_vehicle_id,)
                        )
                        if await vehicle_cur.fetchone() is None:
                            field_errors["vehicle_id"] = "Choose a vehicle that still exists."

                    manual_values = None
                    if stored["source"] == "manual":
                        try:
                            manual_values = parse_manual_trip_input(
                                date, start_time, end_time, distance,
                                request.app.state.config.display_tz,
                            )
                        except ManualTripValidationError as exc:
                            field_errors.update(exc.errors)

                    if field_errors:
                        raise ManualTripValidationError(field_errors)

                    tag_source = stored["tag_source"]
                    if category != stored["category"] or normalized_purpose != stored["purpose"]:
                        tag_source = "human"
                    try:
                        if stored["source"] == "manual":
                            assert manual_values is not None
                            await conn.execute(
                                "UPDATE trips SET started_at = %s, ended_at = %s, distance_m = %s, "
                                "category = %s, purpose = %s, notes = %s, vehicle_id = %s, "
                                "tag_source = %s, updated_at = now() WHERE id = %s",
                                (*manual_values, category, normalized_purpose, normalized_notes,
                                 parsed_vehicle_id, tag_source, trip_id),
                            )
                        else:
                            await conn.execute(
                                "UPDATE trips SET category = %s, purpose = %s, notes = %s, "
                                "vehicle_id = %s, tag_source = %s, updated_at = now() WHERE id = %s",
                                (category, normalized_purpose, normalized_notes, parsed_vehicle_id,
                                 tag_source, trip_id),
                            )
                    except errors.ForeignKeyViolation:
                        raise ManualTripValidationError(
                            {"vehicle_id": "Choose a vehicle that still exists."}
                        )
        except ManualTripValidationError as exc:
            ctx = await _fetch_trip_card_context(pool, trip_id)
            ctx.update({"values": values, "errors": exc.errors})
            return request.app.state.templates.TemplateResponse(
                request, "_trip_edit_card.html", ctx
            )

        ctx = await _fetch_trip_card_context(pool, trip_id)
        return request.app.state.templates.TemplateResponse(request, "_trip_card.html", ctx)

    @router.get("/trips/{trip_id}")
    async def trip_detail(request: Request, trip_id: int, user: dict = Depends(require_user)):
        pool = request.app.state.pool
        trip = await _fetch_trip(pool, trip_id)
        path_geojson = None
        path_snapped_geojson = None
        stay_centroids = []
        has_next_trip = False
        has_prev_trip = False
        async with pool.connection() as conn:
            vehicles = await list_vehicles(conn)
            recent_purposes = await _fetch_recent_purposes(conn)
            if trip["source"] == "detected":
                cur = await conn.execute(
                    "SELECT ST_AsGeoJSON(path), ST_AsGeoJSON(path_snapped) "
                    "FROM trips WHERE id = %s", (trip_id,)
                )
                row = await cur.fetchone()
                path_geojson = row[0] if row else None
                path_snapped_geojson = row[1] if row else None
                cur = await conn.execute(
                    "SELECT ST_AsGeoJSON(centroid::geometry) FROM stays "
                    "WHERE device = %s AND (ended_at = %s OR started_at = %s)",
                    (trip["device"], trip["started_at"], trip["ended_at"]),
                )
                stay_centroids = [json.loads(r[0]) for r in await cur.fetchall()]
                # Merge buttons only render when a genuine adjacent
                # detected trip exists for this device.
                cur = await conn.execute(
                    "SELECT EXISTS (SELECT 1 FROM trips WHERE device = %s "
                    " AND source = 'detected' AND NOT imported AND started_at > %s), "
                    "EXISTS (SELECT 1 FROM trips WHERE device = %s "
                    " AND source = 'detected' AND NOT imported AND started_at < %s)",
                    (trip["device"], trip["started_at"],
                     trip["device"], trip["started_at"]),
                )
                has_next_trip, has_prev_trip = await cur.fetchone()

        return request.app.state.templates.TemplateResponse(
            request, "trip.html",
            {
                "trip": trip,
                "vehicles": vehicles,
                "recent_purposes": recent_purposes,
                "path_geojson": path_geojson,
                "path_snapped_geojson": path_snapped_geojson,
                "stay_centroids": json.dumps(stay_centroids),
                "has_next_trip": has_next_trip,
                "has_prev_trip": has_prev_trip,
                "min_trip_distance_m": request.app.state.config.detector_params.min_trip_distance_m,
                "categories": CATEGORIES,
                "user": user,
                "csrf": request.session.get("csrf", ""),
            },
        )

    @router.post("/trips/{trip_id}/tag", dependencies=[Depends(require_csrf)])
    async def tag_trip(
        request: Request,
        trip_id: int,
        category: str = Form(...),
        user: dict = Depends(require_user),
    ):
        if category not in CATEGORIES:
            raise HTTPException(status_code=400, detail="Unknown category")
        pool = request.app.state.pool
        async with pool.connection() as conn:
            await _apply_human_tag(conn, trip_id, category)
        ctx = await _fetch_trip_card_context(pool, trip_id)
        return request.app.state.templates.TemplateResponse(request, "_trip_card.html", ctx)

    @router.post("/trips/{trip_id}/notes", dependencies=[Depends(require_csrf)])
    async def note_trip(
        request: Request,
        trip_id: int,
        notes: str = Form(""),
        user: dict = Depends(require_user),
    ):
        pool = request.app.state.pool
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE trips SET notes = %s, updated_at = now() WHERE id = %s",
                (notes.strip() or None, trip_id),
            )
        ctx = await _fetch_trip_card_context(pool, trip_id)
        return request.app.state.templates.TemplateResponse(request, "_trip_card.html", ctx)

    @router.post("/trips/{trip_id}/purpose", dependencies=[Depends(require_csrf)])
    async def purpose_trip(
        request: Request,
        trip_id: int,
        purpose: str = Form(""),
        user: dict = Depends(require_user),
    ):
        pool = request.app.state.pool
        async with pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE trips SET purpose = %s, tag_source = 'human', updated_at = now() "
                "WHERE id = %s",
                (purpose.strip() or None, trip_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="No such trip")
        ctx = await _fetch_trip_card_context(pool, trip_id)
        return request.app.state.templates.TemplateResponse(request, "_trip_card.html", ctx)

    @router.post("/trips/{trip_id}/vehicle", dependencies=[Depends(require_csrf)])
    async def set_trip_vehicle(
        request: Request,
        trip_id: int,
        vehicle_id: str = Form(""),
        user: dict = Depends(require_user),
    ):
        parsed_vehicle_id = _parse_vehicle_form(vehicle_id)
        pool = request.app.state.pool
        async with pool.connection() as conn:
            try:
                await conn.execute(
                    "UPDATE trips SET vehicle_id = %s, updated_at = now() WHERE id = %s",
                    (parsed_vehicle_id, trip_id),
                )
            except errors.ForeignKeyViolation:
                raise HTTPException(status_code=400, detail="No such vehicle")
        ctx = await _fetch_trip_card_context(pool, trip_id)
        return request.app.state.templates.TemplateResponse(request, "_trip_card.html", ctx)

    @router.post("/trips/manual", dependencies=[Depends(require_csrf)])
    async def add_manual_trip(
        request: Request,
        date: str = Form(...),
        start_time: str = Form(...),
        end_time: str = Form(...),
        distance: str = Form(...),
        category: str = Form("unclassified"),
        purpose: str = Form(""),
        notes: str = Form(""),
        vehicle_id: str = Form(""),
        user: dict = Depends(require_user),
    ):
        tz = request.app.state.config.display_tz
        try:
            started_at, ended_at, distance_m = parse_manual_trip_input(
                date, start_time, end_time, distance, tz
            )
        except ManualTripValidationError as exc:
            if "distance" in exc.errors and len(exc.errors) == 1:
                raise HTTPException(status_code=400, detail="Invalid distance")
            raise HTTPException(status_code=400, detail="Invalid date/time")
        if category not in CATEGORIES:
            raise HTTPException(status_code=400, detail="Unknown category")
        parsed_vehicle_id = _parse_vehicle_form(vehicle_id)

        pool = request.app.state.pool
        async with pool.connection() as conn:
            try:
                await conn.execute(
                    "INSERT INTO trips (device, source, started_at, ended_at, distance_m,"
                    " category, purpose, notes, vehicle_id)"
                    " VALUES ('manual', 'manual', %s, %s, %s, %s, %s, %s, %s)",
                    (started_at, ended_at, distance_m, category, purpose.strip() or None,
                     notes.strip() or None,
                     parsed_vehicle_id),
                )
            except errors.ForeignKeyViolation:
                raise HTTPException(status_code=400, detail="No such vehicle")
        return Response(status_code=204, headers={"HX-Redirect": "/trips"})

    @router.post("/trips/{trip_id}/delete", dependencies=[Depends(require_csrf)])
    async def delete_trip(
        request: Request,
        trip_id: int,
        user: dict = Depends(require_user),
        fragment: Annotated[bool, Form()] = False,
    ):
        pool = request.app.state.pool
        async with pool.connection() as conn:
            await _delete_trip_in(conn, trip_id)
        if fragment:
            return Response(status_code=200)
        return Response(status_code=204, headers={"HX-Redirect": "/trips"})

    async def _merge_trips_core(
        request: Request, trip_ids: list[int], category: str = "keep", purpose: str = "",
        notes: str = "", vehicle: str = "keep",
    ) -> int:
        """Shared by merge_next/merge_prev and merge_selected: validates the
        selection is a contiguous run of detected trips for one device,
        suppresses the real stay between each consecutive pair, reprocesses
        the device once, then overwrites the resulting trip's
        tag/purpose/notes with what the user submitted — reconcile keeps the
        *longest* original trip's fields, an implementation detail the user
        shouldn't have to think about. Returns the merged trip's id.

        `category` and `vehicle` are both tri-state: "keep" (default)
        preserves whatever reconcile's longest-trip inheritance left,
        including the existing `tag_source`, without human-locking it; a
        real category, or "" / a digit string for vehicle, assigns and (for
        category) also claims human ownership. A concrete category default
        would silently human-lock every merge from callers that don't
        supply one (e.g. a cached old form post, or an untouched select
        whose first option the browser auto-submits); "keep" makes silence
        safe and mirrors `vehicle`.

        Runs on one connection/transaction: the
        override writes, reprocess, and final UPDATE used to span three
        commits, and a failure between them could leave committed
        suppress-overrides with no corresponding merged trip. The advisory
        lock is taken up front so the *entire* merge is serialized against a
        concurrent detector run; `reprocess_device_in` re-taking it later is
        a no-op (advisory locks are reentrant within one session).
        """
        keep_category = category == "keep"
        if not keep_category and category not in CATEGORIES:
            raise HTTPException(status_code=400, detail="Unknown category")
        keep_vehicle = vehicle == "keep"
        parsed_vehicle_id = None if keep_vehicle else _parse_vehicle_form(vehicle)
        trip_ids = sorted(set(trip_ids))

        pool = request.app.state.pool
        runner = request.app.state.detector_runner
        async with pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))

            # Checked before the trip-selection query below excludes imported
            # trips (AND NOT imported): that exclusion alone would just make
            # an imported id look "no longer exist[ent]" below, an accurate
            # but unhelpful reason for a trip that in fact exists and simply
            # can't be merged. An imported trip has no backing points in this
            # instance (see migrations/019_trip_imported.sql), so it was
            # never a legitimate merge candidate -- suppressing the real stay
            # between it and a neighbor is meaningless with no points to
            # reprocess, and the old behavior here was an opaque 500 once the
            # final lookup failed to find a trip the suppress override could
            # never have produced.
            cur = await conn.execute(
                "SELECT 1 FROM trips WHERE id = ANY(%s) AND imported LIMIT 1",
                (trip_ids,),
            )
            if await cur.fetchone():
                raise HTTPException(
                    status_code=400,
                    detail="One or more selected trips came from a data import and have no "
                    "location points in this instance, so they cannot be merged.",
                )

            cur = await conn.execute(
                "SELECT id, device, source::text AS source, started_at, ended_at "
                "FROM trips WHERE id = ANY(%s) AND NOT imported",
                (trip_ids,),
            )
            rows = await cur.fetchall()
            if len(rows) != len(trip_ids):
                raise HTTPException(status_code=400, detail="One or more selected trips no longer exist")
            devices = {r[1] for r in rows}
            if len(devices) > 1:
                raise HTTPException(status_code=400, detail="Selected trips must all be from the same device")
            if any(r[2] != "detected" for r in rows):
                raise HTTPException(status_code=400, detail="Only detected trips can be merged")
            device = next(iter(devices))
            selected = [TripSpan(id=r[0], started_at=r[3], ended_at=r[4]) for r in rows]

            first_start = min(t.started_at for t in selected)
            last_start = max(t.started_at for t in selected)
            cur = await conn.execute(
                "SELECT id, started_at, ended_at FROM trips WHERE device = %s "
                "AND source = 'detected' AND NOT imported AND started_at BETWEEN %s AND %s "
                "ORDER BY started_at",
                (device, first_start, last_start),
            )
            in_range = [TripSpan(id=r[0], started_at=r[1], ended_at=r[2]) for r in await cur.fetchall()]

            try:
                ranges = plan_merge_selected(selected, in_range)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

            for range_start, range_end in ranges:
                await conn.execute(
                    "DELETE FROM trip_boundary_overrides o USING points p "
                    "WHERE o.point_id = p.id AND o.kind = 'force' AND o.device = %s "
                    "AND p.recorded_at BETWEEN %s AND %s",
                    (device, range_start, range_end),
                )
                await conn.execute(
                    "INSERT INTO trip_boundary_overrides (device, kind, range_start, range_end) "
                    "VALUES (%s, 'suppress', %s, %s) ON CONFLICT DO NOTHING",
                    (device, range_start, range_end),
                )

            ordered = sorted(selected, key=lambda t: t.started_at)
            merged_start, merged_end = ordered[0].started_at, ordered[-1].ended_at

            await runner.reprocess_device_in(conn, device)

            cur = await conn.execute(
                "SELECT id FROM trips WHERE device = %s AND source = 'detected' "
                "AND started_at = %s AND ended_at = %s",
                (device, merged_start, merged_end),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=500, detail="Merge did not produce the expected trip")
            merged_id = row[0]
            set_clause = "purpose = %s, notes = %s"
            update_params = [purpose.strip() or None, notes.strip() or None]
            if not keep_category:
                set_clause += ", category = %s, tag_source = 'human'"
                update_params.append(category)
            if not keep_vehicle:
                set_clause += ", vehicle_id = %s"
                update_params.append(parsed_vehicle_id)
            update_params.append(merged_id)
            try:
                await conn.execute(
                    f"UPDATE trips SET {set_clause}, updated_at = now() WHERE id = %s",
                    update_params,
                )
            except errors.ForeignKeyViolation:
                raise HTTPException(status_code=400, detail="No such vehicle")
        _poke_snap_worker(request)
        return merged_id

    async def _merge_with_neighbor(
        request: Request, trip_id: int, direction: str, category: str, purpose: str, notes: str,
        vehicle: str = "keep",
    ) -> Response:
        pool = request.app.state.pool
        trip = await _fetch_trip(pool, trip_id)
        # Keep this early check even though _merge_trips_core repeats it: it
        # reports the real invalid-source error before a neighbor lookup can
        # misleadingly report that no adjacent detected trip exists.
        if trip["source"] != "detected":
            raise HTTPException(status_code=400, detail="Only detected trips can be merged")
        # Same reasoning, for the other reason a trip can't be merged: an
        # imported trip has no backing points in this instance, so it's
        # never a legitimate merge candidate regardless of direction.
        if trip["imported"]:
            raise HTTPException(
                status_code=400,
                detail="This trip came from a data import and has no location points in "
                "this instance, so it cannot be merged.",
            )

        async with pool.connection() as conn:
            if direction == "next":
                cur = await conn.execute(
                    "SELECT id FROM trips WHERE device = %s "
                    "AND source = 'detected' AND NOT imported AND started_at > %s "
                    "ORDER BY started_at ASC LIMIT 1",
                    (trip["device"], trip["started_at"]),
                )
            else:
                cur = await conn.execute(
                    "SELECT id FROM trips WHERE device = %s "
                    "AND source = 'detected' AND NOT imported AND started_at < %s "
                    "ORDER BY started_at DESC LIMIT 1",
                    (trip["device"], trip["started_at"]),
                )
            neighbor = await cur.fetchone()
            if not neighbor:
                raise HTTPException(status_code=400, detail="No adjacent trip to merge with")

        merged_id = await _merge_trips_core(
            request, [trip_id, neighbor[0]], category, purpose, notes, vehicle,
        )
        return Response(status_code=204, headers={"HX-Redirect": f"/trips/{merged_id}"})

    @router.post("/trips/{trip_id}/merge_next", dependencies=[Depends(require_csrf)])
    async def merge_trip_next(
        request: Request,
        trip_id: int,
        category: str = Form("keep"),
        purpose: str = Form(""),
        notes: str = Form(""),
        vehicle_id: str = Form("keep"),
        user: dict = Depends(require_user),
    ):
        return await _merge_with_neighbor(
            request, trip_id, "next", category, purpose, notes, vehicle_id,
        )

    @router.post("/trips/{trip_id}/merge_prev", dependencies=[Depends(require_csrf)])
    async def merge_trip_prev(
        request: Request,
        trip_id: int,
        category: str = Form("keep"),
        purpose: str = Form(""),
        notes: str = Form(""),
        vehicle_id: str = Form("keep"),
        user: dict = Depends(require_user),
    ):
        return await _merge_with_neighbor(
            request, trip_id, "prev", category, purpose, notes, vehicle_id,
        )

    @router.post("/trips/merge_selected", dependencies=[Depends(require_csrf)])
    async def merge_selected_trips(
        request: Request,
        trip_ids: list[int] = Form(...),
        category: str = Form("keep"),
        purpose: str = Form(""),
        notes: str = Form(""),
        vehicle_id: str = Form("keep"),
        user: dict = Depends(require_user),
    ):
        merged_id = await _merge_trips_core(request, trip_ids, category, purpose, notes, vehicle_id)
        return JSONResponse({"trip_id": merged_id})

    @router.post("/trips/batch_update", dependencies=[Depends(require_csrf)])
    async def batch_update_trips(
        request: Request,
        trip_ids: list[int] = Form(...),
        category: str = Form("keep"),
        vehicle_id: str = Form("keep"),
        purpose: str = Form(""),
        set_purpose: bool = Form(False),
        user: dict = Depends(require_user),
    ):
        """Classify several trips without coupling the write to detection.

        Unlike merge, selection is the complete contract here: trips can span
        devices and months and can be either detected or manual. Building the
        SET clause only from explicit choices is load-bearing because a batch
        vehicle assignment must not erase independently maintained tags or
        purposes. Purpose needs a separate boolean gate because every possible
        text value, including an empty string, is meaningful user input.
        """
        trip_ids = sorted(set(trip_ids))
        if len(trip_ids) < 1:
            raise HTTPException(status_code=400, detail="Select at least one trip")
        if category != "keep" and category not in CATEGORIES:
            raise HTTPException(status_code=400, detail="Unknown category")

        keep_vehicle = vehicle_id == "keep"
        parsed_vehicle_id = None if keep_vehicle else _parse_vehicle_form(vehicle_id)

        if category == "keep" and keep_vehicle and not set_purpose:
            raise HTTPException(status_code=400, detail="Nothing to apply")

        set_clauses = []
        params: list = []
        if category != "keep":
            set_clauses.append("category = %s")
            params.append(category)
        if set_purpose:
            set_clauses.append("purpose = %s")
            params.append(purpose.strip() or None)
        if not keep_vehicle:
            set_clauses.append("vehicle_id = %s")
            params.append(parsed_vehicle_id)
        if category != "keep" or set_purpose:
            set_clauses.append("tag_source = 'human'")
        set_clauses.append("updated_at = now()")
        params.append(trip_ids)

        pool = request.app.state.pool
        async with pool.connection() as conn:
            try:
                cur = await conn.execute(
                    f"UPDATE trips SET {', '.join(set_clauses)} WHERE id = ANY(%s)",
                    params,
                )
            except errors.ForeignKeyViolation:
                raise HTTPException(status_code=400, detail="No such vehicle")
            if cur.rowcount != len(trip_ids):
                raise HTTPException(
                    status_code=400,
                    detail="One or more selected trips no longer exist",
                )
        return JSONResponse({"updated": cur.rowcount})

    @router.get("/trips/{trip_id}/points")
    async def trip_points(request: Request, trip_id: int, user: dict = Depends(require_user)):
        trip = await _fetch_trip(request.app.state.pool, trip_id)
        if trip["source"] != "detected":
            raise HTTPException(status_code=400, detail="Only detected trips have points")
        async with request.app.state.pool.connection() as conn:
            rows = await load_trip_points(conn, trip_id)
        return JSONResponse([
            {"id": r[0], "t": r[1].isoformat(), "lat": r[2], "lon": r[3]} for r in rows
        ])

    @router.post("/trips/{trip_id}/split", dependencies=[Depends(require_csrf)])
    async def split_trip(
        request: Request,
        trip_id: int,
        point_id: int = Form(...),
        user: dict = Depends(require_user),
    ):
        """The override insert and the reprocess share one
        connection/transaction, same reasoning as `_merge_trips_core`.
        """
        pool = request.app.state.pool
        trip = await _fetch_trip(pool, trip_id)
        if trip["source"] != "detected":
            raise HTTPException(status_code=400, detail="Only detected trips can be split")

        runner = request.app.state.detector_runner
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT 1 FROM points WHERE id = %s AND device = %s "
                "AND recorded_at > %s AND recorded_at < %s",
                (point_id, trip["device"], trip["started_at"], trip["ended_at"]),
            )
            if not await cur.fetchone():
                raise HTTPException(
                    status_code=400,
                    detail="Point must belong to this trip's interior, strictly between its "
                           "start and end (not already a boundary point)",
                )
            await _validate_split_distance(
                conn,
                trip_id,
                point_id,
                request.app.state.config.detector_params.min_trip_distance_m,
            )
            await conn.execute(
                "INSERT INTO trip_boundary_overrides (device, kind, point_id) "
                "VALUES (%s, 'force', %s) ON CONFLICT DO NOTHING",
                (trip["device"], point_id),
            )
            await runner.reprocess_device_in(conn, trip["device"])
        _poke_snap_worker(request)
        return Response(status_code=204, headers={"HX-Redirect": "/trips"})

    @router.post("/settings/boundary_overrides/{override_id}/delete", dependencies=[Depends(require_csrf)])
    async def delete_boundary_override(
        request: Request, override_id: int, user: dict = Depends(require_user)
    ):
        """Same single-transaction treatment as `split_trip`: the override
        delete and the reprocess it triggers share one connection, so a
        failed reprocess doesn't leave the override gone with the device's
        trips still reflecting it.
        """
        pool = request.app.state.pool
        runner = request.app.state.detector_runner
        reprocessed = False
        async with pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM trip_boundary_overrides WHERE id = %s RETURNING device",
                (override_id,),
            )
            row = await cur.fetchone()
            if row:
                await runner.reprocess_device_in(conn, row[0])
                reprocessed = True
        if reprocessed:
            _poke_snap_worker(request)
        return _redirect_back(request)

    @router.get("/settings")
    async def settings_page(request: Request, user: dict = Depends(require_user)):
        async with request.app.state.pool.connection() as conn:
            db_rates = await _fetch_rates_rows(conn)
            # include_inactive=True: unlike the trip-assignment pickers, the
            # settings table is where a deactivated vehicle is managed, so it
            # must stay visible here even though it's dropped elsewhere.
            vehicles = await list_vehicles(conn, include_inactive=True)
            auto_assign_default_vehicle = await get_auto_assign_default_vehicle(conn)
            odometer = await _fetch_odometer_context(conn)
            places = await _fetch_places_rows(conn)
            rules = await _fetch_rules_rows(conn, places)
            boundary_overrides = await _fetch_boundary_overrides_rows(conn)
            device_fixes = await _fetch_device_fixes(conn)
            schema_version = await _fetch_schema_version(conn)
        cfg = request.app.state.config
        # A second, independent report -- built from its own pool borrows,
        # same as every other fetch above -- rather than folding into the
        # small `diagnostics` dict below: that dict's exact shape is a
        # long-standing contract (tests/test_version_identity.py), and the
        # config-presence/worker/migration detail here is new, additive
        # content, not a replacement for it.
        diagnostics_report = await build_report(cfg, request.app.state.pool, request.app.state)
        return request.app.state.templates.TemplateResponse(
            request, "settings.html",
            {
                "rates": db_rates, "vehicles": vehicles, "odometer": odometer,
                "auto_assign_default_vehicle": auto_assign_default_vehicle,
                "places": places, "rules": rules,
                "boundary_overrides": boundary_overrides,
                "device_fixes": device_fixes,
                "user": user, "csrf": request.session.get("csrf", ""),
                "geocode_enabled": cfg.geocode_provider is not None,
                "diagnostics": {
                    "app_version": cfg.app_version,
                    "git_revision": cfg.app_git_revision,
                    "schema_version": schema_version,
                    "detector_version": DETECTOR_VERSION,
                },
                "diagnostics_report": diagnostics_report,
                "connectivity": None,
            },
        )

    @router.post("/settings/diagnostics/check", dependencies=[Depends(require_csrf)])
    async def check_diagnostics_connectivity(request: Request, user: dict = Depends(require_user)):
        """The D6 "check now" control: OSRM/geocoder/ntfy/SMTP reachability
        is probed only in direct response to this explicit POST, never on a
        timer or from a plain page load -- see app/diagnose.py's module
        docstring for why a background prober was rejected.
        """
        cfg = request.app.state.config
        connectivity = await run_connectivity_checks(cfg)
        return request.app.state.templates.TemplateResponse(
            request, "_diagnostics_connectivity.html", {"connectivity": connectivity},
        )

    @router.post("/settings/vehicles", dependencies=[Depends(require_csrf)])
    async def add_vehicle(
        request: Request,
        name: str = Form(...),
        make: str = Form(""),
        model: str = Form(""),
        plate: str = Form(""),
        is_default: str = Form(""),
        user: dict = Depends(require_user),
    ):
        name = name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name required")
        pool = request.app.state.pool
        async with pool.connection() as conn:
            await create_vehicle(
                conn, name, make.strip() or None, model.strip() or None,
                plate.strip() or None, is_default=(is_default == "1"),
            )
            vehicles = await list_vehicles(conn, include_inactive=True)
        return request.app.state.templates.TemplateResponse(
            request, "_vehicles_table.html", {"vehicles": vehicles}
        )

    @router.post("/settings/vehicles/{vehicle_id}/update", dependencies=[Depends(require_csrf)])
    async def edit_vehicle(
        request: Request,
        vehicle_id: int,
        name: str = Form(...),
        make: str = Form(""),
        model: str = Form(""),
        plate: str = Form(""),
        user: dict = Depends(require_user),
    ):
        name = name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name required")
        pool = request.app.state.pool
        async with pool.connection() as conn:
            await update_vehicle(
                conn, vehicle_id, name, make.strip() or None,
                model.strip() or None, plate.strip() or None,
            )
            vehicles = await list_vehicles(conn, include_inactive=True)
        return request.app.state.templates.TemplateResponse(
            request, "_vehicles_table.html", {"vehicles": vehicles}
        )

    @router.post("/settings/vehicles/{vehicle_id}/default", dependencies=[Depends(require_csrf)])
    async def make_vehicle_default(
        request: Request, vehicle_id: int, user: dict = Depends(require_user)
    ):
        pool = request.app.state.pool
        async with pool.connection() as conn:
            await set_default_vehicle(conn, vehicle_id)
            vehicles = await list_vehicles(conn, include_inactive=True)
        return request.app.state.templates.TemplateResponse(
            request, "_vehicles_table.html", {"vehicles": vehicles}
        )

    @router.post("/settings/vehicles/{vehicle_id}/deactivate", dependencies=[Depends(require_csrf)])
    async def deactivate_vehicle_route(
        request: Request, vehicle_id: int, user: dict = Depends(require_user)
    ):
        pool = request.app.state.pool
        async with pool.connection() as conn:
            await deactivate_vehicle(conn, vehicle_id)
            vehicles = await list_vehicles(conn, include_inactive=True)
        return request.app.state.templates.TemplateResponse(
            request, "_vehicles_table.html", {"vehicles": vehicles}
        )

    @router.post("/settings/vehicles/auto_assign", dependencies=[Depends(require_csrf)])
    async def set_auto_assign_default_vehicle_route(
        request: Request,
        auto_assign_default_vehicle: str = Form(""),
        user: dict = Depends(require_user),
    ):
        # An unchecked HTML checkbox submits nothing at all, so absence must
        # read as false -- there is no "unset" value to distinguish from off.
        pool = request.app.state.pool
        async with pool.connection() as conn:
            await set_auto_assign_default_vehicle(
                conn, auto_assign_default_vehicle == "1"
            )
        return _redirect_back(request)

    @router.post("/settings/odometer", dependencies=[Depends(require_csrf)])
    async def add_odometer_reading(
        request: Request,
        vehicle_id: int = Form(...),
        date: str = Form(...),
        time: str = Form("00:00"),
        value: float = Form(...),
        note: str = Form(""),
        user: dict = Depends(require_user),
    ):
        """Same date/time parse path and mi->meters conversion as
        `add_manual_trip`, so a 100 mi entry stores 160934.4 m — one
        canonical-meters convention across the whole app.
        """
        tz = request.app.state.config.display_tz
        try:
            recorded_at = datetime.fromisoformat(f"{date}T{time}").replace(tzinfo=tz)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date/time")
        # parse_finite_number(minimum=0) alone would newly accept 0, so the
        # strict "> 0" check stays separate from the finite check.
        parsed_value = parse_finite_number(value)
        if parsed_value is None or parsed_value <= 0:
            raise HTTPException(status_code=400, detail="Invalid odometer value")
        odometer_m = parsed_value * METERS_PER_MILE

        pool = request.app.state.pool
        async with pool.connection() as conn:
            try:
                await conn.execute(
                    "INSERT INTO odometer_readings (vehicle_id, recorded_at, odometer_m, note) "
                    "VALUES (%s, %s, %s, %s)",
                    (vehicle_id, recorded_at, odometer_m, note.strip() or None),
                )
            except errors.ForeignKeyViolation:
                raise HTTPException(status_code=400, detail="No such vehicle")
            except errors.UniqueViolation:
                raise HTTPException(status_code=400, detail="A reading already exists at that date/time")
            vehicles = await list_vehicles(conn, include_inactive=True)
            odometer = await _fetch_odometer_context(conn)
        return request.app.state.templates.TemplateResponse(
            request, "_odometer_table.html", {"vehicles": vehicles, "odometer": odometer}
        )

    @router.post("/settings/odometer/{reading_id}/delete", dependencies=[Depends(require_csrf)])
    async def delete_odometer_reading(
        request: Request, reading_id: int, user: dict = Depends(require_user)
    ):
        pool = request.app.state.pool
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM odometer_readings WHERE id = %s", (reading_id,))
            vehicles = await list_vehicles(conn, include_inactive=True)
            odometer = await _fetch_odometer_context(conn)
        return request.app.state.templates.TemplateResponse(
            request, "_odometer_table.html", {"vehicles": vehicles, "odometer": odometer}
        )

    @router.post("/settings/rates", dependencies=[Depends(require_csrf)])
    async def upsert_rate(
        request: Request,
        year: int = Form(...),
        rate_per_mi: float = Form(...),
        mid_year: str = Form(""),
        rate_h2_per_mi: str = Form(""),
        h2_start_month: int = Form(7),
        user: dict = Depends(require_user),
    ):
        parsed_rate = parse_finite_number(rate_per_mi)
        if parsed_rate is None or parsed_rate <= 0:
            raise HTTPException(status_code=400, detail="Rate must be positive")
        rate_per_mi = parsed_rate
        # Mid-year change is opt-in per year: when the toggle is off the year
        # keeps a single flat rate (both split columns NULL). When on, a valid
        # second-half rate and month are required (DB CHECK enforces the pair).
        h2_rate = None
        h2_month = None
        if mid_year == "1":
            try:
                h2_rate = float(rate_h2_per_mi)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400,
                    detail="Second-half rate is required when mid-year change is on",
                )
            parsed_h2_rate = parse_finite_number(h2_rate)
            if parsed_h2_rate is None or parsed_h2_rate <= 0:
                raise HTTPException(status_code=400, detail="Second-half rate must be positive")
            h2_rate = parsed_h2_rate
            if not (1 <= h2_start_month <= 12):
                raise HTTPException(status_code=400, detail="Invalid mid-year start month")
            h2_month = h2_start_month
        async with request.app.state.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO mileage_rates (year, rate_per_mi, rate_h2_per_mi, h2_start_month)"
                " VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (year) DO UPDATE SET"
                " rate_per_mi = EXCLUDED.rate_per_mi,"
                " rate_h2_per_mi = EXCLUDED.rate_h2_per_mi,"
                " h2_start_month = EXCLUDED.h2_start_month, updated_at = now()",
                (year, rate_per_mi, h2_rate, h2_month),
            )
            db_rates = await _fetch_rates_rows(conn)
        return request.app.state.templates.TemplateResponse(
            request, "_rates_table.html", {"rates": db_rates}
        )

    @router.get("/places/search")
    async def search_places(
        request: Request, q: str = Query(""), user: dict = Depends(require_user)
    ):
        cfg = request.app.state.config
        results = []
        q = q.strip()
        provider = cfg.geocode_provider
        if provider is not None and q:
            try:
                results = await provider.autocomplete(request.app.state.geocode_http_client, q)
            except (httpx.HTTPError, ValueError) as e:
                # Not str(e): the provider's request carries both its API
                # key and the user's typed search text as query parameters,
                # and a raise_for_status() HTTPStatusError's message embeds
                # the full URL it failed against.
                log.warning("address search failed: %s", type(e).__name__)
        return request.app.state.templates.TemplateResponse(
            request, "_address_results.html", {"results": results}
        )

    @router.post("/places", dependencies=[Depends(require_csrf)])
    async def create_place(
        request: Request,
        name: str = Form(...),
        kind: str = Form(...),
        lat: float = Form(...),
        lon: float = Form(...),
        radius_m: float = Form(150.0),
        user: dict = Depends(require_user),
    ):
        name = name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name required")
        if kind not in PLACE_KINDS:
            raise HTTPException(status_code=400, detail="Unknown kind")
        parsed_radius = parse_finite_number(radius_m)
        if parsed_radius is None or parsed_radius <= 0:
            raise HTTPException(status_code=400, detail="Radius must be positive")
        radius_m = parsed_radius
        # The geography cast below silently coerces an out-of-range
        # coordinate rather than rejecting it, so the range must be enforced
        # here or a bad lat/lon reaches the database wrong instead of refused.
        parsed_lat = parse_finite_number(lat, minimum=-90, maximum=90)
        parsed_lon = parse_finite_number(lon, minimum=-180, maximum=180)
        if parsed_lat is None or parsed_lon is None:
            raise HTTPException(status_code=400, detail="Invalid coordinates")
        lat, lon = parsed_lat, parsed_lon
        pool = request.app.state.pool
        async with pool.connection() as conn:
            try:
                await conn.execute(
                    "INSERT INTO places (name, kind, geom, radius_m) "
                    "VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)",
                    (name, kind, lon, lat, radius_m),
                )
            except errors.UniqueViolation:
                raise HTTPException(status_code=400, detail="A place with that name already exists")
        await reprocess_places(pool)
        return _redirect_back(request)

    @router.post("/places/{place_id}/update", dependencies=[Depends(require_csrf)])
    async def update_place(
        request: Request,
        place_id: int,
        name: str = Form(...),
        kind: str = Form(...),
        lat: float = Form(...),
        lon: float = Form(...),
        radius_m: float = Form(...),
        user: dict = Depends(require_user),
    ):
        name = name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name required")
        if kind not in PLACE_KINDS:
            raise HTTPException(status_code=400, detail="Unknown kind")
        parsed_radius = parse_finite_number(radius_m)
        if parsed_radius is None or parsed_radius <= 0:
            raise HTTPException(status_code=400, detail="Radius must be positive")
        radius_m = parsed_radius
        # The geography cast below silently coerces an out-of-range
        # coordinate rather than rejecting it, so the range must be enforced
        # here or a bad lat/lon reaches the database wrong instead of refused.
        parsed_lat = parse_finite_number(lat, minimum=-90, maximum=90)
        parsed_lon = parse_finite_number(lon, minimum=-180, maximum=180)
        if parsed_lat is None or parsed_lon is None:
            raise HTTPException(status_code=400, detail="Invalid coordinates")
        lat, lon = parsed_lat, parsed_lon
        pool = request.app.state.pool
        async with pool.connection() as conn:
            try:
                await conn.execute(
                    "UPDATE places SET name = %s, kind = %s, "
                    " geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, radius_m = %s "
                    "WHERE id = %s",
                    (name, kind, lon, lat, radius_m, place_id),
                )
            except errors.UniqueViolation:
                raise HTTPException(status_code=400, detail="A place with that name already exists")
        await reprocess_places(pool)
        return _redirect_back(request)

    @router.post("/places/{place_id}/delete", dependencies=[Depends(require_csrf)])
    async def delete_place(request: Request, place_id: int, user: dict = Depends(require_user)):
        pool = request.app.state.pool
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM places WHERE id = %s", (place_id,))
        await reprocess_places(pool)
        return _redirect_back(request)

    @router.post("/rules", dependencies=[Depends(require_csrf)])
    async def create_rule(
        request: Request,
        a_mode: str = Form(...),
        a_kind: str = Form(""),
        a_place: str = Form(""),
        b_mode: str = Form(...),
        b_kind: str = Form(""),
        b_place: str = Form(""),
        category: str = Form(...),
        user: dict = Depends(require_user),
    ):
        if category not in RULE_CATEGORIES:
            raise HTTPException(status_code=400, detail="Unknown category")

        def resolve(mode: str, kind: str, place: str) -> tuple[int | None, str | None]:
            if mode == "place":
                if not place:
                    raise HTTPException(status_code=400, detail="Select a place")
                return int(place), None
            if mode == "kind":
                if kind not in PLACE_KINDS:
                    raise HTTPException(status_code=400, detail="Unknown kind")
                return None, kind
            return None, None

        a_place_id, a_kind_val = resolve(a_mode, a_kind, a_place)
        b_place_id, b_kind_val = resolve(b_mode, b_kind, b_place)
        if a_place_id is None and a_kind_val is None and b_place_id is None and b_kind_val is None:
            raise HTTPException(
                status_code=400, detail="At least one side must be a specific place or a kind"
            )

        pool = request.app.state.pool
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO tag_rules (a_place, a_kind, b_place, b_kind, category) "
                "VALUES (%s, %s, %s, %s, %s)",
                (a_place_id, a_kind_val, b_place_id, b_kind_val, category),
            )
        await reprocess_places(pool)
        return _redirect_back(request)

    @router.post("/rules/{rule_id}/delete", dependencies=[Depends(require_csrf)])
    async def delete_rule(request: Request, rule_id: int, user: dict = Depends(require_user)):
        pool = request.app.state.pool
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM tag_rules WHERE id = %s", (rule_id,))
        await reprocess_places(pool)
        return _redirect_back(request)

    return router
