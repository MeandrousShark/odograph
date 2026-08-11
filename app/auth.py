from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time
from collections.abc import Mapping

from authlib.integrations.base_client import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from psycopg import errors
from starlette.responses import RedirectResponse

from app.accounts import (
    account_exists,
    create_admin,
    get_account,
    get_sole_account,
    normalize_email,
    replace_password,
    valid_email,
)
from app.ingest import FailedAuthLimiter, client_ip
from app.local_auth import hash_password, verify_password
from app.oidc_identities import (
    IdentityLinkRejectedError,
    create_identity_link,
    establish_legacy_admin_identity,
    get_identity_for_account,
    normalize_issuer,
    resolve_identity_account,
    touch_identity_last_used,
    unlink_identity,
)

log = logging.getLogger(__name__)

MIN_LOCAL_PASSWORD_LENGTH = 8
GENERIC_LOGIN_ERROR = "Invalid email or password."
GENERIC_PASSWORD_ERROR = "Unable to change password."
GENERIC_LINK_ERROR = "Unable to link sign-in provider."
GENERIC_UNLINK_ERROR = "Unable to unlink sign-in provider."
GENERIC_OIDC_ERROR = "Sign-in failed. Please try again."
OIDC_LINK_ATTEMPT_KEY = "oidc_link_attempt"
OIDC_LINK_STATE_PREFIX = "link."
OIDC_LOGIN_STATE_PREFIX = "login."
OIDC_LINK_ATTEMPT_TTL_S = 600


class AuthRedirect(Exception):
    """Raised by require_user; handled in main.py with a redirect to /login."""


def _ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def check_form_csrf(request: Request, token: str) -> None:
    """Check a session token submitted by a plain HTML form."""
    expected = request.session.get("csrf") or ""
    if not (
        expected
        and hmac.compare_digest(
            expected.encode("utf-8"), (token or "").encode("utf-8")
        )
    ):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")


def require_csrf(request: Request) -> None:
    expected = request.session.get("csrf") or ""
    provided = request.headers.get("x-csrf-token") or ""
    if not (
        expected
        and hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))
    ):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")


def _account_user(account: dict) -> dict:
    return {
        "id": account["id"],
        "name": account["email"].split("@", 1)[0],
        "email": account["email"],
        "is_admin": account["is_admin"],
        "legacy_oidc": False,
    }


def _set_account_session(request: Request, account: dict) -> None:
    request.session.clear()
    request.session["account_id"] = account["id"]
    request.session["auth_version"] = account["auth_version"]
    _ensure_csrf(request)


def _positive_session_int(value) -> bool:
    return type(value) is int and value > 0


def _normalized_issuer(value: str) -> str:
    return normalize_issuer(value)


def _safe_oidc_metadata(userinfo: Mapping) -> tuple[str | None, str | None]:
    reported_email = userinfo.get("email")
    email = reported_email if isinstance(reported_email, str) else None
    reported_name = userinfo.get("name") or userinfo.get("preferred_username")
    display_name = reported_name if isinstance(reported_name, str) else None
    return email, display_name


def _consume_link_attempt(request: Request, state: str) -> dict | None:
    attempt = request.session.get(OIDC_LINK_ATTEMPT_KEY)
    if not isinstance(attempt, Mapping) or attempt.get("state") != state:
        return None
    request.session.pop(OIDC_LINK_ATTEMPT_KEY, None)
    account_id = attempt.get("account_id")
    auth_version = attempt.get("auth_version")
    issued_at = attempt.get("issued_at")
    if not (
        _positive_session_int(account_id)
        and _positive_session_int(auth_version)
        and type(issued_at) in (int, float)
    ):
        return None
    age = time.time() - issued_at
    if age < 0 or age > OIDC_LINK_ATTEMPT_TTL_S:
        return None
    return {
        "account_id": account_id,
        "auth_version": auth_version,
    }


