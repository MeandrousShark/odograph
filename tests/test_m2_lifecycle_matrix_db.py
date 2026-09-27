"""Integrated restricted-role onboarding and lifecycle acceptance slice.

The OIDC provider below is a protocol stub. No external provider or mail
service is contacted by this test.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi import Depends, Request
from starlette.responses import RedirectResponse

from app import auth, portable
from app.account_context import AccountPool, AccountPrincipal
from app.accounts import create_admin
from app.application_roles import application_role_pools, prepare_application_roles
from app.db import make_pool
from app.email_challenges import PURPOSE_CURRENT, issue_email_challenge
from app.local_auth import hash_password
from app.tracking import create_device
from conftest import full_schema_reset
from tests.auth_db_fixtures import auth_config
from tests.test_admin_routes_db import _app, _client
from tests.oidc_test_helpers import oidc_authorization_url

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")


class _Provider:
    """An in-process OIDC protocol stub for invitation, linking and login."""

    def __init__(self):
        self.userinfo = {}
        self.redirect_kwargs = None

    async def authorize_redirect(self, _request, _redirect_uri, **kwargs):
        self.redirect_kwargs = kwargs
        query = urlencode({"redirect_uri": _redirect_uri, **kwargs})
        return RedirectResponse(
            f"https://idp.example/authorize?{query}", status_code=303
        )

    async def authorize_access_token(self, _request):
        return {"userinfo": self.userinfo}


def _app_with_auth(pools, provider):
    config = auth_config(
        TEST_DB,
        initial_admin_signup=False,
        dev_no_auth=False,
        oidc_client_id="matrix-client",
        oidc_client_secret="matrix-secret",
        oidc_issuer="https://idp.example/",
    )
    app = _app(pools, config=config)
    app.state.oauth = SimpleNamespace(pocketid=provider)
    app.include_router(auth.make_router())
    app.include_router(portable.make_router())

    @app.get("/test/who")
    async def who(user: dict = Depends(auth.require_user)):
        return {"id": user["id"]}

    @app.get("/test/session")
    async def current_session(request: Request):
        return dict(request.session)

    return app


async def _session(client) -> dict:
    response = await client.get("/test/session")
    assert response.status_code == 200
    return response.json()


async def _form_csrf(client, path: str) -> str:
    response = await client.get(path)
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match, f"{path} did not render a CSRF token"
    return match.group(1)


async def _issue_invitation(admin_client, email: str) -> str:
    response = await admin_client.post(
        "/admin/invitations",
        data={"csrf_token": "route-csrf", "email": email},
    )
    assert response.status_code == 200
    match = re.search(r'id="admin-invitation-token" value="([^"]+)"', response.text)
    assert match, f"admin invitation response omitted the one-time token for {email}"
    return match.group(1)


async def _complete_oidc(client, provider, *, userinfo: dict, authorization_url=None):
    state = (
        parse_qs(urlsplit(authorization_url).query)["state"][0]
        if authorization_url is not None else provider.redirect_kwargs["state"]
    )
    provider.userinfo = userinfo
    response = await client.get("/auth/callback", params={"state": state, "code": "stub"})
    assert response.status_code == 303
    return response


async def _onboard_password(client, token: str, password: str) -> int:
    csrf = await _form_csrf(client, "/invite")
    response = await client.post(
        "/invite",
        data={
            "token": token,
            "password": password,
            "password_confirm": password,
            "display_timezone": "UTC",
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 303
    session = await _session(client)
    assert "account_id" in session
    return session["account_id"]


async def _onboard_oidc(client, provider, token: str, subject: str) -> int:
    csrf = await _form_csrf(client, "/invite")
    response = await client.post(
        "/invite/oidc",
        data={"token": token, "display_timezone": "UTC", "csrf_token": csrf},
    )
    authorization_url = oidc_authorization_url(response)
    await _complete_oidc(
        client,
        provider,
        userinfo={"sub": subject, "email": "provider@example.invalid"},
        authorization_url=authorization_url,
    )
    return (await _session(client))["account_id"]


async def _link_oidc(client, provider, password: str, subject: str):
    csrf = (await _session(client))["csrf"]
    response = await client.post(
        "/settings/account/oidc/link",
        data={"current_password": password, "csrf_token": csrf},
    )
    authorization_url = oidc_authorization_url(response)
    await _complete_oidc(
        client,
        provider,
        userinfo={"sub": subject, "email": "provider@example.invalid"},
        authorization_url=authorization_url,
    )


async def _fresh_login(client, provider, shape: str, email: str, password: str, subject: str):
    if shape == "oidc-only":
        response = await client.get("/login/oidc")
        assert response.status_code == 303
        await _complete_oidc(client, provider, userinfo={"sub": subject})
    else:
        csrf = await _form_csrf(client, "/login")
        response = await client.post(
            "/login/local",
            data={"email": email, "password": password, "csrf_token": csrf},
        )
        assert response.status_code == 303
    session = await _session(client)
    assert session["account_id"]
    return session


async def _method_snapshot(owner, account_id: int):
    async with owner.connection() as conn:
        password_hash = (await (await conn.execute(
            "SELECT password_hash FROM accounts WHERE id=%s", (account_id,),
        )).fetchone())[0]
        identities = await (await conn.execute(
            "SELECT issuer,subject FROM oidc_identities WHERE account_id=%s ORDER BY issuer,subject",
            (account_id,),
        )).fetchall()
    return password_hash, identities


async def _seed_marker_trip(owner, account_id: int, marker: str):
    async with owner.connection() as conn:
        await conn.execute(
            "INSERT INTO trips(account_id,device,source,started_at,ended_at,distance_m,notes) "
            "VALUES (%s,%s,'manual','2026-09-01T10:00:00Z','2026-09-01T11:00:00Z',1234,%s)",
            (account_id, marker, marker),
        )


async def _snapshot_trip(owner, account_id: int):
    async with owner.connection() as conn:
        return await (await conn.execute(
            "SELECT count(*),sum(distance_m),array_agg(notes ORDER BY notes) "
            "FROM trips WHERE account_id=%s", (account_id,),
        )).fetchone()


async def _run_matrix(method_shape: str):
    owner = make_pool(TEST_DB)
    await owner.open(wait=True)
    try:
        async with owner.connection() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS odograph_service CASCADE")
        await full_schema_reset(owner)
        await prepare_application_roles(TEST_DB)

        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")

        async with application_role_pools(TEST_DB) as pools:
            async with pools.control.connection() as conn:
                admin_password = "matrix administrator password"
                admin_hash = await asyncio.to_thread(hash_password, admin_password)
                admin = await create_admin(
                    conn, "matrix-admin@example.invalid", admin_hash,
                )

            provider = _Provider()
            app = _app_with_auth(pools, provider)
            async with await _client(app) as admin_client, \
                    await _client(app) as target_client, \
                    await _client(app) as target_second_session, \
                    await _client(app) as other_client:
                await admin_client.post(f"/test/session/{admin['id']}/1")

                other_email = "unrelated@example.invalid"
                other_token = await _issue_invitation(admin_client, other_email)
                other_id = await _onboard_password(
                    other_client, other_token, "unrelated member password",
                )

                target_email = f"{method_shape.replace('-', '')}@example.invalid"
                target_token = await _issue_invitation(admin_client, target_email)
                subject = f"subject-{method_shape}"
                if method_shape == "oidc-only":
                    target_id = await _onboard_oidc(
                        target_client, provider, target_token, subject,
                    )
                    target_password = "unused oidc-only password"
                else:
                    target_password = f"target {method_shape} password"
                    target_id = await _onboard_password(
                        target_client, target_token, target_password,
                    )
                    if method_shape == "dual":
                        await _link_oidc(
                            target_client, provider, target_password, subject,
                        )

                assert (await admin_client.get("/test/who")).json() == {"id": admin["id"]}
                assert (await target_client.get("/test/who")).json() == {"id": target_id}
                assert (await other_client.get("/test/who")).json() == {"id": other_id}

                baseline_methods = await _method_snapshot(owner, target_id)
                if method_shape == "password-only":
                    assert baseline_methods[0] and baseline_methods[1] == []
                elif method_shape == "oidc-only":
                    assert baseline_methods[0] is None
                    assert baseline_methods[1] == [("https://idp.example", subject)]
                else:
                    assert baseline_methods[0]
                    assert baseline_methods[1] == [("https://idp.example", subject)]

                await _seed_marker_trip(owner, target_id, "target-private-marker")
                await _seed_marker_trip(owner, other_id, "unrelated-private-marker")
                other_ledger = await _snapshot_trip(owner, other_id)
                # This current-schema owner request must stay scoped to the
                # actor even when a target account_id is forged in the query.
                forged_export = await admin_client.get(
                    f"/settings/export/data?account_id={target_id}",
                )
                assert forged_export.status_code == 200
                assert "target-private-marker" not in json.dumps(forged_export.json())
                assert "unrelated-private-marker" not in json.dumps(forged_export.json())

                owner_export = await target_client.get("/settings/export/data")
                assert owner_export.status_code == 200
                assert "target-private-marker" in json.dumps(owner_export.json())

                # A second browser session for the same target lets the
                # acceptance check distinguish targeted revocation from a
                # process-wide or instance-wide logout.
                target_session = await _session(target_client)
                await target_second_session.post(
                    f"/test/session/{target_id}/{target_session['auth_version']}",
                )
                signed_out = await target_client.post(
                    "/settings/account/sign-out-everywhere",
                    headers={"X-CSRF-Token": target_session["csrf"]},
                )
                assert signed_out.status_code == 303
                assert (await target_client.get("/test/who")).status_code == 303
                assert (await target_second_session.get("/test/who")).status_code == 303
                assert (await other_client.get("/test/who")).json() == {"id": other_id}
                assert await _method_snapshot(owner, target_id) == baseline_methods
                async with owner.connection() as conn:
                    target_state = await (await conn.execute(
                        "SELECT auth_version,is_enabled,email,password_hash IS NULL "
                        "FROM accounts WHERE id=%s", (target_id,),
                    )).fetchone()
                assert target_state[1] is True
                assert target_state[2] == target_email

                # Create active account security and ingest state after the
                # global sign-out, then verify disablement and later recovery
                # never revive either capability.
                async with pools.control.connection() as conn:
                    challenge = await issue_email_challenge(
                        conn, target_id, target_state[0], PURPOSE_CURRENT, target_email,
                    )
                assert challenge, target_state
                async with AccountPool(
                    pools.runtime, AccountPrincipal(target_id, True, target_state[0]),
                ).connection() as conn:
                    credential = await create_device(conn, "matrix handset")

                disabled = await admin_client.post(
                    f"/admin/accounts/{target_id}/disable", data={"csrf_token": "route-csrf"},
                )
                assert disabled.status_code == 200
                assert (await target_client.get("/test/who")).status_code == 303
                async with owner.connection() as conn:
                    security_state = await (await conn.execute(
                        "SELECT revoked_at IS NOT NULL FROM email_challenges "
                        "WHERE account_id=%s AND token_digest=%s",
                        (target_id, hashlib.sha256(challenge.encode("ascii")).hexdigest()),
                    )).fetchone()
                    ingest_state = await (await conn.execute(
                        "SELECT revoked_at IS NOT NULL FROM ingest_credentials WHERE public_id=%s",
                        (credential.public_id,),
                    )).fetchone()
                assert security_state == (True,)
                assert ingest_state == (True,)

                enabled = await admin_client.post(
                    f"/admin/accounts/{target_id}/enable", data={"csrf_token": "route-csrf"},
                )
                assert enabled.status_code == 200
                assert (await target_client.get("/test/who")).status_code == 303
                assert (await other_client.get("/test/who")).json() == {"id": other_id}
                assert await _method_snapshot(owner, target_id) == baseline_methods
                current_target = await _fresh_login(
                    target_client, provider, method_shape, target_email,
                    target_password, subject,
                )
                assert current_target["auth_version"] == target_state[0] + 1

                requested = await admin_client.post(
                    f"/admin/accounts/{target_id}/deletion",
                    data={
                        "csrf_token": "route-csrf",
                        "target_email": target_email,
                        "acknowledge": "1",
                    },
                )
                assert requested.status_code == 200
                assert "30-day recovery window" in requested.text
                async with owner.connection() as conn:
                    deletion_state = await (await conn.execute(
                        "SELECT is_enabled,auth_version,deletion_deadline > now()+interval '29 days',"
                        "deletion_deadline < now()+interval '31 days' "
                        "FROM accounts WHERE id=%s", (target_id,),
                    )).fetchone()
                assert deletion_state[0] is False
                assert deletion_state[1] == current_target["auth_version"] + 1
                assert deletion_state[2:] == (True, True)
                assert (await target_client.get("/test/who")).status_code == 303

                cancelled = await admin_client.post(
                    f"/admin/accounts/{target_id}/deletion/cancel",
                    data={"csrf_token": "route-csrf"},
                )
                assert cancelled.status_code == 200
                assert "Deletion cancelled" in cancelled.text
                assert (await target_client.get("/test/who")).status_code == 303
                assert (await target_second_session.get("/test/who")).status_code == 303
                assert (await other_client.get("/test/who")).json() == {"id": other_id}
                assert await _method_snapshot(owner, target_id) == baseline_methods
                async with owner.connection() as conn:
                    restored = await (await conn.execute(
                        "SELECT is_enabled,auth_version,deletion_deadline FROM accounts WHERE id=%s",
                        (target_id,),
                    )).fetchone()
                    revocation_state = await (await conn.execute(
                        "SELECT bool_and(revoked_at IS NOT NULL) FROM email_challenges "
                        "WHERE account_id=%s", (target_id,),
                    )).fetchone()
                    credential_state = await (await conn.execute(
                        "SELECT revoked_at IS NOT NULL FROM ingest_credentials WHERE public_id=%s",
                        (credential.public_id,),
                    )).fetchone()
                assert restored == (True, deletion_state[1], None)
                assert revocation_state == (True,)
                assert credential_state == (True,)
                assert await _snapshot_trip(owner, other_id) == other_ledger

                final_session = await _fresh_login(
                    target_client, provider, method_shape, target_email,
                    target_password, subject,
                )
                assert final_session["auth_version"] == deletion_state[1]
                final_export = await target_client.get("/settings/export/data")
                assert final_export.status_code == 200
                assert "target-private-marker" in json.dumps(final_export.json())
                assert await _snapshot_trip(owner, other_id) == other_ledger
                assert (await other_client.get("/test/who")).json() == {"id": other_id}

                second_request = await admin_client.post(
                    f"/admin/accounts/{target_id}/deletion",
                    data={
                        "csrf_token": "route-csrf",
                        "target_email": target_email,
                        "acknowledge": "1",
                    },
                )
                assert second_request.status_code == 200
                async with owner.connection() as conn:
                    await conn.execute(
                        "UPDATE accounts SET deletion_deadline=clock_timestamp()-interval '1 second' "
                        "WHERE id=%s", (target_id,),
                    )
                purged = await admin_client.post(
                    f"/admin/accounts/{target_id}/purge",
                    data={
                        "csrf_token": "route-csrf",
                        "target_email": target_email,
                        "confirm_purge": "1",
                        "current_password": admin_password,
                    },
                )
                assert purged.status_code == 200
                assert "permanently removed from the live database" in purged.text
                assert (await target_client.get("/test/who")).status_code == 303

                async with owner.connection() as conn:
                    assert await (await conn.execute(
                        "SELECT count(*) FROM accounts WHERE id=%s", (target_id,),
                    )).fetchone() == (0,)
                    leftovers = await (await conn.execute(
                        "SELECT (SELECT count(*) FROM trips WHERE account_id=%s),"
                        "(SELECT count(*) FROM tracking_devices WHERE account_id=%s),"
                        "(SELECT count(*) FROM ingest_credentials WHERE account_id=%s),"
                        "(SELECT count(*) FROM email_challenges WHERE account_id=%s),"
                        "(SELECT count(*) FROM oidc_identities WHERE account_id=%s)",
                        (target_id,) * 5,
                    )).fetchone()
                    purge_audit = await (await conn.execute(
                        "SELECT action,outcome,target_account_id FROM account_security_audit "
                        "WHERE target_account_id=%s ORDER BY id DESC LIMIT 1", (target_id,),
                    )).fetchone()
                assert leftovers == (0, 0, 0, 0, 0)
                assert purge_audit == ("purge_account", "purged", target_id)
                assert await _snapshot_trip(owner, other_id) == other_ledger
                assert (await other_client.get("/test/who")).json() == {"id": other_id}
                other_export = await other_client.get("/settings/export/data")
                assert other_export.status_code == 200
                assert "unrelated-private-marker" in json.dumps(other_export.json())
                assert "target-private-marker" not in json.dumps(other_export.json())
    finally:
        try:
            await full_schema_reset(owner)
        finally:
            await owner.close()

@pytest.mark.parametrize("method_shape", ["password-only", "oidc-only", "dual"])
def test_restricted_lifecycle_matrix_preserves_login_shape_and_account_boundaries(method_shape):
    asyncio.run(_run_matrix(method_shape))
