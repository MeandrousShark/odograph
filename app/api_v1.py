"""First-party JSON API, version 1.

Authentication is `Authorization: Bearer` only. API paths never see the
browser session cookie (see WebSessionMiddleware), so a browser cannot be
used to make a cookie-authenticated API request, and no CORS headers are
sent. Every failed bearer check returns the same 401; checks that look like
guessing (a token no grant holds, or a malformed header) count toward the
per-client failure limiter. The limiter only refuses requests that fail:
a valid credential is always honored, even from a blocked client.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from psycopg import errors
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse, Response

from app.account_context import control_connection
from app.accounts import get_account, get_report_account
from app.api_errors import ApiError, ApiErrorEnvelope, FIELD_REQUIRED, FIELD_UNSUPPORTED, is_api_path
from app.api_grants import (
    ACCESS_TOKEN_SECONDS, GrantLimitReached, IssuedTokens, check_access_token, format_instant,
    refresh_grant, revoke_grant, token_digest,
)
from app.auth import _bind_account
from app.capacity_routes import AdmissionRoute, capacity_policy
from app.detector.runner import DETECTOR_VERSION
from app.ingest import FailedAuthLimiter, client_ip
from app.mobile_authorization import AUTHORIZE_PATH, exchange_code, mobile_signin_enabled

log = logging.getLogger(__name__)

API_VERSIONS = ("v1",)
PRODUCT_NAME = "odograph"
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
    # A report-profile account lacks email, display name and settings.
    report_profile: bool = False


def _limiter(request: Request) -> FailedAuthLimiter:
    return request.app.state.api_auth_limiter


def _rate_limited(limiter: FailedAuthLimiter) -> ApiError:
    return ApiError("rate_limited", headers={"Retry-After": str(math.ceil(limiter.window_s))})


def _refuse(limiter: FailedAuthLimiter, ip: str, error: ApiError, *, count: bool = True) -> ApiError:
    """The error for a failed credential check. A counted failure from a
    blocked IP gets 429 instead and is not recorded again; otherwise it is
    recorded. An uncounted failure (a real grant's outdated token) keeps its
    own error, so the client refreshes instead of waiting. Only failures are
    ever limited: callers reach this after the credential was checked, or
    when it is too malformed to look up."""
    if not count:
        return error
    if limiter.blocked(ip):
        return _rate_limited(limiter)
    limiter.record_failure(ip)
    return error


def _bearer_token(request: Request) -> str | None:
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        return None
    scheme, separator, credentials = values[0].partition(" ")
    if not separator or scheme.lower() != "bearer" or not credentials:
        return None
    return credentials


async def require_api_user(request: Request) -> ApiUser:
    return await _require_api_user(request)


async def require_api_report_user(request: Request) -> ApiUser:
    """The bearer check for prepared files. Like the web's report identity,
    it binds the account without reading its settings or other variable
    data before admission; the preparation snapshot reads those and
    re-checks `is_enabled` and `auth_version`."""
    return await _require_api_user(request, report_profile=True)


async def _require_api_user(request: Request, *, report_profile: bool = False) -> ApiUser:
    # Admission already ran this check for a route with a capacity lane.
    cached = getattr(request.state, "_capacity_authenticated_user", None)
    if isinstance(cached, ApiUser) and (report_profile or not cached.report_profile):
        return cached
    limiter = _limiter(request)
    ip = client_ip(request)
    token = _bearer_token(request)
    principal = account = None
    known = False
    if token_digest(token) is None:
        # Malformed or missing: nothing to look up, so a blocked IP is
        # refused without touching the database.
        raise _refuse(limiter, ip, ApiError("invalid_token", headers=BEARER_HEADERS))
    # The credential is checked before the limiter is consulted, so a valid
    # token is never refused because of earlier failures from the same IP.
    async with control_connection(request.app.state.control_pool) as conn:
        check = await check_access_token(conn, token)
        principal, known = check.principal, check.known
        if principal is not None:
            lookup = get_report_account if report_profile else get_account
            account = await lookup(conn, principal.account_id)
    # Only guesses count toward the limiter: an expired or otherwise outdated
    # token of a real grant is normal client behavior. The 401 is identical.
    if (principal is None or account is None or not account["is_enabled"]
            or account["auth_version"] != principal.auth_version):
        raise _refuse(limiter, ip, ApiError("invalid_token", headers=BEARER_HEADERS), count=not known)
    try:
        user = await _bind_account(request, account, **({"report_profile": True} if report_profile else {}))
    except errors.InsufficientPrivilege:
        # Disabled or signed out between the grant check and admission.
        raise _refuse(limiter, ip, ApiError("invalid_token", headers=BEARER_HEADERS), count=False) from None
    return ApiUser(user, account, principal.grant_id, report_profile)


class TokenRequest(BaseModel):
    grant_type: str
    refresh_token: str | None = Field(default=None, repr=False)
    code: str | None = Field(default=None, repr=False)
    code_verifier: str | None = Field(default=None, repr=False)


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


async def _refresh(request: Request, body: TokenRequest, limiter: FailedAuthLimiter, ip: str) -> IssuedTokens:
    if body.refresh_token is None:
        raise ApiError("validation_failed", fields={"refresh_token": FIELD_REQUIRED})
    if token_digest(body.refresh_token) is None:
        raise _refuse(limiter, ip, ApiError("invalid_grant"))
    async with control_connection(request.app.state.control_pool) as conn:
        outcome = await refresh_grant(conn, body.refresh_token)
    if outcome.tokens is None:
        raise _refuse(limiter, ip, ApiError("invalid_grant"))
    return outcome.tokens


async def _exchange(request: Request, body: TokenRequest, limiter: FailedAuthLimiter, ip: str) -> IssuedTokens:
    if not mobile_signin_enabled(request):
        raise ApiError("signin_disabled")
    missing = {name: FIELD_REQUIRED for name in ("code", "code_verifier") if getattr(body, name) is None}
    if missing:
        raise ApiError("validation_failed", fields=missing)
    issued = None
    if token_digest(body.code) is not None:
        try:
            async with control_connection(request.app.state.control_pool) as conn:
                issued = await exchange_code(conn, body.code, body.code_verifier)
        except GrantLimitReached:
            raise ApiError("grant_limit_reached") from None
    if issued is None:
        raise _refuse(limiter, ip, ApiError("invalid_grant"))
    return issued


def _errors(*statuses: int) -> dict:
    return {status: {"model": ApiErrorEnvelope} for status in statuses}


def make_router() -> APIRouter:
    router = APIRouter(route_class=AdmissionRoute)

    @router.get("/api/v1/capabilities", response_model=CapabilitiesResponse, responses=_errors(503))
    async def capabilities(request: Request):
        return CapabilitiesResponse(
            product=PRODUCT_NAME,
            api_versions=list(API_VERSIONS),
            authorization=CapabilitiesAuthorization(authorize_path=AUTHORIZE_PATH, token_path=TOKEN_PATH),
            mobile_signin_available=mobile_signin_enabled(request),
        )

    @router.post("/api/v1/auth/token", response_model=TokenResponse,
                 responses=_errors(400, 403, 409, 422, 429, 503))
    @capacity_policy("auth_interactive")
    async def token(request: Request, body: TokenRequest):
        limiter = _limiter(request)
        ip = client_ip(request)
        if body.grant_type == "refresh_token":
            issued = await _refresh(request, body, limiter, ip)
        elif body.grant_type == "authorization_code":
            issued = await _exchange(request, body, limiter, ip)
        else:
            raise ApiError("validation_failed", fields={"grant_type": FIELD_UNSUPPORTED})
        response = TokenResponse(
            access_token=issued.access_token, refresh_token=issued.refresh_token,
            token_type="Bearer", access_expires_at=format_instant(issued.access_expires_at),
            expires_in=ACCESS_TOKEN_SECONDS,
        )
        return JSONResponse(response.model_dump(), headers=TOKEN_RESPONSE_HEADERS)

    @router.post("/api/v1/auth/revoke", status_code=204, response_class=Response,
                 responses=_errors(401, 429, 503))
    async def revoke(request: Request, api_user: ApiUser = Depends(require_api_user)):
        async with control_connection(request.app.state.control_pool) as conn:
            await revoke_grant(conn, api_user.account["id"], api_user.grant_id)
        return Response(status_code=204)

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
