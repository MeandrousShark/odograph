from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx

import app.ui as ui
from app.config import Config
from app.db import _fetch_schema_version
from app.detector.runner import DETECTOR_VERSION
from app.main import create_app, make_templates

ROOT = Path(__file__).parents[1]
TZ = ZoneInfo("UTC")


class _Cursor:
    def __init__(self, row=None):
        self.row = row

    async def execute(self, *args, **kwargs):
        return self

    async def fetchone(self):
        return self.row

    async def fetchall(self):
        return [] if self.row is None else [self.row]


class _Connection:
    def __init__(self, row=None):
        self.row = row
        self.queries = []

    def cursor(self, row_factory=None):
        return _Cursor(self.row)

    async def execute(self, query, *args, **kwargs):
        self.queries.append(query)
        return _Cursor(self.row)


class _ConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *exc_info):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection_value = connection

    def connection(self, timeout=None):
        return _ConnectionContext(self.connection_value)

    def get_stats(self):
        return {}


def _render_settings(diagnostics):
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    return templates.env.get_template("settings.html").render(
        boundary_overrides=[], rates=[], vehicles=[], odometer=[], places=[], rules=[],
        geocode_enabled=False, device_fixes=[], diagnostics=diagnostics,
        user={"name": "Tester"}, csrf="test",
    )


def test_docker_build_identity_is_available_to_the_runtime_environment():
    source = (ROOT / "Dockerfile").read_text()

    assert "ARG VERSION=dev" in source
    assert "ARG GIT_REVISION=unknown" in source
    assert "ENV APP_VERSION=$VERSION" in source
    assert "APP_GIT_REVISION=$GIT_REVISION" in source
    assert "org.opencontainers.image.version=$VERSION" in source
    assert "org.opencontainers.image.revision=$GIT_REVISION" in source


def test_docker_base_is_pinned_to_the_verified_multi_arch_index():
    source = (ROOT / "Dockerfile").read_text()

    assert (
        "FROM docker.io/library/python:3.13-slim@"
        "sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91"
    ) in source
    assert "Pinned to the OCI image index" in source
    assert "linux/amd64 and linux/arm64" in source
    assert "podman manifest inspect docker.io/library/python@sha256:<resolved-digest>" in source
    assert "must run on amd64" not in source
    assert "RepoDigests is architecture-specific" not in source


def test_settings_diagnostics_render_all_runtime_versions():
    body = _render_settings({
        "app_version": "v0.6.0-rc.1",
        "git_revision": "0123456789abcdef",
        "schema_version": 17,
        "detector_version": 2,
    })

    diagnostics = body.split("<h2>Diagnostics</h2>", 1)[1].split(
        "<h2>Device status</h2>", 1
    )[0]
    assert "App version" in diagnostics
    assert "v0.6.0-rc.1" in diagnostics
    assert "Git revision" in diagnostics
    assert "0123456789abcdef" in diagnostics
    assert "Schema version" in diagnostics
    assert ">17<" in diagnostics
    assert "Detector version" in diagnostics
    assert ">2<" in diagnostics


def test_authenticated_page_footer_shows_the_configured_app_version():
    # The diagnostics dict's app_version deliberately differs from the
    # make_templates() config's -- proves the footer reads the template
    # global, not whatever a route happened to pass into the page context.
    body = _render_settings({
        "app_version": "diagnostics-dict-version-should-not-appear-in-footer",
        "git_revision": "0123456789abcdef",
        "schema_version": 17,
        "detector_version": 2,
    })

    assert "<footer>" in body
    footer = body.split("<footer>", 1)[1].split("</footer>", 1)[0]
    assert "test" in footer
    assert "diagnostics-dict-version-should-not-appear-in-footer" not in footer
    assert "17" not in footer
    assert ">2<" not in footer
    assert "<a " not in footer


def test_schema_version_is_queried_live_and_detector_version_stays_two():
    connection = _Connection((17,))

    assert asyncio.run(_fetch_schema_version(connection)) == 17
    assert connection.queries == [
        "SELECT COALESCE(max(version), 0) FROM schema_migrations"
    ]
    assert DETECTOR_VERSION == 2


def test_authenticated_settings_context_uses_config_and_live_schema(monkeypatch):
    async def empty_rows(*args, **kwargs):
        return []

    async def schema_version(*args, **kwargs):
        return 17

    async def auto_assign_off(*args, **kwargs):
        return False

    for name in (
        "_fetch_rates_rows",
        "list_vehicles",
        "_fetch_odometer_context",
        "_fetch_places_rows",
        "_fetch_rules_rows",
        "_fetch_boundary_overrides_rows",
        "_fetch_device_fixes",
    ):
        monkeypatch.setattr(ui, name, empty_rows)
    monkeypatch.setattr(ui, "_fetch_schema_version", schema_version)
    monkeypatch.setattr(ui, "get_auto_assign_default_vehicle", auto_assign_off)

    class _Templates:
        def TemplateResponse(self, request, name, context):
            assert name == "settings.html"
            return context

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                pool=_Pool(_Connection()),
                templates=_Templates(),
                config=SimpleNamespace(
                    geocode_provider=None,
                    app_version="v0.6.0-rc.1",
                    app_git_revision="0123456789abcdef",
                    # The rest are only read by the diagnostics report's
                    # config-presence section (app/diagnose.py), added
                    # alongside the four version fields this test already
                    # asserted on.
                    osrm_url="", ntfy_url="", ntfy_topic="", ntfy_token="",
                    ntfy_username="", ntfy_password="", email_enabled=False,
                    smtp_username="", smtp_password="", oidc_configured=False,
                    initial_admin_signup=False, app_url="", dev_no_auth=False,
                ),
            )
        ),
        session={"csrf": "test"},
    )
    settings_endpoint = next(
        route.endpoint for route in ui.make_router().routes if route.path == "/settings"
    )

    context = asyncio.run(settings_endpoint(request, user={"name": "Tester"}))

    assert context["diagnostics"] == {
        "app_version": "v0.6.0-rc.1",
        "git_revision": "0123456789abcdef",
        "schema_version": 17,
        "detector_version": 2,
    }


def test_unauthenticated_surfaces_do_not_disclose_runtime_identity(monkeypatch):
    app_version = "private-version-sentinel"
    git_revision = "private-revision-sentinel"
    for key, value in {
        "DATABASE_URL": "postgresql://unused/unused",
        "INGEST_PASSWORD": "ingest-password",
        "SESSION_SECRET": "session-secret",
        "ADMIN_TOKEN": "setup-token",
        "APP_VERSION": app_version,
        "APP_GIT_REVISION": git_revision,
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("DEV_NO_AUTH", "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)

    app = create_app(Config.from_env())
    app.state.pool = _Pool(_Connection())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=False
        ) as client:
            responses = [
                await client.get("/healthz"),
                await client.get("/login"),
                await client.get("/setup"),
                await client.post("/ingest"),
                await client.get("/settings"),
            ]

        assert responses[0].json() == {"ok": True}
        assert responses[4].status_code == 303
        assert responses[4].headers["location"] == "/login"
        for response in responses:
            assert app_version not in response.text
            assert git_revision not in response.text

    asyncio.run(scenario())
