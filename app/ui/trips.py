from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Path, Query, Request
from psycopg import errors
from psycopg.rows import dict_row
from starlette.responses import JSONResponse, RedirectResponse, Response

from app.account_context import account_id
from app.auth import require_csrf, require_user
from app.page import render_page
from app.dashboard import parse_week_anchor
from app.db import DETECTOR_ADVISORY_LOCK_KEY
from app.detector.runner import load_trip_points
from app.expenses import (
    CATEGORY_LABELS,
    EXPENSE_CATEGORIES,
    EXPENSE_TREATMENTS,
    TREATMENT_LABELS,
)
from app.formatting import format_miles
from app.rates import deduction, load_rates
from app.report import sum_month_deductions
from app.trip_queries import DISPLAY_DISTANCE_SQL
from app.vehicles import list_vehicles

from app.ui._common import (
    CATEGORIES,
    EXCLUSIONS,
    TRIP_COLUMNS,
    _fetch_recent_purposes,
    _fetch_trip,
    _month_bounds,
    _month_page_url,
    _parse_vehicle_form,
    _parse_vehicle_id,
    _trip_filter_sql,
    _url_with_filters,
    normalize_trip_label,
    parse_date_range,
)
from app.ui.manual import (
    MANUAL_ROUTE_UNAVAILABLE_NOTICE,
    ManualTripValidationError,
    parse_manual_trip_input,
)
from app.ui.expenses import _EXPENSE_SELECT_JOIN, _annotate_expense, _parse_expense_input


def _normalize_dashboard_week(request: Request, dashboard_week: str) -> str:
    """Reduce a raw dashboard_week request value to an empty string or a
    valid ISO date before it reaches a template.

    The card/edit routes echo this value straight into an hx-vals JSON
    attribute and a constructed edit URL, so it must be a known-good shape
    rather than whatever text a bookmarked or hand-edited request happened
    to carry. parse_week_anchor already tolerates a malformed value by
    falling back to today, so this only needs to skip that normalization
    for the empty-string case, which routes treat as "no dashboard".
    """
    if not dashboard_week:
        return ""
    tz = request.state.config.display_tz
    now = datetime.now(tz)
    return parse_week_anchor(dashboard_week, tz, now).isoformat()


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
    q: str = "",
    exclusion: str = "",
) -> tuple[list[dict], bool]:
    """Fetch one stable local-month page plus a one-row `has_more` sentinel."""
    month_start, month_end = _month_bounds(year, month, tz)
    where, params = _trip_filter_sql(
        category, from_dt, to_dt, vehicle_id, q=q, exclusion=exclusion, owner_id=account_id(conn)
    )
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


async def _fetch_trip_card_context(
    pool, trip_id: int, *, vehicles: list[dict] | None = None,
    recent_purposes: list[str] | None = None,
) -> dict:
    """Build the one context shape used by every standalone card render.

    Active vehicles keep pickers concise, while TRIP_COLUMNS carries the name
    of an inactive vehicle already assigned to the trip so editing never
    silently makes that valid stored value unrepresentable.
    """
    trip = await _fetch_trip(pool, trip_id)
    if vehicles is None or recent_purposes is None:
        async with pool.connection() as conn:
            if vehicles is None:
                vehicles = await list_vehicles(conn)
            if recent_purposes is None:
                recent_purposes = await _fetch_recent_purposes(conn)
    return {"trip": trip, "vehicles": vehicles, "recent_purposes": recent_purposes}


def _trip_edit_values(trip: dict, tz: ZoneInfo) -> dict[str, str]:
    started_at = trip["started_at"].astimezone(tz)
    ended_at = trip["ended_at"].astimezone(tz)
    return {
        "category": trip["category"],
        "exclusion": trip.get("exclusion") or "",
        "purpose": trip.get("purpose") or "",
        "notes": trip.get("notes") or "",
        "vehicle_id": str(trip["vehicle_id"]) if trip.get("vehicle_id") is not None else "",
        "date": started_at.strftime("%Y-%m-%d"),
        "start_time": started_at.strftime("%H:%M"),
        "end_time": ended_at.strftime("%H:%M"),
        "distance": format_miles(float(trip["distance_m"])),
        "start_label": trip.get("start_label") or "",
        "end_label": trip.get("end_label") or "",
    }


async def _submitted_label_fields(
    request: Request, start_label: str | None, end_label: str | None,
) -> tuple[str | None, str | None]:
    """Preserve the distinction between an omitted and empty label field.

    FastAPI maps an empty optional ``Form(None)`` field to its default before
    the route runs. Reading the raw form restores the browser's submitted
    presence, while the fallback keeps direct route-function tests and other
    non-HTTP callers compatible with the existing optional arguments.
    """
    form_reader = getattr(request, "form", None)
    if form_reader is None:
        return (
            start_label if isinstance(start_label, str) else None,
            end_label if isinstance(end_label, str) else None,
        )
    form = await form_reader()
    values: list[str | None] = []
    for field, parsed in (("start_label", start_label), ("end_label", end_label)):
        if field not in form:
            values.append(None)
            continue
        raw = form[field]
        if isinstance(raw, str):
            values.append(raw)
        elif isinstance(parsed, str):
            values.append(parsed)
        else:
            values.append("")
    return values[0], values[1]


async def _fetch_trip_expenses(conn, trip_id: int, tz: ZoneInfo) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        _EXPENSE_SELECT_JOIN
        + "WHERE expenses.trip_id = %s AND expenses.account_id = %s ORDER BY expenses.incurred_on DESC, expenses.id DESC",
        (trip_id, account_id(conn)),
    )
    return [_annotate_expense(row, tz) for row in await cur.fetchall()]


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
    await conn.execute(
        "SELECT pg_advisory_xact_lock(%s)", (DETECTOR_ADVISORY_LOCK_KEY,)
    )
    cur = await conn.execute(
        "SELECT device, tracking_device_id, source::text, imported, started_at, ended_at "
        "FROM trips WHERE id = %s AND account_id = %s FOR UPDATE",
        (trip_id, account_id(conn)),
    )
    trip = await cur.fetchone()
    if not trip:
        raise HTTPException(status_code=404, detail="No such trip")
    device, tracking_device_id, source, imported, started_at, ended_at = trip
    if source == "manual" or imported:
        await conn.execute("DELETE FROM trips WHERE id = %s AND account_id = %s", (trip_id, account_id(conn)))
    else:
        await conn.execute(
            "INSERT INTO trip_boundary_overrides "
            "(account_id, tracking_device_id, device, kind, range_start, range_end) "
            "VALUES (%s, %s, %s, 'discard', %s, %s) ON CONFLICT DO NOTHING",
            (account_id(conn), tracking_device_id, device, started_at, ended_at),
        )
        await conn.execute("DELETE FROM trips WHERE id = %s AND account_id = %s", (trip_id, account_id(conn)))
    return started_at, trip_id


