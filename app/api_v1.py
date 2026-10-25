"""First-party JSON API, version 1.

Authentication is `Authorization: Bearer` only. API paths never see the
browser session cookie (see WebSessionMiddleware), so a browser cannot be
used to make a cookie-authenticated API request, and no CORS headers are
sent. Every failed bearer check returns the same 401 and counts toward the
per-client failure limiter.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from psycopg import errors
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse

from app.account_context import control_connection
from app.accounts import get_account
from app.api_errors import ApiError, ApiErrorEnvelope, FIELD_REQUIRED, FIELD_UNSUPPORTED, is_api_path
from app.api_grants import ACCESS_TOKEN_SECONDS, authenticate_access_token, format_instant, refresh_grant
from app.auth import _bind_account
from app.capacity_routes import AdmissionRoute, capacity_policy
from app.detector.runner import DETECTOR_VERSION
from app.ingest import FailedAuthLimiter, client_ip

log = logging.getLogger(__name__)

API_VERSIONS = ("v1",)
PRODUCT_NAME = "odograph"
AUTHORIZE_PATH = "/mobile/authorize"
TOKEN_PATH = "/api/v1/auth/token"
BEARER_HEADERS = {"WWW-Authenticate": "Bearer"}
TOKEN_RESPONSE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class WebSessionMiddleware(SessionMiddleware):
    """The browser session, kept away from API paths.

    An API request gets an empty, discarded session: the cookie is never
    decoded, and nothing an API dependency writes to the session is sent
    back as a cookie.
    """

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and is_api_path(scope.get("path", "")):
            scope["session"] = {}
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


@dataclass(frozen=True)
class ApiUser:
    user: dict
    account: dict
    grant_id: str


def _limiter(request: Request) -> FailedAuthLimiter:
    return request.app.state.api_auth_limiter


def _bearer_token(request: Request) -> str | None:
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        return None
    scheme, separator, credentials = values[0].partition(" ")
    if not separator or scheme.lower() != "bearer" or not credentials:
        return None
    return credentials


async def require_api_user(request: Request) -> ApiUser:
    limiter = _limiter(request)
    ip = client_ip(request)
    if limiter.blocked(ip):
        raise ApiError("rate_limited")
    token = _bearer_token(request)
    principal = account = None
    if token is not None:
        async with control_connection(request.app.state.control_pool) as conn:
            principal = await authenticate_access_token(conn, token)
            if principal is not None:
                account = await get_account(conn, principal.account_id)
    if (principal is None or account is None or not account["is_enabled"]
            or account["auth_version"] != principal.auth_version):
        limiter.record_failure(ip)
        raise ApiError("invalid_token", headers=BEARER_HEADERS)
    try:
        user = await _bind_account(request, account)
    except errors.InsufficientPrivilege:
        # Disabled or signed out between the grant check and admission.
        limiter.record_failure(ip)
        raise ApiError("invalid_token", headers=BEARER_HEADERS) from None
    return ApiUser(user, account, principal.grant_id)


class TokenRequest(BaseModel):
    grant_type: str
    refresh_token: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    access_expires_at: str
    expires_in: int


class CapabilitiesAuthorization(BaseModel):
    authorize_path: str
    token_path: str


class CapabilitiesResponse(BaseModel):
    product: str
    api_versions: list[str]
    authorization: CapabilitiesAuthorization
    mobile_signin_available: bool


class MeResponse(BaseModel):
    account_id: int
    display_name: str | None
    display_time_zone: str
    server_version: str
    detector_version: int
    grant_id: str


def _errors(*statuses: int) -> dict:
    return {status: {"model": ApiErrorEnvelope} for status in statuses}


def make_router() -> APIRouter:
    router = APIRouter(route_class=AdmissionRoute)

    @router.get("/api/v1/capabilities", response_model=CapabilitiesResponse, responses=_errors(503))
    async def capabilities():
        return CapabilitiesResponse(
            product=PRODUCT_NAME,
            api_versions=list(API_VERSIONS),
            authorization=CapabilitiesAuthorization(authorize_path=AUTHORIZE_PATH, token_path=TOKEN_PATH),
            mobile_signin_available=False,
        )

    @router.post("/api/v1/auth/token", response_model=TokenResponse,
                 responses=_errors(400, 422, 429, 503))
    @capacity_policy("auth_interactive")
    async def token(request: Request, body: TokenRequest):
        limiter = _limiter(request)
        ip = client_ip(request)
        if limiter.blocked(ip):
            raise ApiError("rate_limited")
        if body.grant_type != "refresh_token":
            raise ApiError("validation_failed", fields={"grant_type": FIELD_UNSUPPORTED})
        if body.refresh_token is None:
            raise ApiError("validation_failed", fields={"refresh_token": FIELD_REQUIRED})
        async with control_connection(request.app.state.control_pool) as conn:
            outcome = await refresh_grant(conn, body.refresh_token)
        if outcome.tokens is None:
            limiter.record_failure(ip)
            raise ApiError("invalid_grant")
        issued = outcome.tokens
        response = TokenResponse(
            access_token=issued.access_token, refresh_token=issued.refresh_token,
            token_type="Bearer", access_expires_at=format_instant(issued.access_expires_at),
            expires_in=ACCESS_TOKEN_SECONDS,
        )
        return JSONResponse(response.model_dump(), headers=TOKEN_RESPONSE_HEADERS)

    @router.get("/api/v1/me", response_model=MeResponse, responses=_errors(401, 429, 503))
    async def me(request: Request, api_user: ApiUser = Depends(require_api_user)):
        return MeResponse(
            account_id=api_user.account["id"],
            display_name=api_user.account["display_name"],
            display_time_zone=request.state.account_settings.display_tz.key,
            server_version=request.app.state.config.app_version,
            detector_version=DETECTOR_VERSION,
            grant_id=api_user.grant_id,
        )

    return router
