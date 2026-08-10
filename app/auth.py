from __future__ import annotations

import asyncio
import hmac
import logging
import secrets

from authlib.integrations.base_client import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from psycopg import errors
from psycopg.rows import dict_row
from starlette.responses import RedirectResponse

from app.ingest import FailedAuthLimiter, client_ip
from app.local_auth import hash_password, sha256_hex, verify_password

log = logging.getLogger(__name__)

MIN_LOCAL_PASSWORD_LENGTH = 8


class AuthRedirect(Exception):
    """Raised by require_user; handled in main.py with a redirect to /login."""


def _ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def check_form_csrf(request: Request, token: str) -> None:
    """CSRF check for a plain (no-JS, no custom-header) <form> POST -- login,
    setup, and the portable data import form (app/portable.py) -- distinct
    from require_csrf's X-CSRF-Token header check, which htmx sets
    automatically but a bare HTML <form> POST cannot.
    """
    expected = request.session.get("csrf") or ""
    if not (expected and hmac.compare_digest(expected.encode("utf-8"), (token or "").encode("utf-8"))):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")


def require_user(request: Request) -> dict:
    cfg = request.app.state.config
    if cfg.dev_no_auth:
        request.session.setdefault("user", {"sub": "dev", "name": "dev (auth disabled)"})
        _ensure_csrf(request)
        return request.session["user"]
    user = request.session.get("user")
    if not user:
        raise AuthRedirect()
    return user


def require_csrf(request: Request) -> None:
    expected = request.session.get("csrf") or ""
    provided = request.headers.get("x-csrf-token") or ""
    if not (expected and hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")


def build_oauth(config) -> OAuth | None:
    if config.dev_no_auth:
        log.warning("DEV_NO_AUTH=1: UI authentication is DISABLED. Never deploy like this.")
        return None
    if not config.oidc_configured:
        # OIDC is optional now (local-login mode) -- no client to register.
        return None
    oauth = OAuth()
    oauth.register(
        "pocketid",
        client_id=config.oidc_client_id,
        client_secret=config.oidc_client_secret,
        server_metadata_url=f"{config.oidc_issuer.rstrip('/')}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email"},
    )
    return oauth


async def _get_local_admin(conn) -> dict | None:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, email, password_hash, consumed_token_hash FROM local_admin WHERE id = 1"
    )
    return await cur.fetchone()


async def _oidc_authorize_redirect(request: Request):
    oauth = request.app.state.oauth
    redirect_uri = str(request.url_for("auth_callback"))
    return await oauth.pocketid.authorize_redirect(request, redirect_uri)