async def _trip_position(conn, trip_id: int) -> tuple[datetime, int] | None:
    """The `(started_at, id)` cursor a review pass advances past. Deliberately
    not filtered by category: tag-and-advance calls this on a trip that has
    just left the unclassified set. None means the trip vanished mid-pass,
    callers fall back to no cursor, restarting from the oldest trip rather
    than 404ing.
    """
    cur = await conn.execute("SELECT started_at FROM trips WHERE id = %s AND account_id = %s", (trip_id, account_id(conn)))
    row = await cur.fetchone()
    return (row[0], trip_id) if row else None


async def _apply_human_tag(
    conn, trip_id: int, category: str, purpose: str | None = None,
    *, update_purpose: bool = False,
) -> None:
    """Set tag_source='human' unconditionally, even on a clear back to
    'unclassified'. This is the load-bearing line for human-tag supremacy: the
    auto-tagger (app.autotag) only touches rows with tag_source NULL or
    'rule', so a deliberate human choice can never be overwritten by a rule.
    Shared by the list-view tag buttons and /review's tag-and-advance so the
    guarantee lives in one place; category validation stays with each caller
    (the list view allows clearing to 'unclassified', review does not).

    Checks rowcount and 404s if the row is gone: the detector's reconcile
    deletes and reinserts trips it replaces, and without this guard a human
    tag racing that delete would silently match zero rows and report success.
    """
    if update_purpose:
        cur = await conn.execute(
            "UPDATE trips SET category = %s, purpose = %s, tag_source = 'human', "
            "updated_at = now() WHERE id = %s AND account_id = %s",
            (category, (purpose or "").strip() or None, trip_id, account_id(conn)),
        )
    else:
        cur = await conn.execute(
            "UPDATE trips SET category = %s, tag_source = 'human', updated_at = now() "
            "WHERE id = %s AND account_id = %s",
            (category, trip_id, account_id(conn)),
        )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="No such trip")


# Shared with the single-month recompute below so a month's trip_count/
# total_m/business_m can never be computed with different SQL in the two
# places that need them.
_MONTH_AGGREGATE_COLUMNS_SQL = (
    "count(*) AS trip_count, "
    f"COALESCE(SUM({DISPLAY_DISTANCE_SQL}) FILTER "
    "(WHERE exclusion IS DISTINCT FROM 'not_my_vehicle'), 0) AS total_m, "
    f"COALESCE(SUM({DISPLAY_DISTANCE_SQL}) "
    "FILTER (WHERE category = 'business' AND exclusion IS NULL), 0) AS business_m"
)


def _build_month_summary(year: int, month_num: int, row: dict, rates) -> dict:
    """Shape one month's raw aggregate row into the dict trips.html's month
    summary partial renders. Used by both the full archive build and the
    inline-classify out-of-band recompute below, so the two can never
    disagree on what a "month summary" contains.
    """
    summary = {
        "label": date(year, month_num, 1).strftime("%B %Y"),
        "year": year, "month_num": month_num,
        "trip_count": row["trip_count"],
        "total_m": float(row["total_m"]),
        "business_m": float(row["business_m"]),
    }
    summary["business_deduction"] = deduction(
        summary["business_m"], year, rates, month_num
    )
    return summary


async def _fetch_month_summary(
    conn, tz: ZoneInfo, year: int, month: int, rates,
    category: str, from_dt: datetime | None, to_dt: datetime | None,
    vehicle_id: int | None | Literal["none"], q: str, exclusion: str,
) -> dict | None:
    """Recompute one archive month's summary figures against the live
    database, reusing the exact filter SQL (`_trip_filter_sql`) and column
    SQL (`_MONTH_AGGREGATE_COLUMNS_SQL`) the full list build uses. None
    means the month has nothing left to show under these filters, so a
    caller must skip its out-of-band fragment rather than push out a
    hollow one.
    """
    month_start, month_end = _month_bounds(year, month, tz)
    where, params = _trip_filter_sql(category, from_dt, to_dt, vehicle_id, q=q, exclusion=exclusion, owner_id=account_id(conn))
    where += " AND" if where else "WHERE"
    where += " started_at >= %s AND started_at < %s"
    params.extend((month_start, month_end))
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(f"SELECT {_MONTH_AGGREGATE_COLUMNS_SQL} FROM trips {where}", params)
    row = await cur.fetchone()
    if not row or not row["trip_count"]:
        return None
    return _build_month_summary(year, month, row, rates)


async def _fetch_ytd_deduction(conn, tz: ZoneInfo, rates) -> tuple[int, float | None]:
    """The archive header's year-to-date business deduction. Unlike every
    other archive figure, this one is filter-independent: it always
    describes the whole current year regardless of what filters, if any,
    are on screen. Shared by the full list build and the inline-classify
    out-of-band response so a reclassification's effect on this figure can
    never drift between the two.
    """
    now = datetime.now(tz)
    ytd_year = now.year
    year_start = datetime(ytd_year, 1, 1, tzinfo=tz)
    next_year_start = datetime(ytd_year + 1, 1, 1, tzinfo=tz)
    # Grouped by local month so a mid-year rate change prices each half at
    # its own rate (see sum_month_deductions).
    cur = await conn.execute(
        "SELECT EXTRACT(MONTH FROM started_at AT TIME ZONE %s)::int AS m,"
        f" COALESCE(SUM({DISPLAY_DISTANCE_SQL}), 0) FROM trips"
        " WHERE account_id = %s AND category = 'business' AND exclusion IS NULL "
        "AND started_at >= %s AND started_at < %s"
        " GROUP BY m",
        (tz.key, account_id(conn), year_start, next_year_start),
    )
    ytd_by_month = [(r[0], r[1]) for r in await cur.fetchall()]
    return ytd_year, sum_month_deductions(ytd_by_month, ytd_year, rates)


def _parse_archive_current_url(url: str) -> dict[str, str] | None:
    """Extract the archive's own filter query params from htmx's
    HX-Current-URL request header, which carries the browser's actual
    address bar URL (not the fragment endpoint's own URL) on every htmx
    request.

    None means "no usable page context": either the header is absent (a
    non-htmx caller, or one predating this feature in a direct route-call
    test) or its path is not the archive itself. Either way a caller must
    not guess at what filters, if any, are currently on screen, and must
    skip anything that would need that context to be correct rather than
    risk emitting a figure that does not match what is actually rendered.
    """
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.path.rstrip("/") != "/trips":
        return None
    qs = parse_qs(parsed.query)

    def first(name: str) -> str:
        values = qs.get(name)
        return values[0] if values else ""

    return {
        "category": first("category"), "from_": first("from"), "to": first("to"),
        "vehicle": first("vehicle"), "q": first("q"), "exclusion": first("exclusion"),
    }


