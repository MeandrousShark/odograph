from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from authlib.integrations.base_client import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from psycopg import errors
from starlette.datastructures import UploadFile
from starlette.responses import RedirectResponse

from app.accounts import (
    account_exists,
    clear_account_avatar,
    create_admin,
    get_account,
    get_account_avatar,
    get_account_by_email,
    normalize_email,
    replace_password,
    safe_delivery_email as _safe_delivery_email,
    set_account_avatar,
    sign_out_everywhere,
    valid_email,
)
from app.account_context import AccountPool, AccountPrincipal, control_connection
from app.account_settings import config_for_account, load_account_settings
from app.avatar_images import (
    MAX_AVATAR_PIXELS,
    MAX_AVATAR_SIDE,
    REASON_TOO_LARGE,
    detect_avatar as _detect_avatar,
)
from app.config import DEFAULT_ACCOUNT_AVATAR_MAX_BYTES, security_link_base
from app.email_challenges import (
    PURPOSE_CHANGE,
    PURPOSE_CURRENT,
    _digest as _challenge_digest,
    consume_email_challenge,
    is_current_email_verified,
    issue_email_challenge,
    revoke_email_challenge,
)
from app.ingest import FailedAuthLimiter, _AuthSaturated, client_ip
from app.ingest import _read_capped_body
from app.invitations import InvitationUnavailable, redeem_invitation
from app.local_auth import hash_password, verify_password
from app.mailer import Mailer
from app.password_reset import consume_password_reset, password_reset_usable
from app.page import render_page
from app.uploads import read_capped_upload
from app.oidc_identities import (
    IdentityLinkRejectedError,
    create_identity_link,
    establish_legacy_admin_identity,
    get_identity_for_account,
    identity_login_available,
    normalize_issuer,
    resolve_identity_account,
    touch_identity_last_used,
    unlink_identity,
)
from app.oidc_attempts import (
    consume_action_proof,
    consume_oidc_attempt,
    finish_oidc_reauth,
    start_oidc_attempt,
)
from app.invitations import redeem_oidc_invitation_by_digest

log = logging.getLogger(__name__)

MIN_LOCAL_PASSWORD_LENGTH = 8
GENERIC_LOGIN_ERROR = "Invalid email or password."
GENERIC_PASSWORD_ERROR = "Unable to change password."
GENERIC_LINK_ERROR = "Unable to link sign-in provider."
GENERIC_UNLINK_ERROR = "Unable to unlink sign-in provider."
GENERIC_OIDC_ERROR = "Sign-in failed. Please try again."
GENERIC_INVITE_ERROR = "Unable to accept invitation. Check the link or ask for a new one."
OIDC_REAUTH_NOTICE = "Provider authentication confirmed. Complete the action within 10 minutes."
PASSWORD_SAVED_NOTICE = "Password saved. Other sessions have been signed out."
MAX_INVITE_FORM_BYTES = 8192
MAX_EMAIL_FORM_BYTES = 8192
GENERIC_EMAIL_ERROR = "Unable to process email request. Please try again."
EMAIL_REQUEST_NOTICE = "If the request is eligible, a verification email has been sent."
RESET_REQUEST_NOTICE = (
    "If an enabled account with a verified email address matches, a password reset "
    "link is on its way. It expires in 30 minutes."
)
GENERIC_RESET_ERROR = "Unable to reset password. Check the link or request a new one."
RESET_COMPLETE_NOTICE = (
    "Password reset. Every Odograph session for that account has been signed out; "
    "sign in with your new password."
)
OIDC_LINK_STATE_PREFIX = "link."
OIDC_LOGIN_STATE_PREFIX = "login."
OIDC_PROTECTED_ATTEMPT_KEY = "oidc_protected_attempt"
OIDC_INVITE_STATE_PREFIX = "invite."
OIDC_REAUTH_STATE_PREFIX = "reauth."


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


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _avatar_version(avatar_updated_at) -> int:
    """A stable, URL-safe integer derived from avatar_updated_at at full
    microsecond precision, used as both the cache-busting query value a
    template would put on the avatar URL and the ETag GET /account/avatar
    serves.

    Postgres stores timestamptz to microsecond resolution. A coarser,
    whole-second validator would let two avatar writes landing in the same
    wall-clock second (a double-clicked upload, a quick "wrong photo" retry)
    collide onto the same version/ETag. Computed via timedelta arithmetic
    rather than timestamp() * 1_000_000 so there's no float round-trip to
    worry about.

    0 when there's no avatar: no real timestamp is ever exactly the epoch,
    so 0 stays an unambiguous no-avatar sentinel.
    """
    if avatar_updated_at is None:
        return 0
    delta = avatar_updated_at - _EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _human_size(num_bytes: int) -> str:
    """Renders a byte count the way an operator configures it -- whole
    kilobytes or megabytes when it divides evenly (every default and every
    sane override does), a raw byte count otherwise -- for the upload-limit
    hint on the Account Settings page, so a rejection isn't the first time
    an operator learns there is a limit.
    """
    if num_bytes % (1024 * 1024) == 0:
        return f"{num_bytes // (1024 * 1024)} MB"
    if num_bytes % 1024 == 0:
        return f"{num_bytes // 1024} KB"
    return f"{num_bytes} bytes"


async def _email_form(request: Request, fields: set[str]) -> dict[str, str]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=400, detail=GENERIC_EMAIL_ERROR)
    body = await _read_capped_body(request, MAX_EMAIL_FORM_BYTES)
    if body is None:
        raise HTTPException(status_code=413, detail=GENERIC_EMAIL_ERROR)
    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True,
                          strict_parsing=True, max_num_fields=len(fields))
        if set(parsed) != fields or any(len(values) != 1 for values in parsed.values()):
            raise ValueError("invalid fields")
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail=GENERIC_EMAIL_ERROR) from None
    return {name: values[0] for name, values in parsed.items()}


def _avatar_upload_exceeds_limit(request: Request) -> bool:
    """Return whether a declared upload is over the configured cap.

    This must run before request.form() so Starlette cannot spool a declared
    oversized multipart body before the account page renders its error. The
    route calls it after require_user has run, preserving authentication while
    still allowing the route to use _render_account for the response. A
    missing Content-Length falls through to read_capped_upload below.
    """
    content_length = request.headers.get("content-length")
    if content_length is None:
        return False
    try:
        declared_bytes = int(content_length)
    except ValueError:
        return False
    cfg = request.app.state.config
    return declared_bytes > cfg.account_avatar_max_bytes


def _if_none_match_matches(header_value: str, etag: str) -> bool:
    """Minimal If-None-Match handling for a single-resource GET, not a
    general cache-control parser: RFC 9110 requires GET's If-None-Match to
    use weak comparison (a client's "W/" prefix must still match our strong
    tag) and to treat "*" as "matches whatever is there" -- both cheap to
    honor even though this app always emits the strong form back to itself,
    so a real client is unlikely to ever send either.
    """
    header_value = header_value.strip()
    if header_value == "*":
        return True
    for candidate in header_value.split(","):
        candidate = candidate.strip()
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


def _account_user(account: dict) -> dict:
    return {
        "id": account["id"],
        "name": account["email"].split("@", 1)[0],
        "email": account["email"],
        "is_admin": account["is_admin"],
        "is_enabled": account["is_enabled"],
        "legacy_oidc": False,
        "has_avatar": account["avatar_mime"] is not None,
        "avatar_version": _avatar_version(account["avatar_updated_at"]),
    }


