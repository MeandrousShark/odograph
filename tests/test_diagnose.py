"""Tests for app/diagnose.py: the one report builder shared by the
Settings page and `python -m app.diagnose`, the on-demand connectivity
checks against stubbed failures (D6), and the no-secrets/no-coordinates
property both surfaces must hold.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.auth import require_csrf
from app.config import Config
from app.diagnose import (
    _check_geocode,
    _check_ntfy,
    _check_osrm,
    _check_smtp,
    _expected_migration_versions,
    _geocode_configured,
    build_report,
    config_presence,
    render_report_text,
    run_connectivity_checks,
    worker_reports_from_config,
    worker_reports_from_state,
)
from app.geocode import GeoapifyProvider
from app.worker import IntervalWorker, WorkerStatus

REQUIRED_VARS = ("DATABASE_URL", "INGEST_PASSWORD", "SESSION_SECRET")


# ---- fake pool/connection, same shape as tests/test_version_identity.py ----

class _Cursor:
    def __init__(self, rows=None):
        self.rows = rows or []

    async def execute(self, *args, **kwargs):
        return self

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, rows=None):
        self.rows = rows or []

    async def execute(self, query, *args, **kwargs):
        return _Cursor(self.rows)


class _ConnectionContext:
    def __init__(self, connection, raise_on_enter=None):
        self.connection = connection
        self.raise_on_enter = raise_on_enter

    async def __aenter__(self):
        if self.raise_on_enter is not None:
            raise self.raise_on_enter
        return self.connection

    async def __aexit__(self, *exc_info):
        return False


class _Pool:
    def __init__(self, connection=None, raise_on_enter=None, stats=None):
        self.connection_value = connection or _Connection()
        self.raise_on_enter = raise_on_enter
        self._stats = stats if stats is not None else {"pool_size": 1, "pool_available": 1}

    def connection(self, timeout=None):
        return _ConnectionContext(self.connection_value, self.raise_on_enter)

    def get_stats(self):
        return dict(self._stats)


def _config(**overrides) -> SimpleNamespace:
    defaults = dict(
        app_version="v1.2.3", app_git_revision="deadbeef",
        osrm_url="", geocode_provider=None, ntfy_url="", ntfy_topic="",
        ntfy_token="", ntfy_username="", ntfy_password="",
        email_enabled=False, smtp_username="", smtp_password="",
        smtp_host="", smtp_port=587, smtp_security="starttls", smtp_tls_insecure=False,
        oidc_configured=False, initial_admin_signup=False, app_url="", dev_no_auth=False,
        raw_message_retention_days=365.0, odometer_reminder_requested=False,
    )
    defaults.update(overrides)
    defaults.setdefault("snap_enabled", bool(defaults["osrm_url"]))
    defaults.setdefault(
        "retention_enabled", defaults["raw_message_retention_days"] > 0
    )
    defaults.setdefault(
        "nudge_enabled", bool(defaults["ntfy_url"] and defaults["ntfy_topic"])
    )
    defaults.setdefault(
        "odometer_reminder_enabled",
        defaults["nudge_enabled"] and defaults["odometer_reminder_requested"],
    )
    return SimpleNamespace(**defaults)


def _misconfigured_nominatim_config(monkeypatch) -> Config:
    """A real `Config` (not a `SimpleNamespace` double) whose
    `geocode_provider` property raises RuntimeError on every access --
    GEOCODE_PROVIDER=nominatim set with no GEOCODE_NOMINATIM_URL, the same
    scenario tests/test_config.py's `test_geocode_provider_nominatim_without_
    url_fails_fast` exercises against `build_geocode_provider` directly.
    """
    for key, value in zip(REQUIRED_VARS, ("postgresql://x/x", "ingest-pw", "session-secret")):
        monkeypatch.setenv(key, value)
    for key in (
        "GEOCODE_API_KEY", "GEOCODE_OMIT_COUNTRY", "GEOCODE_NOMINATIM_URL",
        "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "DEV_NO_AUTH",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GEOCODE_PROVIDER", "nominatim")
    return Config.from_env()


# ---- build_report: database + migrations ----

def test_build_report_marks_migrations_up_to_date_when_every_expected_version_is_applied():
    expected = _expected_migration_versions()
    rows = [(v,) for v in expected]
    pool = _Pool(connection=_Connection(rows=rows))

    report = asyncio.run(build_report(_config(), pool, state=None))

    assert report.database.ok is True
    assert report.migrations.status == "up_to_date"
    assert report.migrations.applied == expected


def test_build_report_flags_migrations_behind_when_the_latest_file_is_unapplied():
    expected = _expected_migration_versions()
    rows = [(v,) for v in expected[:-1]]
    pool = _Pool(connection=_Connection(rows=rows))

    report = asyncio.run(build_report(_config(), pool, state=None))

    assert report.migrations.status == "behind"


def test_build_report_reports_database_down_as_unknown_not_zero_migrations():
    pool = _Pool(raise_on_enter=ConnectionRefusedError("db down"))

    report = asyncio.run(build_report(_config(), pool, state=None))

    assert report.database.ok is False
    assert report.database.error_type == "ConnectionRefusedError"
    # None, not [] -- a database-down report must never claim "zero
    # migrations applied", which would misdescribe a real, healthy instance.
    assert report.migrations.applied is None
    assert report.migrations.status == "unknown"


# ---- worker reports ----

def test_worker_reports_from_state_reports_disabled_for_absent_workers():
    state = SimpleNamespace(detector_scheduler=SimpleNamespace(status=WorkerStatus(label="detector")))
    reports = worker_reports_from_state(state)
    by_name = {w.name: w for w in reports}

    assert by_name["detector"].enabled is True
    assert by_name["snap"].enabled is False
    assert by_name["geocode"].enabled is False


def test_worker_reports_from_state_carries_over_the_live_status_fields():
    status = WorkerStatus(label="snap")
    status.record_run()
    status.record_success()
    state = SimpleNamespace(snap_worker=SimpleNamespace(status=status))

    reports = worker_reports_from_state(state)
    snap = next(w for w in reports if w.name == "snap")

    assert snap.enabled is True
    assert snap.state_available is True
    assert snap.last_run_at == status.last_run_at
    assert snap.last_success_at == status.last_success_at


def test_worker_reports_from_config_has_no_run_history_and_matches_main_py_gates():
    cfg = _config(osrm_url="http://osrm.internal:5000", geocode_provider=None, ntfy_url="", ntfy_topic="")
    reports = worker_reports_from_config(cfg)
    by_name = {w.name: w for w in reports}

    assert by_name["detector"].enabled is True  # no config gate
    assert by_name["snap"].enabled is True  # OSRM_URL set
    assert by_name["geocode"].enabled is False  # no GEOCODE_PROVIDER resolved
    assert by_name["nudge"].enabled is False  # NTFY_URL/NTFY_TOPIC unset
    for report in reports:
        assert report.state_available is False
        assert report.last_run_at is None


# ---- config presence: booleans only ----

def test_config_presence_reports_only_booleans():
    cfg = _config(
        osrm_url="http://osrm.internal:5000",
        geocode_provider=GeoapifyProvider(api_key="topsecretkey", omit_country="United States of America"),
        ntfy_url="http://ntfy.internal", ntfy_topic="mileage", ntfy_token="ntfytoken",
        smtp_username="user", smtp_password="hunter2", email_enabled=True,
        initial_admin_signup=True, app_url="https://mileage.example.com",
    )
    presence = config_presence(cfg)

    assert all(isinstance(v, bool) for v in presence.values())
    assert presence["osrm_configured"] is True
    assert presence["geocode_configured"] is True
    assert presence["ntfy_configured"] is True
    assert presence["ntfy_auth_configured"] is True
    assert presence["smtp_configured"] is True
    assert presence["smtp_auth_configured"] is True
    assert presence["initial_admin_signup"] is True
    assert presence["app_url_configured"] is True
    # Never the values themselves.
    rendered = repr(presence)
    assert "topsecretkey" not in rendered
    assert "ntfytoken" not in rendered
    assert "hunter2" not in rendered


def test_config_presence_tolerates_a_partial_config_double():
    # Several existing tests build a bare SimpleNamespace config carrying
    # only the fields their scenario touches (tests/test_vehicles_db.py's
    # _bare_app, tests/test_version_identity.py) -- config_presence must not
    # crash a real request just because a test double omits an unrelated field.
    presence = config_presence(SimpleNamespace(app_version="x", app_git_revision="y"))
    assert presence["osrm_configured"] is False
    assert presence["dev_no_auth"] is False


# ---- geocode misconfiguration must name the fault, never crash the report ----

def test_geocode_configured_treats_a_build_failure_as_configured_but_broken(monkeypatch):
    # cfg.geocode_provider raises RuntimeError for GEOCODE_PROVIDER=nominatim
    # with no GEOCODE_NOMINATIM_URL -- _geocode_configured must swallow that
    # and report True (configured, just broken), not let it propagate and
    # crash config_presence/worker_gates.
    cfg = _misconfigured_nominatim_config(monkeypatch)
    assert _geocode_configured(cfg) is True


def test_config_presence_does_not_crash_on_geocode_misconfiguration(monkeypatch):
    cfg = _misconfigured_nominatim_config(monkeypatch)
    presence = config_presence(cfg)
    assert presence["geocode_configured"] is True


def test_worker_reports_from_config_does_not_crash_on_geocode_misconfiguration(monkeypatch):
    cfg = _misconfigured_nominatim_config(monkeypatch)
    reports = worker_reports_from_config(cfg)
    by_name = {w.name: w for w in reports}
    assert by_name["geocode"].enabled is True


def test_build_report_does_not_crash_on_geocode_misconfiguration(monkeypatch):
    # The end-to-end path `python -m app.diagnose` drives: a misconfigured
    # geocoder must not turn "run diagnostics" into a stack trace.
    cfg = _misconfigured_nominatim_config(monkeypatch)
    pool = _Pool(connection=_Connection(rows=[(v,) for v in _expected_migration_versions()]))
    report = asyncio.run(build_report(cfg, pool, state=None))
    assert report.config_presence["geocode_configured"] is True


# ---- on-demand connectivity checks against stubbed failures ----

def test_check_osrm_not_configured_when_url_unset():
    result = asyncio.run(_check_osrm(_config(osrm_url=""), httpx.AsyncClient()))
    assert result.configured is False
    assert result.ok is None


def test_check_osrm_reports_unreachable_on_connection_failure():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await _check_osrm(_config(osrm_url="http://osrm.invalid:5000"), client)

    result = asyncio.run(scenario())
    assert result.configured is True
    assert result.ok is False
    assert result.detail == "ConnectError"


def test_check_geocode_not_configured_when_provider_unset():
    result = asyncio.run(_check_geocode(_config(geocode_provider=None), httpx.AsyncClient()))
    assert result.configured is False
    assert result.ok is None
    assert result.detail == "GEOCODE_PROVIDER not set"


def test_check_geocode_reachable_names_which_provider_it_probed():
    def handler(request):
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    provider = GeoapifyProvider(api_key="k", omit_country="United States of America")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await _check_geocode(_config(geocode_provider=provider), client)

    result = asyncio.run(scenario())
    assert result.configured is True
    assert result.ok is True
    assert result.detail == "geoapify: reachable"


def test_check_geocode_reports_http_401_and_never_leaks_the_api_key():
    secret_key = "sentinel-geocode-api-key"

    def handler(request):
        assert f"apiKey={secret_key}" in str(request.url)  # sanity: it's really on the wire
        return httpx.Response(401, json={"error": "Invalid API key"})

    provider = GeoapifyProvider(api_key=secret_key, omit_country="United States of America")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await _check_geocode(_config(geocode_provider=provider), client)

    result = asyncio.run(scenario())
    assert result.configured is True
    assert result.ok is False
    assert result.detail == "geoapify: http 401"
    assert secret_key not in result.detail


def test_check_geocode_names_a_build_misconfiguration_instead_of_crashing(monkeypatch):
    cfg = _misconfigured_nominatim_config(monkeypatch)

    result = asyncio.run(_check_geocode(cfg, httpx.AsyncClient()))

    assert result.configured is True
    assert result.ok is False
    assert "misconfigured" in result.detail
    assert "GEOCODE_NOMINATIM_URL" in result.detail


def test_check_ntfy_not_configured_when_unset():
    result = asyncio.run(_check_ntfy(_config(ntfy_url="", ntfy_topic=""), httpx.AsyncClient()))
    assert result.configured is False
    assert result.ok is None


def test_check_ntfy_reports_unreachable_on_connection_failure():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await _check_ntfy(
                _config(ntfy_url="http://ntfy.invalid", ntfy_topic="mileage"), client
            )

    result = asyncio.run(scenario())
    assert result.configured is True
    assert result.ok is False
    assert result.detail == "ConnectError"


def test_check_smtp_not_configured_when_email_disabled():
    result = asyncio.run(_check_smtp(_config(email_enabled=False)))
    assert result.configured is False
    assert result.ok is None


def test_check_smtp_reports_unreachable_without_touching_credentials(monkeypatch):
    def _boom(*args, **kwargs):
        raise ConnectionRefusedError("dead smtp host")

    monkeypatch.setattr("app.diagnose.smtplib.SMTP", _boom)
    monkeypatch.setattr("app.diagnose.smtplib.SMTP_SSL", _boom)

    cfg = _config(
        email_enabled=True, smtp_host="smtp.invalid", smtp_port=587,
        smtp_username="should-never-be-used", smtp_password="should-never-be-used",
    )
    result = asyncio.run(_check_smtp(cfg))
    assert result.configured is True
    assert result.ok is False
    assert result.detail == "ConnectionRefusedError"


def test_run_connectivity_checks_returns_all_four_services_in_order():
    def handler(request):
        return httpx.Response(200, json={"code": "Ok", "routes": []})

    cfg = _config(
        osrm_url="http://osrm.invalid",
        geocode_provider=GeoapifyProvider(api_key="k", omit_country="United States of America"),
        ntfy_url="http://ntfy.invalid", ntfy_topic="t",
    )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_connectivity_checks(cfg, http_client=client)

    results = asyncio.run(scenario())
    assert [r.service for r in results] == ["osrm", "geocode", "ntfy", "smtp"]


# ---- the no-secrets / no-coordinates property ----

def test_report_and_connectivity_never_contain_secrets_or_coordinates(monkeypatch):
    secrets = {
        "DATABASE_URL": "postgresql://mileage:db-secret-value@db/mileage",
        "INGEST_PASSWORD": "ingest-secret-value",
        "SESSION_SECRET": "session-secret-value",
        "GEOCODE_API_KEY": "geocode-secret-value",
        "NTFY_URL": "http://ntfy.internal",
        "NTFY_TOPIC": "mileage",
        "NTFY_TOKEN": "ntfy-secret-value",
        "NTFY_USERNAME": "ntfy-user",
        "NTFY_PASSWORD": "ntfy-pw-secret-value",
        "SMTP_HOST": "smtp.internal",
        "SMTP_USERNAME": "smtp-user",
        "SMTP_PASSWORD": "smtp-pw-secret-value",
        "EMAIL_FROM": "mileage@example.com",
        "EMAIL_TO": "me@example.com",
        "ADMIN_TOKEN": "admin-secret-value",
        "OSRM_URL": "http://osrm.internal:5000",
        "APP_VERSION": "v9.9.9",
        "APP_GIT_REVISION": "cafef00d",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    for key in ("DEV_NO_AUTH", "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "app.diagnose.smtplib.SMTP", lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError())
    )
    cfg = Config.from_env()

    class _CoordinateLeakError(Exception):
        pass

    coordinate_message = "failed near 47.606209,-122.332069 using key geocode-secret-value"
    status = WorkerStatus(label="snap")
    try:
        raise _CoordinateLeakError(coordinate_message)
    except _CoordinateLeakError as exc:
        status.record_failure(exc)
    state = SimpleNamespace(snap_worker=SimpleNamespace(status=status))

    pool = _Pool(connection=_Connection(rows=[(v,) for v in _expected_migration_versions()]))
    report = asyncio.run(build_report(cfg, pool, state))

    def handler(request):
        # Prove the secret really does travel on the wire here, so a pass
        # below is a genuine property of our own code, not an untested path.
        return httpx.Response(401, text="invalid credentials")

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_connectivity_checks(cfg, http_client=client)

    connectivity = asyncio.run(check())
    rendered = render_report_text(report, connectivity)

    forbidden = [
        "db-secret-value", "ingest-secret-value", "session-secret-value",
        "geocode-secret-value", "ntfy-secret-value", "ntfy-pw-secret-value",
        "smtp-pw-secret-value", "admin-secret-value",
        "47.606209", "-122.332069", coordinate_message,
    ]
    for value in forbidden:
        assert value not in rendered, f"{value!r} leaked into the diagnostics report"
    assert "_CoordinateLeakError" in rendered  # the class name is allowed


def test_check_now_route_requires_csrf():
    from app.ui import make_router

    route = next(
        r for r in make_router().routes if r.path == "/settings/diagnostics/check"
    )
    assert any(dep.dependency is require_csrf for dep in route.dependencies)