def _archive_history_directive(current_url: str, archive_state: dict) -> tuple[str, str]:
    """Choose the history header a list response carries, and its value.

    The value is always the archive's own canonical ``/trips`` URL: a
    fragment endpoint must never become the address bar URL or the document
    a later history restore asks for. Pushing unconditionally would add a
    duplicate entry whenever a filter re-resolves to the state already in
    the address bar (a preset resolving to the same concrete dates, or a
    control set back to what it already was), so an unchanged canonical URL
    replaces instead. Without usable page context there is nothing to
    compare against, so the response pushes.
    """
    page_filters = _parse_archive_current_url(current_url)
    unchanged = page_filters is not None and page_filters == {
        "category": archive_state["category"],
        "from_": archive_state["from"],
        "to": archive_state["to"],
        "vehicle": archive_state["vehicle"],
        "q": archive_state["q"],
        "exclusion": archive_state["exclusion"],
    }
    header = "HX-Replace-Url" if unchanged else "HX-Push-Url"
    return header, archive_state["url"]


def _mark_archive_write(response: Response) -> Response:
    """Mark a committed archive row write for the client coordinator.

    Edit validation redisplays intentionally omit this marker, even though
    they use HTTP 200, so the archive keeps the user's invalid values visible
    instead of replacing them with a refresh.
    """
    response.headers["X-Archive-Write"] = "success"
    return response


