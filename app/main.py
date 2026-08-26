from __future__ import annotations

import calendar
import logging
import pathlib
import secrets
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime

import httpx
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse, RedirectResponse
from starlette.staticfiles import StaticFiles

from app import auth, ingest, portable, ui
from app.auth import AuthRedirect
from app.config import (
    DEFAULT_MAP_TILE_ATTRIBUTION,
    DEFAULT_MAP_TILE_URL,
    DEFAULT_MISSING_TRIP_GAP_M,
    Config,
)
from app.dashboard import format_week_range
from app.db import make_pool, run_migrations
from app.detector.runner import DetectorRunner, DetectorScheduler
from app.email_digest import EmailDigestWorker
from app.expenses import comparison_caveat_lines, comparison_status
from app.formatting import format_duration, format_miles, format_usd
from app.geocode import GeocodeWorker
from app.ingest import FailedAuthLimiter
from app.mailer import Mailer
from app.missing_trip import missing_trip_badge
from app.nudge import NudgeWorker
from app.odometer import coverage_line
from app.odometer_reminder import OdometerReminderWorker
from app.places_desc import describe_compact_endpoint, describe_endpoint
from app.report import caveat_lines, format_rate_periods, quarter_bounds, range_label
from app.retention import RetentionWorker
from app.snap import SnapWorker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

# httpx logs each request's full URL at INFO by default, which would put a
# geocode provider's API key (a query param on every Geoapify call, per
# app/geocode.py) in plaintext in the logs -- secrets from .env must never
# be logged in full.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent


class RevalidatingStaticFiles(StaticFiles):
    """StaticFiles serves no Cache-Control at all by default, which makes
    browsers fall back to RFC 7234 heuristic caching -- a client can decide
    on its own, with no way for us to intervene, to treat a months-old
    style.css as fresh for hours or days after a deploy changes it. `no-cache`
    forces a conditional revalidation (an If-None-Match round trip) on every
    load instead, so a change is never more than one request away from being
    seen, at the cost of one cheap 304 per asset per load."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class SecurityHeadersMiddleware:
    """Emits CSP and the rest of the D2 header set on every HTML response, with
    a fresh per-request nonce that lets the inline <script> blocks run under a
    'self'-only script-src instead of 'unsafe-inline'. The nonce is stashed on
    ASGI scope state (shared with every Request built from this scope, per
    Starlette's Request.state) so a Jinja context processor can hand it to
    templates without every route threading it through by hand, the way csrf
    is today.

    style-src keeps 'unsafe-inline' deliberately: refactoring the ~39 inline
    style="" attributes across templates buys little here -- this app renders
    no third-party HTML, and style injection is not the risk this policy
    exists to stop.

    A pure ASGI middleware, not BaseHTTPMiddleware, so it never has to buffer
    or replay a streamed response body just to add headers.

    HSTS is opt-in via HSTS_MAX_AGE (0/unset = no header) and, even then, only
    ever emitted on a request the app already sees as HTTPS -- this app never
    terminates TLS itself, so a wrongly scoped max-age would be a proxy-level
    mistake this process can't see or undo.
    """

    def __init__(self, app, *, tile_host: str, hsts_max_age: int):
        self.app = app
        self.tile_host = tile_host
        self.hsts_max_age = hsts_max_age

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        nonce = secrets.token_urlsafe(16)
        scope.setdefault("state", {})["csp_nonce"] = nonce
        is_https = scope.get("scheme") == "https"

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                if headers.get("content-type", "").startswith("text/html"):
                    headers["Content-Security-Policy"] = (
                        f"script-src 'self' 'nonce-{nonce}'; "
                        "style-src 'self' 'unsafe-inline'; "
                        f"img-src 'self' data: {self.tile_host}; "
                        "connect-src 'self'; "
                        "frame-ancestors 'none'; "
                        "base-uri 'none'; "
                        "object-src 'none'; "
                        "form-action 'self'"
                    )
                    headers["X-Content-Type-Options"] = "nosniff"
                    # Origin only on cross-origin requests, so the tile host
                    # learns this deployment's origin but never the URL of the
                    # page being viewed. Deliberately not "same-origin": that
                    # strips the Referer header outright on cross-origin
                    # requests, and OpenStreetMap's tile servers answer a
                    # refererless request with a 403 error tile rather than
                    # the map, which is how it shipped broken once already.
                    headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
                    headers["Cross-Origin-Opener-Policy"] = "same-origin"
                    headers["Permissions-Policy"] = (
                        "geolocation=(), camera=(), microphone=(), payment=()"
                    )
                    if self.hsts_max_age and is_https:
                        headers["Strict-Transport-Security"] = f"max-age={self.hsts_max_age}"
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _csp_nonce_context(request: Request) -> dict:
    # getattr twice, not request.state.csp_nonce directly: a number of
    # route-level tests build a bare SimpleNamespace(app=..., session=...)
    # request double with no .state attribute at all (unlike a real Starlette
    # Request, which always has one), and this context processor now runs on
    # every TemplateResponse call, including theirs.
    state = getattr(request, "state", None)
    return {"csp_nonce": getattr(state, "csp_nonce", "")}


def make_templates(config: Config) -> Jinja2Templates:
    templates = Jinja2Templates(
        directory=str(BASE_DIR / "app" / "templates"),
        context_processors=[_csp_nonce_context],
    )
    tz = config.display_tz
    static_dir = BASE_DIR / "static"

    def local_dt(dt, fmt="%a %Y-%m-%d %H:%M"):
        return dt.astimezone(tz).strftime(fmt)

    def local_time(dt):
        return dt.astimezone(tz).strftime("%H:%M")

    def local_date(dt):
        return dt.astimezone(tz).strftime("%a %b %-d")

    def duration(trip):
        return format_duration(trip["started_at"], trip["ended_at"])

    def km(meters):
        return f"{meters / 1000.0:.1f}"

    def now_local():
        """Current instant in the display timezone, for prefilling date/time
        form inputs. Matches the tz the odometer/manual-trip routes stamp on
        the submitted naive date+time, so the default a user sees and the
        value the server stores agree.
        """
        return datetime.now(tz)

    templates.env.filters.update(
        local_dt=local_dt, local_time=local_time, local_date=local_date,
        duration=duration, km=km, mi=format_miles, usd=format_usd,
    )
    templates.env.globals["now_local"] = now_local
    templates.env.globals["display_tz"] = str(tz)
    # No getattr fallback here (unlike map_tile_url/missing_trip_gap_m below):
    # those degrade to a working default when absent, but a missing app
    # version in production would be a real misconfiguration worth an
    # AttributeError, not a silently blank footer.
    templates.env.globals["app_version"] = config.app_version
    templates.env.globals["describe_endpoint"] = describe_endpoint
    templates.env.globals["describe_compact_endpoint"] = describe_compact_endpoint
    templates.env.globals["format_rate_periods"] = format_rate_periods
    templates.env.globals["caveat_lines"] = caveat_lines
    templates.env.globals["quarter_bounds"] = quarter_bounds
    templates.env.globals["range_label"] = range_label
    templates.env.globals["format_week_range"] = format_week_range
    templates.env.globals["coverage_line"] = coverage_line
    templates.env.globals["comparison_status"] = comparison_status
    templates.env.globals["comparison_caveat_lines"] = comparison_caveat_lines
    templates.env.globals["month_abbr"] = list(calendar.month_abbr)  # ['', 'Jan', ..., 'Dec']
    # Same bare-SimpleNamespace-config fallback reasoning as missing_trip_gap_m
    # below -- the map templates are exercised by template-only tests too.
    templates.env.globals["map_tile_url"] = getattr(
        config, "map_tile_url", DEFAULT_MAP_TILE_URL
    )
    templates.env.globals["map_tile_attribution"] = getattr(
        config, "map_tile_attribution", DEFAULT_MAP_TILE_ATTRIBUTION,
    )
    # getattr, not config.missing_trip_gap_m directly: several template-only
    # tests build a bare SimpleNamespace(display_tz=...) config fake (no
    # other Config fields), and missing_trip_badge() already degrades to no
    # badge whenever a trip dict lacks the predecessor TRIP_COLUMNS keys
    # regardless of the threshold, so a fallback here can't mask a real bug.
    threshold_m = getattr(config, "missing_trip_gap_m", DEFAULT_MISSING_TRIP_GAP_M)
    templates.env.globals["missing_trip_badge"] = (
        lambda trip: missing_trip_badge(trip, threshold_m, tz)
    )
    # A query-string version, not the Cache-Control header alone, is what
    # actually unsticks a browser that cached style.css *before* this
    # value existed on a response -- that old cache entry's freshness was
    # decided at fetch time and won't re-check the server just because a
    # later deploy adds headers. Bumping the URL makes it a different
    # resource, forcing a fetch regardless of what was cached before.
    templates.env.globals["static_version"] = int((static_dir / "style.css").stat().st_mtime)
    return templates


def _make_worker_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # AsyncExitStack, not a bare try/finally after yield: if any startup
        # step below raises (a worker's start(), run_migrations, a bad
        # config), the pool and every resource already opened/started must
        # still be torn down in reverse order, the same as a normal
        # shutdown. Each push_async_callback is registered immediately after
        # its resource is successfully created, so a resource that never
        # came up is never torn down.
        async with AsyncExitStack() as stack:
            pool = make_pool(cfg.database_url)
            await pool.open(wait=True)
            stack.push_async_callback(pool.close)
            await run_migrations(pool)

            http_client = None
            snap_worker = None
            if cfg.snap_enabled:
                http_client = _make_worker_http_client()
                stack.push_async_callback(http_client.aclose)
                snap_worker = SnapWorker(
                    pool, http_client, cfg.osrm_url, cfg.osrm_min_confidence,
                    cfg.osrm_max_coords, cfg.snap_debounce_s, cfg.snap_sweep_s,
                )
                await snap_worker.start()
                stack.push_async_callback(snap_worker.stop)

            geocode_http_client = None
            geocode_worker = None
            geocode_provider = cfg.geocode_provider
            if geocode_provider is not None:
                geocode_http_client = _make_worker_http_client()
                stack.push_async_callback(geocode_http_client.aclose)
                geocode_worker = GeocodeWorker(
                    pool, geocode_http_client, geocode_provider, cfg.geocode_min_interval_s,
                    cfg.geocode_debounce_s, cfg.geocode_sweep_s,
                )
                await geocode_worker.start()
                stack.push_async_callback(geocode_worker.stop)

            runner = DetectorRunner(
                pool, cfg.detector_params, cfg.full_reprocess_warn_points
            )
            scheduler = DetectorScheduler(
                runner, cfg.detect_debounce_s, cfg.detect_sweep_s,
                snap_worker=snap_worker, geocode_worker=geocode_worker,
            )

            retention_worker = None
            if cfg.retention_enabled:
                retention_worker = RetentionWorker(pool, cfg.raw_message_retention_days)
                await retention_worker.start()
                stack.push_async_callback(retention_worker.stop)

            nudge_http_client = None
            nudge_worker = None
            if cfg.nudge_enabled:
                nudge_http_client = _make_worker_http_client()
                stack.push_async_callback(nudge_http_client.aclose)
                nudge_worker = NudgeWorker(
                    pool, nudge_http_client, cfg.ntfy_url, cfg.ntfy_topic, cfg.ntfy_token,
                    cfg.ntfy_username, cfg.ntfy_password,
                    cfg.app_url, cfg.display_tz, cfg.nudge_weekly_hour,
                )
                await nudge_worker.start()
                stack.push_async_callback(nudge_worker.stop)
                log.info("nudge worker enabled")

            odometer_reminder_http_client = None
            odometer_reminder_worker = None
            if cfg.odometer_reminder_enabled:
                odometer_reminder_http_client = _make_worker_http_client()
                stack.push_async_callback(odometer_reminder_http_client.aclose)
                odometer_reminder_worker = OdometerReminderWorker(
                    pool, odometer_reminder_http_client, cfg.ntfy_url, cfg.ntfy_topic, cfg.ntfy_token,
                    cfg.ntfy_username, cfg.ntfy_password,
                    cfg.app_url, cfg.display_tz, cfg.odometer_reminder_hour,
                )
                await odometer_reminder_worker.start()
                stack.push_async_callback(odometer_reminder_worker.stop)
                log.info("odometer reminder worker enabled")

            email_digest_worker = None
            if cfg.email_enabled:
                mailer = Mailer(
                    cfg.smtp_host, cfg.smtp_port, cfg.smtp_username, cfg.smtp_password,
                    cfg.smtp_security, cfg.smtp_tls_insecure, cfg.email_from, cfg.email_to,
                )
                email_digest_worker = EmailDigestWorker(
                    pool, mailer, cfg.app_url, cfg.display_tz,
                    cfg.nudge_weekly_hour, cfg.odometer_reminder_hour, cfg.email_digest_hour,
                    cfg.email_filing_reminder_mmdd,
                    cfg.email_weekly_nudge, cfg.email_monthly_summary,
                    cfg.email_filing_reminder, cfg.email_odometer_reminder,
                )
                await email_digest_worker.start()
                stack.push_async_callback(email_digest_worker.stop)
                log.info("email digest worker enabled")

            app.state.pool = pool
            app.state.detector_runner = runner
            app.state.detector_scheduler = scheduler
            app.state.snap_worker = snap_worker
            # Reused directly (not just by SnapWorker) for the missing-trip
            # OSRM `/route` suggestion (app/ui.py) -- same pattern as
            # geocode_http_client below, which /places/search already calls
            # on-demand outside its worker.
            app.state.osrm_http_client = http_client
            app.state.geocode_http_client = geocode_http_client
            # Needed for its WorkerStatus (app/worker.py), read by the
            # diagnostics report builder (app/diagnose.py) -- previously only
            # its http client was kept on app.state.
            app.state.geocode_worker = geocode_worker
            app.state.retention_worker = retention_worker
            app.state.nudge_worker = nudge_worker
            app.state.odometer_reminder_worker = odometer_reminder_worker
            app.state.email_digest_worker = email_digest_worker
            await scheduler.start()
            # Registered last, so it stops first on the way out -- LIFO
            # matches the pre-AsyncExitStack teardown order, where the
            # scheduler always stopped before any other worker.
            stack.push_async_callback(scheduler.stop)
            yield

    # FastAPI's /docs and openapi.json disabled: they'd be reachable without OIDC
    app = FastAPI(
        title="odograph", lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    app.state.config = cfg
    app.state.templates = make_templates(cfg)
    app.state.oauth = auth.build_oauth(cfg)
    app.state.ingest_limiter = FailedAuthLimiter(
        cfg.ingest_auth_max_failures, cfg.ingest_auth_window_s
    )
    # Shared by local credential checks and /auth/callback (app/auth.py), since
    # each is the same "unauthenticated caller feeding the app plausible-looking
    # credentials" surface. The
    # callback's failure is a rejected/garbage OIDC code rather than a wrong
    # password, but the risk it caps is worse than a wasted login attempt:
    # every check counts as a real outbound token-exchange request to the
    # identity provider from this app's own egress address, so an unmetered
    # caller could loop it indefinitely and get the instance's address
    # throttled by its own IdP.
    app.state.login_limiter = FailedAuthLimiter(
        cfg.login_auth_max_failures, cfg.login_auth_window_s
    )

    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.session_secret,
        same_site="lax",
        https_only=not cfg.dev_no_auth,
    )
    # Added after SessionMiddleware so it's the outermost user middleware
    # (Starlette's add_middleware prepends -- the most recently added wraps
    # everything else): the nonce lands in scope state before routing/session
    # handling runs, and the response headers get set last, after any cookie
    # the session sets.
    app.add_middleware(
        SecurityHeadersMiddleware,
        tile_host=cfg.map_tile_host,
        hsts_max_age=cfg.hsts_max_age,
    )

    @app.exception_handler(AuthRedirect)
    async def _auth_redirect(request: Request, exc: AuthRedirect):
        return RedirectResponse("/login", status_code=303)

    @app.get("/healthz")
    async def healthz(request: Request):
        async with request.app.state.pool.connection() as conn:
            await conn.execute("SELECT 1")
        return JSONResponse({"ok": True})

    app.include_router(ingest.make_router())
    app.include_router(auth.make_router())
    app.include_router(ui.make_router())
    app.include_router(portable.make_router())
    app.mount("/static", RevalidatingStaticFiles(directory=str(BASE_DIR / "static")), name="static")

    return app
