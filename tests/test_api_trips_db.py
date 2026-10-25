"""API v1 trip reads through the real application: shapes, account
isolation, keyset paging, cursor binding, the review queue and bearer-only
admission."""
from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.account_context import AccountPool, AccountPrincipal, account_id, control_connection
from app.api_errors import ERRORS
from app.api_grants import create_grant
from app.api_trips import TripDetailResponse, TripSummary
from app.db import make_pool
from app.main import create_app
from api_trip_support import BASE, seed_trip_kinds
from auth_db_fixtures import auth_config
from conftest import add_test_account, reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")
FIXTURES = Path(__file__).parent / "fixtures" / "api_v1"
TZ_NAME = "America/Los_Angeles"
OTHER = 77


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def application():
    raw = make_pool(TEST_DB)
    await raw.open(wait=True)
    try:
        await reset_db(raw)
        config = auth_config(TEST_DB, dev_no_auth=False, initial_admin_signup=True,
                             raw_message_retention_days=0)
        app = create_app(config)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="https://test") as client:
                page = await client.get("/signup")
                csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
                response = await client.post("/signup", data={
                    "email": "api-owner@example.invalid", "password": "api trip read password",
                    "password_confirm": "api trip read password", "csrf_token": csrf,
                    "display_timezone": TZ_NAME})
                assert response.status_code == 303
                async with raw.connection() as conn:
                    owner = (await (await conn.execute(
                        "SELECT id FROM accounts WHERE email = %s",
                        ("api-owner@example.invalid",))).fetchone())[0]
                await add_test_account(raw, OTHER)
                async with raw.connection() as conn:
                    await conn.execute("UPDATE account_settings SET display_tz = %s", (TZ_NAME,))
                tokens = {}
                async with control_connection(app.state.control_pool) as conn:
                    for account in (owner, OTHER):
                        tokens[account] = (await create_grant(conn, account, 1, "Phone")).access_token
                pools = {account: AccountPool(app.state.runtime_pool, AccountPrincipal(account, True, 1))
                         for account in (owner, OTHER)}
                yield SimpleNamespace(raw=raw, app=app, client=client, owner=owner, pools=pools,
                                      tokens=tokens, cookie=client.cookies.get("session"))
    finally:
        await raw.close()


def _run(check):
    async def run():
        async with application() as state:
            await check(state)
    asyncio.run(run())


async def _get(state, path, account=None, **params):
    response = await state.client.get(
        path, params=params, headers=_bearer(state.tokens[account or state.owner]))
    return response


async def _seed(state, account=None) -> dict[str, int]:
    async with state.pools[account or state.owner].connection() as conn:
        return await seed_trip_kinds(conn)


async def _insert(state, started_at, account=None, **values) -> int:
    columns = {"device": "manual", "source": "manual", "started_at": started_at,
               "ended_at": started_at + timedelta(minutes=5), "distance_m": 100.0,
               "category": "unclassified", **values}
    async with state.pools[account or state.owner].connection() as conn:
        cur = await conn.execute(
            f"INSERT INTO trips (account_id, {', '.join(columns)}) "
            f"VALUES (%s, {', '.join(['%s'] * len(columns))}) RETURNING id",
            [account_id(conn), *columns.values()])
        return (await cur.fetchone())[0]


async def _sql(state, statement, params=(), account=None):
    async with state.pools[account or state.owner].connection() as conn:
        await conn.execute(statement, params)


def _without_ids(node):
    if isinstance(node, dict):
        return {key: _without_ids(value) for key, value in node.items()
                if key not in ("id", "vehicle_id")}
    if isinstance(node, list):
        return [_without_ids(value) for value in node]
    return node


def _envelope(code):
    return {"error": {"code": code, "message": ERRORS[code][1], "fields": None}}


async def _walk(state, account=None, limit=2, **params):
    """Every page of the archive, as (ids per page, last response)."""
    pages, cursor = [], None
    while True:
        query = {**params, "limit": limit, **({"cursor": cursor} if cursor else {})}
        response = await _get(state, "/api/v1/trips", account, **query)
        assert response.status_code == 200, response.text
        body = response.json()
        pages.append([item["id"] for item in body["items"]])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages


def test_trip_detail_matches_the_synthetic_fixtures():
    async def check(state):
        ids = await _seed(state)
        for kind in ("gapped", "snapped_only", "geometry_free", "imported", "manual", "routed_manual"):
            response = await _get(state, f"/api/v1/trips/{ids[kind]}")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store, private"
            body = response.json()
            assert set(body) == set(TripDetailResponse.model_fields)
            fixture = json.loads((FIXTURES / f"trip_detail_{kind}.json").read_text())
            assert _without_ids(body) == _without_ids(fixture), kind
            assert body["id"] == ids[kind]
        listing = (await _get(state, "/api/v1/trips")).json()
        assert listing["next_cursor"] is None
        assert [item["id"] for item in listing["items"]] == sorted(ids.values(), reverse=True)
        for item in listing["items"]:
            assert set(item) == set(TripSummary.model_fields)
        fixture = json.loads((FIXTURES / "trips_page_response.json").read_text())
        assert _without_ids(listing["items"][:2]) == _without_ids(fixture["items"])
    _run(check)