def _valid_legacy_oidc_session(value, issuer: str) -> bool:
    if not isinstance(value, Mapping):
        return False
    subject = value.get("subject")
    return (
        isinstance(subject, str)
        and bool(subject)
        and isinstance(value.get("issuer"), str)
        and value["issuer"] == _normalized_issuer(issuer)
    )


async def _legacy_oidc_available(request: Request) -> bool:
    cfg = request.app.state.config
    if (
        cfg.dev_no_auth
        or getattr(cfg, "initial_admin_signup", False)
        or request.app.state.oauth is None
    ):
        return False
    async with request.app.state.pool.connection() as conn:
        return not await account_exists(conn)


async def _oidc_login_available(request: Request) -> bool:
    cfg = request.app.state.config
    if cfg.dev_no_auth or request.app.state.oauth is None:
        return False
    async with request.app.state.pool.connection() as conn:
        account = await get_sole_account(conn)
        if account is not None:
            identity = await get_identity_for_account(
                conn, account["id"], cfg.oidc_issuer
            )
            return identity is not None
    return not getattr(cfg, "initial_admin_signup", False)


async def require_user(request: Request) -> dict:
    cfg = request.app.state.config
    if cfg.dev_no_auth:
        _ensure_csrf(request)
        return {
            "id": None,
            "name": "dev (auth disabled)",
            "email": None,
            "is_admin": True,
            "legacy_oidc": False,
        }

    has_account_id = "account_id" in request.session
    has_auth_version = "auth_version" in request.session
    account_id = request.session.get("account_id")
    session_version = request.session.get("auth_version")
    if has_account_id or has_auth_version:
        if not (
            has_account_id
            and has_auth_version
            and _positive_session_int(account_id)
            and _positive_session_int(session_version)
        ):
            request.session.clear()
            raise AuthRedirect()
        async with request.app.state.pool.connection() as conn:
            account = await get_account(conn, account_id)
        if (
            account is not None
            and account["is_enabled"]
            and session_version == account["auth_version"]
        ):
            return _account_user(account)
        request.session.clear()
        raise AuthRedirect()

    has_legacy = "legacy_oidc" in request.session
    legacy = request.session.get("legacy_oidc")
    if has_legacy and not _valid_legacy_oidc_session(legacy, cfg.oidc_issuer):
        request.session.clear()
        raise AuthRedirect()

    has_old_user = "user" in request.session
    old_user = request.session.get("user")
    old_subject = old_user.get("sub") if isinstance(old_user, Mapping) else None
    if has_old_user and not (
        isinstance(old_user, Mapping)
        and isinstance(old_subject, str)
        and bool(old_subject)
    ):
        request.session.clear()
        raise AuthRedirect()
    if (
        not has_legacy
        and has_old_user
        and await _legacy_oidc_available(request)
    ):
        # Before accounts, both local and OIDC sessions used a free-standing
        # `user` dictionary. With no account row, the installation cannot have
        # had local login, so a valid old session in the OIDC-only upgrade
        # state is unambiguous. Convert it once into the isolated compatibility
        # shape that linked-identity work can later claim and retire.
        legacy = {
            "issuer": _normalized_issuer(cfg.oidc_issuer),
            "subject": old_subject,
            "name": old_user.get("name"),
            "email": old_user.get("email"),
        }
        request.session.clear()
        request.session["legacy_oidc"] = legacy
        _ensure_csrf(request)
        has_legacy = True
        has_old_user = False
    if has_legacy and await _legacy_oidc_available(request):
        return {
            "id": None,
            "name": legacy.get("name"),
            "email": legacy.get("email"),
            "is_admin": False,
            "legacy_oidc": True,
        }

    if has_legacy or has_old_user:
        request.session.clear()
    raise AuthRedirect()


async def require_admin(request: Request) -> dict:
    user = await require_user(request)
    if not user["is_admin"]:
        raise HTTPException(status_code=403)
    return user


