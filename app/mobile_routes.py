"""Browser side of mobile sign-in: start, confirm, approve or deny.

Every route is 404 unless MOBILE_SIGNIN is on. The client id and callback are
checked before anything else and a mismatch never redirects. Completion
returns to the app through a page that links to the callback, because the
page's `form-action 'self'` policy would block a form submission redirecting
to the app's custom scheme.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import RedirectResponse

from app.account_context import control_connection
from app.api_grants import ACTIVE_GRANT_LIMIT, normalize_device_label, valid_public_id
from app.auth import check_form_csrf, require_user, _ensure_csrf
from app.capacity_routes import AdmissionRoute, busy_response
from app.ingest import client_ip
from app.mobile_authorization import (
    AUTHORIZE_PATH, CONFIRM_PATH, COOKIE_NAME, REQUEST_SECONDS, approve_request, callback_url,
    deny_request, mobile_signin_enabled, registered_callback, review_request, start_request,
    valid_challenge, valid_state,
)

INVALID_CLIENT_HEADING = "Sign-in request not recognized"
INVALID_CLIENT_MESSAGE = "This sign-in request did not come from the Odograph app."
RATE_LIMITED_HEADING = "Too many sign-in attempts"
RATE_LIMITED_MESSAGE = "Too many sign-in attempts from this network. Try again later."
EXPIRED_HEADING = "Sign-in request expired"
EXPIRED_MESSAGE = "This sign-in request has expired or was already finished."
APPROVED_MESSAGE = "The app is signed in. Return to it to continue."
NOT_APPROVED_MESSAGE = "The app was not signed in. Return to it to continue."


def _require_mobile_signin(request: Request) -> None:
    if not mobile_signin_enabled(request):
        raise HTTPException(status_code=404)


def _single(request: Request, name: str) -> str | None:
    """One value, or None when the parameter is absent or repeated."""
    values = request.query_params.getlist(name)
    return values[0] if len(values) == 1 else None


def _cookie_options(request: Request) -> dict:
    return {"path": "/", "httponly": True, "samesite": "lax",
            "secure": not request.app.state.config.dev_no_auth}


def _clear_cookie(request: Request, response):
    response.delete_cookie(COOKIE_NAME, **_cookie_options(request))
    return response


def _error_page(request: Request, heading: str, message: str, status_code: int, *, user=None):
    return request.app.state.templates.TemplateResponse(
        request, "mobile_authorize_error.html",
        {"user": user, "csrf": _ensure_csrf(request), "heading": heading, "message": message},
        status_code=status_code,
    )


def _expired(request: Request, user=None):
    return _clear_cookie(request, _error_page(request, EXPIRED_HEADING, EXPIRED_MESSAGE, 410, user=user))


def make_router() -> APIRouter:
    router = APIRouter(route_class=AdmissionRoute, dependencies=[Depends(_require_mobile_signin)])

    @router.get(AUTHORIZE_PATH)
    async def authorize(request: Request):
        client_id = _single(request, "client_id")
        redirect_uri = _single(request, "redirect_uri")
        if not registered_callback(client_id, redirect_uri):
            return _error_page(request, INVALID_CLIENT_HEADING, INVALID_CLIENT_MESSAGE, 400)
        if not request.app.state.mobile_authorize_limiter.allow(client_ip(request)):
            return _error_page(request, RATE_LIMITED_HEADING, RATE_LIMITED_MESSAGE, 429)
        state = _single(request, "state")
        challenge = _single(request, "code_challenge")
        replaces = request.query_params.getlist("replaces")
        labels = request.query_params.getlist("device_label")
        if (not valid_state(state) or not valid_challenge(challenge)
                or _single(request, "code_challenge_method") != "S256"
                or len(replaces) > 1 or (replaces and not valid_public_id(replaces[0]))
                or len(labels) > 1):
            params = {"error": "invalid_request"}
            if valid_state(state):
                params["state"] = state
            return RedirectResponse(callback_url(redirect_uri, **params), status_code=303)
        async with control_connection(request.app.state.control_pool) as conn:
            started = await start_request(
                conn, previous_cookie=request.cookies.get(COOKIE_NAME), client_id=client_id,
                redirect_uri=redirect_uri, code_challenge=challenge, state=state,
                device_label=normalize_device_label(labels[0] if labels else None),
                replaces=replaces[0] if replaces else None,
            )
        if started is None:
            return busy_response(request)
        signed_in = request.app.state.config.dev_no_auth or "account_id" in request.session
        response = RedirectResponse(CONFIRM_PATH if signed_in else "/login", status_code=303)
        response.set_cookie(COOKIE_NAME, started.cookie_value, max_age=REQUEST_SECONDS,
                            **_cookie_options(request))
        return response

    @router.get(CONFIRM_PATH)
    async def confirm(request: Request, user: dict = Depends(require_user)):
        async with control_connection(request.app.state.control_pool) as conn:
            review = await review_request(
                conn, request.cookies.get(COOKIE_NAME), user["id"], request.state.principal.auth_version)
        if review is None:
            return _expired(request, user)
        return request.app.state.templates.TemplateResponse(
            request, "mobile_authorize_confirm.html",
            {"user": user, "csrf": _ensure_csrf(request), "account_email": user["email"],
             "review": review, "grant_limit": ACTIVE_GRANT_LIMIT},
        )

    @router.post(CONFIRM_PATH)
    async def decide(
        request: Request,
        decision: str = Form(...),
        csrf_token: str = Form(...),
        user: dict = Depends(require_user),
    ):
        check_form_csrf(request, csrf_token)
        if decision not in ("approve", "deny"):
            raise HTTPException(status_code=400)
        finish = approve_request if decision == "approve" else deny_request
        async with control_connection(request.app.state.control_pool) as conn:
            completion = await finish(
                conn, request.cookies.get(COOKIE_NAME), user["id"], request.state.principal.auth_version)
        if completion is None:
            return _expired(request, user)
        response = request.app.state.templates.TemplateResponse(
            request, "mobile_authorize_return.html",
            {"callback_url": completion.url(),
             "message": APPROVED_MESSAGE if completion.code is not None else NOT_APPROVED_MESSAGE},
        )
        return _clear_cookie(request, response)

    return router
