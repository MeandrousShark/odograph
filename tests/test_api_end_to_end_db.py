"""Mobile sign-in and API v1 end to end against PostGIS, with mobile sign-in
on: a scripted client signs in through the browser flow, writes a trip and
an expense through the API, the web shows the same records, and revoking the
app from the account page ends its access. Nothing mints a grant or writes
through a service directly, and a second signed-in account sees none of the
records at any step."""
from __future__ import annotations

import html
import uuid
from datetime import timezone

import pytest

from app.account_context import AccountPool, AccountPrincipal
from app.trip_reads import fetch_trip_detail
from test_mobile_signin_db import (
    TEST_DB, _authorize, _callback, _csrf, _decide, _exchange, _login_local, _login_oidc, _pkce, _sign_in_app,
    _site,
)
from test_signed_in_apps_db import PAGE, _grant_ids, _revoke

pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _sign_in_local(client) -> dict:
    """Authorize from a signed-out browser, log in, confirm and exchange."""
    verifier, challenge = _pkce()
    started = await _authorize(client, challenge, state="end-to-end")
    assert started.status_code == 303 and started.headers["location"] == "/login"
    login = await _login_local(client)
    assert login.headers["location"] == "/mobile/authorize/confirm"
    _, finished = await _decide(client)
    params = _callback(finished)
    assert params["state"] == "end-to-end"
    exchanged = await _exchange(client, params["code"], verifier)
    assert exchanged.status_code == 200, exchanged.text
    return exchanged.json()


async def _sees_nothing(client, token: str, trip_id: int | None, expense_id: int | None) -> None:
    """The second account's API and web views hold none of the records."""
    headers = _bearer(token)
    if trip_id is not None:
        assert (await client.get(f"/api/v1/trips/{trip_id}", headers=headers)).status_code == 404
        assert (await client.patch(f"/api/v1/trips/{trip_id}", json={"notes": "theirs"},
                                   headers=headers)).status_code == 404
        assert (await client.get(f"/trips/{trip_id}")).status_code == 404
    trips = (await client.get("/api/v1/trips", headers=headers)).json()
    assert trips["items"] == []
    expenses = (await client.get("/api/v1/expenses", params={"year": 2026}, headers=headers)).json()
    assert expenses["items"] == []
    if expense_id is not None:
        assert (await client.patch(f"/api/v1/expenses/{expense_id}", json={"notes": "theirs"},
                                   headers=headers)).status_code == 404
        assert (await client.delete(f"/api/v1/expenses/{expense_id}", headers=headers)).status_code == 404
    comparison = (await client.get("/api/v1/expenses/comparison", params={"year": 2026}, headers=headers)).json()
    assert comparison["vehicles"] == [] and comparison["ledger_total"] == "0.00"
    week = (await client.get("/api/v1/dashboard/week", params={"date": "2026-04-14"}, headers=headers)).json()
    assert week["trip_count"] == 0 and week["expense_total"] == "0.00"
    stats = (await client.get("/api/v1/stats", params={"year": 2026}, headers=headers)).json()
    assert stats["totals"]["trip_count"] == 0
    page = (await client.get("/expenses", params={"year": 2026})).text
    assert "Garage" not in page and "Parking lot" not in page