def test_vehicles_and_recent_purposes():
    async def check(state):
        await _seed(state)
        await _seed(state, OTHER)
        await _sql(state, "UPDATE trips SET purpose = 'Other account purpose'", account=OTHER)
        vehicles = (await _get(state, "/api/v1/vehicles")).json()
        assert vehicles["next_cursor"] is None
        assert [v["name"] for v in vehicles["items"]] == ["My Car"]
        assert set(vehicles["items"][0]) == {"id", "name", "make", "model", "plate", "is_default"}
        purposes = (await _get(state, "/api/v1/purposes/recent")).json()
        assert purposes == {"items": ["Client visit", "Supplies"], "next_cursor": None}
        other = (await _get(state, "/api/v1/purposes/recent", OTHER)).json()
        assert other["items"] == ["Other account purpose"]
    _run(check)


def test_another_accounts_rows_never_leak():
    async def check(state):
        mine = await _seed(state)
        theirs = await _seed(state, OTHER)
        await _sql(state, "UPDATE trips SET notes = 'shared needle'", account=OTHER)
        await _sql(state, "UPDATE trips SET notes = 'shared needle' WHERE id = %s", (mine["manual"],))
        async with state.pools[OTHER].connection() as conn:
            other_vehicle = (await (await conn.execute(
                "SELECT id FROM vehicles WHERE account_id = %s", (OTHER,))).fetchone())[0]
        for trip_id in theirs.values():
            response = await _get(state, f"/api/v1/trips/{trip_id}")
            assert response.status_code == 404
            assert response.json() == _envelope("not_found")
        assert (await _get(state, "/api/v1/trips/0")).status_code == 404
        assert (await _get(state, f"/api/v1/trips/{2**63}")).status_code == 404
        mine_ids = set(mine.values())
        for params in ({}, {"q": "needle"}, {"vehicle": str(other_vehicle)}, {"vehicle": "none"},
                       {"category": "unclassified"}, {"exclusion": "none"}):
            seen = {trip_id for page in await _walk(state, **params) for trip_id in page}
            assert seen <= mine_ids, params
        assert {i for page in await _walk(state, q="needle") for i in page} == {mine["manual"]}
        assert await _walk(state, vehicle=str(other_vehicle)) == [[]]
        # A cursor taken from the other account's listing moves only within ours.
        their_page = (await _get(state, "/api/v1/trips", OTHER, limit=1)).json()
        response = await _get(state, "/api/v1/trips", cursor=their_page["next_cursor"], limit=1)
        assert response.status_code == 200
        assert {item["id"] for item in response.json()["items"]} <= mine_ids
        review = (await _get(state, "/api/v1/review")).json()
        unclassified_mine = {mine[k] for k in ("snapped_only", "raw_only", "imported", "routed_manual")}
        assert review["remaining"] == len(unclassified_mine)
        assert review["trip"]["id"] in unclassified_mine
        their_review = (await _get(state, "/api/v1/review", OTHER)).json()
        response = await _get(state, "/api/v1/review", cursor=their_review["next_cursor"])
        assert response.status_code == 200
        body = response.json()
        assert body["trip"] is None or body["trip"]["id"] in unclassified_mine
        assert body["remaining"] <= len(unclassified_mine)
    _run(check)


def test_paging_returns_each_unchanged_trip_once():
    async def check(state):
        tied = [await _insert(state, BASE) for _ in range(5)]
        spread = [await _insert(state, BASE - timedelta(hours=h)) for h in (1, 2, 3, 4)]
        doomed = await _insert(state, BASE - timedelta(hours=5))
        await _insert(state, BASE, OTHER)
        pages, cursor = [], None
        first = (await _get(state, "/api/v1/trips", limit=3)).json()
        pages.append([item["id"] for item in first["items"]])
        cursor = first["next_cursor"]
        # Between pages: one newer and one older insert, one deletion ahead
        # of the cursor and one behind it.
        await _insert(state, BASE + timedelta(hours=1))
        older = await _insert(state, BASE - timedelta(hours=6))
        await _sql(state, "DELETE FROM trips WHERE id = %s", (doomed,))
        await _sql(state, "DELETE FROM trips WHERE id = %s", (pages[0][0],))
        while cursor:
            body = (await _get(state, "/api/v1/trips", limit=3, cursor=cursor)).json()
            pages.append([item["id"] for item in body["items"]])
            cursor = body["next_cursor"]
        seen = [trip_id for page in pages for trip_id in page]
        expected = sorted(tied, reverse=True) + spread + [older]
        assert seen == expected
        assert len(seen) == len(set(seen))
    _run(check)