def _set_account_session(request: Request, account: dict) -> None:
    request.session.clear()
    request.session["account_id"] = account["id"]
    request.session["auth_version"] = account["auth_version"]
    principal = AccountPrincipal(account["id"], account["is_enabled"], account["auth_version"])
    request.state.principal = principal
    runtime_pool = getattr(request.app.state, "runtime_pool", None)
    if runtime_pool is not None:
        pool = AccountPool(runtime_pool, principal)
        request.state.account_pool = pool
        make_detector_runner = getattr(request.app.state, "make_detector_runner", None)
        if make_detector_runner is not None:
            request.state.detector_runner = make_detector_runner(pool)
    _ensure_csrf(request)


def _positive_session_int(value) -> bool:
    return type(value) is int and 1 <= value <= 2**63 - 1


def _safe_oidc_metadata(userinfo: Mapping) -> tuple[str | None, str | None]:
    reported_email = userinfo.get("email")
    email = reported_email if isinstance(reported_email, str) else None
    reported_name = userinfo.get("name") or userinfo.get("preferred_username")
    display_name = reported_name if isinstance(reported_name, str) else None
    return email, display_name


def _valid_legacy_oidc_session(value, issuer: str) -> bool:
    if not isinstance(value, Mapping):
        return False
    subject = value.get("subject")
    return (
        isinstance(subject, str)
        and bool(subject)
        and isinstance(value.get("issuer"), str)
        and value["issuer"] == normalize_issuer(issuer)
    )


def _signup_gate(cfg) -> bool:
    return not cfg.dev_no_auth and getattr(cfg, "initial_admin_signup", False)


def _legacy_gate(request: Request) -> bool:
    cfg = request.app.state.config
    return (
        not cfg.dev_no_auth
        and not getattr(cfg, "initial_admin_signup", False)
        and request.app.state.oauth is not None
    )


async def _legacy_oidc_available(request: Request) -> bool:
    if not _legacy_gate(request):
        return False
    async with control_connection(request.app.state.control_pool) as conn:
        return not await account_exists(conn)


async def _oidc_login_available(request: Request) -> bool:
    cfg = request.app.state.config
    if cfg.dev_no_auth or request.app.state.oauth is None:
        return False
    async with control_connection(request.app.state.control_pool) as conn:
        if await account_exists(conn):
            return await identity_login_available(conn, cfg.oidc_issuer)
    return not getattr(cfg, "initial_admin_signup", False)


async def _bind_account(request: Request, account: dict) -> dict:
    principal = AccountPrincipal(account["id"], account["is_enabled"], account["auth_version"])
    pool = AccountPool(request.app.state.runtime_pool, principal)
    async with pool.connection() as conn:
        settings = await load_account_settings(conn)
    request.state.principal = principal
    request.state.account_pool = pool
    request.state.detector_runner = request.app.state.make_detector_runner(pool)
    request.state.config = config_for_account(request.app.state.config, settings)
    request.state.account_settings = settings
    _ensure_csrf(request)
    return _account_user(account)


async def require_user(request: Request) -> dict:
    cfg = request.app.state.config
    if cfg.dev_no_auth:
        principal = request.app.state.dev_principal
        async with control_connection(request.app.state.control_pool) as conn:
            account = await get_account(conn, principal.account_id)
        if account is None or not account["is_enabled"]:
            raise AuthRedirect()
        return await _bind_account(request, account)

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
        async with control_connection(request.app.state.control_pool) as conn:
            account = await get_account(conn, account_id)
        if (
            account is not None
            and account["is_enabled"]
            and session_version == account["auth_version"]
        ):
            page_account = getattr(request, "headers", {}).get("X-Odograph-Account")
            if page_account is not None and page_account != str(account_id):
                raise HTTPException(status_code=409, detail="Account changed. Reload the page.")
            return await _bind_account(request, account)
        request.session.clear()
        raise AuthRedirect()

    raise AuthRedirect()


async def require_legacy_establishment(request: Request) -> dict:
    """An accountless OIDC session can establish identity, never read a ledger."""
    cfg = request.app.state.config
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
            "issuer": normalize_issuer(cfg.oidc_issuer),
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
            # No account row exists yet for a legacy session that hasn't
            # completed /account/establish -- same no-avatar shape as
            # dev_no_auth's hand-built user, below.
            "has_avatar": False,
            "avatar_version": 0,
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


async def _oidc_authorize_redirect(request: Request):
    oauth = request.app.state.oauth
    redirect_uri = str(request.url_for("auth_callback"))
    state = f"login.{secrets.token_urlsafe(32)}"
    nonce = secrets.token_urlsafe(32)
    request.session.clear()
    return await oauth.pocketid.authorize_redirect(
        request,
        redirect_uri,
        state=state,
        nonce=nonce,
    )


async def _oidc_protected_redirect(
    request: Request, *, action: str, account: dict | None = None,
    invite_token: str | None = None, timezone_name: str | None = None,
    proof_action: str | None = None, target: str | None = None,
):
    state = f"{action}.{secrets.token_urlsafe(32)}"
    nonce = secrets.token_urlsafe(32)
    browser_nonce = request.session.get("oidc_browser_nonce")
    if not (isinstance(browser_nonce, str) and browser_nonce.isascii()
            and len(browser_nonce) == 43):
        browser_nonce = secrets.token_urlsafe(32)
    async with control_connection(request.app.state.control_pool) as conn:
        started = await start_oidc_attempt(
            conn, action=action, state=state, nonce=nonce,
            browser_nonce=browser_nonce,
            account_id=account["id"] if account else None,
            auth_version=account["auth_version"] if account else None,
            invite_token=invite_token, proof_action=proof_action,
            target=(target if target is not None else timezone_name
                    if timezone_name is not None else
                    normalize_issuer(request.app.state.config.oidc_issuer)
                    if action == "link" else ""),
        )
    if not started:
        raise HTTPException(status_code=400, detail=GENERIC_OIDC_ERROR)
    request.session.pop("oidc_action_proof_nonce", None)
    if action == "invite":
        request.session.clear()
    request.session["oidc_browser_nonce"] = browser_nonce
    request.session[OIDC_PROTECTED_ATTEMPT_KEY] = {
        "action": action, "state": state, "nonce": nonce,
        "browser_nonce": browser_nonce,
        "account_id": account["id"] if account else None,
        "auth_version": account["auth_version"] if account else None,
    }
    try:
        return await request.app.state.oauth.pocketid.authorize_redirect(
            request, str(request.url_for("auth_callback")), state=state, nonce=nonce,
            **({"max_age": 0, "prompt": "login"} if action == "reauth" else {}),
        )
    except Exception:
        request.session.pop(OIDC_PROTECTED_ATTEMPT_KEY, None)
        async with control_connection(request.app.state.control_pool) as conn:
            await consume_oidc_attempt(
                conn, action=action, state=state, nonce=nonce,
                browser_nonce=browser_nonce,
                account_id=account["id"] if account else None,
                auth_version=account["auth_version"] if account else None,
            )
        raise


