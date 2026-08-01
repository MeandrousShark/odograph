"""One diagnostics report, two surfaces: the authenticated Settings page
(app/ui.py) and `python -m app.diagnose`, run from inside the container for
exactly the case the page can't help with -- the app (or its database) is
down. Both call `build_report()`; the CLI additionally prints the on-demand
connectivity checks immediately, since invoking this command at all is
already the explicit, operator-initiated action D6 requires before any
outbound probe of OSRM/the geocoder/ntfy/SMTP -- there is no passive polling
here, only ever a direct response to something a human just did.

Nothing built here may carry a coordinate, address, secret, or raw payload
(see `test_diagnose.py`'s no-secrets property test). `WorkerStatus.record_failure`
(app/worker.py) already restricts a worker failure to its exception *class*;
every connectivity check below applies the same discipline to its own
failure detail -- an HTTP status code or an exception class name, never
`str(exc)`, since an `httpx.HTTPStatusError`'s message embeds the full
request URL, and a geocode provider's URL can carry its API key as a query
parameter (the same fact that already forces httpx's logger to WARNING in
app/main.py).
"""
from __future__ import annotations

import asyncio
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from psycopg_pool import AsyncConnectionPool

from app.config import Config
from app.db import MIGRATIONS_DIR, MIGRATION_FILENAME_RE, make_pool
from app.detector.runner import DETECTOR_VERSION
from app.snap import route_distance_m

# Bounded and short: these run synchronously in front of an operator who
# clicked "check now" (or is waiting on the CLI to exit), and a hung
# third-party service must not turn a diagnostic into a multi-minute stall.
CONNECTIVITY_TIMEOUT_S = 5.0

# A fixed, offshore sentinel coordinate -- never a real device fix -- used
# only to prove OSRM/the geocoder answer requests at all. It is never
# retained, logged, or echoed back in any report.
_PROBE_LAT, _PROBE_LON = 0.0, 0.0

# (app.state attribute name, report label) -- also the display order on both
# surfaces. `detector_scheduler` has no config gate (it always runs), so it
# is always "enabled" whenever the app is up at all.
WORKER_SPECS = [
    ("detector_scheduler", "detector"),
    ("snap_worker", "snap"),
    ("geocode_worker", "geocode"),
    ("retention_worker", "retention"),
    ("nudge_worker", "nudge"),
    ("odometer_reminder_worker", "odometer_reminder"),
    ("email_digest_worker", "email_digest"),
]


@dataclass
class WorkerReport:
    name: str
    enabled: bool
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_failure_type: str | None = None
    next_run_at: datetime | None = None
    # False only for the standalone CLI path, which has no running app
    # process to ask -- `WorkerStatus` (app/worker.py) lives entirely in
    # that process's memory by design (D7), so a fresh `python -m
    # app.diagnose` invocation can report *whether* a worker would be
    # enabled from config alone, but never its run history.
    state_available: bool = True


@dataclass
class PoolReport:
    ok: bool
    error_type: str | None = None
    stats: dict[str, int] = field(default_factory=dict)


@dataclass
class MigrationReport:
    # None (not []) means "unknown" -- the query itself failed (database
    # down, or `schema_migrations` unreachable), not "zero migrations
    # applied", which would be a false claim about a real instance.
    applied: list[int] | None
    expected: list[int]

    @property
    def status(self) -> str:
        if self.applied is None:
            return "unknown"
        return "up_to_date" if set(self.expected) <= set(self.applied) else "behind"


@dataclass
class ConnectivityResult:
    service: str
    configured: bool
    ok: bool | None = None  # None when not configured -- never probed
    detail: str = ""


@dataclass
class DiagnosticsReport:
    app_version: str
    git_revision: str
    detector_version: int
    database: PoolReport
    migrations: MigrationReport
    workers: list[WorkerReport]
    config_presence: dict[str, bool]


def _expected_migration_versions() -> list[int]:
    """Mirrors `app.db.run_migrations`'s own filename parsing without
    duplicating its raise -- by the time diagnostics ever runs, a
    malformed filename would already have failed startup loudly, so a
    stray one here is silently skipped rather than crashing a report whose
    purpose is to stay usable when something else is broken.
    """
    versions = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if not MIGRATION_FILENAME_RE.match(path.name):
            continue
        versions.append(int(path.name.split("_", 1)[0]))
    return versions


