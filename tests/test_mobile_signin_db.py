"""Mobile sign-in end to end against PostGIS: the real application, browser
logins (local and OIDC), the pending request cookie, the confirm page, code
exchange, the active-grant limit, revocation and the setting."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from psycopg import errors
from starlette.responses import RedirectResponse

import app.main as main_module
from app.account_context import control_connection
from app.accounts import create_admin, get_account, sign_out_everywhere
from app.api_grants import ACTIVE_GRANT_LIMIT, create_grant, token_digest
from app.config import Config
from app.db import make_pool
from app.local_auth import hash_password
from app.mobile_authorization import (
    COOKIE_NAME, PENDING_REQUEST_CAP, approve_request, exchange_code, start_request,
)
from app.oidc_identities import create_identity_link
from conftest import provisioned_role_pools, reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")

CALLBACK = "odograph://auth/callback"
ISSUER = "https://idp.example.invalid"
LOCAL_EMAIL = "local-owner@example.invalid"
OIDC_EMAIL = "oidc-member@example.invalid"
PASSWORD = "mobile sign-in password"
SUBJECT = "linked-subject"


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class _Provider:
    def __init__(self):
        self.userinfo = {}

    async def authorize_redirect(self, _request, redirect_uri, **kwargs):
        return RedirectResponse(
            f"https://idp.example.invalid/authorize?{urlencode({'redirect_uri': redirect_uri, **kwargs})}",
            status_code=303)

    async def authorize_access_token(self, _request):
        return {"userinfo": self.userinfo}


def _env(monkeypatch, *, mobile_signin: bool):
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    monkeypatch.setenv("SESSION_SECRET", "disposable-session-secret-long-enough-for-config")
    monkeypatch.setenv("DEV_NO_AUTH", "0")
    monkeypatch.setenv("MOBILE_SIGNIN", "1" if mobile_signin else "0")
    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("OIDC_CLIENT_ID", "test-client")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "test-secret")
    for name in ("INITIAL_ADMIN_SIGNUP", "EMAIL_TO", "OSRM_URL", "GEOCODE_API_KEY", "GEOCODE_PROVIDER",
                 "NTFY_URL", "SMTP_HOST", "EMAIL_FROM", "APP_URL"):
        monkeypatch.delenv(name, raising=False)


class _Site:
    def __init__(self, app, owner, provider, local, member):
        self.app, self.owner, self.provider = app, owner, provider
        self.local, self.member = local, member
        self.cookies_seen: dict[str, list[str | None]] = {}

    def client(self):
        async def record(request):
            self.cookies_seen.setdefault(request.url.path, []).append(request.headers.get("cookie"))

        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://test",
                                 event_hooks={"request": [record]})

    async def sql(self, statement, params=()):
        async with self.owner.connection() as conn:
            cur = await conn.execute(statement, params)
            return await cur.fetchall() if cur.description else None

    async def mint(self, account_id, label="Phone", replaces=None):
        async with control_connection(self.app.state.control_pool) as conn:
            account = await get_account(conn, account_id)
            return await create_grant(conn, account_id, account["auth_version"], label, replaces=replaces)


async def _run_site(monkeypatch, check, *, mobile_signin=True):
    _env(monkeypatch, mobile_signin=mobile_signin)
    owner = make_pool(TEST_DB)
    await owner.open(wait=True)
    try:
        await reset_db(owner)
        app = main_module.create_app(Config.from_env())
        async with app.router.lifespan_context(app):
            provider = _Provider()
            app.state.oauth = SimpleNamespace(pocketid=provider)
            async with control_connection(app.state.control_pool) as conn:
                local = await create_admin(conn, LOCAL_EMAIL, hash_password(PASSWORD))
            async with owner.connection() as conn:
                member_id = (await (await conn.execute(
                    "INSERT INTO accounts(email,password_hash,is_admin) VALUES (%s,NULL,false) RETURNING id",
                    (OIDC_EMAIL,))).fetchone())[0]
                await conn.execute("INSERT INTO account_settings(account_id) VALUES (%s)", (member_id,))
            async with control_connection(app.state.control_pool) as conn:
                await create_identity_link(conn, member_id, ISSUER, SUBJECT)
                member = await get_account(conn, member_id)
            await check(_Site(app, owner, provider, local, member))
    finally:
        await owner.close()


def _site(monkeypatch, check, **kwargs):
    asyncio.run(_run_site(monkeypatch, check, **kwargs))


def _csrf(page) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


async def _login_local(client, email=LOCAL_EMAIL, password=PASSWORD):
    page = await client.get("/login")
    return await client.post("/login/local", data={"email": email, "password": password,
                                                   "csrf_token": _csrf(page)})


async def _login_oidc(site, client, subject=SUBJECT):
    started = await client.get("/login/oidc")
    assert started.status_code == 303
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    site.provider.userinfo = {"sub": subject}
    return await client.get("/auth/callback", params={"state": state, "code": "provider-code"})


async def _authorize(client, challenge, *, state="app-state-1", label="Phone", replaces=None):
    params = {"client_id": "odograph-ios", "redirect_uri": CALLBACK, "code_challenge": challenge,
              "code_challenge_method": "S256", "state": state, "device_label": label}
    if replaces is not None:
        params["replaces"] = replaces
    return await client.get("/mobile/authorize", params=params)


def _callback(page) -> dict[str, str]:
    assert page.status_code == 200, page.text
    href = html.unescape(re.search(r'id="mobile-callback-link"[^>]*href="([^"]+)"', page.text).group(1))
    target = urlsplit(href)
    assert f"{target.scheme}://{target.netloc}{target.path}" == CALLBACK
    return {key: values[0] for key, values in parse_qs(target.query).items()}


async def _decide(client, decision="approve"):
    page = await client.get("/mobile/authorize/confirm")
    assert page.status_code == 200, page.text
    finished = await client.post("/mobile/authorize/confirm",
                                 data={"decision": decision, "csrf_token": _csrf(page)})
    return page, finished


async def _exchange(client, code, verifier):
    return await client.post("/api/v1/auth/token", json={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier})


def _cleared(response) -> bool:
    header = response.headers.get("set-cookie", "")
    return f'{COOKIE_NAME}=""' in header and "Max-Age=0" in header


async def _sign_in_app(client, *, state="app-state-1", label="Phone", replaces=None):
    """Authorize from an already signed-in browser and exchange the code."""
    verifier, challenge = _pkce()
    started = await _authorize(client, challenge, state=state, label=label, replaces=replaces)
    assert started.headers["location"] == "/mobile/authorize/confirm"
    _, finished = await _decide(client)
    params = _callback(finished)
    assert params["state"] == state
    return await _exchange(client, params["code"], verifier)


def test_local_account_signs_in_from_a_signed_out_browser(monkeypatch):
    async def check(site):
        async with site.client() as client:
            verifier, challenge = _pkce()
            started = await _authorize(client, challenge, state="local-state")
            assert started.status_code == 303 and started.headers["location"] == "/login"
            assert started.headers["cache-control"] == "no-store, private"
            assert started.headers["referrer-policy"] == "no-referrer"
            cookie = started.headers["set-cookie"]
            assert cookie.startswith(f"{COOKIE_NAME}=")
            for attribute in ("HttpOnly", "Max-Age=600", "Path=/", "SameSite=lax", "Secure"):
                assert attribute in cookie, attribute
            value = client.cookies.get(COOKIE_NAME)
            assert re.fullmatch(r"[A-Za-z0-9_-]{43}", value)
            assert (await site.sql("SELECT count(*) FROM mobile_authorization_requests "
                                   "WHERE request_digest=%s", (token_digest(value),))) == [(1,)]

            login = await _login_local(client)
            assert login.status_code == 303 and login.headers["location"] == "/mobile/authorize/confirm"
            assert f"{COOKIE_NAME}={value}" in site.cookies_seen["/login/local"][-1]

            page, finished = await _decide(client)
            assert "local-owner" in page.text and LOCAL_EMAIL in page.text and "Phone" in page.text
            assert page.headers["cache-control"] == "no-store, private"
            assert _cleared(finished)
            assert finished.headers["cache-control"] == "no-store, private"
            params = _callback(finished)
            assert set(params) == {"code", "state"} and params["state"] == "local-state"
            assert client.cookies.get(COOKIE_NAME) is None

            exchanged = await _exchange(client, params["code"], verifier)
            assert exchanged.status_code == 200, exchanged.text
            assert exchanged.headers["cache-control"] == "no-store"
            tokens = exchanged.json()
            assert set(tokens) == {"access_token", "refresh_token", "token_type", "access_expires_at",
                                   "expires_in"}
            me = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
            assert me.status_code == 200 and me.json()["account_id"] == site.local["id"]

            # A replayed code is refused and the grant it produced keeps working.
            replay = await _exchange(client, params["code"], verifier)
            assert replay.status_code == 400 and replay.json()["error"]["code"] == "invalid_grant"
            again = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
            assert again.status_code == 200
            assert (await site.sql("SELECT count(*), count(revoked_at) FROM api_grants")) == [(1, 0)]
            assert (await site.sql("SELECT count(*) FROM mobile_authorization_requests")) == [(0,)]
    _site(monkeypatch, check)


def test_local_account_signs_in_from_a_signed_in_browser(monkeypatch):
    async def check(site):
        async with site.client() as client:
            assert (await _login_local(client)).headers["location"] == "/"
            exchanged = await _sign_in_app(client)
            assert exchanged.status_code == 200
            assert (await site.sql("SELECT account_id FROM api_grants")) == [(site.local["id"],)]
    _site(monkeypatch, check)


def test_linked_oidc_account_signs_in_from_signed_out_and_signed_in_browsers(monkeypatch):
    async def check(site):
        async with site.client() as client:
            verifier, challenge = _pkce()
            started = await _authorize(client, challenge, state="oidc-state")
            assert started.headers["location"] == "/login"
            value = client.cookies.get(COOKIE_NAME)
            callback = await _login_oidc(site, client)
            assert callback.status_code == 303
            assert callback.headers["location"] == "/mobile/authorize/confirm"
            assert f"{COOKIE_NAME}={value}" in site.cookies_seen["/auth/callback"][-1]
            page, finished = await _decide(client)
            assert OIDC_EMAIL in page.text
            params = _callback(finished)
            assert params["state"] == "oidc-state"
            first = await _exchange(client, params["code"], verifier)
            assert first.status_code == 200

            # Already signed in through OIDC: straight to the confirm page.
            second = await _sign_in_app(client, state="oidc-state-2")
            assert second.status_code == 200
            assert (await site.sql("SELECT account_id, count(*) FROM api_grants GROUP BY account_id")) == [
                (site.member["id"], 2)]
    _site(monkeypatch, check)


def test_denial_returns_access_denied_and_clears_the_cookie(monkeypatch):
    async def check(site):
        async with site.client() as client:
            await _login_local(client)
            _, challenge = _pkce()
            await _authorize(client, challenge, state="deny-state")
            _, finished = await _decide(client, "deny")
            assert _cleared(finished)
            assert _callback(finished) == {"error": "access_denied", "state": "deny-state"}
            assert (await site.sql("SELECT count(*) FROM mobile_authorization_codes")) == [(0,)]
            assert (await site.sql("SELECT count(*) FROM mobile_authorization_requests")) == [(0,)]
            # The request is finished: the confirm page now reports it expired.
            late = await client.get("/mobile/authorize/confirm")
            assert late.status_code == 410
    _site(monkeypatch, check)


def test_expired_requests_show_the_expired_page_and_logins_go_home(monkeypatch):
    async def check(site):
        async with site.client() as client:
            _, challenge = _pkce()
            await _authorize(client, challenge)
            await site.sql("UPDATE mobile_authorization_requests SET created_at=now()-interval '11 minutes', "
                           "expires_at=now()-interval '1 minute'")
            assert (await _login_local(client)).headers["location"] == "/"
            expired = await client.get("/mobile/authorize/confirm")
            assert expired.status_code == 410 and "expired" in expired.text
            assert _cleared(expired)
            # Without the cookie the confirm page and its form are the same expired page.
            again = await client.get("/mobile/authorize/confirm")
            assert again.status_code == 410
            refused = await client.post("/mobile/authorize/confirm",
                                        data={"decision": "approve",
                                              "csrf_token": _csrf(await client.get("/login"))})
            assert refused.status_code == 410
    _site(monkeypatch, check)


def test_confirm_and_decision_require_a_session_and_csrf(monkeypatch):
    async def check(site):
        async with site.client() as client:
            _, challenge = _pkce()
            await _authorize(client, challenge)
            anonymous = await client.get("/mobile/authorize/confirm")
            assert anonymous.status_code == 303 and anonymous.headers["location"] == "/login"
            await _login_local(client)
            page = await client.get("/mobile/authorize/confirm")
            forged = await client.post("/mobile/authorize/confirm",
                                       data={"decision": "approve", "csrf_token": "forged"})
            assert forged.status_code == 403
            unknown = await client.post("/mobile/authorize/confirm",
                                        data={"decision": "maybe", "csrf_token": _csrf(page)})
            assert unknown.status_code == 400
            assert (await site.sql("SELECT count(*) FROM mobile_authorization_requests")) == [(1,)]
            assert (await site.sql("SELECT count(*) FROM mobile_authorization_codes")) == [(0,)]
    _site(monkeypatch, check)


def test_codes_expire_and_a_wrong_verifier_consumes_the_code(monkeypatch):
    async def check(site):
        async with site.client() as client:
            await _login_local(client)
            verifier, challenge = _pkce()
            await _authorize(client, challenge)
            _, finished = await _decide(client)
            code = _callback(finished)["code"]
            wrong = await _exchange(client, code, _pkce()[0])
            assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "invalid_grant"
            assert (await site.sql("SELECT consumed_at IS NOT NULL FROM mobile_authorization_codes")) == [(True,)]
            right = await _exchange(client, code, verifier)
            assert right.status_code == 400
            assert (await site.sql("SELECT count(*) FROM api_grants")) == [(0,)]

            verifier, challenge = _pkce()
            await _authorize(client, challenge)
            _, finished = await _decide(client)
            code = _callback(finished)["code"]
            await site.sql("UPDATE mobile_authorization_codes SET created_at=now()-interval '2 minutes', "
                           "expires_at=now()-interval '1 minute' WHERE consumed_at IS NULL")
            late = await _exchange(client, code, verifier)
            assert late.status_code == 400 and late.json()["error"]["code"] == "invalid_grant"
            assert (await site.sql("SELECT count(*) FROM api_grants")) == [(0,)]
    _site(monkeypatch, check)


def test_a_code_issued_before_an_auth_version_bump_fails(monkeypatch):
    async def check(site):
        async with site.client() as client:
            await _login_local(client)
            verifier, challenge = _pkce()
            await _authorize(client, challenge)
            _, finished = await _decide(client)
            code = _callback(finished)["code"]
            async with control_connection(site.app.state.control_pool) as conn:
                assert await sign_out_everywhere(conn, site.local["id"], expected_auth_version=1)
            refused = await _exchange(client, code, verifier)
            assert refused.status_code == 400 and refused.json()["error"]["code"] == "invalid_grant"
            assert (await site.sql("SELECT count(*) FROM api_grants")) == [(0,)]
    _site(monkeypatch, check)


def test_replaces_ends_only_the_named_grant_of_the_signed_in_account(monkeypatch):
    async def check(site):
        old = await site.mint(site.local["id"], label="Old <Phone>")
        other = await site.mint(site.local["id"], label="Tablet")
        foreign = await site.mint(site.member["id"], label="Member phone")
        async with site.client() as client:
            await _login_local(client)
            # Another account's grant id is ignored: not named, not revoked.
            _, challenge = _pkce()
            await _authorize(client, challenge, replaces=foreign.grant_id)
            page = await client.get("/mobile/authorize/confirm")
            assert "Member phone" not in page.text and "ends the earlier sign-in" not in page.text
            await client.post("/mobile/authorize/confirm", data={"decision": "deny", "csrf_token": _csrf(page)})

            verifier, challenge = _pkce()
            await _authorize(client, challenge, replaces=old.grant_id)
            page = await client.get("/mobile/authorize/confirm")
            assert "ends the earlier sign-in from <strong>Old &lt;Phone&gt;</strong>" in page.text
            finished = await client.post("/mobile/authorize/confirm",
                                         data={"decision": "approve", "csrf_token": _csrf(page)})
            assert (await _exchange(client, _callback(finished)["code"], verifier)).status_code == 200

            rows = dict(await site.sql("SELECT public_id, revoked_at IS NOT NULL FROM api_grants"))
            assert rows[old.grant_id] is True
            assert rows[other.grant_id] is False and rows[foreign.grant_id] is False
            for kept in (other, foreign):
                me = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {kept.access_token}"})
                assert me.status_code == 200
    _site(monkeypatch, check)


def test_at_the_limit_sign_in_is_refused_without_revoking_and_replaces_still_works(monkeypatch):
    async def check(site):
        grants = [await site.mint(site.local["id"], label=f"Device {n}") for n in range(ACTIVE_GRANT_LIMIT)]
        async with site.client() as client:
            await _login_local(client)
            _, challenge = _pkce()
            await _authorize(client, challenge, state="limit-state")
            page = await client.get("/mobile/authorize/confirm")
            assert page.status_code == 200
            assert f"{ACTIVE_GRANT_LIMIT} signed-in apps" in page.text and 'href="/settings/account"' in page.text
            assert 'value="approve"' not in page.text
            # A forged approval is refused the same way.
            forged = await client.post("/mobile/authorize/confirm",
                                       data={"decision": "approve", "csrf_token": _csrf(page)})
            assert _callback(forged) == {"error": "grant_limit_reached", "state": "limit-state"}
            assert _cleared(forged)

            _, challenge = _pkce()
            await _authorize(client, challenge, state="limit-state-2")
            page = await client.get("/mobile/authorize/confirm")
            finished = await client.post("/mobile/authorize/confirm",
                                         data={"decision": "deny", "csrf_token": _csrf(page)})
            assert _callback(finished) == {"error": "grant_limit_reached", "state": "limit-state-2"}
            assert (await site.sql("SELECT count(*), count(revoked_at) FROM api_grants")) == [(10, 0)]
            assert (await site.sql("SELECT count(*) FROM mobile_authorization_codes")) == [(0,)]

            # A sign-in that replaces one of the ten is net zero.
            replaced = await _sign_in_app(client, replaces=grants[3].grant_id)
            assert replaced.status_code == 200
            assert (await site.sql("SELECT count(*) FILTER (WHERE revoked_at IS NULL), "
                                   "count(*) FILTER (WHERE revoked_at IS NOT NULL) FROM api_grants")) == [(10, 1)]
    _site(monkeypatch, check)


def test_a_racing_exchange_at_the_limit_is_409_and_leaves_the_code_unconsumed(monkeypatch):
    async def check(site):
        grants = [await site.mint(site.local["id"]) for _ in range(ACTIVE_GRANT_LIMIT - 1)]
        async with site.client() as client:
            await _login_local(client)
            verifier, challenge = _pkce()
            await _authorize(client, challenge)
            _, finished = await _decide(client)
            code = _callback(finished)["code"]
            await site.mint(site.local["id"], label="Racer")
            refused = await _exchange(client, code, verifier)
            assert refused.status_code == 409
            assert refused.json()["error"] == {"code": "grant_limit_reached",
                                               "message": "This account has reached its limit of signed-in apps.",
                                               "fields": None}
            assert (await site.sql("SELECT consumed_at FROM mobile_authorization_codes")) == [(None,)]
            assert (await site.sql("SELECT count(*), count(revoked_at) FROM api_grants")) == [(10, 0)]
            async with control_connection(site.app.state.control_pool) as conn:
                from app.api_grants import revoke_grant
                assert await revoke_grant(conn, site.local["id"], grants[0].grant_id)
            assert (await _exchange(client, code, verifier)).status_code == 200
    _site(monkeypatch, check)


def test_an_over_allowance_account_can_sign_in_and_rows_are_uncharged(monkeypatch):
    async def check(site):
        await site.sql("UPDATE storage_grants SET limit_mode='custom', account_limit_bytes=1, "
                       "raw_limit_bytes=1, enhancement_limit_bytes=1 WHERE account_id=%s", (site.member["id"],))
        before = await site.sql("SELECT account_id, actual_bytes, reserved_bytes, raw_bytes, enhancement_bytes "
                                "FROM account_usage ORDER BY account_id")
        async with site.client() as client:
            verifier, challenge = _pkce()
            await _authorize(client, challenge)
            await _login_oidc(site, client)
            _, finished = await _decide(client)
            assert (await _exchange(client, _callback(finished)["code"], verifier)).status_code == 200
            _, challenge = _pkce()
            await _authorize(client, challenge)
        assert await site.sql("SELECT account_id, actual_bytes, reserved_bytes, raw_bytes, enhancement_bytes "
                              "FROM account_usage ORDER BY account_id") == before
        assert await site.sql("SELECT public.storage_usage_consistent()") == [(True,)]
        for table in ("mobile_authorization_requests", "mobile_authorization_codes"):
            assert await site.sql("SELECT count(*) FROM pg_trigger WHERE tgrelid=%s::regclass "
                                  "AND NOT tgisinternal", (f"public.{table}",)) == [(0,)]
    _site(monkeypatch, check)


def test_labels_are_stored_normalized_and_render_escaped(monkeypatch):
    async def check(site):
        label = "<script>alert(1)</script>\u0000​Pixel" + "x" * 80
        async with site.client() as client:
            await _login_local(client)
            verifier, challenge = _pkce()
            await _authorize(client, challenge, label=label)
            stored = (await site.sql("SELECT device_label FROM mobile_authorization_requests"))[0][0]
            assert stored == ("<script>alert(1)</script>Pixel" + "x" * 80)[:64]
            page, finished = await _decide(client)
            assert "<script>alert(1)" not in page.text and "&lt;script&gt;alert(1)&lt;/script&gt;Pixel" in page.text
            assert (await _exchange(client, _callback(finished)["code"], verifier)).status_code == 200
            assert (await site.sql("SELECT device_label FROM api_grants")) == [(stored,)]
    _site(monkeypatch, check)


def test_a_new_authorize_from_the_same_browser_replaces_its_pending_request(monkeypatch):
    async def check(site):
        async with site.client() as first, site.client() as second:
            for _ in range(3):
                await _authorize(first, _pkce()[1])
            assert (await site.sql("SELECT count(*) FROM mobile_authorization_requests")) == [(1,)]
            await _authorize(second, _pkce()[1])
            assert (await site.sql("SELECT count(*) FROM mobile_authorization_requests")) == [(2,)]
            digest = token_digest(first.cookies.get(COOKIE_NAME))
            assert (await site.sql("SELECT count(*) FROM mobile_authorization_requests "
                                   "WHERE request_digest=%s", (digest,))) == [(1,)]
    _site(monkeypatch, check)


def test_the_pending_request_cap_returns_the_busy_page_and_expired_rows_free_it(monkeypatch):
    async def check(site):
        await site.sql(
            "INSERT INTO mobile_authorization_requests(request_digest,client_id,redirect_uri,code_challenge,"
            "state,device_label,created_at,expires_at) SELECT md5(i::text)||md5((i+1)::text),'odograph-ios',"
            "%s,repeat('c',43),'s','Phone',now(),now()+interval '10 minutes' FROM generate_series(1,%s) i",
            (CALLBACK, PENDING_REQUEST_CAP))
        async with site.client() as client:
            busy = await _authorize(client, _pkce()[1], state="cap-state")
            assert busy.status_code == 503 and busy.headers["retry-after"] == "1"
            assert COOKIE_NAME not in busy.headers.get("set-cookie", "")
            assert "cap-state" not in busy.text
            await site.sql("UPDATE mobile_authorization_requests SET created_at=now()-interval '11 minutes', "
                           "expires_at=now()-interval '1 minute'")
            started = await _authorize(client, _pkce()[1])
            assert started.status_code == 303
            assert (await site.sql("SELECT count(*) FROM mobile_authorization_requests")) == [(1,)]
    _site(monkeypatch, check)


def test_logins_without_a_pending_request_keep_their_destinations(monkeypatch):
    async def check(site):
        async with site.client() as client:
            local = await _login_local(client)
            assert (local.status_code, local.headers["location"]) == (303, "/")
            assert COOKIE_NAME not in local.headers.get("set-cookie", "")
        async with site.client() as client:
            oidc = await _login_oidc(site, client)
            assert (oidc.status_code, oidc.headers["location"]) == (303, "/")
        async with site.client() as client:
            # A cookie naming no live request changes nothing.
            client.cookies.set(COOKIE_NAME, secrets.token_urlsafe(32), domain="test")
            assert (await _login_local(client)).headers["location"] == "/"
            client.cookies.set(COOKIE_NAME, "not a request id", domain="test")
            assert (await _login_oidc(site, client)).headers["location"] == "/"
    _site(monkeypatch, check)


def test_with_signin_off_existing_grants_refresh_call_me_and_revoke_themselves(monkeypatch):
    async def check(site):
        issued = await site.mint(site.local["id"])
        async with site.client() as client:
            _, challenge = _pkce()
            assert (await _authorize(client, challenge)).status_code == 404
            assert (await client.get("/mobile/authorize/confirm")).status_code == 404
            await _login_local(client)
            assert (await client.get("/mobile/authorize/confirm")).status_code == 404
            assert (await client.post("/mobile/authorize/confirm",
                                      data={"decision": "approve", "csrf_token": "x"})).status_code == 404
            # A leftover cookie does not divert the login either.
            client.cookies.set(COOKIE_NAME, secrets.token_urlsafe(32), domain="test")
            assert (await _login_local(client)).headers["location"] == "/"

            exchange = await _exchange(client, secrets.token_urlsafe(32), _pkce()[0])
            assert exchange.status_code == 403 and exchange.json()["error"]["code"] == "signin_disabled"
            capabilities = await client.get("/api/v1/capabilities")
            assert capabilities.json()["mobile_signin_available"] is False

            refreshed = await client.post("/api/v1/auth/token", json={
                "grant_type": "refresh_token", "refresh_token": issued.refresh_token})
            assert refreshed.status_code == 200
            bearer = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
            assert (await client.get("/api/v1/me", headers=bearer)).status_code == 200
            revoked = await client.post("/api/v1/auth/revoke", headers=bearer)
            assert revoked.status_code == 204 and revoked.content == b""
            assert (await client.get("/api/v1/me", headers=bearer)).status_code == 401
            assert (await client.post("/api/v1/auth/revoke", headers=bearer)).status_code == 401
            assert (await site.sql("SELECT count(revoked_at) FROM api_grants")) == [(1,)]
    _site(monkeypatch, check, mobile_signin=False)


def test_capabilities_and_revoke_with_signin_on(monkeypatch):
    async def check(site):
        async with site.client() as client:
            assert (await client.get("/api/v1/capabilities")).json()["mobile_signin_available"] is True
            await _login_local(client)
            first = (await _sign_in_app(client)).json()
            second = (await _sign_in_app(client, state="second")).json()
            revoked = await client.post("/api/v1/auth/revoke",
                                        headers={"Authorization": f"Bearer {first['access_token']}"})
            assert revoked.status_code == 204
            assert (await client.get("/api/v1/me", headers={
                "Authorization": f"Bearer {first['access_token']}"})).status_code == 401
            assert (await client.get("/api/v1/me", headers={
                "Authorization": f"Bearer {second['access_token']}"})).status_code == 200
    _site(monkeypatch, check)


def test_logs_responses_and_audit_rows_never_contain_secrets(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    async def check(site):
        secrets_seen = []
        haystacks = []
        async with site.client() as client:
            verifier, challenge = _pkce()
            state = "secret-state-" + secrets.token_urlsafe(8)
            started = await _authorize(client, challenge, state=state)
            cookie = client.cookies.get(COOKIE_NAME)
            secrets_seen += [verifier, challenge, state, cookie, token_digest(cookie)]
            haystacks.append(started.text + repr(dict(started.headers)).replace(cookie, ""))
            login = await _login_local(client)
            page, finished = await _decide(client)
            code = _callback(finished)["code"]
            secrets_seen += [code, token_digest(code)]
            wrong = await _exchange(client, code, _pkce()[0])
            verifier2, challenge2 = _pkce()
            await _authorize(client, challenge2, state=state)
            page2, finished2 = await _decide(client)
            code2 = _callback(finished2)["code"]
            exchanged = await _exchange(client, code2, verifier2)
            tokens = exchanged.json()
            secrets_seen += [verifier2, challenge2, code2, token_digest(code2), tokens["access_token"],
                             tokens["refresh_token"], token_digest(tokens["access_token"]),
                             token_digest(tokens["refresh_token"])]
            replay = await _exchange(client, code2, verifier2)
            for response in (login, page, wrong, page2, replay):
                haystacks.append(response.text + repr(dict(response.headers)))
            haystacks.append(exchanged.text.replace(tokens["access_token"], "").replace(
                tokens["refresh_token"], ""))
        audit = await site.sql("SELECT * FROM account_security_audit")
        haystacks += [caplog.text, repr(audit)]
        for secret in secrets_seen:
            assert secret
            for haystack in haystacks:
                assert secret not in haystack
        assert "mobile authorization approved" in caplog.text
        assert "verifier mismatch" in caplog.text
    _site(monkeypatch, check)


async def _wait_for_lock_waiters(owner, pids):
    deadline = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < deadline:
        async with owner.connection() as conn:
            rows = await (await conn.execute(
                "SELECT pid FROM pg_stat_activity WHERE pid = ANY(%s) AND wait_event_type = 'Lock'",
                (list(pids),))).fetchall()
        if {row[0] for row in rows} == set(pids):
            return
        await asyncio.sleep(0.05)
    pytest.fail("exchanges did not both wait on the code row lock")


@asynccontextmanager
async def _role_pools():
    owner = make_pool(TEST_DB)
    await owner.open(wait=True)
    try:
        await reset_db(owner)
        async with provisioned_role_pools(owner) as pools:
            yield owner, pools
    finally:
        await owner.close()


def test_two_concurrent_exchanges_of_one_code_yield_exactly_one_grant():
    async def scenario():
        async with _role_pools() as (owner, pools):
            async with pools.control.connection() as conn:
                account = await create_admin(conn, LOCAL_EMAIL, "unused")
                verifier, challenge = _pkce()
                started = await start_request(
                    conn, previous_cookie=None, client_id="odograph-ios", redirect_uri=CALLBACK,
                    code_challenge=challenge, state="race", device_label="Phone", replaces=None)
                completion = await approve_request(conn, started.cookie_value, account["id"], 1)
            pids = []

            async def exchange():
                async with pools.control.connection() as conn:
                    pids.append(conn.info.backend_pid)
                    return await exchange_code(conn, completion.code, verifier)

            async with owner.connection() as holder:
                await holder.execute("SELECT 1 FROM mobile_authorization_codes FOR UPDATE")
                tasks = [asyncio.create_task(exchange()) for _ in range(2)]
                while len(pids) < 2:
                    await asyncio.sleep(0.01)
                await _wait_for_lock_waiters(owner, pids)
            outcomes = await asyncio.gather(*tasks)
            assert sorted(outcome is None for outcome in outcomes) == [False, True]
            async with owner.connection() as conn:
                assert await (await conn.execute("SELECT count(*) FROM api_grants")).fetchone() == (1,)
    asyncio.run(scenario())


def test_rows_are_reached_only_through_their_functions():
    async def scenario():
        async with _role_pools() as (owner, pools):
            async with pools.control.connection() as conn:
                await create_admin(conn, LOCAL_EMAIL, "unused")
            async with owner.connection() as conn:
                member = (await (await conn.execute(
                    "INSERT INTO accounts(email,password_hash,is_admin) VALUES (%s,'unused',false) "
                    "RETURNING id", (OIDC_EMAIL,))).fetchone())[0]
            async with pools.control.connection() as conn:
                started = await start_request(
                    conn, previous_cookie=None, client_id="odograph-ios", redirect_uri=CALLBACK,
                    code_challenge=_pkce()[1], state="s", device_label="Phone", replaces=None)
                assert (await approve_request(conn, started.cookie_value, member, 1)).code
            for pool in (pools.control, pools.runtime):
                for table in ("mobile_authorization_requests", "mobile_authorization_codes"):
                    async with pool.connection() as conn:
                        with pytest.raises(errors.InsufficientPrivilege):
                            await conn.execute(f"SELECT * FROM {table}")
            async with owner.connection() as conn:
                await conn.execute("DELETE FROM accounts WHERE id=%s", (member,))
                assert await (await conn.execute(
                    "SELECT count(*) FROM mobile_authorization_codes")).fetchone() == (0,)
    asyncio.run(scenario())
