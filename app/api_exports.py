"""API v1 exports: the archive as CSV or a workbook, and the annual and range
report workbooks.

Files come from the same preparation pipelines as the web's downloads, so a
file is complete before its first byte is sent and carries `Content-Length`.
Every failure is an error envelope rather than a truncated file. The HTML
reports are web pages and are not offered here.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from starlette.responses import Response

from app.api_errors import ApiError, ApiErrorEnvelope
from app.api_trips import (
    MAX_INSTANT_YEAR, MIN_INSTANT_YEAR, Category, invalid_field, parse_day, parse_search, parse_vehicle,
)
from app.api_v1 import BEARER_HEADERS, ApiUser, require_api_report_user
from app.capacity_routes import AdmissionRoute, capacity_policy
from app.report_preparation import AccountBindingChanged

CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DATE_OUT_OF_RANGE = "Enter a date from 1900 through 9998."
RANGE_REVERSED = "End the range on or after its start."
RANGE_CROSSES_YEAR = "Keep the range within one calendar year."
_YEAR = re.compile(r"[0-9]{4}")


def _responses(*media_types: str) -> dict:
    binary = {"schema": {"type": "string", "format": "binary"}}
    responses = {200: {"description": "The complete file, sent as an attachment.",
                       "content": {media_type: binary for media_type in media_types}}}
    responses.update({status: {"model": ApiErrorEnvelope} for status in (401, 422, 429, 503)})
    return responses


def _day(field: str, value: str | None) -> date | None:
    day = parse_day(field, value)
    if day is not None and not MIN_INSTANT_YEAR <= day.year <= MAX_INSTANT_YEAR:
        raise ApiError("validation_failed", fields={field: DATE_OUT_OF_RANGE})
    return day


async def _prepared(preparation):
    try:
        return await preparation
    except AccountBindingChanged:
        # Disabled or signed out everywhere after admission, before the snapshot.
        raise ApiError("invalid_token", headers=BEARER_HEADERS) from None


def make_router() -> APIRouter:
    router = APIRouter(route_class=AdmissionRoute)

    @router.get("/api/v1/exports/trips", response_class=Response,
                responses=_responses(CSV_MEDIA_TYPE, XLSX_MEDIA_TYPE))
    @capacity_policy("foreground")
    async def export_trips(
        request: Request,
        format: Literal["csv", "xlsx"],
        api_user: ApiUser = Depends(require_api_report_user),
        category: Category | None = Query(None),
        from_: str | None = Query(None, alias="from"),
        to: str | None = Query(None),
        vehicle: str | None = Query(None),
        q: str | None = Query(None),
        exclusion: Literal["not_my_vehicle", "not_deductible", "none"] | None = Query(None),
    ):
        _day("from", from_)
        _day("to", to)
        parse_vehicle(vehicle)
        parse_search(q)
        from app.export_preparation import prepare_export
        # The validated values go through as the web's query strings, so the
        # file is the one the web prepares for the same filters.
        return await _prepared(prepare_export(
            request, api_user.user, format, category or "", from_ or "", to or "",
            vehicle or "", q or "", exclusion or ""))

    # Ahead of "/api/v1/reports/{year}", which would otherwise match "range".
    @router.get("/api/v1/reports/range", response_class=Response, responses=_responses(XLSX_MEDIA_TYPE))
    @capacity_policy("foreground")
    async def range_report(
        request: Request,
        format: Literal["xlsx"],
        from_: str = Query(alias="from"),
        to: str = Query(),
        api_user: ApiUser = Depends(require_api_report_user),
    ):
        start, end = _day("from", from_), _day("to", to)
        if start > end:
            raise ApiError("validation_failed", fields={"to": RANGE_REVERSED})
        if start.year != end.year:
            raise ApiError("validation_failed", fields={"to": RANGE_CROSSES_YEAR})
        from app.report_preparation import prepare_report
        return await _prepared(prepare_report(request, api_user.user, "range_xlsx", start=start, end=end))

    @router.get("/api/v1/reports/{year}", response_class=Response, responses=_responses(XLSX_MEDIA_TYPE))
    @capacity_policy("foreground")
    async def annual_report(
        request: Request,
        format: Literal["xlsx"],
        year: int = Path(ge=MIN_INSTANT_YEAR, le=MAX_INSTANT_YEAR),
        api_user: ApiUser = Depends(require_api_report_user),
    ):
        # The integer conversion accepts "+2026", "02026" and the like.
        if not _YEAR.fullmatch(request.path_params["year"]):
            raise invalid_field("year")
        from app.report_preparation import prepare_report
        return await _prepared(prepare_report(request, api_user.user, "annual_xlsx", year=year))

    return router