def _require_oidc_enabled(request: Request) -> None:
    if request.app.state.oauth is None or request.app.state.config.dev_no_auth:
        raise HTTPException(status_code=404)


class _AccountActionRejected(Exception):
    """Carries the account row and the response detail a route should
    re-render with, so the shared preamble can refuse an action without every
    route repeating the same limiter and password branches.
    """

    def __init__(self, account: dict, error: str, status_code: int):
        super().__init__(error)
        self.account = account
        self.error = error
        self.status_code = status_code


async def _verified_account(
    request: Request,
    user: dict,
    current_password: str,
    *,
    generic_error: str,
    precheck_error: str | None = None,
) -> dict:
    """Shared re-authentication preamble for account-mutating routes
    (link/unlink OIDC, change password): load the caller's account, apply
    the failed-auth limiter, and verify the submitted current password.

    `precheck_error`, when set, is reported after the limiter check but
    before password verification, so a route-specific validation failure
    (e.g. unlink's missing confirmation checkbox) returns its own status
    without also counting as a failed credential attempt against the
    limiter.
    """
    limiter: FailedAuthLimiter = request.app.state.login_limiter
    ip = client_ip(request)
    async with control_connection(request.app.state.control_pool) as conn:
        account = await get_account(conn, user["id"])
    if (
        account is None
        or not account["is_enabled"]
        or account["auth_version"] != request.state.principal.auth_version
    ):
        request.session.clear()
        raise AuthRedirect()
    if limiter.blocked(ip):
        raise _AccountActionRejected(
            account, "Too many failed attempts. Try again later.", 429
        )
    if precheck_error is not None:
        raise _AccountActionRejected(account, precheck_error, 400)
    password_ok = await asyncio.to_thread(
        verify_password, current_password, account["password_hash"]
    )
    if not password_ok:
        limiter.record_failure(ip)
        raise _AccountActionRejected(account, generic_error, 401)
    return account


