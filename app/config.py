from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from app.detector.core import Params
from app.geocode import GeocodeProvider, build_geocode_provider, resolve_geocode_provider_name

log = logging.getLogger(__name__)

DEFAULT_MAP_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
DEFAULT_MAP_TILE_ATTRIBUTION = (
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
)
DEFAULT_MISSING_TRIP_GAP_M = 1000.0
# Single source of truth for the avatar upload cap: also read back by
# app/auth.py as the getattr fallback for test doubles whose config double
# predates this field.
DEFAULT_ACCOUNT_AVATAR_MAX_BYTES = 512000


def security_link_base(app_url: str) -> str:
    """Return APP_URL when it is a usable base for emailed security links.

    Security links are built only from configuration, never from a request's
    Host or forwarded headers. The base must be an absolute HTTP(S) URL with
    an authority and no userinfo, query or fragment; anything else disables
    those links rather than guessing.
    """
    if not isinstance(app_url, str) or not app_url.isascii() or any(
        ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in app_url
    ):
        return ""
    try:
        parsed = urlsplit(app_url)
        parsed.port  # Raises on a malformed port.
    except ValueError:
        return ""
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or "@" in parsed.netloc or "?" in app_url or "#" in app_url):
        return ""
    return app_url.rstrip("/")


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass
class Config:
    database_url: str
    app_version: str
    app_git_revision: str
    ingest_username: str
    ingest_password: str
    session_secret: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str
    allowed_email: str
    initial_admin_signup: bool
    login_auth_max_failures: int
    login_auth_window_s: float
    display_tz: ZoneInfo
    dev_no_auth: bool
    detector_params: Params
    detect_debounce_s: float
    detect_sweep_s: float
    ingest_auth_max_failures: int
    ingest_auth_window_s: float
    ingest_max_body_bytes: int
    osrm_url: str
    osrm_min_confidence: float
    osrm_max_coords: int
    snap_debounce_s: float
    snap_sweep_s: float
    geocode_api_key: str
    geocode_provider_name: str
    geocode_nominatim_url: str
    geocode_omit_country: str
    geocode_min_interval_s: float
    geocode_debounce_s: float
    geocode_sweep_s: float
    raw_message_retention_days: float
    ntfy_url: str
    ntfy_topic: str
    ntfy_token: str
    ntfy_username: str
    ntfy_password: str
    app_url: str
    nudge_weekly_hour: int
    odometer_reminder_requested: bool
    odometer_reminder_hour: int
    trips_page_size: int
    full_reprocess_warn_points: int
    missing_trip_gap_m: float
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_security: str
    smtp_tls_insecure: bool
    email_from: str
    email_to: str
    email_weekly_nudge: bool
    email_monthly_summary: bool
    email_filing_reminder: bool
    email_odometer_reminder: bool
    email_digest_hour: int
    email_filing_reminder_mmdd: str
    map_tile_url: str
    map_tile_attribution: str
    hsts_max_age: int
    portable_import_max_bytes: int
    account_avatar_max_bytes: int

    @property
    def map_tile_host(self) -> str:
        """CSP's img-src needs just the tile origin, not the full
        {z}/{x}/{y} template URL -- derived here so the policy and the
        templates can never drift onto two different tile hosts.
        """
        parsed = urlsplit(self.map_tile_url)
        return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else parsed.netloc

    @property
    def security_link_base(self) -> str:
        return security_link_base(self.app_url)

    @property
    def snap_enabled(self) -> bool:
        return bool(self.osrm_url)

    @property
    def retention_enabled(self) -> bool:
        return self.raw_message_retention_days > 0

    @property
    def nudge_enabled(self) -> bool:
        return bool(self.ntfy_url and self.ntfy_topic)

    @property
    def odometer_reminder_enabled(self) -> bool:
        return self.nudge_enabled and self.odometer_reminder_requested

    @property
    def email_enabled(self) -> bool:
        """Email capability is gated on all three of host/from/to being set,
        mirroring the ntfy gate (`NTFY_URL` and `NTFY_TOPIC` both required) --
        an operator who sets only some of these almost certainly meant to
        finish the job, not silently get a half-configured mailer.
        """
        return bool(self.smtp_host and self.email_from and self.email_to)

    @property
    def oidc_configured(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret)

    @property
    def geocode_provider(self) -> GeocodeProvider | None:
        """The single enablement predicate every geocoding call site
        consults -- `None` when geocoding is off, a provider object
        otherwise. Rebuilt on each access rather than cached: it holds
        only its own configuration, the same "small provider class"
        `app/geocode.py` documents, so recomputing it is cheap.
        """
        return build_geocode_provider(
            self.geocode_provider_name,
            api_key=self.geocode_api_key,
            omit_country=self.geocode_omit_country,
            nominatim_url=self.geocode_nominatim_url,
            app_version=self.app_version,
        )

    @classmethod
    def from_env(cls) -> "Config":
        dev_no_auth = os.environ.get("DEV_NO_AUTH", "") == "1"
        required = ["DATABASE_URL", "SESSION_SECRET"]
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

        oidc_issuer = os.environ.get("OIDC_ISSUER", "")
        oidc_client_id = os.environ.get("OIDC_CLIENT_ID", "")
        oidc_client_secret = os.environ.get("OIDC_CLIENT_SECRET", "")
        oidc_fields = (oidc_issuer, oidc_client_id, oidc_client_secret)
        # OIDC is optional. A bare instance uses local accounts. But *some*
        # OIDC vars set and others missing is neither a working provider nor
        # a clean absence; that's almost certainly an operator typo, so it
        # still fails loudly at startup instead of silently landing on the
        # local-only branch.
        if not dev_no_auth and any(oidc_fields) and not all(oidc_fields):
            raise RuntimeError(
                "OIDC_ISSUER, OIDC_CLIENT_ID, and OIDC_CLIENT_SECRET must be set "
                "together, or all left unset to use local-login mode instead"
            )

        initial_admin_signup = os.environ.get("INITIAL_ADMIN_SIGNUP", "0") == "1"
        if dev_no_auth:
            log.info("UI auth: DEV_NO_AUTH=1 -- authentication is disabled")
        else:
            paths = []
            if all(oidc_fields):
                paths.append("OIDC")
            paths.append("local-login")
            if initial_admin_signup:
                paths.append("initial administrator signup")
            log.info("UI auth paths active: %s", ", ".join(paths))

        forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS", "")
        if not dev_no_auth and forwarded_allow_ips in ("", "*"):
            # `*` trusts X-Forwarded-For from any peer, so a client reaching the
            # app directly can spoof the address both FailedAuthLimiters key on;
            # empty trusts no peer, so a real proxy's forwarding is silently
            # ignored and every client behind it collapses onto one IP bucket.
            # Both are fine only because the shipped compose file binds the app
            # port to loopback behind a trusted proxy -- never fatal, since that
            # setup is correct today and must not break on upgrade.
            log.warning(
                "FORWARDED_ALLOW_IPS=%s -- safe only while this port stays "
                "loopback-bound behind a trusted reverse proxy. If the port is "
                "published directly, set this to the proxy's exact IP/CIDR instead.",
                forwarded_allow_ips or "<empty>",
            )

        app_url = os.environ.get("APP_URL", "").rstrip("/")
        if app_url and not security_link_base(app_url):
            log.warning(
                "APP_URL is not an absolute http(s) URL without userinfo, query or "
                "fragment; emailed verification and password reset links are disabled"
            )

        geocode_api_key = os.environ.get("GEOCODE_API_KEY", "")
        # Raises RuntimeError on an unrecognised value -- fail fast, not a
        # silently-disabled geocoder (see resolve_geocode_provider_name's
        # docstring for the GEOCODE_API_KEY-only upgrade fallback).
        geocode_provider_name = resolve_geocode_provider_name(
            os.environ.get("GEOCODE_PROVIDER", ""), geocode_api_key
        )

        return cls(
            database_url=os.environ["DATABASE_URL"],
            app_version=os.environ.get("APP_VERSION", "dev"),
            app_git_revision=os.environ.get("APP_GIT_REVISION", "unknown"),
            ingest_username=os.environ.get("INGEST_USERNAME", "owntracks"),
            ingest_password=os.environ.get("INGEST_PASSWORD", ""),
            session_secret=os.environ["SESSION_SECRET"],
            oidc_issuer=oidc_issuer,
            oidc_client_id=oidc_client_id,
            oidc_client_secret=oidc_client_secret,
            allowed_email=os.environ.get("ALLOWED_EMAIL", "").strip().lower(),
            initial_admin_signup=initial_admin_signup,
            login_auth_max_failures=int(os.environ.get("LOGIN_AUTH_MAX_FAILURES", 10)),
            login_auth_window_s=_f("LOGIN_AUTH_WINDOW_S", 900.0),
            display_tz=ZoneInfo(os.environ.get("DISPLAY_TZ", "UTC")),
            dev_no_auth=dev_no_auth,
            detector_params=Params(
                max_accuracy_m=_f("MAX_ACCURACY_M", 100.0),
                max_speed_ms=_f("MAX_SPEED_MS", 60.0),
                stay_radius_m=_f("STAY_RADIUS_M", 150.0),
                stay_min_duration_s=_f("STAY_MIN_DURATION_S", 300.0),
                min_trip_distance_m=_f("MIN_TRIP_DISTANCE_M", 300.0),
                gap_flag_threshold_s=_f("GAP_FLAG_THRESHOLD_S", 600.0),
                walk_max_speed_ms=_f("WALK_MAX_SPEED_MS", 2.0),
            ),
            detect_debounce_s=_f("DETECT_DEBOUNCE_S", 60.0),
            detect_sweep_s=_f("DETECT_SWEEP_S", 900.0),
            ingest_auth_max_failures=int(os.environ.get("INGEST_AUTH_MAX_FAILURES", 10)),
            ingest_auth_window_s=_f("INGEST_AUTH_WINDOW_S", 900.0),
            # Real OwnTracks payloads are tiny (well under 1KB); 64KB gives
            # generous headroom while still capping how much an authenticated
            # client can force into raw_messages per request.
            ingest_max_body_bytes=int(os.environ.get("INGEST_MAX_BODY_BYTES", 65536)),
            osrm_url=os.environ.get("OSRM_URL", "").rstrip("/"),
            osrm_min_confidence=_f("OSRM_MIN_CONFIDENCE", 0.5),
            osrm_max_coords=int(os.environ.get("OSRM_MAX_COORDS", 250)),
            snap_debounce_s=_f("SNAP_DEBOUNCE_S", 15.0),
            snap_sweep_s=_f("SNAP_SWEEP_S", 300.0),
            geocode_api_key=geocode_api_key,
            geocode_provider_name=geocode_provider_name,
            geocode_nominatim_url=os.environ.get("GEOCODE_NOMINATIM_URL", "").rstrip("/"),
            geocode_omit_country=os.environ.get(
                "GEOCODE_OMIT_COUNTRY", "United States of America"
            ),
            geocode_min_interval_s=_f("GEOCODE_MIN_INTERVAL_S", 1.0),
            geocode_debounce_s=_f("GEOCODE_DEBOUNCE_S", 15.0),
            geocode_sweep_s=_f("GEOCODE_SWEEP_S", 300.0),
            raw_message_retention_days=_f("RAW_MESSAGE_RETENTION_DAYS", 365.0),
            ntfy_url=os.environ.get("NTFY_URL", "").rstrip("/"),
            ntfy_topic=os.environ.get("NTFY_TOPIC", "").strip("/"),
            ntfy_token=os.environ.get("NTFY_TOKEN", ""),
            ntfy_username=os.environ.get("NTFY_USERNAME", ""),
            ntfy_password=os.environ.get("NTFY_PASSWORD", ""),
            app_url=app_url,
            nudge_weekly_hour=int(os.environ.get("NUDGE_WEEKLY_HOUR", 18)),
            # Default on whenever ntfy is configured (same gate the weekly
            # nudge itself uses in app/main.py), but independently
            # disableable -- an explicit ODOMETER_REMINDER always wins.
            odometer_reminder_requested=os.environ.get(
                "ODOMETER_REMINDER",
                "1" if (os.environ.get("NTFY_URL") and os.environ.get("NTFY_TOPIC")) else "0",
            ) == "1",
            odometer_reminder_hour=int(os.environ.get("ODOMETER_REMINDER_HOUR", 9)),
            trips_page_size=max(1, int(os.environ.get("TRIPS_PAGE_SIZE", 25))),
            full_reprocess_warn_points=max(
                0, int(os.environ.get("FULL_REPROCESS_WARN_POINTS", 500000))
            ),
            # Distance-only, no time criterion: a long time gap is usually
            # just "parked overnight", while a large spatial gap is
            # suspicious at any duration. `0` disables the feature entirely
            # rather than needing a separate on/off switch.
            missing_trip_gap_m=_f("MISSING_TRIP_GAP_M", DEFAULT_MISSING_TRIP_GAP_M),
            smtp_host=os.environ.get("SMTP_HOST", ""),
            smtp_port=int(os.environ.get("SMTP_PORT", 587)),
            smtp_username=os.environ.get("SMTP_USERNAME", ""),
            smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            smtp_security=os.environ.get("SMTP_SECURITY", "starttls"),
            smtp_tls_insecure=os.environ.get("SMTP_TLS_INSECURE", "0") == "1",
            email_from=os.environ.get("EMAIL_FROM", ""),
            email_to=os.environ.get("EMAIL_TO", ""),
            # Opt-in (decision 5): the live instance already gets the weekly
            # nudge over ntfy, so defaulting the email twin on would double
            # every Sunday ping. Monthly/filing default on -- neither has an
            # ntfy equivalent. Odometer email is opt-in like the weekly nudge,
            # since ntfy already covers it (unlike ODOMETER_REMINDER's ntfy
            # channel, which defaults on with NTFY_URL/NTFY_TOPIC).
            email_weekly_nudge=os.environ.get("EMAIL_WEEKLY_NUDGE", "0") == "1",
            email_monthly_summary=os.environ.get("EMAIL_MONTHLY_SUMMARY", "1") == "1",
            email_filing_reminder=os.environ.get("EMAIL_FILING_REMINDER", "1") == "1",
            email_odometer_reminder=os.environ.get("EMAIL_ODOMETER_REMINDER", "0") == "1",
            email_digest_hour=int(os.environ.get("EMAIL_DIGEST_HOUR", 9)),
            email_filing_reminder_mmdd=os.environ.get("EMAIL_FILING_REMINDER_MMDD", "01-15"),
            map_tile_url=os.environ.get("MAP_TILE_URL", DEFAULT_MAP_TILE_URL),
            map_tile_attribution=os.environ.get(
                "MAP_TILE_ATTRIBUTION", DEFAULT_MAP_TILE_ATTRIBUTION
            ),
            # 0/unset means no HSTS header at all -- see the SecurityHeadersMiddleware
            # docstring in app/main.py for why this stays opt-in.
            hsts_max_age=int(os.environ.get("HSTS_MAX_AGE", 0)),
            # A portable export is one JSON document covering the whole
            # ledger core (no route geometry, no raw points) -- even a large,
            # multi-year single-user instance stays well under this before
            # json.loads even has to run on it.
            portable_import_max_bytes=int(
                os.environ.get("PORTABLE_IMPORT_MAX_BYTES", 50 * 1024 * 1024)
            ),
            # A profile picture, not a photo library: 500KB comfortably fits
            # a PNG/JPEG/WebP at the small size this app ever displays one,
            # while still keeping a single-account instance's accounts row
            # far from unwieldy.
            account_avatar_max_bytes=int(
                os.environ.get("ACCOUNT_AVATAR_MAX_BYTES", DEFAULT_ACCOUNT_AVATAR_MAX_BYTES)
            ),
        )
