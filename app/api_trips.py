"""API v1 trips: the archive, trip detail, the review queue, vehicles and
recent purposes, and trip edits, manual creates and deletes.

Every read and write goes through the services the web pages use. Unlike the
web's forgiving query strings and forms, requests accept only documented
values: anything else is `validation_failed`, never silently "no filter".
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse, Response

from app.api_cursor import decode_cursor, encode_cursor, filter_digest
from app.api_common import (
    IDEMPOTENCY_KEY_HEADER, IDEMPOTENT_REPLAYED_HEADER, MAX_YEAR, MIN_YEAR, storable_text, unstorable_fields,
    year_in_range,
)
from app.api_common import error_responses as _errors, idempotency_key as _idempotency_key
from app.api_errors import FIELD_INVALID, ApiError
from app.api_grants import format_instant
from app.api_v1 import ApiUser, require_api_user
from app.capacity_routes import AdmissionRoute, capacity_policy, mark_committed_write
from app.expenses import expense_conflicts
from app.places_desc import describe_endpoint
from app.review_queue import next_review_trip, review_filter
from app.trip_archive import fetch_trip_page
from app.trip_queries import VEHICLE_FILTER_UNASSIGNED, TripFilter
from app.trip_reads import (
    TripDetail, detail_for_trip, fetch_recent_purposes, fetch_trip_detail, route_parts,
)
from app.trip_writes import (
    TIME_INVALID, VEHICLE_MISSING, IdempotencyKeyReused, ManualTripValidationError, create_manual_trip,
    delete_trip, manual_trip_digest, update_trip, validate_manual_trip,
)
from app.vehicles import list_vehicles

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_VEHICLE_ID = re.compile(r"[1-9][0-9]{0,18}")
_MAX_ID = 2**63 - 1
_INSTANT = re.compile(
    r"(\d{4}-\d{2}-\d{2})[Tt](\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?([Zz]|[+-]\d{2}:\d{2})")
_INSTANT_FIELDS = ("started_at", "ended_at")
TIME_OUT_OF_RANGE = f"Enter a time from {MIN_YEAR} through {MAX_YEAR}."


class MultiLineString(BaseModel):
    type: Literal["MultiLineString"]
    coordinates: list[list[list[float]]]


class TripEndpoint(BaseModel):
    name: str | None
    label: str | None
    address: str | None
    lat: float | None
    lon: float | None
    description: str


class TripSummary(BaseModel):
    id: int
    source: str
    imported: bool
    started_at: str
    ended_at: str
    local_date: str
    distance_m: float
    distance_raw_m: float
    distance_snapped_m: float | None
    snap_status: str | None
    category: str
    exclusion: str | None
    purpose: str | None
    notes: str | None
    vehicle_id: int | None
    vehicle_name: str | None
    start: TripEndpoint
    end: TripEndpoint
    has_gap: bool
    point_count: int
    has_route: bool
    expense_count: int


class TripStay(BaseModel):
    position: str
    lat: float
    lon: float


class TripExpense(BaseModel):
    id: int
    vehicle_id: int
    vehicle_name: str
    incurred_on: str
    category: str
    amount: str
    treatment: str
    notes: str | None
    conflicts: list[str]


class TripDetailResponse(TripSummary):
    route_raw: MultiLineString | None
    route_snapped: MultiLineString | None
    stays: list[TripStay]
    has_previous_trip: bool
    has_next_trip: bool
    expenses: list[TripExpense]


class TripPage(BaseModel):
    items: list[TripSummary]
    next_cursor: str | None


class ReviewResponse(BaseModel):
    state: str
    trip: TripDetailResponse | None
    remaining: int
    next_cursor: str | None


Category = Literal["business", "personal", "unclassified"]
Exclusion = Literal["not_my_vehicle", "not_deductible"]
# RFC 3339 with an offset, parsed by the handler so a number or a local time
# is refused rather than coerced.
Instant = Annotated[str, Field(json_schema_extra={"format": "date-time"})]


class _WriteBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TripPatch(_WriteBody):
    """Only the fields present change. `null` clears a nullable field;
    `category`, the times and `distance_m` cannot be null. Times, distance
    and labels exist only on manual trips, and a label only on an endpoint
    with no place or route."""

    category: Category = None
    purpose: str | None = None
    notes: str | None = None
    vehicle_id: int | None = None
    exclusion: Exclusion | None = None
    started_at: Instant = None
    ended_at: Instant = None
    distance_m: float = None
    start_label: str | None = None
    end_label: str | None = None


class TripCreate(_WriteBody):
    """A plain manual trip, with no route."""

    started_at: Instant
    ended_at: Instant
    distance_m: float
    category: Category = "unclassified"
    purpose: str | None = None
    notes: str | None = None
    vehicle_id: int | None = None
    exclusion: Exclusion | None = None
    start_label: str | None = None
    end_label: str | None = None


class Vehicle(BaseModel):
    id: int
    name: str
    make: str | None
    model: str | None
    plate: str | None
    is_default: bool


class VehicleList(BaseModel):
    items: list[Vehicle]
    next_cursor: str | None


class PurposeList(BaseModel):
    items: list[str]
    next_cursor: str | None


def invalid_field(field: str) -> ApiError:
    return ApiError("validation_failed", fields={field: FIELD_INVALID})


def parse_day(field: str, value: str | None) -> date | None:
    if value is None:
        return None
    if not _DAY.fullmatch(value):
        raise invalid_field(field)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise invalid_field(field) from None


def parse_search(value: str | None) -> str:
    """The trimmed search term. Control characters, NUL among them, and text
    the database cannot encode are refused: no stored text holds them."""
    if value is not None and (any(unicodedata.category(char) == "Cc" for char in value)
                              or not storable_text(value)):
        raise invalid_field("q")
    return (value or "").strip()


def parse_vehicle(value: str | None) -> int | str | None:
    if value is None or value == VEHICLE_FILTER_UNASSIGNED:
        return value
    if not _VEHICLE_ID.fullmatch(value) or int(value) > _MAX_ID:
        raise invalid_field("vehicle")
    return int(value)


def _range(start: date | None, end: date | None, tz: ZoneInfo) -> tuple[datetime | None, datetime | None]:
    """Display-zone calendar days, `to` inclusive, as the web archive reads them."""
    from_dt = datetime(start.year, start.month, start.day, tzinfo=tz) if start else None
    to_dt = None
    if end is not None and end < date.max:
        following = end + timedelta(days=1)
        to_dt = datetime(following.year, following.month, following.day, tzinfo=tz)
    return from_dt, to_dt


def _endpoint(row: dict, side: str) -> TripEndpoint:
    name, lat, lon = row[f"{side}_place_name"], row[f"{side}_lat"], row[f"{side}_lon"]
    address = row[f"{side}_address"]
    return TripEndpoint(
        name=name, label=row[f"{side}_label"], address=address, lat=lat, lon=lon,
        description=describe_endpoint(name, lat, lon, address),
    )


def _summary_fields(row: dict, tz: ZoneInfo) -> dict:
    return {
        "id": row["id"],
        "source": row["source"],
        "imported": row["imported"],
        "started_at": format_instant(row["started_at"]),
        "ended_at": format_instant(row["ended_at"]),
        "local_date": row["started_at"].astimezone(tz).date().isoformat(),
        "distance_m": float(row["display_distance_m"]),
        "distance_raw_m": float(row["distance_m"]),
        "distance_snapped_m": (None if row["distance_snapped_m"] is None
                               else float(row["distance_snapped_m"])),
        "snap_status": row["snap_status"],
        "category": row["category"],
        "exclusion": row["exclusion"],
        "purpose": row["purpose"],
        "notes": row["notes"],
        "vehicle_id": row["vehicle_id"],
        "vehicle_name": row["vehicle_name"],
        "start": _endpoint(row, "start"),
        "end": _endpoint(row, "end"),
        "has_gap": row["has_gap"],
        "point_count": row["point_count"],
        "has_route": row["has_route_geometry"],
        "expense_count": row["expense_count"],
    }


def _instant(value: str) -> datetime | None:
    match = _INSTANT.fullmatch(value)
    if match is None:
        return None
    day, clock, fraction, offset = match.groups()
    fraction = (fraction or "")[:6].ljust(6, "0")
    offset = "+00:00" if offset in ("Z", "z") else offset
    try:
        return datetime.fromisoformat(f"{day}T{clock}.{fraction}{offset}")
    except ValueError:
        return None


def _write_values(values: dict) -> dict:
    """Parse instants, and refuse text no column can hold and ids no stored
    vehicle can have."""
    problems = unstorable_fields(values)
    vehicle_id = values.get("vehicle_id")
    if vehicle_id is not None and not 1 <= vehicle_id <= _MAX_ID:
        problems["vehicle_id"] = VEHICLE_MISSING
    for name in _INSTANT_FIELDS:
        if isinstance(values.get(name), str) and name not in problems:
            parsed = _instant(values[name])
            if parsed is None:
                problems[name] = TIME_INVALID
            elif not year_in_range(parsed.year):
                problems[name] = TIME_OUT_OF_RANGE
            else:
                values[name] = parsed
    if problems:
        raise ApiError("validation_failed", fields=problems)
    return values


def _trip_id(trip_id: int) -> int:
    if not 1 <= trip_id <= _MAX_ID:
        raise ApiError("not_found")
    return trip_id


def _route(geojson: str | None) -> MultiLineString | None:
    parts = route_parts(geojson)
    return None if parts is None else MultiLineString(type="MultiLineString", coordinates=parts)


def _detail(detail: TripDetail, tz: ZoneInfo) -> TripDetailResponse:
    return TripDetailResponse(
        **_summary_fields(detail.trip, tz),
        route_raw=_route(detail.path_geojson),
        route_snapped=_route(detail.path_snapped_geojson),
        stays=[
            TripStay(position=stay.position, lat=stay.centroid["coordinates"][1],
                     lon=stay.centroid["coordinates"][0])
            for stay in sorted(detail.stays, key=lambda stay: stay.position != "start")
        ],
        has_previous_trip=detail.has_prev_trip,
        has_next_trip=detail.has_next_trip,
        expenses=[
            TripExpense(
                id=row["id"], vehicle_id=row["vehicle_id"], vehicle_name=row["vehicle_name"],
                incurred_on=row["incurred_on"].isoformat(), category=row["category"],
                amount=format(row["amount"], ".2f"), treatment=row["treatment"], notes=row["notes"],
                conflicts=list(expense_conflicts(row, tz)),
            )
            for row in detail.expenses
        ],
    )


def make_router() -> APIRouter:
    router = APIRouter(route_class=AdmissionRoute)

    @router.get("/api/v1/trips", response_model=TripPage, responses=_errors(400, 401, 422, 429, 503))
    async def list_trips(
        request: Request,
        api_user: ApiUser = Depends(require_api_user),
        category: Literal["business", "personal", "unclassified"] | None = Query(None),
        from_: str | None = Query(None, alias="from"),
        to: str | None = Query(None),
        vehicle: str | None = Query(None),
        q: str | None = Query(None),
        exclusion: Literal["not_my_vehicle", "not_deductible", "none"] | None = Query(None),
        limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        cursor: str | None = Query(None),
    ):
        tz = request.state.config.display_tz
        start, end = parse_day("from", from_), parse_day("to", to)
        vehicle_id = parse_vehicle(vehicle)
        term = parse_search(q)
        digest = filter_digest("trips", {
            "category": category or "", "from": start.isoformat() if start else "",
            "to": end.isoformat() if end else "", "vehicle": "" if vehicle_id is None else str(vehicle_id),
            "q": term, "exclusion": exclusion or "",
        })
        after = None if cursor is None else decode_cursor("trips", cursor, digest)
        from_dt, to_dt = _range(start, end, tz)
        filters = TripFilter(category or "", from_dt, to_dt, vehicle_id, term, exclusion or "")
        async with request.state.account_pool.connection() as conn:
            rows, more = await fetch_trip_page(conn, filters, after, limit)
        next_cursor = None
        if more:
            next_cursor = encode_cursor("trips", (rows[-1]["started_at"], rows[-1]["id"]), digest)
        return TripPage(items=[TripSummary(**_summary_fields(row, tz)) for row in rows],
                        next_cursor=next_cursor)

    @router.get("/api/v1/trips/{trip_id}", response_model=TripDetailResponse,
                responses=_errors(401, 404, 422, 429, 503))
    @capacity_policy("foreground")
    async def get_trip(request: Request, trip_id: int, api_user: ApiUser = Depends(require_api_user)):
        tz = request.state.config.display_tz
        if not 1 <= trip_id <= _MAX_ID:
            raise ApiError("not_found")
        async with request.state.account_pool.connection() as conn:
            detail = await fetch_trip_detail(conn, trip_id, tz)
        if detail is None:
            raise ApiError("not_found")
        return _detail(detail, tz)

    @router.patch("/api/v1/trips/{trip_id}", response_model=TripDetailResponse,
                  responses=_errors(401, 404, 409, 413, 422, 429, 503))
    @capacity_policy("form")
    async def patch_trip(request: Request, trip_id: int, body: TripPatch,
                         api_user: ApiUser = Depends(require_api_user)):
        tz = request.state.config.display_tz
        _trip_id(trip_id)
        changes = _write_values(body.model_dump(include=body.model_fields_set))
        async with request.state.account_pool.connection() as conn:
            try:
                written = await update_trip(conn, trip_id, changes)
            except ManualTripValidationError as exc:
                raise ApiError("validation_failed", fields=exc.errors) from None
            detail = await fetch_trip_detail(conn, trip_id, tz)
        if written:
            mark_committed_write(request)
        return _detail(detail, tz)

    @router.delete("/api/v1/trips/{trip_id}", status_code=204, response_class=Response,
                   responses=_errors(401, 404, 409, 413, 422, 429, 503))
    @capacity_policy("form")
    async def remove_trip(request: Request, trip_id: int, api_user: ApiUser = Depends(require_api_user)):
        _trip_id(trip_id)
        try:
            async with request.state.account_pool.connection() as conn:
                await delete_trip(conn, trip_id)
        except HTTPException as exc:
            if exc.status_code == 409:
                raise ApiError("trip_in_progress") from None
            raise
        mark_committed_write(request)
        return Response(status_code=204)

    @router.post("/api/v1/trips", status_code=201, response_model=TripDetailResponse,
                 responses={200: {"model": TripDetailResponse, "description": "Replayed"},
                            **_errors(401, 409, 413, 422, 429, 503)})
    @capacity_policy("foreground")
    async def create_trip(
        request: Request, body: TripCreate,
        idempotency_key: str = Header(alias=IDEMPOTENCY_KEY_HEADER),
        api_user: ApiUser = Depends(require_api_user),
    ):
        tz = request.state.config.display_tz
        key = _idempotency_key(idempotency_key)
        values = _write_values(body.model_dump())
        async with request.state.account_pool.connection() as conn:
            try:
                trip = await validate_manual_trip(conn, values)
                trip_id, created = await create_manual_trip(conn, trip, key, manual_trip_digest(trip))
            except ManualTripValidationError as exc:
                raise ApiError("validation_failed", fields=exc.errors) from None
            except IdempotencyKeyReused:
                raise ApiError("idempotency_key_reused") from None
            detail = await fetch_trip_detail(conn, trip_id, tz)
        content = _detail(detail, tz).model_dump(mode="json")
        if not created:
            return JSONResponse(content, status_code=200, headers={IDEMPOTENT_REPLAYED_HEADER: "true"})
        mark_committed_write(request)
        return JSONResponse(content, status_code=201, headers={"Location": f"/api/v1/trips/{trip_id}"})

    @router.get("/api/v1/review", response_model=ReviewResponse, responses=_errors(400, 401, 422, 429, 503))
    async def review_next(
        request: Request,
        api_user: ApiUser = Depends(require_api_user),
        from_: str | None = Query(None, alias="from"),
        to: str | None = Query(None),
        vehicle: str | None = Query(None),
        q: str | None = Query(None),
        cursor: str | None = Query(None),
    ):
        tz = request.state.config.display_tz
        start, end = parse_day("from", from_), parse_day("to", to)
        vehicle_id = parse_vehicle(vehicle)
        term = parse_search(q)
        digest = filter_digest("review", {
            "from": start.isoformat() if start else "", "to": end.isoformat() if end else "",
            "vehicle": "" if vehicle_id is None else str(vehicle_id), "q": term,
        })
        after = None if cursor is None else decode_cursor("review", cursor, digest)
        from_dt, to_dt = _range(start, end, tz)
        async with request.state.account_pool.connection() as conn:
            trip, remaining = await next_review_trip(conn, review_filter(from_dt, to_dt, vehicle_id, term), after)
            detail = None if trip is None else await detail_for_trip(conn, trip, tz)
        if detail is None:
            return ReviewResponse(state="empty" if after is None else "done", trip=None,
                                  remaining=0, next_cursor=None)
        return ReviewResponse(
            state="card", trip=_detail(detail, tz), remaining=remaining,
            next_cursor=encode_cursor("review", (trip["started_at"], trip["id"]), digest),
        )

    @router.get("/api/v1/vehicles", response_model=VehicleList, responses=_errors(401, 429, 503))
    async def vehicles(request: Request, api_user: ApiUser = Depends(require_api_user)):
        async with request.state.account_pool.connection() as conn:
            rows = await list_vehicles(conn)
        return VehicleList(items=[
            Vehicle(id=row["id"], name=row["name"], make=row["make"], model=row["model"],
                    plate=row["plate"], is_default=row["is_default"])
            for row in rows
        ], next_cursor=None)

    @router.get("/api/v1/purposes/recent", response_model=PurposeList, responses=_errors(401, 429, 503))
    async def recent_purposes(request: Request, api_user: ApiUser = Depends(require_api_user)):
        async with request.state.account_pool.connection() as conn:
            items = await fetch_recent_purposes(conn)
        return PurposeList(items=items, next_cursor=None)

    return router
