"""Exact OIDC reauthentication can authorize a target-bound administrator purge."""
from __future__ import annotations

import asyncio
import os
import time

import pytest
from fastapi import HTTPException

import app.auth as auth
from app.account_lifecycle import request_account_deletion, purge_account
from app.oidc_identities import create_identity_link
from tests.test_admin_deletion_db import _activate, _elapsed
from tests.test_admin_lifecycle_routes_db import _scenario
from tests.test_oidc_routes_integration_db import _Provider, _endpoint, _request

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="requires disposable PostGIS")


@pytest.mark.parametrize("method", ["oidc", "dual"])
def test_purge_reauthentication_has_exact_identity_freshness_and_admin_return(method):
    async def check(owner,pools,actor):
        target = await _activate(owner)
        async with pools.control.connection() as conn:
            await create_identity_link(conn,actor["id"],"https://idp.example","actor")
            await request_account_deletion(conn,actor,target,email="member@example.invalid",acknowledge=True)
        if method == "oidc":
            async with owner.connection() as conn:
                await conn.execute("UPDATE accounts SET password_hash=NULL WHERE id=%s", (actor["id"],))
        await _elapsed(owner,target)
        provider = _Provider()
        request = _request(pools,provider)
        request.session.update(account_id=actor["id"],auth_version=1)
        user = await auth.require_user(request)
        reauth = _endpoint("/settings/account/oidc/reauth","POST")
        for claims in [{"sub":"wrong", "auth_time":time.time()}, {"sub":"actor", "auth_time":time.time()-300}]:
            response = await reauth(request,action="purge_account",target=str(target),target_confirm="",csrf_token="csrf",user=user)
            assert response.status_code == 303
            assert provider.redirect_kwargs["max_age"] == 0
            assert provider.redirect_kwargs["prompt"] == "login"
            request.query_params = {"state":provider.redirect_kwargs["state"],"code":"valid"}
            provider.userinfo = claims
            with pytest.raises(HTTPException):
                await _endpoint("/auth/callback","GET")(request)
            assert "oidc_action_proof_nonce" not in request.session
        await reauth(request,action="purge_account",target=str(target),target_confirm="",csrf_token="csrf",user=user)
        request.query_params = {"state":provider.redirect_kwargs["state"],"code":"valid"}
        provider.userinfo = {"sub":"actor", "auth_time":time.time()}
        response = await _endpoint("/auth/callback","GET")(request)
        assert response.headers["location"] == "/admin/accounts"
        async with pools.control.connection() as conn:
            assert await purge_account(conn,actor,target,email="member@example.invalid",confirm=True,browser_nonce=request.session["oidc_action_proof_nonce"]) == "purged"
    asyncio.run(_scenario(check))