def build_oauth(config) -> OAuth | None:
    if config.dev_no_auth:
        log.warning("DEV_NO_AUTH=1: UI authentication is DISABLED. Never deploy like this.")
        return None
    if not config.oidc_configured:
        return None
    oauth = OAuth()
    oauth.register(
        "pocketid",
        client_id=config.oidc_client_id,
        client_secret=config.oidc_client_secret,
        server_metadata_url=(
            f"{config.oidc_issuer.rstrip('/')}/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": "openid profile email"},
    )
    return oauth


async def _oidc_authorize_redirect(
    request: Request, *, flow: str, account: dict | None = None
):
    oauth = request.app.state.oauth
    redirect_uri = str(request.url_for("auth_callback"))
    state = f"{flow}.{secrets.token_urlsafe(32)}"
    nonce = secrets.token_urlsafe(32)
    if flow == "link":
        if account is None:
            raise ValueError("link flow requires an account")
        request.session[OIDC_LINK_ATTEMPT_KEY] = {
            "state": state,
            "account_id": account["id"],
            "auth_version": account["auth_version"],
            "issued_at": time.time(),
        }
    else:
        request.session.clear()
    return await oauth.pocketid.authorize_redirect(
        request,
        redirect_uri,
        state=state,
        nonce=nonce,
    )


