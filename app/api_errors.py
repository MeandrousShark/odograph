"""The API error envelope: `{"error": {"code", "message", "fields"}}`.

Codes are stable identifiers and messages are fixed strings per code, so an
error body never echoes request input, tokens or account data. Web paths keep
FastAPI's default handlers.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

API_PATH_PREFIX = "/api/"

ERRORS = {
    "invalid_token": (401, "The access token is invalid or has expired."),
    "invalid_grant": (400, "The grant is invalid or has expired. Sign in again."),
    "signin_disabled": (403, "Mobile sign-in is not enabled on this server."),
    "grant_limit_reached": (409, "This account has reached its limit of signed-in apps."),
    "rate_limited": (429, "Too many failed attempts. Try again later."),
    "validation_failed": (422, "The request is not valid."),
    "not_found": (404, "Not found."),
    "busy": (503, "The server is busy. Please try again."),
}
FIELD_REQUIRED = "This field is required."
FIELD_INVALID = "This value is not valid."
FIELD_UNSUPPORTED = "This value is not supported."
BODY_NOT_JSON = "The request body must be a JSON object."


class ApiErrorBody(BaseModel):
    code: str
    message: str
    fields: dict[str, str] | None


class ApiErrorEnvelope(BaseModel):
    error: ApiErrorBody


class ApiError(Exception):
    def __init__(self, code: str, *, fields: dict[str, str] | None = None,
                 headers: dict[str, str] | None = None):
        if code not in ERRORS:
            raise ValueError("unknown API error code")
        super().__init__(code)
        self.code = code
        self.fields = fields
        self.headers = headers or {}


def is_api_path(path: str) -> bool:
    return path.startswith(API_PATH_PREFIX)


def error_response(code: str, *, fields: dict[str, str] | None = None,
                   headers: dict[str, str] | None = None) -> JSONResponse:
    status, message = ERRORS[code]
    return JSONResponse(
        {"error": {"code": code, "message": message, "fields": fields}},
        status_code=status, headers=headers,
    )


def busy_response() -> JSONResponse:
    return error_response("busy", headers={"Retry-After": "1"})


def _validation_fields(exc: RequestValidationError) -> dict[str, str]:
    fields: dict[str, str] = {}
    for error in exc.errors():
        location = tuple(error.get("loc") or ())
        kind = error.get("type", "")
        path = location[1:] if location[:1] == ("body",) else location
        if kind in ("json_invalid", "model_attributes_type", "dict_type") or not path:
            fields.setdefault("body", BODY_NOT_JSON)
            continue
        key = ".".join(str(part) for part in path)
        fields.setdefault(key, FIELD_REQUIRED if kind == "missing" else FIELD_INVALID)
    return fields


def register_api_errors(app) -> None:
    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return error_response(exc.code, fields=exc.fields, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if not is_api_path(request.url.path):
            return await request_validation_exception_handler(request, exc)
        return error_response("validation_failed", fields=_validation_fields(exc))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if is_api_path(request.url.path) and exc.status_code == 404:
            return error_response("not_found")
        return await http_exception_handler(request, exc)