async def _check_database(pool: AsyncConnectionPool) -> PoolReport:
    try:
        async with pool.connection(timeout=CONNECTIVITY_TIMEOUT_S) as conn:
            await conn.execute("SELECT 1")
        return PoolReport(ok=True, stats=pool.get_stats())
    except Exception as exc:
        return PoolReport(ok=False, error_type=type(exc).__name__, stats=pool.get_stats())


async def _check_migrations(pool: AsyncConnectionPool) -> MigrationReport:
    expected = _expected_migration_versions()
    try:
        async with pool.connection(timeout=CONNECTIVITY_TIMEOUT_S) as conn:
            cur = await conn.execute("SELECT version FROM schema_migrations ORDER BY version")
            applied = [row[0] for row in await cur.fetchall()]
    except Exception:
        applied = None
    return MigrationReport(applied=applied, expected=expected)


def config_presence(cfg: Config) -> dict[str, bool]:
    """Presence only -- whether a setting is non-empty -- never the value
    itself. `dev_no_auth` is the one non-presence flag included: it's a
    security-relevant on/off switch already logged at startup
    (app/config.py), not a secret or a value an operator configures with a
    string worth hiding.

    `getattr(cfg, name, "")`, not direct attribute access: several existing
    tests build a bare `SimpleNamespace` config double carrying only the
    fields their scenario touches (same tolerance `app/main.py`'s
    `make_templates` already applies to `missing_trip_gap_m`) -- `Config.
    from_env()` always sets every field in the real app, so this never
    changes production behavior.
    """
    osrm_url = getattr(cfg, "osrm_url", "")
    geocode_provider = getattr(cfg, "geocode_provider", None)
    ntfy_url = getattr(cfg, "ntfy_url", "")
    ntfy_topic = getattr(cfg, "ntfy_topic", "")
    ntfy_token = getattr(cfg, "ntfy_token", "")
    ntfy_username = getattr(cfg, "ntfy_username", "")
    ntfy_password = getattr(cfg, "ntfy_password", "")
    smtp_username = getattr(cfg, "smtp_username", "")
    smtp_password = getattr(cfg, "smtp_password", "")
    admin_token = getattr(cfg, "admin_token", "")
    app_url = getattr(cfg, "app_url", "")
    return {
        "osrm_configured": bool(osrm_url),
        "geocode_configured": bool(geocode_provider),
        "ntfy_configured": bool(ntfy_url and ntfy_topic),
        "ntfy_auth_configured": bool(ntfy_token or (ntfy_username and ntfy_password)),
        "smtp_configured": bool(getattr(cfg, "email_enabled", False)),
        "smtp_auth_configured": bool(smtp_username and smtp_password),
        "oidc_configured": bool(getattr(cfg, "oidc_configured", False)),
        "admin_token_configured": bool(admin_token),
        "app_url_configured": bool(app_url),
        "dev_no_auth": bool(getattr(cfg, "dev_no_auth", False)),
    }


def _worker_gates_from_config(cfg: Config) -> dict[str, bool]:
    """Same gating `app/main.py`'s lifespan uses to decide which workers to
    construct, duplicated here (not imported) because the CLI path has no
    live app to ask -- see `WorkerReport.state_available`.
    """
    return {
        "detector": True,
        "snap": bool(cfg.osrm_url),
        "geocode": bool(cfg.geocode_provider),
        "retention": cfg.raw_message_retention_days > 0,
        "nudge": bool(cfg.ntfy_url and cfg.ntfy_topic),
        "odometer_reminder": bool(
            cfg.ntfy_url and cfg.ntfy_topic and cfg.odometer_reminder_enabled
        ),
        "email_digest": cfg.email_enabled,
    }


def worker_reports_from_state(state) -> list[WorkerReport]:
    """Built from the running app's `app.state` -- the ground truth for
    both "is this worker enabled" (was it constructed at all) and its
    `WorkerStatus` run history.
    """
    reports = []
    for attr, label in WORKER_SPECS:
        worker = getattr(state, attr, None)
        if worker is None:
            reports.append(WorkerReport(name=label, enabled=False))
            continue
        status = worker.status
        reports.append(WorkerReport(
            name=label, enabled=True,
            last_run_at=status.last_run_at,
            last_success_at=status.last_success_at,
            last_failure_at=status.last_failure_at,
            last_failure_type=status.last_failure_type,
            next_run_at=status.next_run_at,
        ))
    return reports


