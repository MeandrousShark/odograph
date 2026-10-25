"""API v1 trip reads: the archive, trip detail, the review queue, vehicles
and recent purposes.

Every read goes through the services the web pages use. Unlike the web's
forgiving query strings, filters accept only documented values: anything
else is `validation_failed`, never silently "no filter".
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.api_cursor import decode_cursor, encode_cursor, filter_digest
from app.api_errors import FIELD_INVALID, ApiError, ApiErrorEnvelope
from app.api_grants import format_instant
from app.api_v1 import ApiUser, require_api_user
from app.capacity_routes import AdmissionRoute, capacity_policy
from app.expenses import expense_conflicts
from app.places_desc import describe_endpoint
from app.review_queue import next_review_trip, review_filter
from app.trip_archive import fetch_trip_page
from app.trip_queries import VEHICLE_FILTER_UNASSIGNED, TripFilter
from app.trip_reads import (
    TripDetail, detail_for_trip, fetch_recent_purposes, fetch_trip_detail, route_parts,
)
from app.vehicles import list_vehicles

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_VEHICLE_ID = re.compile(r"[1-9][0-9]{0,18}")
_MAX_ID = 2**63 - 1


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


def _errors(*statuses: int) -> dict:
    return {status: {"model": ApiErrorEnvelope} for status in statuses}


def _invalid(field: str) -> ApiError:
    return ApiError("validation_failed", fields={field: FIELD_INVALID})


def _day(field: str, value: str | None) -> date | None:
    if value is None:
        return None
    if not _DAY.fullmatch(value):
        raise _invalid(field)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _invalid(field) from None


def _search(value: str | None) -> str:
    """The trimmed search term. Control characters, NUL among them, are
    refused: no stored text holds them, and the database rejects NUL."""
    if value is not None and any(unicodedata.category(char) == "Cc" for char in value):
        raise _invalid("q")
    return (value or "").strip()


def _vehicle(value: str | None) -> int | str | None:
    if value is None or value == VEHICLE_FILTER_UNASSIGNED:
        return value
    if not _VEHICLE_ID.fullmatch(value) or int(value) > _MAX_ID:
        raise _invalid("vehicle")
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
        start, end = _day("from", from_), _day("to", to)
        vehicle_id = _vehicle(vehicle)
        term = _search(q)
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
        start, end = _day("from", from_), _day("to", to)
        vehicle_id = _vehicle(vehicle)
        term = _search(q)
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
