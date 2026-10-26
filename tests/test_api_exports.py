"""API v1 exports: admission wiring, the bearer report identity and the
attachment header."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from starlette.requests import Request

import app.api_v1 as api_v1
from app import auth
from app.account_context import AccountPrincipal
from app.api_errors import ApiError
from app.api_exports import XLSX_MEDIA_TYPE, make_router
from app.api_v1 import ApiUser, require_api_report_user
from app.capacity_routes import PREPARED_REPORT_ROUTES
from app.ingest import FailedAuthLimiter
from app.preparation import PreparedFileResponse

TOKEN = "A" * 43
PATHS = {"/api/v1/exports/trips", "/api/v1/reports/range", "/api/v1/reports/{year}"}


def test_export_routes_are_prepared_foreground_routes_behind_the_bearer_report_identity():
    routes = {route.path: route for route in make_router().routes}
    assert set(routes) == PATHS
    assert PATHS <= PREPARED_REPORT_ROUTES
    # Registered first, so "range" is never read as a year.
    assert list(routes)[:2] == ["/api/v1/exports/trips", "/api/v1/reports/range"]
    for route in routes.values():
        assert route.methods == {"GET"}
        assert route.capacity_lane == "foreground"
        assert route.protected_dependency() is require_api_report_user


def _request(headers=((b"authorization", b"Bearer " + TOKEN.encode()),)):
    app = SimpleNamespace(state=SimpleNamespace(
        config=SimpleNamespace(dev_no_auth=False), control_pool=object(), runtime_pool=object(),
        api_auth_limiter=FailedAuthLimiter(50, 900),
    ))
    return Request({"type": "http", "app": app, "session": {}, "headers": list(headers),
                    "client": ("192.0.2.1", 1000), "path": "/api/v1/reports/2026"})


def _install(monkeypatch, account, principal=None):
    calls = []

    @asynccontextmanager
    async def control(pool):
        yield object()

    async def check_access_token(conn, token):
        calls.append(("check", token))
        return SimpleNamespace(principal=principal or SimpleNamespace(
            account_id=7, auth_version=3, grant_id="grant"), known=True)

    async def lookup(conn, owner):
        calls.append(("lookup", owner))
        return account

    async def forbidden(*args, **kwargs):
        raise AssertionError("variable account/settings data read before report admission")

    monkeypatch.setattr(api_v1, "control_connection", control)
    monkeypatch.setattr(api_v1, "check_access_token", check_access_token)
    monkeypatch.setattr(api_v1, "get_report_account", lookup)
    monkeypatch.setattr(api_v1, "get_account", forbidden)
    monkeypatch.setattr(auth, "load_account_settings", forbidden)
    return calls


def _profile(**changes):
    return {"id": 7, "is_admin": False, "is_enabled": True, "auth_version": 3, "has_avatar": True,
            "avatar_updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc), **changes}


def test_bearer_report_identity_binds_the_principal_without_variable_data(monkeypatch):
    calls = _install(monkeypatch, _profile())
    request = _request()
    api_user = asyncio.run(require_api_report_user(request))
    assert calls == [("check", TOKEN), ("lookup", 7)]
    assert isinstance(api_user, ApiUser) and api_user.grant_id == "grant"
    # The same identity the web's report profile hands the pipeline.
    assert api_user.user == {"id": 7, "is_admin": False, "is_enabled": True, "legacy_oidc": False,
                             "has_avatar": True, "avatar_version": api_user.user["avatar_version"]}
    assert request.state.principal == AccountPrincipal(7, True, 3)
    assert request.state.account_pool.principal == request.state.principal
    assert not hasattr(request.state, "account_settings")
    # Admission caches the binding, so the handler does not check again.
    request.state._capacity_authenticated_user = api_user
    assert asyncio.run(require_api_report_user(request)) is api_user
    assert len(calls) == 2


@pytest.mark.parametrize("changes", [{"is_enabled": False}, {"auth_version": 4}])
def test_bearer_report_identity_refuses_a_disabled_or_signed_out_account(monkeypatch, changes):
    _install(monkeypatch, _profile(**changes))
    request = _request()
    with pytest.raises(ApiError) as raised:
        asyncio.run(require_api_report_user(request))
    assert raised.value.code == "invalid_token"
    assert raised.value.headers == {"WWW-Authenticate": "Bearer"}
    assert not hasattr(request.state, "principal")


def test_bearer_report_identity_refuses_a_missing_token_without_a_lookup(monkeypatch):
    calls = _install(monkeypatch, _profile())
    with pytest.raises(ApiError) as raised:
        asyncio.run(require_api_report_user(_request(headers=())))
    assert raised.value.code == "invalid_token" and calls == []


@pytest.mark.parametrize("filename,disposition", [
    ("trips.csv", 'attachment; filename="trips.csv"'),
    ("mileage-report-2026-Q2.xlsx", 'attachment; filename="mileage-report-2026-Q2.xlsx"'),
    ("Fahrtenbuch ä車.xlsx", "attachment; filename*=utf-8''Fahrtenbuch%20%C3%A4%E8%BB%8A.xlsx"),
])
def test_prepared_file_attachment_header(filename, disposition):
    # No pipeline filename is non-ASCII today; the header form is the response's own.
    response = PreparedFileResponse(None, None, 1234, media_type=XLSX_MEDIA_TYPE, filename=filename)
    assert response.headers["content-disposition"] == disposition
    assert response.headers["content-length"] == "1234"
    assert response.headers["content-type"] == XLSX_MEDIA_TYPE


def test_a_cached_report_identity_never_satisfies_a_full_profile_request(monkeypatch):
    calls = _install(monkeypatch, _profile())
    full_account = {"id": 7, "is_enabled": True, "auth_version": 3, "email": "owner@example.invalid"}

    async def get_account(conn, owner):
        calls.append(("full", owner))
        return full_account

    async def bind_account(request, account, **kwargs):
        request.state.principal = AccountPrincipal(account["id"], True, 3)
        return {"id": account["id"], "profile": "report" if kwargs.get("report_profile") else "full"}

    monkeypatch.setattr(api_v1, "get_account", get_account)
    monkeypatch.setattr(api_v1, "_bind_account", bind_account)
    request = _request()
    thin = asyncio.run(require_api_report_user(request))
    assert thin.report_profile and thin.user["profile"] == "report"
    request.state._capacity_authenticated_user = thin
    assert asyncio.run(require_api_report_user(request)) is thin
    full = asyncio.run(api_v1.require_api_user(request))
    assert not full.report_profile and full.user["profile"] == "full"
    assert full.account is full_account
    assert calls == [("check", TOKEN), ("lookup", 7), ("check", TOKEN), ("full", 7)]
    # A full binding already holds everything a report needs.
    request.state._capacity_authenticated_user = full
    assert asyncio.run(require_api_report_user(request)) is full
    assert asyncio.run(api_v1.require_api_user(request)) is full
    assert len(calls) == 4