def worker_reports_from_config(cfg: Config) -> list[WorkerReport]:
    """Standalone-CLI fallback: no live process to ask, so only "would this
    worker be enabled" is knowable, never its run history.
    """
    gates = _worker_gates_from_config(cfg)
    return [
        WorkerReport(name=label, enabled=gates[label], state_available=False)
        for _, label in WORKER_SPECS
    ]


async def build_report(
    cfg: Config, pool: AsyncConnectionPool, state=None,
) -> DiagnosticsReport:
    """The one report builder both surfaces call. `state` is the running
    app's `app.state` (worker run history included) from the Settings page,
    or `None` from the standalone CLI, which has no process to ask and
    falls back to config-derived enablement only.
    """
    database, migrations = await asyncio.gather(
        _check_database(pool), _check_migrations(pool),
    )
    workers = (
        worker_reports_from_state(state) if state is not None
        else worker_reports_from_config(cfg)
    )
    return DiagnosticsReport(
        app_version=cfg.app_version,
        git_revision=cfg.app_git_revision,
        detector_version=DETECTOR_VERSION,
        database=database,
        migrations=migrations,
        workers=workers,
        config_presence=config_presence(cfg),
    )


async def _check_osrm(cfg: Config, client: httpx.AsyncClient) -> ConnectivityResult:
    if not cfg.osrm_url:
        return ConnectivityResult("osrm", configured=False, detail="OSRM_URL not set")
    try:
        # ~200m apart, offshore -- proves the /route endpoint answers,
        # regardless of whether it finds an actual road route there.
        await route_distance_m(
            client, cfg.osrm_url, _PROBE_LAT, _PROBE_LON, _PROBE_LAT, _PROBE_LON + 0.002
        )
        return ConnectivityResult("osrm", configured=True, ok=True, detail="reachable")
    except httpx.HTTPStatusError as exc:
        return ConnectivityResult(
            "osrm", configured=True, ok=False, detail=f"http {exc.response.status_code}"
        )
    except httpx.HTTPError as exc:
        return ConnectivityResult("osrm", configured=True, ok=False, detail=type(exc).__name__)


async def _check_geocode(cfg: Config, client: httpx.AsyncClient) -> ConnectivityResult:
    # The `detail` prefix names which provider was probed (D6/M5's
    # "which provider" requirement) without adding a field only this one
    # service uses -- config_presence/render_report_text stay shaped the
    # same across every service.
    provider = cfg.geocode_provider
    if provider is None:
        return ConnectivityResult("geocode", configured=False, detail="GEOCODE_PROVIDER not set")
    try:
        await provider.reverse(client, _PROBE_LAT, _PROBE_LON)
        return ConnectivityResult(
            "geocode", configured=True, ok=True, detail=f"{provider.name}: reachable"
        )
    except httpx.HTTPStatusError as exc:
        return ConnectivityResult(
            "geocode", configured=True, ok=False,
            detail=f"{provider.name}: http {exc.response.status_code}",
        )
    except httpx.HTTPError as exc:
        return ConnectivityResult(
            "geocode", configured=True, ok=False, detail=f"{provider.name}: {type(exc).__name__}"
        )


async def _check_ntfy(cfg: Config, client: httpx.AsyncClient) -> ConnectivityResult:
    if not (cfg.ntfy_url and cfg.ntfy_topic):
        return ConnectivityResult(
            "ntfy", configured=False, detail="NTFY_URL/NTFY_TOPIC not set"
        )
    url = f"{cfg.ntfy_url.rstrip('/')}/v1/health"
    try:
        resp = await client.get(url, timeout=CONNECTIVITY_TIMEOUT_S)
        # No auth attempted -- /v1/health is meant to be a public liveness
        # probe on the ntfy server itself; a 401/403 still proves the host
        # answered, which is the only thing this check claims to verify.
        ok = resp.status_code < 500
        return ConnectivityResult(
            "ntfy", configured=True, ok=ok, detail=f"http {resp.status_code}"
        )
    except httpx.HTTPError as exc:
        return ConnectivityResult("ntfy", configured=True, ok=False, detail=type(exc).__name__)


