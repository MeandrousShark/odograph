"""The API error envelope: `{"error": {"code", "message", "fields"}}`.

Codes are stable identifiers and messages are fixed strings per code, so an
error body never echoes request input, tokens or account data. Web paths keep
FastAPI's and Starlette's default responses.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, PlainTextResponse

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
    "invalid_cursor": (400, "The cursor is not valid for this request. Start again from the first page."),
    "method_not_allowed": (405, "This method is not allowed here."),
    "payload_too_large": (413, "The request body is too large."),
    "internal_error": (500, "The server could not complete the request."),
    "idempotency_key_reused": (409, "This idempotency key was already used for a different request."),
    "storage_full": (409, "Storage is full. This change was not saved."),
    "trip_in_progress": (409, "This trip is still in progress. Try again once it ends."),
}
# Framework HTTP errors on API paths, answered in the envelope.
HTTP_STATUS_CODES = {404: "not_found", 405: "method_not_allowed", 413: "payload_too_large"}
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
        path = location[1:] if location[:1] in (("body",), ("query",), ("path",), ("header",)) else location
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
        code = HTTP_STATUS_CODES.get(exc.status_code)
        if is_api_path(request.url.path) and code is not None:
            # Keeps headers such as a 405's Allow.
            return error_response(code, headers=exc.headers)
        return await http_exception_handler(request, exc)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        # Starlette still logs and re-raises the exception after this response.
        if is_api_path(request.url.path):
            # Sent outside the middleware that marks API responses private.
            return error_response("internal_error", headers={"Cache-Control": "no-store, private"})
        return PlainTextResponse("Internal Server Error", status_code=500)