def make_router() -> APIRouter:
    router = APIRouter()

    async def _render_invite(request: Request, *, error: str | None = None,
                             status_code: int = 200):
        return request.app.state.templates.TemplateResponse(
            request, "invite.html",
            {"user": None, "csrf": _ensure_csrf(request), "error": error,
             "display_timezone": str(request.app.state.config.display_tz),
             "oidc_invite_available": getattr(request.app.state, "oauth", None) is not None},
            status_code=status_code,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    async def _render_login(
        request: Request, *, error: str | None, status_code: int = 200
    ):
        cfg = request.app.state.config
        async with control_connection(request.app.state.control_pool) as conn:
            has_account = await account_exists(conn)
            linked_identity = (
                await identity_login_available(conn, cfg.oidc_issuer)
                if has_account and request.app.state.oauth is not None else False
            )
        signup_available = _signup_gate(cfg) and not has_account
        legacy_oidc_available = _legacy_gate(request) and not has_account
        oidc_login_available = bool(linked_identity and not cfg.dev_no_auth)
        return request.app.state.templates.TemplateResponse(
            request,
            "login.html",
            {
                "user": None,
                "csrf": _ensure_csrf(request),
                "account_exists": has_account,
                "signup_available": signup_available,
                "legacy_oidc_available": legacy_oidc_available,
                "oidc_login_available": oidc_login_available,
                "password_reset_available": has_account and password_reset_available(cfg),
                "notice": request.session.pop("login_notice", None),
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
            {"user": None, "csrf": _ensure_csrf(request), "error": error,
             "display_timezone": str(request.app.state.config.display_tz)},
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
        include_review_count: bool = False,
    ):
        oidc_configured = bool(
            request.app.state.oauth is not None
            and not request.app.state.config.dev_no_auth
        )
        linked_identity = None
        if oidc_configured:
            async with control_connection(request.app.state.control_pool) as conn:
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
        # getattr, not cfg.account_avatar_max_bytes directly: a number of
        # route-level tests build a bare config double that predates this
        # field, and _render_account now runs on every Account Settings
        # render, including theirs. The fallback is Config's own default
        # constant, not a second copy of it, so the two cannot drift.
        avatar_max_bytes = getattr(
            request.app.state.config,
            "account_avatar_max_bytes",
            DEFAULT_ACCOUNT_AVATAR_MAX_BYTES,
        )
        cfg = request.app.state.config
        async with control_connection(request.app.state.control_pool) as conn:
            account_email_verified = await is_current_email_verified(conn, account["id"])
        context = {
            "user": user,
            "csrf": _ensure_csrf(request),
            "account_email": account["email"],
            "has_password": account["password_hash"] is not None,
            "can_sign_out_everywhere": not cfg.dev_no_auth,
            "account_email_verified": account_email_verified,
            "email_challenge_available": bool(
                getattr(cfg, "smtp_host", "") and getattr(cfg, "email_from", "")
                and security_link_base(getattr(cfg, "app_url", ""))
                and (account["password_hash"] is not None or oidc_configured)
                and not cfg.dev_no_auth
            ),
            "oidc_configured": oidc_configured,
            "linked_identity": linked_identity,
            "avatar_max_bytes": avatar_max_bytes,
            "avatar_max_label": _human_size(avatar_max_bytes),
            "error": error,
            "success": success,
            "method_notice": success if success in (OIDC_REAUTH_NOTICE, PASSWORD_SAVED_NOTICE) else None,
        }
        if include_review_count:
            return await render_page(
                request, "account_security.html", context, status_code=status_code
            )
        return request.app.state.templates.TemplateResponse(
            request, "account_security.html", context, status_code=status_code
        )

    async def _render_establish(
        request: Request,
        user: dict,
        *,
        error: str | None = None,
        status_code: int = 200,
        include_review_count: bool = False,
    ):
        legacy = request.session.get("legacy_oidc")
        reported_email = legacy.get("email") if isinstance(legacy, Mapping) else None
        email = normalize_email(reported_email) if isinstance(reported_email, str) else ""
        if not valid_email(email):
            email = ""
        context = {
            "user": user,
            "csrf": _ensure_csrf(request),
            "email": email,
            "error": error,
            "display_timezone": str(request.app.state.config.display_tz),
        }
        return request.app.state.templates.TemplateResponse(
            request, "establish_account.html", context, status_code=status_code
        )

    async def _render_rejection(
        request: Request, user: dict, rejected: _AccountActionRejected
    ):
        return await _render_account(
            request,
            rejected.account,
            user,
            error=rejected.error,
            status_code=rejected.status_code,
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

        async with control_connection(request.app.state.control_pool) as conn:
            account = await get_account_by_email(conn, normalize_email(email))

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

    @router.get("/invite")
    async def invite_page(request: Request):
        if request.app.state.config.dev_no_auth:
            raise HTTPException(status_code=404)
        return await _render_invite(request)

    @router.post("/invite")
    async def invite_submit(request: Request):
        if request.app.state.config.dev_no_auth:
            raise HTTPException(status_code=404)
        limiter: FailedAuthLimiter = request.app.state.login_limiter
        ip = client_ip(request)
        if limiter.blocked(ip):
            return await _render_invite(request, error=GENERIC_INVITE_ERROR, status_code=429)

        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            limiter.record_failure(ip)
            return await _render_invite(request, error=GENERIC_INVITE_ERROR, status_code=400)
        body = await _read_capped_body(request, MAX_INVITE_FORM_BYTES)
        if body is None:
            limiter.record_failure(ip)
            return await _render_invite(request, error=GENERIC_INVITE_ERROR, status_code=413)
        try:
            fields = parse_qs(body.decode("utf-8"), keep_blank_values=True,
                              strict_parsing=True, max_num_fields=5)
            if set(fields) != {"token", "password", "password_confirm", "display_timezone", "csrf_token"}:
                raise ValueError("invalid fields")
            if any(len(values) != 1 for values in fields.values()):
                raise ValueError("duplicate fields")
            values = {name: items[0] for name, items in fields.items()}
        except (UnicodeDecodeError, ValueError):
            limiter.record_failure(ip)
            return await _render_invite(request, error=GENERIC_INVITE_ERROR, status_code=400)

        check_form_csrf(request, values["csrf_token"])
        token = values["token"]
        password = values["password"]
        timezone_name = values["display_timezone"].strip()
        if not token or not token.isascii() or len(token) > 256:
            limiter.record_failure(ip)
            return await _render_invite(request, error=GENERIC_INVITE_ERROR, status_code=400)
        error = _new_password_error(password, values["password_confirm"])
        try:
            ZoneInfo(timezone_name)
        except (ValueError, ZoneInfoNotFoundError):
            error = "Choose a valid time zone, such as America/Los_Angeles or UTC."
        if error:
            limiter.record_failure(ip)
            return await _render_invite(request, error=error, status_code=400)

        async def provision():
            try:
                async with control_connection(request.app.state.control_pool) as conn:
                    async with conn.transaction():
                        account_id = await redeem_invitation(
                            conn, token, password, display_timezone=timezone_name
                        )
                        account = await get_account(conn, account_id)
                        if account is None or not account["is_enabled"]:
                            raise InvitationUnavailable()
                return account
            except InvitationUnavailable:
                # A cancelled HTTP request must still count a failed redemption.
                limiter.record_failure(ip)
                raise

        try:
            account = await limiter.run_bounded(provision)
        except _AuthSaturated:
            return await _render_invite(request, error=GENERIC_INVITE_ERROR, status_code=429)
        except InvitationUnavailable:
            return await _render_invite(request, error=GENERIC_INVITE_ERROR, status_code=400)

        _set_account_session(request, account)
        return RedirectResponse("/", status_code=303)

    @router.post("/invite/oidc")
    async def invite_oidc(request: Request):
        _require_oidc_enabled(request)
        limiter: FailedAuthLimiter = request.app.state.login_limiter
        ip = client_ip(request)
        if limiter.blocked(ip):
            return await _render_invite(request, error=GENERIC_INVITE_ERROR, status_code=429)
        try:
            values = await _email_form(request, {"token", "display_timezone", "csrf_token"})
        except HTTPException:
            limiter.record_failure(ip)
            return await _render_invite(request, error=GENERIC_INVITE_ERROR, status_code=400)
        check_form_csrf(request, values["csrf_token"])
        try:
            token = values["token"]
            timezone_name = values["display_timezone"].strip()
            if not token or not token.isascii() or len(token) > 256:
                raise ValueError("invalid invite token")
            ZoneInfo(timezone_name)
            return await _oidc_protected_redirect(
                request, action="invite", invite_token=token,
                timezone_name=timezone_name,
            )
        except (ValueError, ZoneInfoNotFoundError, HTTPException):
            limiter.record_failure(ip)
            return await _render_invite(request, error=GENERIC_INVITE_ERROR, status_code=400)

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
        display_timezone: str = Form(""),
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
        timezone_name = display_timezone.strip() or str(request.app.state.config.display_tz)
        try:
            ZoneInfo(timezone_name)
        except (ValueError, ZoneInfoNotFoundError):
            error = "Choose a valid time zone, such as America/Los_Angeles or UTC."
        if error:
            limiter.record_failure(ip)
            return await _render_signup(
                request, error=error, status_code=400
            )

        password_hash = await asyncio.to_thread(hash_password, password)
        try:
            async with control_connection(request.app.state.control_pool) as conn:
                account = await create_admin(
                    conn, email_norm, password_hash, display_timezone=timezone_name
                )
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
        return await _oidc_authorize_redirect(request)

    @router.get("/auth/callback", name="auth_callback")
    async def auth_callback(request: Request):
        _require_oidc_enabled(request)

        callback_state = request.query_params.get("state") or ""
        is_link_callback = callback_state.startswith(OIDC_LINK_STATE_PREFIX)
        is_login_callback = callback_state.startswith(OIDC_LOGIN_STATE_PREFIX)
        is_invite_callback = callback_state.startswith(OIDC_INVITE_STATE_PREFIX)
        is_reauth_callback = callback_state.startswith(OIDC_REAUTH_STATE_PREFIX)
        if not (is_link_callback or is_invite_callback or is_reauth_callback) and not await _oidc_login_available(request):
            raise HTTPException(status_code=404)

        limiter: FailedAuthLimiter = request.app.state.login_limiter
        ip = client_ip(request)
        if limiter.blocked(ip):
            return Response(status_code=429)
        if not (is_link_callback or is_login_callback or is_invite_callback or is_reauth_callback):
            limiter.record_failure(ip)
            raise HTTPException(status_code=401, detail=GENERIC_OIDC_ERROR)
        if is_login_callback and OIDC_PROTECTED_ATTEMPT_KEY in request.session:
            limiter.record_failure(ip)
            raise HTTPException(status_code=401, detail=GENERIC_OIDC_ERROR)

        protected_action = (
            "link" if is_link_callback else "invite" if is_invite_callback
            else "reauth" if is_reauth_callback else None
        )
        protected_attempt = None
        pending = None
        if protected_action is not None:
            protected_attempt = request.session.get(OIDC_PROTECTED_ATTEMPT_KEY)
            if (not isinstance(protected_attempt, Mapping)
                or protected_attempt.get("action") != protected_action
                or protected_attempt.get("state") != callback_state
                or not isinstance(protected_attempt.get("nonce"), str)
                or not isinstance(protected_attempt.get("browser_nonce"), str)):
                limiter.record_failure(ip)
                raise HTTPException(status_code=401, detail=GENERIC_OIDC_ERROR)
            request.session.pop(OIDC_PROTECTED_ATTEMPT_KEY, None)
            if protected_action in ("link", "reauth"):
                try:
                    actor = await require_user(request)
                except (AuthRedirect, HTTPException):
                    limiter.record_failure(ip)
                    raise HTTPException(status_code=401, detail=GENERIC_OIDC_ERROR)
                if (actor["id"] != protected_attempt.get("account_id")
                    or request.state.principal.auth_version != protected_attempt.get("auth_version")):
                    limiter.record_failure(ip)
                    raise HTTPException(status_code=401, detail=GENERIC_OIDC_ERROR)
            else:
                if "account_id" in request.session:
                    limiter.record_failure(ip)
                    raise HTTPException(status_code=401, detail=GENERIC_OIDC_ERROR)
            if protected_action != "reauth":
                async with control_connection(request.app.state.control_pool) as conn:
                    pending = await consume_oidc_attempt(
                        conn, action=protected_action, state=callback_state,
                        nonce=protected_attempt["nonce"],
                        browser_nonce=protected_attempt["browser_nonce"],
                        account_id=protected_attempt.get("account_id"),
                        auth_version=protected_attempt.get("auth_version"),
                    )
                if pending is None:
                    limiter.record_failure(ip)
                    raise HTTPException(status_code=401, detail=GENERIC_OIDC_ERROR)

        oauth = request.app.state.oauth
        try:
            token = await oauth.pocketid.authorize_access_token(request)
        except OAuthError as exc:
            if protected_action == "reauth":
                async with control_connection(request.app.state.control_pool) as conn:
                    await consume_oidc_attempt(
                        conn, action="reauth", state=callback_state,
                        nonce=protected_attempt["nonce"],
                        browser_nonce=protected_attempt["browser_nonce"],
                        account_id=protected_attempt["account_id"],
                        auth_version=protected_attempt["auth_version"],
                    )
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
            if protected_action == "reauth":
                async with control_connection(request.app.state.control_pool) as conn:
                    await consume_oidc_attempt(
                        conn, action="reauth", state=callback_state,
                        nonce=protected_attempt["nonce"],
                        browser_nonce=protected_attempt["browser_nonce"],
                        account_id=protected_attempt["account_id"],
                        auth_version=protected_attempt["auth_version"],
                    )
            limiter.record_failure(ip)
            raise HTTPException(status_code=401, detail=GENERIC_OIDC_ERROR)
        provider_email, display_name = _safe_oidc_metadata(userinfo)
        cfg = request.app.state.config
        issuer = normalize_issuer(cfg.oidc_issuer)

        if protected_action == "reauth":
            auth_time = userinfo.get("auth_time")
            if type(auth_time) not in (int, float):
                async with control_connection(request.app.state.control_pool) as conn:
                    await consume_oidc_attempt(
                        conn, action="reauth", state=callback_state,
                        nonce=protected_attempt["nonce"],
                        browser_nonce=protected_attempt["browser_nonce"],
                        account_id=protected_attempt["account_id"],
                        auth_version=protected_attempt["auth_version"],
                    )
                limiter.record_failure(ip)
                raise HTTPException(status_code=401, detail=GENERIC_OIDC_ERROR)
            async with control_connection(request.app.state.control_pool) as conn:
                verified = await finish_oidc_reauth(
                    conn, state=callback_state, nonce=protected_attempt["nonce"],
                    browser_nonce=protected_attempt["browser_nonce"],
                    account_id=protected_attempt["account_id"],
                    auth_version=protected_attempt["auth_version"],
                    issuer=issuer, subject=subject, auth_time=auth_time,
                )
            if not verified:
                limiter.record_failure(ip)
                raise HTTPException(status_code=401, detail=GENERIC_OIDC_ERROR)
            request.session["oidc_action_proof_nonce"] = protected_attempt["browser_nonce"]
            request.session["account_notice"] = OIDC_REAUTH_NOTICE
            return RedirectResponse("/settings/account", status_code=303)

        if protected_action == "invite":
            try:
                async with control_connection(request.app.state.control_pool) as conn:
                    async with conn.transaction():
                        account_id = await redeem_oidc_invitation_by_digest(
                            conn, pending["invitation_digest"], issuer, subject,
                            provider_email=provider_email,
                            provider_display_name=display_name,
                            display_timezone=pending["target"],
                        )
                        account = await get_account(conn, account_id)
                        if account is None or not account["is_enabled"]:
                            raise InvitationUnavailable()
            except InvitationUnavailable:
                limiter.record_failure(ip)
                raise HTTPException(status_code=401, detail=GENERIC_INVITE_ERROR)
            _set_account_session(request, account)
            return RedirectResponse("/", status_code=303)

        if protected_action == "link":
            async with control_connection(request.app.state.control_pool) as conn:
                linked = await create_identity_link(
                    conn,
                    pending["account_id"],
                    issuer,
                    subject,
                    provider_email=provider_email,
                    provider_display_name=display_name,
                    expected_auth_version=pending["auth_version"],
                )
            if linked is None:
                raise HTTPException(status_code=409, detail=GENERIC_LINK_ERROR)
            _set_account_session(request, linked)
            request.session["account_notice"] = "Sign-in provider linked."
            return RedirectResponse("/settings/account", status_code=303)

        async with control_connection(request.app.state.control_pool) as conn:
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

        async with control_connection(request.app.state.control_pool) as conn:
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
        request: Request, user: dict = Depends(require_legacy_establishment)
    ):
        if not user["legacy_oidc"]:
            raise HTTPException(status_code=404)
        return await _render_establish(request, user, include_review_count=True)

    @router.post("/account/establish")
    async def establish_account(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        password_confirm: str = Form(...),
        csrf_token: str = Form(...),
        display_timezone: str = Form(""),
        user: dict = Depends(require_legacy_establishment),
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
        timezone_name = display_timezone.strip() or str(request.app.state.config.display_tz)
        try:
            ZoneInfo(timezone_name)
        except (ValueError, ZoneInfoNotFoundError):
            error = "Choose a valid time zone, such as America/Los_Angeles or UTC."
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
            async with control_connection(request.app.state.control_pool) as conn:
                account, _identity = await establish_legacy_admin_identity(
                    conn,
                    email=email_norm,
                    password_hash=password_hash,
                    display_timezone=timezone_name,
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
        user: dict = Depends(require_user),
    ):
        _require_oidc_enabled(request)
        check_form_csrf(request, csrf_token)
        try:
            account = await _verified_account(
                request, user, current_password, generic_error=GENERIC_LINK_ERROR
            )
        except _AccountActionRejected as rejected:
            return await _render_rejection(request, user, rejected)
        async with control_connection(request.app.state.control_pool) as conn:
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
        return await _oidc_protected_redirect(request, action="link", account=account)

    @router.post("/settings/account/oidc/reauth")
    async def oidc_reauth(
        request: Request,
        action: str = Form(...),
        target: str = Form(""),
        target_confirm: str = Form(""),
        csrf_token: str = Form(...),
        user: dict = Depends(require_user),
    ):
        _require_oidc_enabled(request)
        check_form_csrf(request, csrf_token)
        limiter: FailedAuthLimiter = request.app.state.login_limiter
        if limiter.blocked(client_ip(request)):
            return await _render_account_unavailable(request, user, status_code=429)
        async with control_connection(request.app.state.control_pool) as conn:
            account = await get_account(conn, user["id"])
            identity = await get_identity_for_account(
                conn, user["id"], request.app.state.config.oidc_issuer
            )
        if (account is None or account["password_hash"] is not None
            or identity is None or action not in (PURPOSE_CURRENT, PURPOSE_CHANGE, "add_password")):
            raise HTTPException(status_code=403, detail=GENERIC_OIDC_ERROR)
        if action == PURPOSE_CURRENT:
            exact_target = account["email"]
        elif action == PURPOSE_CHANGE:
            exact_target = normalize_email(target)
            if (exact_target != normalize_email(target_confirm)
                or not _safe_delivery_email(exact_target)
                or exact_target == account["email"]):
                return await _render_account(request, account, user, error=GENERIC_EMAIL_ERROR, status_code=400)
        else:
            exact_target = ""
        return await _oidc_protected_redirect(
            request, action="reauth", account=account,
            proof_action=action, target=exact_target,
        )

    @router.post("/settings/account/oidc/unlink")
    async def unlink_oidc(
        request: Request,
        current_password: str = Form(...),
        csrf_token: str = Form(...),
        confirm_unlink: str | None = Form(None),
        user: dict = Depends(require_user),
    ):
        _require_oidc_enabled(request)
        check_form_csrf(request, csrf_token)
        try:
            account = await _verified_account(
                request,
                user,
                current_password,
                generic_error=GENERIC_UNLINK_ERROR,
                precheck_error=(
                    GENERIC_UNLINK_ERROR if confirm_unlink != "yes" else None
                ),
            )
        except _AccountActionRejected as rejected:
            return await _render_rejection(request, user, rejected)
        async with control_connection(request.app.state.control_pool) as conn:
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
                    expected_auth_version=request.state.principal.auth_version,
                )
        if updated is None:
            return await _render_account(
                request,
                account,
                user,
                error=GENERIC_UNLINK_ERROR,
                status_code=409,
            )
        _set_account_session(request, updated)
        request.session["account_notice"] = "Sign-in provider unlinked. Other sessions have been signed out."
        return RedirectResponse("/settings/account", status_code=303)

    @router.get("/settings/account")
    async def account_security(
        request: Request, user: dict = Depends(require_user)
    ):
        async with control_connection(request.app.state.control_pool) as conn:
            account = await get_account(conn, user["id"])
        if account is None:
            raise AuthRedirect()
        return await _render_account(request, account, user, include_review_count=True)

    async def _render_email_challenge(
        request: Request, user: dict | None, *, error: str | None = None,
        success: str | None = None, status_code: int = 200,
    ):
        return request.app.state.templates.TemplateResponse(
            request, "email_challenge_confirm.html",
            {"user": user, "csrf": _ensure_csrf(request), "error": error, "success": success},
            status_code=status_code,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    async def _request_email_challenge(
        request: Request, user: dict, values: dict[str, str], purpose: str,
    ):
        check_form_csrf(request, values["csrf_token"])
        cfg = request.app.state.config
        link_base = security_link_base(cfg.app_url)
        if not (cfg.smtp_host and cfg.email_from and link_base) or cfg.dev_no_auth:
            return await _render_account_unavailable(request, user)
        limiter: FailedAuthLimiter = request.app.state.login_limiter
        async with control_connection(request.app.state.control_pool) as conn:
            account = await get_account(conn, user["id"])
        if account is None:
            request.session.clear()
            raise AuthRedirect()
        if account["password_hash"] is not None:
            try:
                account = await limiter.run_bounded(lambda: _verified_account(
                    request, user, values["current_password"], generic_error=GENERIC_EMAIL_ERROR
                ))
            except _AuthSaturated:
                return await _render_account_unavailable(request, user, status_code=503)
            except _AccountActionRejected as rejected:
                return await _render_rejection(request, user, rejected)
        elif limiter.blocked(client_ip(request)):
            return await _render_account_unavailable(request, user, status_code=429)
        target = account["email"] if purpose == PURPOSE_CURRENT else normalize_email(values["new_email"])
        if purpose == PURPOSE_CHANGE and target != normalize_email(values["new_email_confirm"]):
            return await _render_account(request, account, user, error="Email addresses do not match.", status_code=400)
        if not _safe_delivery_email(target):
            return await _render_account(request, account, user, error="Enter a valid email address.", status_code=400)
        if purpose == PURPOSE_CHANGE and target == account["email"]:
            return await _render_account(request, account, user, error="Enter a different email address.", status_code=400)
        async with control_connection(request.app.state.control_pool) as conn:
            if account["password_hash"] is None:
                async with conn.transaction():
                    proof_nonce = request.session.pop("oidc_action_proof_nonce", None)
                    proven = bool(proof_nonce) and await consume_action_proof(
                        conn, account_id=account["id"],
                        auth_version=request.state.principal.auth_version,
                        action=purpose, target=target, browser_nonce=proof_nonce,
                    )
                    token = await issue_email_challenge(
                        conn, account["id"], request.state.principal.auth_version,
                        purpose, target,
                    ) if proven else None
            else:
                proven = True
                token = await issue_email_challenge(
                    conn, account["id"], request.state.principal.auth_version, purpose, target
                )
        if not proven:
            return await _render_account(request, account, user, error=GENERIC_EMAIL_ERROR, status_code=401)
        if token is not None:
            mailer = Mailer(
                cfg.smtp_host, cfg.smtp_port, cfg.smtp_username, cfg.smtp_password,
                cfg.smtp_security, cfg.smtp_tls_insecure, cfg.email_from, target,
            )
            link = f"{link_base}/settings/account/email/confirm#purpose={purpose}&token={token}"
            try:
                message = mailer.compose(
                    "Confirm your Odograph email address",
                    "To confirm your email address, open this link while signed in:\n"
                    f"{link}\n\nIf the link does not fill the form, choose {purpose} "
                    f"and enter this code manually: {token}\n\n"
                    "The code expires in 30 minutes. If you did not request this, ignore this email.",
                )
                await request.app.state.security_mail.send(mailer, message)
            except Exception:
                async with control_connection(request.app.state.control_pool) as conn:
                    await revoke_email_challenge(conn, account["id"], purpose, token)
                return await _render_account(request, account, user, error=GENERIC_EMAIL_ERROR, status_code=503)
        return await _render_account(request, account, user, success=EMAIL_REQUEST_NOTICE)

    async def _render_account_unavailable(request: Request, user: dict, *, status_code: int = 503):
        async with control_connection(request.app.state.control_pool) as conn:
            account = await get_account(conn, user["id"])
        if account is None:
            request.session.clear()
            raise AuthRedirect()
        return await _render_account(request, account, user, error=GENERIC_EMAIL_ERROR, status_code=status_code)

    @router.post("/settings/account/email/verify/request")
    async def request_current_email(request: Request, user: dict = Depends(require_user)):
        async with control_connection(request.app.state.control_pool) as conn:
            account = await get_account(conn, user["id"])
        if account is None:
            raise AuthRedirect()
        fields = {"current_password", "csrf_token"} if account["password_hash"] else {"csrf_token"}
        values = await _email_form(request, fields)
        return await _request_email_challenge(request, user, values, PURPOSE_CURRENT)

    @router.post("/settings/account/email/change/request")
    async def request_change_email(request: Request, user: dict = Depends(require_user)):
        async with control_connection(request.app.state.control_pool) as conn:
            account = await get_account(conn, user["id"])
        if account is None:
            raise AuthRedirect()
        fields = {"new_email", "new_email_confirm", "csrf_token"}
        if account["password_hash"]:
            fields.add("current_password")
        values = await _email_form(request, fields)
        return await _request_email_challenge(request, user, values, PURPOSE_CHANGE)

    @router.get("/settings/account/email/confirm")
    async def email_confirmation(request: Request):
        try:
            user = await require_user(request)
        except AuthRedirect:
            user = None
        return await _render_email_challenge(request, user)

    @router.post("/settings/account/email/confirm")
    async def confirm_email(request: Request, user: dict = Depends(require_user)):
        values = await _email_form(request, {"purpose", "token", "csrf_token"})
        check_form_csrf(request, values["csrf_token"])
        purpose, token = values["purpose"], values["token"]
        if purpose not in (PURPOSE_CURRENT, PURPOSE_CHANGE) or not token or len(token) > 256 or not token.isascii():
            return await _render_email_challenge(request, user, error=GENERIC_EMAIL_ERROR, status_code=400)
        limiter: FailedAuthLimiter = request.app.state.login_limiter
        ip = client_ip(request)
        if limiter.blocked(ip):
            return await _render_email_challenge(request, user, error=GENERIC_EMAIL_ERROR, status_code=429)
        async with control_connection(request.app.state.control_pool) as conn:
            account = await consume_email_challenge(
                conn, user["id"], request.state.principal.auth_version, purpose, token
            )
        if account is None:
            limiter.record_failure(ip)
            return await _render_email_challenge(request, user, error=GENERIC_EMAIL_ERROR, status_code=400)
        if purpose == PURPOSE_CHANGE:
            _set_account_session(request, account)
            user = _account_user(account)
        return await _render_email_challenge(request, user, success="Email address confirmed.")

    @router.get("/account/avatar")
    async def account_avatar(request: Request, user: dict = Depends(require_user)):
        async with control_connection(request.app.state.control_pool) as conn:
            avatar = await get_account_avatar(conn, user["id"])
        if avatar is None or avatar["avatar_mime"] is None:
            raise HTTPException(status_code=404)

        # Strong, byte-exact validator: avatar_bytes, avatar_mime, and
        # avatar_updated_at only ever change together (migrations/
        # 024_account_avatar.sql's all-or-nothing CHECK), so the same
        # microsecond-precision version _account_user hands out as
        # avatar_version alone identifies this exact image.
        etag = f'"{_avatar_version(avatar["avatar_updated_at"])}"'
        # "private" prevents shared caches leaking an account image across
        # users; "no-cache" makes a browser revalidate after a sign-out or
        # account change before it reuses its local copy.
        cache_control = "private, no-cache"

        if_none_match = request.headers.get("if-none-match")
        if if_none_match is not None and _if_none_match_matches(if_none_match, etag):
            return Response(
                status_code=304, headers={"Cache-Control": cache_control, "ETag": etag}
            )

        return Response(
            content=avatar["avatar_bytes"],
            media_type=avatar["avatar_mime"],
            headers={
                "Cache-Control": cache_control,
                "ETag": etag,
                # The SecurityHeadersMiddleware in app/main.py only sets this
                # on text/html responses, so an image response has to set it
                # itself rather than relying on that middleware.
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": "inline",
            },
        )

    @router.post("/settings/account/avatar")
    async def upload_avatar(request: Request, user: dict = Depends(require_user)):
        cfg = request.app.state.config
        if _avatar_upload_exceeds_limit(request):
            async with control_connection(request.app.state.control_pool) as conn:
                account = await get_account(conn, user["id"])
            if account is None:
                request.session.clear()
                raise AuthRedirect()
            return await _render_account(
                request,
                account,
                user,
                error=f"Avatar exceeds the {_human_size(cfg.account_avatar_max_bytes)} limit.",
                status_code=413,
            )

        # file/csrf_token are read from the parsed form by hand, not
        # declared as File()/Form() parameters on this function -- see
        # _avatar_upload_exceeds_limit's docstring for why that's load-
        # bearing rather than a style choice.
        form = await request.form()
        raw_csrf_token = form.get("csrf_token", "")
        csrf_token = raw_csrf_token if isinstance(raw_csrf_token, str) else ""
        check_form_csrf(request, csrf_token)

        async with control_connection(request.app.state.control_pool) as conn:
            account = await get_account(conn, user["id"])
        if account is None:
            request.session.clear()
            raise AuthRedirect()

        file = form.get("file")
        if not isinstance(file, UploadFile):
            return await _render_account(
                request, account, user,
                error="Choose an image file to upload.", status_code=422,
            )

        raw = await read_capped_upload(file, cfg.account_avatar_max_bytes)
        if raw is None:
            return await _render_account(
                request, account, user,
                error=f"Avatar exceeds the {_human_size(cfg.account_avatar_max_bytes)} limit.",
                status_code=413,
            )
        if not raw:
            return await _render_account(
                request, account, user,
                error="Choose an image file to upload.", status_code=422,
            )

        detection = _detect_avatar(raw)
        if detection.reason == REASON_TOO_LARGE:
            return await _render_account(
                request, account, user,
                error=(
                    "This image is too large. Avatars can be up to "
                    f"{MAX_AVATAR_PIXELS // 1_048_576} megapixels, with no side "
                    f"longer than {MAX_AVATAR_SIDE} pixels."
                ),
                status_code=422,
            )
        if detection.reason is not None:
            return await _render_account(
                request, account, user,
                error="Unsupported file type. Upload a PNG, JPEG, or WebP image.",
                status_code=422,
            )
        avatar_mime = detection.mime

        # Neither this route nor remove_avatar below re-verifies the current
        # password, unlike the OIDC link/unlink routes: a display image
        # doesn't change how this account authenticates, so there's nothing
        # to re-verify, and neither route bumps auth_version or signs out
        # other sessions.
        async with control_connection(request.app.state.control_pool) as conn:
            updated = await set_account_avatar(
                conn, account["id"], raw, avatar_mime,
                expected_auth_version=request.state.principal.auth_version,
            )
        if updated is None:
            request.session.clear()
            raise AuthRedirect()

        request.session["account_notice"] = "Avatar updated."
        return RedirectResponse("/settings/account", status_code=303)

    @router.post("/settings/account/avatar/remove")
    async def remove_avatar(
        request: Request,
        csrf_token: str = Form(...),
        confirm_remove: str | None = Form(None),
        user: dict = Depends(require_user),
    ):
        check_form_csrf(request, csrf_token)
        if confirm_remove != "yes":
            raise HTTPException(status_code=400, detail="Avatar removal not confirmed")
        # See upload_avatar above: no current-password re-verification here
        # either, for the same reason. Succeeds harmlessly whether or not an
        # avatar was set (clear_account_avatar clears all three columns
        # together either way).
        async with control_connection(request.app.state.control_pool) as conn:
            updated = await clear_account_avatar(
                conn, user["id"],
                expected_auth_version=request.state.principal.auth_version,
            )
        if updated is None:
            request.session.clear()
            raise AuthRedirect()

        request.session["account_notice"] = "Avatar removed."
        return RedirectResponse("/settings/account", status_code=303)

    @router.post("/settings/account/password")
    async def change_password(
        request: Request,
        current_password: str = Form(""),
        password: str = Form(...),
        password_confirm: str = Form(...),
        csrf_token: str = Form(...),
        user: dict = Depends(require_user),
    ):
        check_form_csrf(request, csrf_token)
        async with control_connection(request.app.state.control_pool) as conn:
            account = await get_account(conn, user["id"])
        if account is None:
            request.session.clear()
            raise AuthRedirect()
        limiter: FailedAuthLimiter = request.app.state.login_limiter
        ip = client_ip(request)
        if limiter.blocked(ip):
            return await _render_account(
                request, account, user, error="Too many failed attempts. Try again later.",
                status_code=429,
            )
        if account["password_hash"] is not None:
            try:
                account = await limiter.run_bounded(lambda: _verified_account(
                    request, user, current_password, generic_error=GENERIC_PASSWORD_ERROR
                ))
            except _AuthSaturated:
                return await _render_account(request, account, user, error=GENERIC_PASSWORD_ERROR, status_code=503)
            except _AccountActionRejected as rejected:
                return await _render_rejection(request, user, rejected)

        error = _new_password_error(password, password_confirm)
        if error:
            return await _render_account(
                request, account, user, error=error, status_code=400
            )

        proof_nonce = None
        if account["password_hash"] is None:
            proof_nonce = request.session.pop("oidc_action_proof_nonce", None)
            if not isinstance(proof_nonce, str) or not proof_nonce:
                limiter.record_failure(ip)
                return await _render_account(
                    request, account, user, error=GENERIC_PASSWORD_ERROR, status_code=401
                )

        async def save_password():
            async with control_connection(request.app.state.control_pool) as conn:
                async with conn.transaction():
                    if proof_nonce is not None:
                        proven = await consume_action_proof(
                            conn, account_id=account["id"],
                            auth_version=request.state.principal.auth_version,
                            action="add_password", target="", browser_nonce=proof_nonce,
                        )
                        if not proven:
                            limiter.record_failure(ip)
                            return None, False
                    password_hash = await asyncio.to_thread(hash_password, password)
                    updated = await replace_password(
                        conn, account["id"], password_hash,
                        expected_auth_version=request.state.principal.auth_version,
                    )
                    return updated, True

        try:
            updated, proven = await limiter.run_bounded(save_password)
        except _AuthSaturated:
            return await _render_account(request, account, user, error=GENERIC_PASSWORD_ERROR, status_code=503)
        if not proven:
            return await _render_account(request, account, user, error=GENERIC_PASSWORD_ERROR, status_code=401)
        if updated is None:
            request.session.clear()
            raise AuthRedirect()

        _set_account_session(request, updated)
        return await _render_account(
            request,
            updated,
            _account_user(updated),
            success=PASSWORD_SAVED_NOTICE,
        )

    async def _render_reset_page(request: Request, name: str, *, error: str | None = None,
                                 notice: str | None = None, status_code: int = 200):
        return request.app.state.templates.TemplateResponse(
            request, name,
            {"user": None, "csrf": _ensure_csrf(request), "error": error, "notice": notice,
             "password_reset_available": password_reset_available(request.app.state.config)},
            status_code=status_code,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @router.get("/forgot-password")
    async def forgot_password_page(request: Request):
        if request.app.state.config.dev_no_auth:
            raise HTTPException(status_code=404)
        return await _render_reset_page(request, "forgot_password.html")

    @router.post("/forgot-password")
    async def forgot_password(request: Request):
        """Same acknowledgement for unknown, disabled, unverified, limited and
        eligible accounts. Lookup and delivery happen after the reply."""
        cfg = request.app.state.config
        if cfg.dev_no_auth:
            raise HTTPException(status_code=404)
        values = await _email_form(request, {"email", "csrf_token"})
        check_form_csrf(request, values["csrf_token"])
        queue = getattr(request.app.state, "password_reset_queue", None)
        if queue is None or not password_reset_available(cfg):
            return await _render_reset_page(request, "forgot_password.html")
        email = normalize_email(values["email"])
        client_allowed = request.app.state.reset_request_client_limiter.allow(client_ip(request))
        identifier_allowed = (
            _safe_delivery_email(email)
            and request.app.state.reset_request_identifier_limiter.allow(email)
        )
        if client_allowed and identifier_allowed:
            queue.submit_public(email)
        return await _render_reset_page(request, "forgot_password.html", notice=RESET_REQUEST_NOTICE)

    @router.get("/reset-password")
    async def reset_password_page(request: Request):
        if request.app.state.config.dev_no_auth:
            raise HTTPException(status_code=404)
        return await _render_reset_page(request, "reset_password.html")

    @router.post("/reset-password")
    async def reset_password(request: Request):
        """Signed-out bearer-proof reset. It never signs the holder in."""
        if request.app.state.config.dev_no_auth:
            raise HTTPException(status_code=404)
        values = await _email_form(request, {"token", "password", "password_confirm", "csrf_token"})
        check_form_csrf(request, values["csrf_token"])
        if not request.app.state.reset_validation_limiter.allow(client_ip(request)):
            return await _render_reset_page(
                request, "reset_password.html", error=GENERIC_RESET_ERROR, status_code=429)
        token = values["token"].strip()
        if _challenge_digest(token) is None:
            return await _render_reset_page(
                request, "reset_password.html", error=GENERIC_RESET_ERROR, status_code=400)
        error = _new_password_error(values["password"], values["password_confirm"])
        if error:
            return await _render_reset_page(request, "reset_password.html", error=error, status_code=400)
        pool = request.app.state.control_pool

        async def replace():
            # Check the proof before paying for a password hash.
            async with control_connection(pool) as conn:
                if not await password_reset_usable(conn, token):
                    return None
            password_hash = await asyncio.to_thread(hash_password, values["password"])
            async with control_connection(pool) as conn:
                return await consume_password_reset(conn, token, password_hash)

        try:
            account_id = await request.app.state.login_limiter.run_bounded(replace)
        except _AuthSaturated:
            return await _render_reset_page(
                request, "reset_password.html", error=GENERIC_RESET_ERROR, status_code=429)
        if account_id is None:
            return await _render_reset_page(
                request, "reset_password.html", error=GENERIC_RESET_ERROR, status_code=400)
        request.session.clear()
        request.session["login_notice"] = RESET_COMPLETE_NOTICE
        # signed_out clears the shared cross-tab account marker (base.html).
        return RedirectResponse("/login?signed_out=1", status_code=303)

    @router.post("/settings/account/sign-out-everywhere", dependencies=[Depends(require_csrf)])
    async def sign_out_everywhere_route(
        request: Request, user: dict = Depends(require_user),
    ):
        if request.app.state.config.dev_no_auth:
            raise HTTPException(status_code=403)
        async with control_connection(request.app.state.control_pool) as conn:
            signed_out = await sign_out_everywhere(
                conn, user["id"],
                expected_auth_version=request.state.principal.auth_version,
            )
        request.session.clear()
        if not signed_out:
            raise AuthRedirect()
        if request.headers.get("HX-Request", "").lower() == "true":
            return Response(status_code=204, headers={"HX-Redirect": "/login?signed_out=1"})
        return RedirectResponse("/login?signed_out=1", status_code=303)

    @router.post("/logout", dependencies=[Depends(require_csrf)])
    async def logout(request: Request):
        request.session.clear()
        # The query marker (read by base.html's inline script, never by the
        # server) is how a real sign-out still clears the shared cross-tab
        # account marker, so every other signed-in tab still hides and
        # reloads -- merely landing on /login some other way must not.
        return Response(status_code=204, headers={"HX-Redirect": "/login?signed_out=1"})

    return router


def password_reset_available(cfg) -> bool:
    """Public reset needs SMTP delivery and a trusted link base. Notification
    EMAIL_TO is irrelevant: resets go only to the account's verified address."""
    return bool(
        not cfg.dev_no_auth and getattr(cfg, "smtp_host", "") and getattr(cfg, "email_from", "")
        and security_link_base(getattr(cfg, "app_url", ""))
    )


async def _signup_available(request: Request) -> bool:
    if not _signup_gate(request.app.state.config):
        return False
    async with control_connection(request.app.state.control_pool) as conn:
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