def _parse_archive_loaded_depth(raw: str, page_size: int) -> dict[tuple[int, int], int]:
    """Parse transient per-month row depths used by an archive refresh.

    The client sends a JSON object whose keys are local ``YYYY-MM`` buckets
    and whose values are the number of rows it has already rendered. This is
    deliberately separate from the archive's canonical filter URL. Values
    are bounded before they reach the query limit, and malformed metadata is
    rejected rather than silently changing a refresh back to the first page.
    """
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid archive loaded depth") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid archive loaded depth")

    depths: dict[tuple[int, int], int] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            raise HTTPException(status_code=400, detail="Invalid archive loaded depth")
        if (
            len(key) != 7
            or key[4] != "-"
            or not key[:4].isdigit()
            or not key[5:].isdigit()
        ):
            raise HTTPException(status_code=400, detail="Invalid archive loaded depth")
        try:
            year_text, month_text = key.split("-", 1)
            year, month = int(year_text), int(month_text)
        except (ValueError, AttributeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid archive loaded depth") from exc
        if year < 1 or year > 9998 or month < 1 or month > 12:
            raise HTTPException(status_code=400, detail="Invalid archive loaded depth")
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(status_code=400, detail="Invalid archive loaded depth")
        if value < 0 or value > 100_000:
            raise HTTPException(status_code=400, detail="Invalid archive loaded depth")
        # A short final page can legitimately be smaller than the configured
        # page size, so preserve the submitted count exactly. Zero is also a
        # valid transient depth for a month that had no rendered rows yet.
        depths[(year, month)] = value
    return depths


ARCHIVE_DATE_PRESETS = ("all", "this_month", "last_month", "this_year", "custom")


def _archive_date_preset_ranges(
    tz: ZoneInfo, now: datetime | None = None,
) -> dict[str, tuple[str, str]]:
    """Return concrete inclusive archive date bounds in the display timezone."""
    local_now = (now or datetime.now(tz)).astimezone(tz)
    current_month = date(local_now.year, local_now.month, 1)
    if local_now.month == 1:
        previous_month = date(local_now.year - 1, 12, 1)
    else:
        previous_month = date(local_now.year, local_now.month - 1, 1)

    def month_end(start: date) -> date:
        if start.month == 12:
            return date(start.year, 12, 31)
        return date(start.year, start.month + 1, 1) - timedelta(days=1)

    current_year = date(local_now.year, 1, 1)
    return {
        "all": ("", ""),
        "this_month": (current_month.isoformat(), month_end(current_month).isoformat()),
        "last_month": (previous_month.isoformat(), month_end(previous_month).isoformat()),
        "this_year": (current_year.isoformat(), date(local_now.year, 12, 31).isoformat()),
        "custom": ("", ""),
    }


def _infer_archive_date_preset(
    from_: str, to: str, tz: ZoneInfo, now: datetime | None = None,
) -> str:
    """Infer the control choice from concrete canonical date bounds."""
    ranges = _archive_date_preset_ranges(tz, now)
    for preset in ("all", "this_month", "last_month", "this_year"):
        if (from_, to) == ranges[preset]:
            return preset
    return "custom"


def _resolve_archive_date_filter(
    from_: str, to: str, date_preset: str, tz: ZoneInfo,
    now: datetime | None = None,
) -> tuple[str, str, str]:
    """Resolve a transient preset, then return concrete bounds and its state."""
    if date_preset in ARCHIVE_DATE_PRESETS and date_preset not in ("all", "custom"):
        from_, to = _archive_date_preset_ranges(tz, now)[date_preset]
    elif date_preset == "all":
        from_, to = "", ""
    elif date_preset == "custom":
        # An explicit Custom request stays "custom" even with empty or open
        # bounds, so it doesn't collapse back to "all" (its inferred state
        # for empty bounds) the moment another control changes and re-resolves
        # this filter, which would hide the Custom inputs out from under
        # someone who deliberately opened them.
        return from_, to, "custom"
    return from_, to, _infer_archive_date_preset(from_, to, tz, now)


async def _fetch_archive_context(
    request: Request,
    *,
    category: str,
    from_: str,
    to: str,
    vehicle: str,
    q: str,
    exclusion: str,
    date_preset: str = "",
    loaded_depth: str = "",
    include_recent_purposes: bool = False,
) -> dict:
    """Build the shared filtered archive data and rendering context.

    Both the full archive page and ``GET /trips/list`` use this path, so month
    grouping, row ordering, summaries, pagination, exports, and YTD all come
    from one source. Vehicles are always loaded for archive filters and bulk
    dialogs. Recent purpose suggestions are loaded only for the full document,
    while the dedicated manual page owns its own place lookup.
    """
    exclusion = exclusion if isinstance(exclusion, str) else ""
    tz = request.state.config.display_tz
    from_, to, resolved_date_preset = _resolve_archive_date_filter(
        from_, to, date_preset, tz,
    )
    from_dt, to_dt = parse_date_range(from_, to, tz)
    vehicle_id = _parse_vehicle_id(vehicle)
    where, params = _trip_filter_sql(
        category, from_dt, to_dt, vehicle_id, q=q, exclusion=exclusion,
        owner_id=request.state.principal.account_id,
    )
    page_size = request.state.config.trips_page_size
    depths = _parse_archive_loaded_depth(loaded_depth, page_size)
    max_depth = max([page_size, *depths.values()])

    async with request.state.account_pool.connection() as conn:
        aggregate_cur = conn.cursor(row_factory=dict_row)
        await aggregate_cur.execute(
            "SELECT date_trunc('month', started_at AT TIME ZONE %s)::date AS local_month, "
            f"{_MONTH_AGGREGATE_COLUMNS_SQL} "
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
            [tz.key, tz.key, *params, max_depth + 1],
        )
        initial_rows = await page_cur.fetchall()
        rates = await load_rates(conn)
        vehicles = await list_vehicles(conn)
        recent_purposes = (
            await _fetch_recent_purposes(conn) if include_recent_purposes else []
        )
        ytd_year, ytd_deduction = await _fetch_ytd_deduction(conn, tz, rates)

    page_rows_by_month: dict = {}
    for trip in initial_rows:
        page_rows_by_month.setdefault(trip["local_month"], []).append(trip)

    months = []
    for summary in month_rows:
        local_month = summary["local_month"]
        month_key = (local_month.year, local_month.month)
        depth = depths.get(month_key, page_size)
        candidates = page_rows_by_month.get(local_month, [])
        trips = candidates[:depth]
        month = _build_month_summary(local_month.year, local_month.month, summary, rates)
        month["trips"] = trips
        month["has_more"] = len(candidates) > depth
        month["loaded_depth"] = len(trips)
        month["next_url"] = _month_page_url(
            month["year"], month["month_num"], depth,
            category, from_, to, vehicle, q, exclusion,
        )
        months.append(month)

    # Keep the link builder in this shared context so the list response can
    # update export targets in the same response as its rows.
    from app.ui.review import _review_url

    def export_url(fmt: str) -> str:
        return _url_with_filters(
            "/export", from_, to, vehicle, q, format=fmt, category=category,
            exclusion=exclusion,
        )

    return {
        "months": months,
        "vehicles": vehicles,
        "recent_purposes": recent_purposes,
        "filter_category": category,
        "filter_from": from_,
        "filter_to": to,
        "filter_vehicle": vehicle,
        "filter_q": q,
        "filter_exclusion": exclusion,
        "archive_date_preset": resolved_date_preset,
        "archive_filters_active": bool(
            category or from_ or to or vehicle or q.strip() or exclusion
        ),
        "export_url": export_url,
        "review_url": _review_url(from_, to, vehicle, q),
        "ytd_year": ytd_year,
        "ytd_deduction": ytd_deduction,
        # History restoration replaces the results root only, so everything
        # a sibling control needs (including the export targets that live
        # outside that root) has to travel inside it.
        "archive_state": {
            "url": _url_with_filters(
                "/trips", from_, to, vehicle, q,
                category=category, exclusion=exclusion,
            ),
            "date_preset": resolved_date_preset,
            "from": from_,
            "to": to,
            "category": category,
            "vehicle": vehicle,
            "q": q.strip(),
            "exclusion": exclusion,
            "export_csv": export_url("csv"),
            "export_xlsx": export_url("xlsx"),
        },
    }


def register_archive(router: APIRouter) -> None:
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
            notice: str = Query(""),
            q: str = Query(""),
            exclusion: str = Query(""),
            date_preset: str = Query(""),
        ):
            legacy_manual_params = {
                name: value for name, value in (
                    ("manual_date", manual_date),
                    ("manual_start", manual_start),
                    ("manual_notes", manual_notes),
                    ("bridge_trip", bridge_trip),
                ) if value
            }
            if legacy_manual_params or manual_open:
                target = "/trips/manual"
                if legacy_manual_params:
                    target += "?" + urlencode(legacy_manual_params)
                return RedirectResponse(target + "#manual-trip", status_code=302)
            archive = await _fetch_archive_context(
                request,
                category=category,
                from_=from_,
                to=to,
                vehicle=vehicle,
                q=q,
                exclusion=exclusion,
                date_preset=date_preset,
                include_recent_purposes=True,
            )
            months = archive["months"]
            vehicles = archive["vehicles"]
            recent_purposes = archive["recent_purposes"]

            # Whitelisted, not reflected as-is: `notice` is an attacker-
            # controlled query param, and the only thing it may ever mean is
            # "the manual trip you just saved has no route" -- anything else
            # collapses to no notice rather than echoing arbitrary query text
            # onto the page.
            notice_value = notice if notice == MANUAL_ROUTE_UNAVAILABLE_NOTICE else ""

            return await render_page(
                request, "trips.html",
                {
                    **archive,
                    "months": months, "vehicles": vehicles, "recent_purposes": recent_purposes,
                    "notice": notice_value,
                    "user": user, "csrf": request.session.get("csrf", ""),
                },
            )

        @router.get("/trips/list")
        async def trips_archive_list(
            request: Request,
            user: dict = Depends(require_user),
            category: str = Query(""),
            from_: str = Query("", alias="from"),
            to: str = Query(""),
            vehicle: str = Query(""),
            q: str = Query(""),
            exclusion: str = Query(""),
            date_preset: str = Query(""),
            loaded_depth: str = Query(""),
        ):
            """Return the swappable archive rows and sibling header fragments.

            This endpoint intentionally renders only archive fragments. A
            browser request for the document remains on ``GET /trips`` so an
            htmx response can never become the page used by history restore.
            """
            archive = await _fetch_archive_context(
                request,
                category=category,
                from_=from_,
                to=to,
                vehicle=vehicle,
                q=q,
                exclusion=exclusion,
                date_preset=date_preset,
                loaded_depth=loaded_depth,
            )
            archive.update({
                "export_oob": True,
                "ytd_oob": True,
            })
            response = request.app.state.templates.TemplateResponse(
                request, "_trip_archive_response.html", archive
            )
            header, url = _archive_history_directive(
                request.headers.get("HX-Current-URL", ""), archive["archive_state"],
            )
            response.headers[header] = url
            return response

        @router.get("/trips/selection")
        async def trips_selection(
            request: Request,
            user: dict = Depends(require_user),
            category: str = Query(""),
            from_: str = Query("", alias="from"),
            to: str = Query(""),
            vehicle: str = Query(""),
            q: str = Query(""),
            exclusion: str = Query(""),
            date_preset: str = Query(""),
        ):
            """Return the complete matching trip-ID snapshot for selection.

            Selection intentionally ignores archive pagination. The returned
            IDs are the mutation contract, so later writes can target this
            exact snapshot even if the rows no longer match these filters.
            """
            exclusion = exclusion if isinstance(exclusion, str) else ""
            tz = request.state.config.display_tz
            from_, to, _ = _resolve_archive_date_filter(
                from_, to, date_preset, tz,
            )
            from_dt, to_dt = parse_date_range(from_, to, tz)
            where, params = _trip_filter_sql(
                category, from_dt, to_dt, _parse_vehicle_id(vehicle),
                q=q, exclusion=exclusion, owner_id=request.state.principal.account_id,
            )
            async with request.state.account_pool.connection() as conn:
                cur = await conn.execute(
                    f"SELECT id FROM trips {where} ORDER BY started_at DESC, id DESC",
                    params,
                )
                trip_ids = [row[0] for row in await cur.fetchall()]
            return JSONResponse(
                {"trip_ids": trip_ids, "count": len(trip_ids)},
                headers={"Cache-Control": "no-store"},
            )