def _smtp_probe(cfg: Config) -> None:
    """Connects (and, for starttls, negotiates TLS) then immediately NOOPs
    and quits -- deliberately never calls `login()`. This check exists to
    answer "is the SMTP host reachable", not "are these credentials still
    valid"; repeatedly authenticating from a diagnostic click is exactly
    the kind of traffic that gets an operator's account rate-limited or
    locked by a provider. Blocking (stdlib `smtplib`), run off the event
    loop via `asyncio.to_thread` -- same shape as `app.mailer.smtp_transport`,
    which this deliberately does not call, since that composes and would
    attempt to send a real message.
    """
    context = (
        ssl._create_unverified_context() if cfg.smtp_tls_insecure
        else ssl.create_default_context()
    )
    if cfg.smtp_security == "ssl":
        with smtplib.SMTP_SSL(
            cfg.smtp_host, cfg.smtp_port, context=context, timeout=CONNECTIVITY_TIMEOUT_S
        ) as smtp:
            smtp.noop()
    elif cfg.smtp_security == "starttls":
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=CONNECTIVITY_TIMEOUT_S) as smtp:
            smtp.starttls(context=context)
            smtp.noop()
    else:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=CONNECTIVITY_TIMEOUT_S) as smtp:
            smtp.noop()


async def _check_smtp(cfg: Config) -> ConnectivityResult:
    if not cfg.email_enabled:
        return ConnectivityResult(
            "smtp", configured=False, detail="SMTP_HOST/EMAIL_FROM/EMAIL_TO not set"
        )
    try:
        await asyncio.to_thread(_smtp_probe, cfg)
        return ConnectivityResult("smtp", configured=True, ok=True, detail="reachable")
    except Exception as exc:
        return ConnectivityResult("smtp", configured=True, ok=False, detail=type(exc).__name__)


async def run_connectivity_checks(
    cfg: Config, http_client: httpx.AsyncClient | None = None,
) -> list[ConnectivityResult]:
    """The on-demand "check now" surface (D6): every call here is a direct
    result of an explicit operator action (a button click, or running this
    module at all), never a background timer -- see the module docstring.

    `http_client` is injectable (tests substitute an `httpx.MockTransport`);
    left `None`, a short-lived client is opened and closed around the three
    HTTP-based checks. SMTP has no httpx client to share -- it's a
    stdlib-`smtplib` probe, run off the event loop, entirely separate.
    """
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient()
    try:
        osrm, geocode, ntfy, smtp = await asyncio.gather(
            _check_osrm(cfg, client), _check_geocode(cfg, client),
            _check_ntfy(cfg, client), _check_smtp(cfg),
        )
    finally:
        if owns_client:
            await client.aclose()
    return [osrm, geocode, ntfy, smtp]


def render_report_text(
    report: DiagnosticsReport, connectivity: list[ConnectivityResult] | None = None,
) -> str:
    lines = [
        f"app_version: {report.app_version}",
        f"git_revision: {report.git_revision}",
        f"detector_version: {report.detector_version}",
        "",
        f"database: {'ok' if report.database.ok else 'FAILED (' + str(report.database.error_type) + ')'}",
    ]
    for key, value in sorted(report.database.stats.items()):
        lines.append(f"  {key}: {value}")
    lines.append("")
    lines.append(f"migrations: {report.migrations.status}")
    lines.append(f"  expected: {report.migrations.expected}")
    lines.append(f"  applied: {report.migrations.applied}")
    lines.append("")
    lines.append("workers (in-memory; resets on every process restart since process start):")
    for worker in report.workers:
        if not worker.enabled:
            lines.append(f"  {worker.name}: disabled")
            continue
        if not worker.state_available:
            lines.append(f"  {worker.name}: enabled (run history unavailable outside the running app process)")
            continue
        lines.append(
            f"  {worker.name}: enabled"
            f" last_run={worker.last_run_at} last_success={worker.last_success_at}"
            f" last_failure={worker.last_failure_at} ({worker.last_failure_type})"
            f" next_run={worker.next_run_at}"
        )
    lines.append("")
    lines.append("config presence:")
    for key, value in sorted(report.config_presence.items()):
        lines.append(f"  {key}: {value}")
    if connectivity is not None:
        lines.append("")
        lines.append("connectivity (checked now):")
        for result in connectivity:
            if not result.configured:
                lines.append(f"  {result.service}: not configured ({result.detail})")
            else:
                status = "ok" if result.ok else "FAILED"
                lines.append(f"  {result.service}: {status} ({result.detail})")
    return "\n".join(lines) + "\n"


async def _main() -> None:
    cfg = Config.from_env()
    pool = make_pool(cfg.database_url)
    await pool.open()
    try:
        report = await build_report(cfg, pool, state=None)
        connectivity = await run_connectivity_checks(cfg)
    finally:
        await pool.close()
    print(render_report_text(report, connectivity))


if __name__ == "__main__":
    asyncio.run(_main())