def test_an_edit_across_the_cursor_is_tolerated_not_hidden():
    async def check(state):
        trips = [await _insert(state, BASE - timedelta(hours=h)) for h in range(6)]
        await _insert(state, BASE - timedelta(hours=2, minutes=30), OTHER)
        first = (await _get(state, "/api/v1/trips", limit=2)).json()
        assert [item["id"] for item in first["items"]] == trips[:2]
        # Seen trip moved behind the cursor; unseen trip moved ahead of it.
        await _sql(state, "UPDATE trips SET started_at = %s WHERE id = %s",
                   (BASE - timedelta(hours=10), trips[0]))
        await _sql(state, "UPDATE trips SET started_at = %s WHERE id = %s",
                   (BASE + timedelta(hours=1), trips[4]))
        cursor, seen = first["next_cursor"], [item["id"] for item in first["items"]]
        while cursor:
            response = await _get(state, "/api/v1/trips", limit=2, cursor=cursor)
            assert response.status_code == 200
            body = response.json()
            seen += [item["id"] for item in body["items"]]
            cursor = body["next_cursor"]
        assert set(seen) <= set(trips)
        # Documented, not promised: the moved trips repeat or go missing
        # until the client refreshes from the top.
        assert seen.count(trips[0]) == 2
        assert trips[4] not in seen
    _run(check)


def test_cursor_is_bound_to_its_filters_and_rejects_tampering():
    async def check(state):
        await _seed(state)
        for _ in range(3):
            await _insert(state, BASE, category="business")
        page = (await _get(state, "/api/v1/trips", category="business", limit=1)).json()
        cursor = page["next_cursor"]
        assert cursor
        good = await _get(state, "/api/v1/trips", category="business", limit=1, cursor=cursor)
        assert good.status_code == 200
        flipped = cursor[:-2] + ("A" if cursor[-2] != "A" else "B") + cursor[-1]
        review = (await _get(state, "/api/v1/review")).json()["next_cursor"]
        for params in (
            {"category": "personal", "cursor": cursor},
            {"cursor": cursor},
            {"category": "business", "q": "x", "cursor": cursor},
            {"category": "business", "cursor": flipped},
            {"category": "business", "cursor": cursor + "A"},
            {"category": "business", "cursor": "not a cursor"},
            {"category": "business", "cursor": ""},
            {"category": "business", "cursor": review},
        ):
            response = await _get(state, "/api/v1/trips", limit=1, **params)
            assert response.status_code == 400, params
            assert response.json() == _envelope("invalid_cursor")
        response = await _get(state, "/api/v1/review", cursor=cursor)
        assert response.json() == _envelope("invalid_cursor")
        response = await _get(state, "/api/v1/review", q="other", cursor=review)
        assert response.json() == _envelope("invalid_cursor")
        # Equivalent spellings of one filter set share a cursor.
        same = await _get(state, "/api/v1/trips", category="business", q="  ", limit=1, cursor=cursor)
        assert same.status_code == 200
    _run(check)


def test_filters_accept_only_documented_values():
    async def check(state):
        for params, field in (
            ({"category": "bogus"}, "category"), ({"exclusion": "bogus"}, "exclusion"),
            ({"vehicle": "abc"}, "vehicle"), ({"vehicle": "0"}, "vehicle"),
            ({"vehicle": str(2**63)}, "vehicle"), ({"from": "2026-13-01"}, "from"),
            ({"to": "20260401"}, "to"), ({"limit": "0"}, "limit"), ({"limit": "201"}, "limit"),
        ):
            response = await _get(state, "/api/v1/trips", **params)
            assert response.status_code == 422, params
            assert response.json()["error"]["code"] == "validation_failed"
            assert list(response.json()["error"]["fields"]) == [field]
        response = await _get(state, "/api/v1/review", vehicle="x")
        assert response.status_code == 422
    _run(check)