def make_router() -> APIRouter:
    router = APIRouter()

    async def _render_login(request: Request, *, error: str | None, status_code: int = 200):
        cfg = request.app.state.config
        oauth = request.app.state.oauth
        pool = request.app.state.pool
        async with pool.connection() as conn:
            admin = await _get_local_admin(conn)
        return request.app.state.templates.TemplateResponse(
            request, "login.html",
            {
                "user": None,
                "csrf": _ensure_csrf(request),
                "local_admin_exists": admin is not None,
                "oidc_available": oauth is not None,
                "setup_available": bool(cfg.admin_token),
                "error": error,
            },
            status_code=status_code,
        )

    async def _render_setup(request: Request, admin: dict | None, *, error: str | None, status_code: int = 200):
        return request.app.state.templates.TemplateResponse(
            request, "setup.html",
            {
                "user": None,
                "csrf": _ensure_csrf(request),
                "mode": "reset" if admin else "create",
                "admin_email": admin["email"] if admin else None,
                "error": error,
            },
            status_code=status_code,
        )

    @router.get("/login")
    async def login(request: Request):
        cfg = request.app.state.config
        if cfg.dev_no_auth:
            return RedirectResponse("/", status_code=303)
        return await _render_login(request, error=None)

    @router.get("/login/oidc", name="login_oidc")
    async def login_oidc(request: Request):
        # Reachable when OIDC is configured, so the rendered login page's
        # "Sign in with OIDC" link has somewhere to go -- plain GET /login
        # never auto-redirects to the provider.
        cfg = request.app.state.config
        oauth = request.app.state.oauth
        if cfg.dev_no_auth or oauth is None:
            raise HTTPException(status_code=404)
        return await _oidc_authorize_redirect(request)

    @router.post("/login/local")
    async def login_local(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        csrf_token: str = Form(...),
    ):
        cfg = request.app.state.config
        if cfg.dev_no_auth:
            raise HTTPException(status_code=404)
        check_form_csrf(request, csrf_token)

        limiter: FailedAuthLimiter = request.app.state.login_limiter
        ip = client_ip(request)
        if limiter.blocked(ip):
            return await _render_login(
                request, error="Too many failed attempts. Try again later.", status_code=429
            )

        pool = request.app.state.pool
        async with pool.connection() as conn:
            admin = await _get_local_admin(conn)

        email_norm = email.strip().lower()
        # Same generic failure regardless of *why* -- no admin row, wrong
        # email, or wrong password must all look identical to the caller.
        ok = (
            admin is not None
            and hmac.compare_digest(email_norm.encode("utf-8"), admin["email"].encode("utf-8"))
            and await asyncio.to_thread(verify_password, password, admin["password_hash"])
        )
        if not ok:
            limiter.record_failure(ip)
            return await _render_login(request, error="Invalid email or password.", status_code=401)

        # Session fixation defense: a fresh session (and CSRF token), not
        # just an updated `user` key in the pre-login one.
        request.session.clear()
        local_part = admin["email"].split("@", 1)[0]
        request.session["user"] = {"sub": "local:1", "name": local_part, "email": admin["email"]}
        _ensure_csrf(request)
        return RedirectResponse("/", status_code=303)

    @router.get("/setup")
    async def setup_page(request: Request):
        cfg = request.app.state.config
        if not cfg.admin_token:
            raise HTTPException(status_code=404)
        pool = request.app.state.pool
        async with pool.connection() as conn:
            admin = await _get_local_admin(conn)
        return await _render_setup(request, admin, error=None)

    @router.post("/setup")
    async def setup_submit(
        request: Request,
        token: str = Form(...),
        email: str = Form(""),
        password: str = Form(...),
        password_confirm: str = Form(...),
        csrf_token: str = Form(...),
    ):
        cfg = request.app.state.config
        if not cfg.admin_token:
            raise HTTPException(status_code=404)
        check_form_csrf(request, csrf_token)

        pool = request.app.state.pool
        async with pool.connection() as conn:
            admin = await _get_local_admin(conn)

        limiter: FailedAuthLimiter = request.app.state.login_limiter
        ip = client_ip(request)
        if limiter.blocked(ip):
            return await _render_setup(
                request, admin, error="Too many failed attempts. Try again later.", status_code=429
            )

        if not hmac.compare_digest(token.encode("utf-8"), cfg.admin_token.encode("utf-8")):
            limiter.record_failure(ip)
            return await _render_setup(request, admin, error="Invalid setup token.", status_code=401)

        # Correct token, but already spent -- explicit and distinct from the
        # generic wrong-token failure above (which also feeds the limiter;
        # this doesn't, since the token itself is genuinely valid).
        token_hash = sha256_hex(token)
        if admin is not None and hmac.compare_digest(token_hash, admin["consumed_token_hash"]):
            return await _render_setup(
                request, admin,
                error="This token was already used. Generate a fresh one.",
                status_code=400,
            )

        if password != password_confirm:
            return await _render_setup(request, admin, error="Passwords do not match.", status_code=400)
        if len(password) < MIN_LOCAL_PASSWORD_LENGTH:
            return await _render_setup(
                request, admin,
                error=f"Password must be at least {MIN_LOCAL_PASSWORD_LENGTH} characters.",
                status_code=400,
            )

        password_hash = await asyncio.to_thread(hash_password, password)

        if admin is None:
            email_norm = email.strip().lower()
            if not email_norm:
                return await _render_setup(request, admin, error="Email is required.", status_code=400)
            if not email_norm.isascii():
                return await _render_setup(
                    request, admin, error="Email must use ASCII characters.", status_code=400
                )
            try:
                async with pool.connection() as conn:
                    await conn.execute(
                        "INSERT INTO local_admin (id, email, password_hash, consumed_token_hash) "
                        "VALUES (1, %s, %s, %s)",
                        (email_norm, password_hash, token_hash),
                    )
            except errors.UniqueViolation:
                # Lost a create-vs-create race against a concurrent /setup
                # POST -- the single-row CHECK(id=1) constraint did its job.
                # Refuse rather than silently reset a password nobody asked
                # to reset.
                async with pool.connection() as conn:
                    admin = await _get_local_admin(conn)
                return await _render_setup(
                    request, admin,
                    error="An administrator was just created by another request. Refresh and use the reset form.",
                    status_code=409,
                )
        else:
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "UPDATE local_admin SET password_hash = %s, consumed_token_hash = %s,"
                    " updated_at = now() WHERE id = 1 AND consumed_token_hash <> %s"
                    " RETURNING id",
                    (password_hash, token_hash, token_hash),
                )
                if await cur.fetchone() is None:
                    admin = await _get_local_admin(conn)
                    return await _render_setup(
                        request, admin,
                        error="This token was already used. Generate a fresh one.",
                        status_code=400,
                    )

        return RedirectResponse("/login", status_code=303)

    @router.get("/auth/callback", name="auth_callback")
    async def auth_callback(request: Request):
        cfg = request.app.state.config
        oauth = request.app.state.oauth
        if cfg.dev_no_auth or oauth is None:
            raise HTTPException(status_code=404)

        # Checked before authorize_access_token, not after -- that call is a
        # real outbound token-exchange request to the identity provider, so a
        # blocked caller must never trigger one. Authlib's `state` check stops
        # a cold drive-by, but a scripted caller can loop "fetch a fresh
        # state-bearing session, then hand back a garbage code" indefinitely,
        # and each loop burns a request against the IdP from this app's own
        # egress address -- unmetered, that's a way to get the instance
        # throttled by its own provider. Shares login_limiter's ledger; see
        # app/main.py for why.
        limiter: FailedAuthLimiter = request.app.state.login_limiter
        ip = client_ip(request)
        if limiter.blocked(ip):
            return Response(status_code=429)

        try:
            token = await oauth.pocketid.authorize_access_token(request)
        except OAuthError as exc:
            # Specifically OAuthError, not a bare Exception: this is what
            # authlib raises for a mismatched/replayed `state` or a code the
            # provider itself rejected (invalid_grant and friends) -- exactly
            # the caller-supplied garbage this limiter exists to cap. A
            # transport failure reaching the provider, or the provider
            # itself being down, is not the caller's fault and must not
            # burn their ledger or masquerade as a credential failure; it's
            # deliberately left to propagate to the app's generic 500 so an
            # operator sees a diagnosable error instead of everyone getting
            # rate-limited during an IdP outage.
            limiter.record_failure(ip)
            # Type only: OAuthError can carry the provider's raw response,
            # which is untrusted input.
            log.warning("auth callback: token exchange rejected (%s)", type(exc).__name__)
            raise HTTPException(status_code=401, detail="Sign-in failed. Please try again.")

        userinfo = token.get("userinfo") or {}
        email = (userinfo.get("email") or "").strip().lower()

        if cfg.allowed_email and email != cfg.allowed_email:
            limiter.record_failure(ip)
            log.warning("Rejected OIDC login from unauthorized email: %s", email)
            raise HTTPException(status_code=403, detail="This instance is not configured for your account.")

        # Session fixation defense, mirroring login_local: a fresh session
        # (and CSRF token), not just an updated `user` key in the pre-login
        # one. Must come after authorize_access_token above, which reads the
        # OAuth state/nonce out of the pre-login session.
        request.session.clear()
        request.session["user"] = {
            "sub": userinfo.get("sub"),
            "name": userinfo.get("name") or userinfo.get("preferred_username"),
            "email": userinfo.get("email"),
        }
        _ensure_csrf(request)
        return RedirectResponse("/", status_code=303)

    @router.post("/logout", dependencies=[Depends(require_csrf)])
    async def logout(request: Request):
        # POST-only (a bare GET link was CSRF-able) and behind the same
        # X-CSRF-Token check as every other UI POST.
        # 204 + HX-Redirect rather than a 303: the request now comes from
        # htmx (base.html's logout form), and a 3xx here would just have
        # htmx swap the redirected page's body into the (about-to-vanish)
        # logout form instead of navigating the browser.
        request.session.clear()
        return Response(status_code=204, headers={"HX-Redirect": "/login"})

    return router