def test_a_signed_in_app_writes_what_the_web_shows_until_it_is_revoked(monkeypatch):
    async def check(site):
        async with site.client() as browser, site.client() as other:
            # (1) Sign in through the browser flow; the second account signs in through OIDC.
            tokens = await _sign_in_local(browser)
            await _login_oidc(site, other)
            other_tokens = (await _sign_in_app(other, state="other")).json()
            other_token = other_tokens["access_token"]
            headers = _bearer(tokens["access_token"])
            await _sees_nothing(other, other_token, None, None)

            # (2) The access token works.
            me = await browser.get("/api/v1/me", headers=headers)
            assert me.status_code == 200 and me.json()["account_id"] == site.local["id"]

            # (3) Create a plain manual trip.
            vehicle_id = (await browser.get("/api/v1/vehicles", headers=headers)).json()["items"][0]["id"]
            created = await browser.post("/api/v1/trips", headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
                                         json={"started_at": "2026-04-14T16:00:00Z", "ended_at": "2026-04-14T16:30:00Z",
                                               "distance_m": 8046.72, "vehicle_id": vehicle_id})
            assert created.status_code == 201, created.text
            trip_id = created.json()["id"]
            await _sees_nothing(other, other_token, trip_id, None)

            # (4) Edit it: notes leave the tag alone, a category makes it human.
            assert (await browser.patch(f"/api/v1/trips/{trip_id}", headers=headers,
                                        json={"notes": "Site visit"})).status_code == 200
            assert await site.sql("SELECT tag_source::text FROM trips WHERE id = %s", (trip_id,)) == [(None,)]
            patched = await browser.patch(f"/api/v1/trips/{trip_id}", headers=headers,
                                          json={"category": "business", "purpose": " Client meeting "})
            assert patched.status_code == 200, patched.text
            assert await site.sql("SELECT tag_source::text FROM trips WHERE id = %s", (trip_id,)) == [("human",)]
            pool = AccountPool(site.app.state.runtime_pool,
                               AccountPrincipal(site.local["id"], True, site.local["auth_version"]))
            async with pool.connection() as conn:
                detail = await fetch_trip_detail(conn, trip_id, timezone.utc)
            assert (detail.trip["category"], detail.trip["purpose"], detail.trip["notes"]) == (
                "business", "Client meeting", "Site visit")
            page = await browser.get(f"/trips/{trip_id}")
            assert page.status_code == 200
            assert "Client meeting" in page.text and "Site visit" in page.text
            await _sees_nothing(other, other_token, trip_id, None)

            # (5) An expense linked to the trip, created and edited.
            expense = await browser.post(
                "/api/v1/expenses", headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
                json={"vehicle_id": vehicle_id, "incurred_on": "2026-04-14", "category": "parking",
                      "amount": "12.34", "notes": "Parking lot", "trip_id": trip_id})
            assert expense.status_code == 201, expense.text
            expense_id = expense.json()["id"]
            edited = await browser.patch(f"/api/v1/expenses/{expense_id}", headers=headers,
                                         json={"amount": "23.45", "notes": "Garage"})
            assert edited.status_code == 200, edited.text
            assert (edited.json()["amount"], edited.json()["trip_id"], edited.json()["conflicts"]) == (
                "23.45", trip_id, [])
            ledger = (await browser.get("/expenses", params={"year": 2026})).text
            assert "$23.45" in ledger and "Garage" in ledger and "Parking lot" not in ledger
            comparison = (await browser.get("/api/v1/expenses/comparison", params={"year": 2026},
                                            headers=headers)).json()
            assert comparison["fully_business_total"] == "23.45"
            [line] = comparison["vehicles"]
            report = (await browser.get("/report/2026")).text
            for field in ("fully_business_expenses", "standard_total", "actual_total"):
                assert f"${float(line[field]):,.2f}" in report, field
            assert html.escape(line["vehicle_name"]) in report
            await _sees_nothing(other, other_token, trip_id, expense_id)

            # (6) Revoke the app from the signed-in apps page.
            account_page = await browser.get(PAGE)
            [grant_id] = _grant_ids(account_page)
            done = await _revoke(browser, grant_id, _csrf(account_page))
            assert done.status_code == 303

            # (7) The access token and the refresh token are both dead.
            denied = await browser.get("/api/v1/me", headers=headers)
            assert (denied.status_code, denied.json()["error"]["code"]) == (401, "invalid_token")
            denied = await browser.get("/api/v1/expenses", headers=headers)
            assert (denied.status_code, denied.json()["error"]["code"]) == (401, "invalid_token")
            refreshed = await browser.post("/api/v1/auth/token", json={
                "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
            assert (refreshed.status_code, refreshed.json()["error"]["code"]) == (400, "invalid_grant")
            # The second account's app is untouched and still sees nothing.
            assert (await other.get("/api/v1/me", headers=_bearer(other_token))).status_code == 200
            await _sees_nothing(other, other_token, trip_id, expense_id)
            assert "Garage" in (await browser.get("/expenses", params={"year": 2026})).text
    _site(monkeypatch, check)