def register_month_page(router: APIRouter) -> None:
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
            q: str = Query(""),
            exclusion: str = Query(""),
        ):
            exclusion = exclusion if isinstance(exclusion, str) else ""
            tz = request.state.config.display_tz
            page_size = request.state.config.trips_page_size
            from_dt, to_dt = parse_date_range(from_, to, tz)
            async with request.state.account_pool.connection() as conn:
                trips, has_more = await _fetch_month_page(
                    conn, tz, year, month, page_size, offset, category,
                    from_dt, to_dt, _parse_vehicle_id(vehicle), q, exclusion,
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
                        year, month, offset + page_size, category, from_, to, vehicle, q,
                        exclusion,
                    ),
                },
            )


def register(router: APIRouter) -> None:
        @router.get("/trips/{trip_id}/card")
        async def trip_card(
            request: Request,
            trip_id: int,
            user: dict = Depends(require_user),
            dashboard_week: Annotated[str, Query()] = "",
        ):
            dashboard_week = _normalize_dashboard_week(request, dashboard_week)
            ctx = await _fetch_trip_card_context(request.state.account_pool, trip_id)
            if dashboard_week:
                ctx["dashboard_week"] = dashboard_week
                return request.app.state.templates.TemplateResponse(
                    request, "_dashboard_trip_row.html", ctx
                )
            return request.app.state.templates.TemplateResponse(request, "_trip_archive_row.html", ctx)

        @router.get("/trips/{trip_id}/edit")
        async def edit_trip_card(
            request: Request,
            trip_id: int,
            user: dict = Depends(require_user),
            dashboard_week: Annotated[str, Query()] = "",
        ):
            dashboard_week = _normalize_dashboard_week(request, dashboard_week)
            ctx = await _fetch_trip_card_context(request.state.account_pool, trip_id)
            ctx.update({
                "values": _trip_edit_values(ctx["trip"], request.state.config.display_tz),
                "errors": {},
            })
            if dashboard_week:
                ctx["dashboard_week"] = dashboard_week
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
            exclusion: str = Form(""),
            start_label: str | None = Form(None),
            end_label: str | None = Form(None),
            dashboard_week: Annotated[str, Form()] = "",
        ):
            dashboard_week = _normalize_dashboard_week(request, dashboard_week)
            exclusion = exclusion if isinstance(exclusion, str) else ""
            start_label, end_label = await _submitted_label_fields(
                request, start_label, end_label
            )
            values = {
                "category": category,
                "exclusion": exclusion,
                "purpose": purpose,
                "notes": notes,
                "vehicle_id": vehicle_id,
                "date": date,
                "start_time": start_time,
                "end_time": end_time,
                "distance": distance,
                # `or ""`: an omitted field is None from
                # `_submitted_label_fields`, same as a submitted-blank one
                # for this redisplay's purposes -- the edit card's own
                # per-endpoint eligibility check decides whether the input
                # renders at all, not this value.
                "start_label": start_label or "",
                "end_label": end_label or "",
            }
            field_errors: dict[str, str] = {}
            if category not in CATEGORIES:
                field_errors["category"] = "Choose a valid category."
            if exclusion and exclusion not in EXCLUSIONS:
                field_errors["exclusion"] = "Choose a valid exclusion."
            try:
                parsed_vehicle_id = int(vehicle_id) if vehicle_id else None
            except ValueError:
                parsed_vehicle_id = None
                field_errors["vehicle_id"] = "Choose a valid vehicle."

            normalized_purpose = purpose.strip() or None
            normalized_notes = notes.strip() or None
            pool = request.state.account_pool
            try:
                async with request.state.account_pool.connection() as conn:
                    async with conn.transaction():
                        cur = conn.cursor(row_factory=dict_row)
                        await cur.execute(
                            "SELECT source::text AS source, category::text AS category, purpose, "
                            "tag_source::text AS tag_source, start_place_id, end_place_id, "
                            "start_geom IS NOT NULL AS has_start_geom, "
                            "end_geom IS NOT NULL AS has_end_geom "
                            "FROM trips WHERE id = %s AND account_id = %s FOR UPDATE",
                            (trip_id, account_id(conn)),
                        )
                        stored = await cur.fetchone()
                        if stored is None:
                            raise HTTPException(status_code=404, detail="No such trip")

                        if parsed_vehicle_id is not None and "vehicle_id" not in field_errors:
                            vehicle_cur = await conn.execute(
                                "SELECT 1 FROM vehicles WHERE id = %s AND account_id = %s", (parsed_vehicle_id, account_id(conn))
                            )
                            if await vehicle_cur.fetchone() is None:
                                field_errors["vehicle_id"] = "Choose a vehicle that still exists."

                        # A label only names an endpoint that has nothing
                        # else naming it (see migrations/025_manual_trip_labels.sql):
                        # a detected trip or a routed manual trip's endpoint
                        # already has a saved place or geometry describing
                        # it, so a nonblank label submitted for one is
                        # rejected rather than silently dropped. Checked per
                        # endpoint, matching the database constraint's own
                        # per-column shape rather than assuming the two
                        # endpoints are always equally eligible.
                        label_eligible = {
                            "start_label": (
                                stored["source"] == "manual"
                                and stored["start_place_id"] is None
                                and not stored["has_start_geom"]
                            ),
                            "end_label": (
                                stored["source"] == "manual"
                                and stored["end_place_id"] is None
                                and not stored["has_end_geom"]
                            ),
                        }
                        # A field this request never submitted (raw_value is
                        # None from `_submitted_label_fields`) is left out
                        # of `normalized_labels` entirely, and the
                        # UPDATE below only assigns the columns present in
                        # that dict, so an absent field leaves the stored
                        # label untouched. A field that *was* submitted,
                        # even blank, still normalizes to None and clears
                        # it, matching the add/change/clear behavior the
                        # plan requires.
                        normalized_labels: dict[str, str | None] = {}
                        for field, raw_value in (
                            ("start_label", start_label), ("end_label", end_label),
                        ):
                            if raw_value is None:
                                continue
                            try:
                                normalized_labels[field] = normalize_trip_label(raw_value, field)
                            except ManualTripValidationError as exc:
                                field_errors.update(exc.errors)
                                continue
                            if normalized_labels[field] is not None and not label_eligible[field]:
                                field_errors[field] = (
                                    "Location names are only available for a trip with no route."
                                )

                        manual_values = None
                        if stored["source"] == "manual":
                            try:
                                manual_values = parse_manual_trip_input(
                                    date, start_time, end_time, distance,
                                    request.state.config.display_tz,
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
                                # Only assign the label columns that were
                                # actually submitted (present in
                                # `normalized_labels`), so an omitted field
                                # is excluded from the SET clause entirely
                                # and its stored value is left alone rather
                                # than overwritten with NULL.
                                label_clauses: list[str] = []
                                label_params: list[str | None] = []
                                for field in ("start_label", "end_label"):
                                    if field in normalized_labels:
                                        label_clauses.append(f"{field} = %s")
                                        label_params.append(normalized_labels[field])
                                await conn.execute(
                                    "UPDATE trips SET started_at = %s, ended_at = %s, distance_m = %s, "
                                    "category = %s, exclusion = %s, purpose = %s, notes = %s, vehicle_id = %s"
                                    + "".join(f", {clause}" for clause in label_clauses)
                                    + ", tag_source = %s, updated_at = now() WHERE id = %s AND account_id = %s",
                                    (*manual_values, category, exclusion or None,
                                     normalized_purpose, normalized_notes,
                                     parsed_vehicle_id,
                                     *label_params,
                                     tag_source, trip_id, account_id(conn)),
                                )
                            else:
                                await conn.execute(
                                    "UPDATE trips SET category = %s, exclusion = %s, purpose = %s, notes = %s, "
                                    "vehicle_id = %s, tag_source = %s, updated_at = now() WHERE id = %s AND account_id = %s",
                                    (category, exclusion or None, normalized_purpose, normalized_notes,
                                     parsed_vehicle_id,
                                     tag_source, trip_id, account_id(conn)),
                                )
                        except errors.ForeignKeyViolation:
                            raise ManualTripValidationError(
                                {"vehicle_id": "Choose a vehicle that still exists."}
                            )
            except ManualTripValidationError as exc:
                ctx = await _fetch_trip_card_context(pool, trip_id)
                ctx.update({"values": values, "errors": exc.errors})
                if dashboard_week:
                    ctx["dashboard_week"] = dashboard_week
                return request.app.state.templates.TemplateResponse(
                    request, "_trip_edit_card.html", ctx
                )

            ctx = await _fetch_trip_card_context(pool, trip_id)
            if dashboard_week:
                ctx["dashboard_week"] = dashboard_week
                response = request.app.state.templates.TemplateResponse(
                    request, "_dashboard_trip_row.html", ctx
                )
                response.headers["HX-Refresh"] = "true"
                return response
            return _mark_archive_write(
                request.app.state.templates.TemplateResponse(
                    request, "_trip_archive_row.html", ctx
                )
            )

        @router.post("/trips/{trip_id}/exclusion", dependencies=[Depends(require_csrf)])
        async def set_trip_exclusion(
            request: Request,
            trip_id: int,
            exclusion: str = Form(""),
            user: dict = Depends(require_user),
        ):
            if exclusion and exclusion not in EXCLUSIONS:
                raise HTTPException(status_code=400, detail="Unknown exclusion")
            pool = request.state.account_pool
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "UPDATE trips SET exclusion = %s, updated_at = now() WHERE id = %s AND account_id = %s",
                    (exclusion or None, trip_id, account_id(conn)),
                )
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404, detail="No such trip")
            ctx = await _fetch_trip_card_context(pool, trip_id)
            return _mark_archive_write(
                request.app.state.templates.TemplateResponse(
                    request, "_trip_archive_row.html", ctx
                )
            )

        @router.post("/trips/{trip_id}/expenses", dependencies=[Depends(require_csrf)])
        async def add_trip_expense(
            request: Request,
            trip_id: int,
            category: str = Form(...),
            amount: str = Form(...),
            treatment: str = Form(""),
            notes: str = Form(""),
            # These fields are displayed as prefilled values on Trip Detail.
            # The stored trip remains authoritative for the vehicle and date.
            vehicle_id: str = Form(""),
            incurred_on: str = Form(""),
            user: dict = Depends(require_user),
        ):
            tz = request.state.config.display_tz
            async with request.state.account_pool.connection() as conn:
                cur = conn.cursor(row_factory=dict_row)
                await cur.execute(
                    "SELECT vehicle_id, started_at FROM trips WHERE id = %s AND account_id = %s FOR UPDATE",
                    (trip_id, account_id(conn)),
                )
                trip = await cur.fetchone()
                if trip is None:
                    raise HTTPException(status_code=404, detail="No such trip")
                if trip["vehicle_id"] is None:
                    raise HTTPException(
                        status_code=400, detail="Cannot add an expense to a trip without a vehicle"
                    )
                local_date = trip["started_at"].astimezone(tz).date()
                _, category, parsed_amount, treatment = _parse_expense_input(
                    local_date.isoformat(), category, amount, treatment,
                )
                notes_value = notes if isinstance(notes, str) else ""
                await conn.execute(
                    "INSERT INTO expenses "
                    "(account_id, vehicle_id, incurred_on, category, amount, treatment, notes, trip_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        account_id(conn), trip["vehicle_id"], local_date, category, parsed_amount, treatment,
                        notes_value.strip() or None, trip_id,
                    ),
                )
            return Response(
                status_code=204,
                headers={"HX-Redirect": f"/trips/{trip_id}"},
            )

        @router.get("/trips/{trip_id}")
        async def trip_detail(request: Request, trip_id: int, user: dict = Depends(require_user)):
            pool = request.state.account_pool
            trip = await _fetch_trip(pool, trip_id)
            path_geojson = None
            path_snapped_geojson = None
            stay_centroids = []
            has_next_trip = False
            has_prev_trip = False
            expenses = []
            async with request.state.account_pool.connection() as conn:
                vehicles = await list_vehicles(conn)
                recent_purposes = await _fetch_recent_purposes(conn)
                expenses = await _fetch_trip_expenses(
                    conn, trip_id, request.state.config.display_tz
                )
                # A routed manual trip has a real `path` too (see add_manual_trip),
                # so it needs the same geometry fetch a detected trip gets; the
                # stay-centroid and adjacent-trip queries below stay detected-only,
                # since neither concept exists for a manual trip.
                if trip["source"] == "detected" or trip["has_route_geometry"]:
                    cur = await conn.execute(
                        "SELECT ST_AsGeoJSON(path), ST_AsGeoJSON(path_snapped) "
                        "FROM trips WHERE id = %s AND account_id = %s", (trip_id, account_id(conn))
                    )
                    row = await cur.fetchone()
                    path_geojson = row[0] if row else None
                    path_snapped_geojson = row[1] if row else None
                if trip["source"] == "detected":
                    cur = await conn.execute(
                        "SELECT ST_AsGeoJSON(centroid::geometry) FROM stays "
                        "WHERE account_id = %s AND tracking_device_id = %s AND (ended_at = %s OR started_at = %s)",
                        (account_id(conn), trip["tracking_device_id"], trip["started_at"], trip["ended_at"]),
                    )
                    stay_centroids = [json.loads(r[0]) for r in await cur.fetchall()]
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

            return await render_page(
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
                    "min_trip_distance_m": request.state.config.detector_params.min_trip_distance_m,
                    "categories": CATEGORIES,
                    "expenses": expenses,
                    "expense_categories": EXPENSE_CATEGORIES,
                    "expense_category_labels": CATEGORY_LABELS,
                    "expense_treatments": EXPENSE_TREATMENTS,
                    "expense_treatment_labels": TREATMENT_LABELS,
                    "user": user,
                    "csrf": request.session.get("csrf", ""),
                },
            )

        @router.post("/trips/{trip_id}/labels", dependencies=[Depends(require_csrf)])
        async def save_trip_labels(
            request: Request,
            trip_id: int,
            start_label: str | None = Form(None),
            end_label: str | None = Form(None),
            user: dict = Depends(require_user),
        ):
            start_label, end_label = await _submitted_label_fields(
                request, start_label, end_label
            )
            values = {
                "start_label": start_label or "",
                "end_label": end_label or "",
            }
            field_errors: dict[str, str] = {}
            normalized_labels: dict[str, str | None] = {}
            pool = request.state.account_pool
            try:
                async with pool.connection() as conn:
                    async with conn.transaction():
                        cur = conn.cursor(row_factory=dict_row)
                        await cur.execute(
                            "SELECT source::text AS source, start_place_id, end_place_id, "
                            "start_geom IS NOT NULL AS has_start_geom, "
                            "end_geom IS NOT NULL AS has_end_geom "
                            "FROM trips WHERE id = %s AND account_id = %s FOR UPDATE",
                            (trip_id, account_id(conn)),
                        )
                        stored = await cur.fetchone()
                        if stored is None:
                            raise HTTPException(status_code=404, detail="No such trip")

                        label_eligible = {
                            "start_label": (
                                stored["source"] == "manual"
                                and stored["start_place_id"] is None
                                and not stored["has_start_geom"]
                            ),
                            "end_label": (
                                stored["source"] == "manual"
                                and stored["end_place_id"] is None
                                and not stored["has_end_geom"]
                            ),
                        }
                        for field, raw_value in (
                            ("start_label", start_label), ("end_label", end_label),
                        ):
                            if raw_value is None:
                                continue
                            if raw_value.strip() and not label_eligible[field]:
                                raise HTTPException(
                                    status_code=400,
                                    detail="Location names are only available for a trip with no route.",
                                )
                            try:
                                normalized_labels[field] = normalize_trip_label(
                                    raw_value, field
                                )
                            except ManualTripValidationError as exc:
                                field_errors.update(exc.errors)
                                continue
                        if field_errors:
                            raise ManualTripValidationError(field_errors)

                        label_clauses: list[str] = []
                        label_params: list[str | None] = []
                        for field in ("start_label", "end_label"):
                            if field in normalized_labels:
                                label_clauses.append(f"{field} = %s")
                                label_params.append(normalized_labels[field])
                        if label_clauses:
                            await conn.execute(
                                "UPDATE trips SET "
                                + ", ".join(label_clauses)
                                + ", updated_at = now() WHERE id = %s AND account_id = %s",
                                (*label_params, trip_id, account_id(conn)),
                            )
            except ManualTripValidationError as exc:
                trip = await _fetch_trip(pool, trip_id)
                return request.app.state.templates.TemplateResponse(
                    request,
                    "_trip_label_form.html",
                    {"trip": trip, "values": values, "errors": exc.errors},
                )

            return Response(
                status_code=204,
                headers={"HX-Redirect": f"/trips/{trip_id}"},
            )

        @router.post("/trips/{trip_id}/tag", dependencies=[Depends(require_csrf)])
        async def tag_trip(
            request: Request,
            trip_id: int,
            category: str = Form(...),
            user: dict = Depends(require_user),
            dashboard_week: Annotated[str, Form()] = "",
        ):
            if category not in CATEGORIES:
                raise HTTPException(status_code=400, detail="Unknown category")
            pool = request.state.account_pool
            async with request.state.account_pool.connection() as conn:
                await _apply_human_tag(conn, trip_id, category)
            if dashboard_week:
                # Importing here avoids coupling the trips module's import
                # path to stats while the router is assembled.
                from app.dashboard import parse_week_anchor
                from app.ui.stats import _build_week_dashboard_context

                tz = request.state.config.display_tz
                now = datetime.now(tz)
                anchor = parse_week_anchor(dashboard_week, tz, now)
                dashboard_context = await _build_week_dashboard_context(request, anchor, now)
                ctx = await _fetch_trip_card_context(
                    pool, trip_id,
                    vehicles=dashboard_context["vehicles"],
                    recent_purposes=dashboard_context["recent_purposes"],
                )
                return request.app.state.templates.TemplateResponse(
                    request,
                    "_dashboard_tag_response.html",
                    {**ctx, **dashboard_context, "dashboard_oob": True},
                )

            ctx = await _fetch_trip_card_context(pool, trip_id)

            # HX-Current-URL is the browser's actual address bar URL, sent by
            # htmx on every request; it is what tells this handler which
            # archive filters (if any) are on screen, since the row's own
            # <form> carries none of them (see _trip_category_pair.html).
            # An absent header (a non-htmx caller) falls through to "no
            # usable page context" via _parse_archive_current_url below.
            current_url = request.headers.get("HX-Current-URL", "")
            page_filters = _parse_archive_current_url(current_url)
            if page_filters and (page_filters["category"] or page_filters["exclusion"]):
                # A reclassified trip may no longer belong to a category- or
                # exclusion-filtered set. The archive coordinator will fetch
                # the canonical filtered list after this committed write.
                return _mark_archive_write(Response(status_code=204))

            if page_filters is None:
                # No usable page context (e.g. a non-htmx caller): fall back
                # to the plain row swap rather than guess at figures this
                # handler cannot otherwise recompute correctly.
                return _mark_archive_write(
                    request.app.state.templates.TemplateResponse(
                        request, "_trip_archive_row.html", ctx
                    )
                )

            tz = request.state.config.display_tz
            async with request.state.account_pool.connection() as conn:
                rates = await load_rates(conn)
                local_started_at = ctx["trip"]["started_at"].astimezone(tz)
                from_dt, to_dt = parse_date_range(
                    page_filters["from_"], page_filters["to"], tz
                )
                month = await _fetch_month_summary(
                    conn, tz, local_started_at.year, local_started_at.month, rates,
                    "", from_dt, to_dt, _parse_vehicle_id(page_filters["vehicle"]),
                    page_filters["q"], "",
                )
                ytd_year, ytd_deduction = await _fetch_ytd_deduction(conn, tz, rates)

            return _mark_archive_write(
                request.app.state.templates.TemplateResponse(
                    request,
                    "_trip_tag_response.html",
                    {
                        **ctx,
                        "month": month, "month_oob": True,
                        "ytd_year": ytd_year, "ytd_deduction": ytd_deduction, "ytd_oob": True,
                    },
                )
            )

        @router.post("/trips/{trip_id}/notes", dependencies=[Depends(require_csrf)])
        async def note_trip(
            request: Request,
            trip_id: int,
            notes: str = Form(""),
            user: dict = Depends(require_user),
        ):
            pool = request.state.account_pool
            async with request.state.account_pool.connection() as conn:
                await conn.execute(
                    "UPDATE trips SET notes = %s, updated_at = now() WHERE id = %s AND account_id = %s",
                    (notes.strip() or None, trip_id, account_id(conn)),
                )
            ctx = await _fetch_trip_card_context(pool, trip_id)
            return _mark_archive_write(
                request.app.state.templates.TemplateResponse(
                    request, "_trip_archive_row.html", ctx
                )
            )

        @router.post("/trips/{trip_id}/purpose", dependencies=[Depends(require_csrf)])
        async def purpose_trip(
            request: Request,
            trip_id: int,
            purpose: str = Form(""),
            user: dict = Depends(require_user),
        ):
            pool = request.state.account_pool
            async with request.state.account_pool.connection() as conn:
                cur = await conn.execute(
                    "UPDATE trips SET purpose = %s, tag_source = 'human', updated_at = now() "
                    "WHERE id = %s AND account_id = %s",
                    (purpose.strip() or None, trip_id, account_id(conn)),
                )
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404, detail="No such trip")
            ctx = await _fetch_trip_card_context(pool, trip_id)
            return _mark_archive_write(
                request.app.state.templates.TemplateResponse(
                    request, "_trip_archive_row.html", ctx
                )
            )

        @router.post("/trips/{trip_id}/vehicle", dependencies=[Depends(require_csrf)])
        async def set_trip_vehicle(
            request: Request,
            trip_id: int,
            vehicle_id: str = Form(""),
            user: dict = Depends(require_user),
        ):
            parsed_vehicle_id = _parse_vehicle_form(vehicle_id)
            pool = request.state.account_pool
            async with request.state.account_pool.connection() as conn:
                try:
                    await conn.execute(
                        "UPDATE trips SET vehicle_id = %s, updated_at = now() WHERE id = %s AND account_id = %s",
                        (parsed_vehicle_id, trip_id, account_id(conn)),
                    )
                except errors.ForeignKeyViolation:
                    raise HTTPException(status_code=400, detail="No such vehicle")
            ctx = await _fetch_trip_card_context(pool, trip_id)
            return _mark_archive_write(
                request.app.state.templates.TemplateResponse(
                    request, "_trip_archive_row.html", ctx
                )
            )