def make_router() -> APIRouter:
    router = APIRouter()

    async def _render_login(
        request: Request, *, error: str | None, status_code: int = 200
    ):
        cfg = request.app.state.config
        async with request.app.state.pool.connection() as conn:
            account = await get_sole_account(conn)
            linked_identity = None
            if account is not None and request.app.state.oauth is not None:
                linked_identity = await get_identity_for_account(
                    conn, account["id"], cfg.oidc_issuer
                )
        signup_available = bool(
            account is None
            and getattr(cfg, "initial_admin_signup", False)
            and not cfg.dev_no_auth
        )
        legacy_oidc_available = bool(
            account is None
            and not getattr(cfg, "initial_admin_signup", False)
            and request.app.state.oauth is not None
            and not cfg.dev_no_auth
        )
        oidc_login_available = bool(
            account is not None
            and linked_identity is not None
            and not cfg.dev_no_auth
        )
        return request.app.state.templates.TemplateResponse(
            request,
            "login.html",
            {
                "user": None,
                "csrf": _ensure_csrf(request),
                "account_exists": account is not None,
                "signup_available": signup_available,
                "legacy_oidc_available": legacy_oidc_available,
                "oidc_login_available": oidc_login_available,
                "error": error,
            },
            status_code=status_code,
        )

    async def _render_signup(
        request: Request, *, error: str | None, status_code: int = 200
    ):
        return request.app.state.templates.TemplateResponse(
            request,
            "signup.html",
            {"user": None, "csrf": _ensure_csrf(request), "error": error},
            status_code=status_code,
        )

    async def _render_account(
        request: Request,
        account: dict,
        user: dict,
        *,
        error: str | None = None,
        success: str | None = None,
        status_code: int = 200,
    ):
        oidc_configured = bool(
            request.app.state.oauth is not None
            and not request.app.state.config.dev_no_auth
        )
        linked_identity = None
        if oidc_configured:
            async with request.app.state.pool.connection() as conn:
                identity = await get_identity_for_account(
                    conn,
                    account["id"],
                    request.app.state.config.oidc_issuer,
                )
            if identity is not None:
                linked_identity = {
                    "email": identity["provider_email"],
                    "display_name": identity["provider_display_name"],
                }
        if success is None:
            success = request.session.pop("account_notice", None)
        return request.app.state.templates.TemplateResponse(
            request,
            "account_security.html",
            {
                "user": user,
                "csrf": _ensure_csrf(request),
                "account_email": account["email"],
                "oidc_configured": oidc_configured,
                "linked_identity": linked_identity,
                "error": error,
                "success": success,
            },
            status_code=status_code,
        )

    async def _render_establish(
        request: Request,
        user: dict,
        *,
        error: str | None = None,
        status_code: int = 200,
    ):
        legacy = request.session.get("legacy_oidc")
        reported_email = legacy.get("email") if isinstance(legacy, Mapping) else None
        email = normalize_email(reported_email) if isinstance(reported_email, str) else ""
        if not valid_email(email):
            email = ""
        return request.app.state.templates.TemplateResponse(
            request,
            "establish_account.html",
            {
                "user": user,
                "csrf": _ensure_csrf(request),
                "email": email,
                "error": error,
            },
            status_code=status_code,
        )

    @router.get("/login")
    async def login(request: Request):
        if request.app.state.config.dev_no_auth:
            return RedirectResponse("/", status_code=303)
        return await _render_login(request, error=None)

    @router.post("/login/local")
    async def login_local(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        csrf_token: str = Form(...),
    ):
        if request.app.state.config.dev_no_auth:
            raise HTTPException(status_code=404)
        check_form_csrf(request, csrf_token)

        limiter: FailedAuthLimiter = request.app.state.login_limiter
        ip = client_ip(request)
        if limiter.blocked(ip):
            return await _render_login(
                request,
                error="Too many failed attempts. Try again later.",
                status_code=429,
            )

        async with request.app.state.pool.connection() as conn:
            account = await get_sole_account(conn)

        email_norm = normalize_email(email)
        ok = (
            account is not None
            and account["is_enabled"]
            and hmac.compare_digest(
                email_norm.encode("utf-8"), account["email"].encode("utf-8")
            )
            and await asyncio.to_thread(
                verify_password, password, account["password_hash"]
            )
        )
        if not ok:
            limiter.record_failure(ip)
            return await _render_login(
                request, error=GENERIC_LOGIN_ERROR, status_code=401
            )

        _set_account_session(request, account)
        return RedirectResponse("/", status_code=303)

    @router.get("/signup")
    async def signup_page(request: Request):
        if not await _signup_available(request):
            raise HTTPException(status_code=404)
        return await _render_signup(request, error=None)

    @router.post("/signup")
    async def signup_submit(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        password_confirm: str = Form(...),
        csrf_token: str = Form(...),
    ):
        if not await _signup_available(request):
            raise HTTPException(status_code=404)
        check_form_csrf(request, csrf_token)

        limiter: FailedAuthLimiter = request.app.state.login_limiter
        ip = client_ip(request)
        if limiter.blocked(ip):
            return await _render_signup(
                request,
                error="Too many failed attempts. Try again later.",
                status_code=429,
            )

        email_norm = normalize_email(email)
        error = _new_credential_error(email_norm, password, password_confirm)
        if error:
            limiter.record_failure(ip)
            return await _render_signup(
                request, error=error, status_code=400
            )

        password_hash = await asyncio.to_thread(hash_password, password)
        try:
            async with request.app.state.pool.connection() as conn:
                account = await create_admin(conn, email_norm, password_hash)
        except (errors.UniqueViolation, errors.CheckViolation):
            return await _render_signup(
                request,
                error="An administrator account already exists. Sign in to continue.",
                status_code=409,
            )

        _set_account_session(request, account)
        return RedirectResponse("/", status_code=303)

    @router.get("/login/oidc", name="login_oidc")
    async def login_oidc(request: Request):
        if not await _oidc_login_available(request):
            raise HTTPException(status_code=404)
        return await _oidc_authorize_redirect(request, flow="login")

    @router.get("/auth/callback", name="auth_callback")
    async def auth_callback(request: Request):
        if request.app.state.oauth is None or request.app.state.config.dev_no_auth:
            raise HTTPException(status_code=404)

        callback_state = request.query_params.get("state") or ""
        is_link_callback = callback_state.startswith(OIDC_LINK_STATE_PREFIX)
        is_login_callback = callback_state.startswith(OIDC_LOGIN_STATE_PREFIX)
        if not is_link_callback and not await _oidc_login_available(request):
            raise HTTPException(status_code=404)

        limiter: FailedAuthLimiter = request.app.state.login_limiter
        ip = client_ip(request)
        if limiter.blocked(ip):
            return Response(status_code=429)
        if not (is_link_callback or is_login_callback):
            limiter.record_failure(ip)
            raise HTTPException(status_code=401, detail=GENERIC_OIDC_ERROR)

        link_attempt = None
        link_user = None
        if is_link_callback:
            link_attempt = _consume_link_attempt(request, callback_state)
            if link_attempt is None:
                limiter.record_failure(ip)
                raise HTTPException(status_code=401, detail=GENERIC_OIDC_ERROR)
            try:
                link_user = await require_admin(request)
            except (AuthRedirect, HTTPException):
                limiter.record_failure(ip)
                raise HTTPException(status_code=401, detail=GENERIC_OIDC_ERROR)
            if (
                link_user["id"] != link_attempt["account_id"]
                or request.session.get("auth_version")
                != link_attempt["auth_version"]
            ):
                limiter.record_failure(ip)
                raise HTTPException(status_code=401, detail=GENERIC_OIDC_ERROR)

        oauth = request.app.state.oauth
        try:
            token = await oauth.pocketid.authorize_access_token(request)
        except OAuthError as exc:
            limiter.record_failure(ip)
            log.warning(
                "auth callback: token exchange rejected (%s)", type(exc).__name__
            )
            raise HTTPException(status_code=401, detail=GENERIC_OIDC_ERROR)

        userinfo = token.get("userinfo") or {}
        if not isinstance(userinfo, Mapping):
            userinfo = {}
        subject = userinfo.get("sub")
        if not isinstance(subject, str) or not subject:
            limiter.record_failure(ip)
            raise HTTPException(status_code=401, detail=GENERIC_OIDC_ERROR)
        provider_email, display_name = _safe_oidc_metadata(userinfo)
        cfg = request.app.state.config
        issuer = _normalized_issuer(cfg.oidc_issuer)

        if link_attempt is not None:
            async with request.app.state.pool.connection() as conn:
                linked = await create_identity_link(
                    conn,
                    link_attempt["account_id"],
                    issuer,
                    subject,
                    provider_email=provider_email,
                    provider_display_name=display_name,
                    expected_auth_version=link_attempt["auth_version"],
                )
            if linked is None:
                raise HTTPException(status_code=409, detail=GENERIC_LINK_ERROR)
            request.session["account_notice"] = "Sign-in provider linked."
            return RedirectResponse("/settings/account", status_code=303)

        async with request.app.state.pool.connection() as conn:
            account = await resolve_identity_account(conn, issuer, subject)
            if account is not None:
                await touch_identity_last_used(
                    conn,
                    issuer,
                    subject,
                    provider_email=provider_email,
                    provider_display_name=display_name,
                )
        if account is not None:
            _set_account_session(request, account)
            return RedirectResponse("/", status_code=303)

        async with request.app.state.pool.connection() as conn:
            has_account = await account_exists(conn)
        if has_account:
            limiter.record_failure(ip)
            raise HTTPException(status_code=401, detail=GENERIC_OIDC_ERROR)

        if not await _legacy_oidc_available(request):
            raise HTTPException(status_code=403, detail=GENERIC_OIDC_ERROR)

        email = normalize_email(provider_email) if provider_email else ""
        if cfg.allowed_email and email != cfg.allowed_email:
            limiter.record_failure(ip)
            log.warning("Rejected legacy OIDC login from unauthorized account")
            raise HTTPException(
                status_code=403,
                detail="This instance is not configured for your account.",
            )

        request.session.clear()
        request.session["legacy_oidc"] = {
            "issuer": issuer,
            "subject": subject,
            "name": display_name,
            "email": provider_email,
        }
        _ensure_csrf(request)
        return RedirectResponse("/account/establish", status_code=303)

    @router.get("/account/establish")
    async def establish_account_page(
        request: Request, user: dict = Depends(require_user)
    ):
        if not user["legacy_oidc"]:
            raise HTTPException(status_code=404)
        return await _render_establish(request, user)

    @router.post("/account/establish")
    async def establish_account(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        password_confirm: str = Form(...),
        csrf_token: str = Form(...),
        user: dict = Depends(require_user),
    ):
        if not user["legacy_oidc"]:
            raise HTTPException(status_code=404)
        check_form_csrf(request, csrf_token)
        limiter: FailedAuthLimiter = request.app.state.login_limiter
        ip = client_ip(request)
        if limiter.blocked(ip):
            return await _render_establish(
                request,
                user,
                error="Too many failed attempts. Try again later.",
                status_code=429,
            )

        email_norm = normalize_email(email)
        error = _new_credential_error(email_norm, password, password_confirm)
        if error:
            limiter.record_failure(ip)
            return await _render_establish(
                request, user, error=error, status_code=400
            )

        legacy = request.session.get("legacy_oidc")
        if not _valid_legacy_oidc_session(
            legacy, request.app.state.config.oidc_issuer
        ):
            request.session.clear()
            raise AuthRedirect()
        password_hash = await asyncio.to_thread(hash_password, password)
        try:
            async with request.app.state.pool.connection() as conn:
                account, _identity = await establish_legacy_admin_identity(
                    conn,
                    email=email_norm,
                    password_hash=password_hash,
                    issuer=legacy["issuer"],
                    subject=legacy["subject"],
                    provider_email=(
                        legacy.get("email")
                        if isinstance(legacy.get("email"), str)
                        else None
                    ),
                    provider_display_name=(
                        legacy.get("name")
                        if isinstance(legacy.get("name"), str)
                        else None
                    ),
                )
        except (
            errors.UniqueViolation,
            errors.CheckViolation,
            IdentityLinkRejectedError,
        ):
            account = None
        if account is None:
            return await _render_establish(
                request,
                user,
                error="Unable to establish the administrator account.",
                status_code=409,
            )

        _set_account_session(request, account)
        request.session["account_notice"] = (
            "Administrator account established and sign-in provider linked."
        )
        return RedirectResponse("/settings/account", status_code=303)

    @router.post("/settings/account/oidc/link")
    async def link_oidc(
        request: Request,
        current_password: str = Form(...),
        csrf_token: str = Form(...),
        user: dict = Depends(require_admin),
    ):
        if request.app.state.oauth is None or request.app.state.config.dev_no_auth:
            raise HTTPException(status_code=404)
        check_form_csrf(request, csrf_token)
        limiter: FailedAuthLimiter = request.app.state.login_limiter
        ip = client_ip(request)
        async with request.app.state.pool.connection() as conn:
            account = await get_account(conn, user["id"])
        if account is None:
            request.session.clear()
            raise AuthRedirect()
        if limiter.blocked(ip):
            return await _render_account(
                request,
                account,
                user,
                error="Too many failed attempts. Try again later.",
                status_code=429,
            )
        password_ok = await asyncio.to_thread(
            verify_password, current_password, account["password_hash"]
        )
        if not password_ok:
            limiter.record_failure(ip)
            return await _render_account(
                request,
                account,
                user,
                error=GENERIC_LINK_ERROR,
                status_code=401,
            )
        async with request.app.state.pool.connection() as conn:
            existing = await get_identity_for_account(
                conn, account["id"], request.app.state.config.oidc_issuer
            )
        if existing is not None:
            return await _render_account(
                request,
                account,
                user,
                error=GENERIC_LINK_ERROR,
                status_code=409,
            )
        return await _oidc_authorize_redirect(
            request, flow="link", account=account
        )

    @router.post("/settings/account/oidc/unlink")
    async def unlink_oidc(
        request: Request,
        current_password: str = Form(...),
        csrf_token: str = Form(...),
        confirm_unlink: str | None = Form(None),
        user: dict = Depends(require_admin),
    ):
        if request.app.state.oauth is None or request.app.state.config.dev_no_auth:
            raise HTTPException(status_code=404)
        check_form_csrf(request, csrf_token)
        limiter: FailedAuthLimiter = request.app.state.login_limiter
        ip = client_ip(request)
        async with request.app.state.pool.connection() as conn:
            account = await get_account(conn, user["id"])
        if account is None:
            request.session.clear()
            raise AuthRedirect()
        if limiter.blocked(ip):
            return await _render_account(
                request,
                account,
                user,
                error="Too many failed attempts. Try again later.",
                status_code=429,
            )
        if confirm_unlink != "yes":
            return await _render_account(
                request,
                account,
                user,
                error=GENERIC_UNLINK_ERROR,
                status_code=400,
            )
        password_ok = await asyncio.to_thread(
            verify_password, current_password, account["password_hash"]
        )
        if not password_ok:
            limiter.record_failure(ip)
            return await _render_account(
                request,
                account,
                user,
                error=GENERIC_UNLINK_ERROR,
                status_code=401,
            )
        async with request.app.state.pool.connection() as conn:
            identity = await get_identity_for_account(
                conn, account["id"], request.app.state.config.oidc_issuer
            )
            updated = None
            if identity is not None:
                updated = await unlink_identity(
                    conn,
                    account["id"],
                    identity["issuer"],
                    identity["subject"],
                    expected_auth_version=account["auth_version"],
                )
        if updated is None:
            return await _render_account(
                request,
                account,
                user,
                error=GENERIC_UNLINK_ERROR,
                status_code=409,
            )
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @router.get("/settings/account")
    async def account_security(
        request: Request, user: dict = Depends(require_admin)
    ):
        async with request.app.state.pool.connection() as conn:
            account = await get_account(conn, user["id"])
        if account is None:
            raise AuthRedirect()
        return await _render_account(request, account, user)

    @router.post("/settings/account/password")
    async def change_password(
        request: Request,
        current_password: str = Form(...),
        password: str = Form(...),
        password_confirm: str = Form(...),
        csrf_token: str = Form(...),
        user: dict = Depends(require_admin),
    ):
        check_form_csrf(request, csrf_token)
        limiter: FailedAuthLimiter = request.app.state.login_limiter
        ip = client_ip(request)

        async with request.app.state.pool.connection() as conn:
            account = await get_account(conn, user["id"])
        if account is None:
            request.session.clear()
            raise AuthRedirect()

        if limiter.blocked(ip):
            return await _render_account(
                request,
                account,
                user,
                error="Too many failed attempts. Try again later.",
                status_code=429,
            )

        password_ok = await asyncio.to_thread(
            verify_password, current_password, account["password_hash"]
        )
        if not password_ok:
            limiter.record_failure(ip)
            return await _render_account(
                request,
                account,
                user,
                error=GENERIC_PASSWORD_ERROR,
                status_code=401,
            )

        error = _new_password_error(password, password_confirm)
        if error:
            return await _render_account(
                request, account, user, error=error, status_code=400
            )

        password_hash = await asyncio.to_thread(hash_password, password)
        async with request.app.state.pool.connection() as conn:
            updated = await replace_password(
                conn,
                account["id"],
                password_hash,
                expected_auth_version=account["auth_version"],
            )
        if updated is None:
            request.session.clear()
            raise AuthRedirect()

        _set_account_session(request, updated)
        return await _render_account(
            request,
            updated,
            _account_user(updated),
            success="Password changed. Other sessions have been signed out.",
        )

    @router.post("/logout", dependencies=[Depends(require_csrf)])
    async def logout(request: Request):
        request.session.clear()
        return Response(status_code=204, headers={"HX-Redirect": "/login"})

    return router


async def _signup_available(request: Request) -> bool:
    cfg = request.app.state.config
    if cfg.dev_no_auth or not getattr(cfg, "initial_admin_signup", False):
        return False
    async with request.app.state.pool.connection() as conn:
        return not await account_exists(conn)


def _new_password_error(password: str, password_confirm: str) -> str | None:
    if password != password_confirm:
        return "Passwords do not match."
    if len(password) < MIN_LOCAL_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_LOCAL_PASSWORD_LENGTH} characters."
    return None


def _new_credential_error(
    email: str, password: str, password_confirm: str
) -> str | None:
    if not valid_email(email):
        return "Enter a valid ASCII email address."
    return _new_password_error(password, password_confirm)