def test_filters_select_the_same_trips_as_the_web_archive():
    async def check(state):
        ids = await _seed(state)
        async with state.pools[state.owner].connection() as conn:
            vehicle = (await (await conn.execute(
                "SELECT id FROM vehicles WHERE account_id = %s", (state.owner,))).fetchone())[0]
        cases = (
            ({"category": "business"}, {ids["gapped"], ids["manual"]}),
            ({"exclusion": "not_deductible"}, {ids["raw_only"]}),
            ({"exclusion": "none"}, set(ids.values()) - {ids["raw_only"]}),
            ({"vehicle": str(vehicle)}, {ids["gapped"], ids["manual"]}),
            ({"vehicle": "none"}, set(ids.values()) - {ids["gapped"], ids["manual"]}),
            ({"q": "hardware"}, {ids["manual"]}),
            ({"q": "TUNNEL"}, {ids["gapped"]}),
            ({"from": "2026-04-14", "to": "2026-04-14"}, set(ids.values())),
            ({"from": "2026-04-15"}, set()),
            ({"to": "2026-04-13"}, set()),
        )
        for params, expected in cases:
            seen = {i for page in await _walk(state, limit=200, **params) for i in page}
            assert seen == expected, params
    _run(check)


def test_review_queue_order_and_remaining_count():
    async def check(state):
        ids = await _seed(state)
        tied = [await _insert(state, BASE + timedelta(hours=1)) for _ in range(2)]
        await _insert(state, BASE - timedelta(days=3), OTHER)
        expected = sorted(
            [(BASE + timedelta(hours=1), ids["snapped_only"]), (BASE + timedelta(hours=2), ids["raw_only"]),
             (BASE + timedelta(hours=4), ids["imported"]), (BASE + timedelta(hours=6), ids["routed_manual"]),
             *((BASE + timedelta(hours=1), trip_id) for trip_id in tied)])
        order, remaining, cursor = [], [], None
        while True:
            response = await _get(state, "/api/v1/review", **({"cursor": cursor} if cursor else {}))
            body = response.json()
            if body["trip"] is None:
                assert body == {"state": "done", "trip": None, "remaining": 0, "next_cursor": None}
                break
            assert body["state"] == "card"
            assert set(body["trip"]) == set(TripDetailResponse.model_fields)
            order.append(body["trip"]["id"])
            remaining.append(body["remaining"])
            cursor = body["next_cursor"]
        assert order == [trip_id for _, trip_id in expected]
        assert remaining == list(range(len(expected), 0, -1))
        # Tagging the shown trip leaves the queue; the count is point in time.
        first = (await _get(state, "/api/v1/review")).json()
        await _sql(state, "UPDATE trips SET category = 'business' WHERE id = %s", (first["trip"]["id"],))
        after = (await _get(state, "/api/v1/review", cursor=first["next_cursor"])).json()
        assert after["trip"]["id"] == order[1]
        assert after["remaining"] == len(expected) - 1
        empty = (await _get(state, "/api/v1/review", q="nothing matches")).json()
        assert empty == {"state": "empty", "trip": None, "remaining": 0, "next_cursor": None}
        fixture = json.loads((FIXTURES / "review_response.json").read_text())
        assert _without_ids(first["trip"]) == _without_ids(fixture["trip"])
    _run(check)


def test_api_routes_authenticate_by_bearer_never_by_cookie():
    async def check(state):
        ids = await _seed(state)
        assert state.cookie
        # The signed-in browser session reaches the web page...
        assert (await state.client.get(f"/trips/{ids['gapped']}")).status_code == 200
        # ...but never an API route, with or without an admission lane.
        for path in (f"/api/v1/trips/{ids['gapped']}", "/api/v1/trips", "/api/v1/review",
                     "/api/v1/vehicles", "/api/v1/purposes/recent"):
            response = await state.client.get(path)
            assert response.status_code == 401, path
            assert response.json() == _envelope("invalid_token")
            assert "set-cookie" not in response.headers
            assert (await _get(state, path)).status_code == 200
        response = await state.client.post("/api/v1/trips", headers=_bearer(state.tokens[state.owner]))
        assert response.status_code == 405
        assert response.json() == _envelope("method_not_allowed")
    _run(check)


def test_token_body_cap_and_revoke_keep_api_semantics():
    async def check(state):
        # The token route's admission body cap answers in the envelope.
        oversized = b'{"grant_type": "refresh_token", "refresh_token": "' + b"x" * 70000 + b'"}'
        response = await state.client.post("/api/v1/auth/token", content=oversized,
                                           headers={"content-type": "application/json"})
        assert response.status_code == 413
        assert response.json() == _envelope("payload_too_large")
        response = await state.client.get("/api/v1/auth/token")
        assert response.status_code == 405
        assert response.json() == _envelope("method_not_allowed")
        # Revoke is bearer-only: the signed-in browser session cannot reach it.
        assert state.cookie
        response = await state.client.post("/api/v1/auth/revoke")
        assert response.status_code == 401
        assert response.json() == _envelope("invalid_token")
        token = state.tokens[state.owner]
        assert (await state.client.post("/api/v1/auth/revoke", headers=_bearer(token))).status_code == 204
        response = await _get(state, "/api/v1/trips")
        assert response.status_code == 401
        assert (await _get(state, "/api/v1/trips", OTHER)).status_code == 200
    _run(check)