def register_delete(router: APIRouter) -> None:
        @router.post("/trips/{trip_id}/delete", dependencies=[Depends(require_csrf)])
        async def delete_trip(
            request: Request,
            trip_id: int,
            user: dict = Depends(require_user),
            fragment: Annotated[bool, Form()] = False,
            dashboard_week: Annotated[str, Form()] = "",
        ):
            async with request.state.account_pool.connection() as conn:
                await _delete_trip_in(conn, trip_id)
            if dashboard_week:
                return Response(status_code=200, headers={"HX-Refresh": "true"})
            if fragment:
                return _mark_archive_write(Response(status_code=200))
            return Response(status_code=204, headers={"HX-Redirect": "/trips"})


def register_batch_and_points(router: APIRouter) -> None:
        @router.post("/trips/batch_update", dependencies=[Depends(require_csrf)])
        async def batch_update_trips(
            request: Request,
            trip_ids: list[int] = Form(...),
            category: str = Form("keep"),
            vehicle_id: str = Form("keep"),
            purpose: str = Form(""),
            set_purpose: bool = Form(False),
            user: dict = Depends(require_user),
            exclusion: str = Form("keep"),
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
            exclusion = exclusion if isinstance(exclusion, str) else "keep"
            if len(trip_ids) < 1:
                raise HTTPException(status_code=400, detail="Select at least one trip")
            if category != "keep" and category not in CATEGORIES:
                raise HTTPException(status_code=400, detail="Unknown category")
            if exclusion != "keep" and exclusion not in (*EXCLUSIONS, ""):
                raise HTTPException(status_code=400, detail="Unknown exclusion")

            keep_vehicle = vehicle_id == "keep"
            parsed_vehicle_id = None if keep_vehicle else _parse_vehicle_form(vehicle_id)

            if category == "keep" and exclusion == "keep" and keep_vehicle and not set_purpose:
                raise HTTPException(status_code=400, detail="Nothing to apply")

            set_clauses = []
            params: list = []
            if category != "keep":
                set_clauses.append("category = %s")
                params.append(category)
            if exclusion != "keep":
                set_clauses.append("exclusion = %s")
                params.append(exclusion or None)
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

            async with request.state.account_pool.connection() as conn:
                try:
                    cur = await conn.execute(
                        f"UPDATE trips SET {', '.join(set_clauses)} WHERE id = ANY(%s) AND account_id = %s",
                        [*params, account_id(conn)],
                    )
                except errors.ForeignKeyViolation:
                    raise HTTPException(status_code=400, detail="No such vehicle")
                if cur.rowcount != len(trip_ids):
                    raise HTTPException(
                        status_code=400,
                        detail="One or more selected trips no longer exist",
                    )
            return JSONResponse({"updated": cur.rowcount})

        @router.post("/trips/batch_delete", dependencies=[Depends(require_csrf)])
        async def batch_delete_trips(
            request: Request,
            trip_ids: list[int] = Form(...),
            user: dict = Depends(require_user),
        ):
            """Delete exactly the explicitly selected trips atomically."""
            trip_ids = sorted(set(trip_ids))
            if not trip_ids:
                raise HTTPException(status_code=400, detail="Select at least one trip")

            async with request.state.account_pool.connection() as conn:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(%s)", (DETECTOR_ADVISORY_LOCK_KEY,)
                )
                cur = await conn.execute(
                    "SELECT id FROM trips WHERE id = ANY(%s) AND account_id = %s FOR UPDATE", (trip_ids, account_id(conn))
                )
                existing = {row[0] for row in await cur.fetchall()}
                if len(existing) != len(trip_ids):
                    raise HTTPException(
                        status_code=400,
                        detail="One or more selected trips no longer exist",
                    )
                for trip_id in trip_ids:
                    await _delete_trip_in(conn, trip_id)
            return JSONResponse({"deleted": len(trip_ids)})

        @router.get("/trips/{trip_id}/points")
        async def trip_points(request: Request, trip_id: int, user: dict = Depends(require_user)):
            trip = await _fetch_trip(request.state.account_pool, trip_id)
            if trip["source"] != "detected":
                raise HTTPException(status_code=400, detail="Only detected trips have points")
            async with request.state.account_pool.connection() as conn:
                rows = await load_trip_points(conn, trip_id)
            return JSONResponse([
                {"id": r[0], "t": r[1].isoformat(), "lat": r[2], "lon": r[3]} for r in rows
            ])
