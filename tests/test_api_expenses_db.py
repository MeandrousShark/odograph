"""API v1 expenses through the real application: the same durable rows as the
web routes they mirror, partial updates, idempotent creates, keyset paging,
the comparison, account isolation and bearer-first admission."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest

from app import api_expenses, capacity_routes
from app.account_context import AccountPool, AccountPrincipal, account_id
from app.api_errors import ERRORS
from app.application_roles import application_role_pools
from app.expense_ledger import Expense, create_expense, expense_digest
from app.report_data import fetch_expense_report_inputs, fetch_range_trips
from app.expenses import build_expense_report
from api_expense_support import seed_ledger
from test_api_trips_db import OTHER, TEST_DB, TZ_NAME, _bearer, _envelope, _get, _run, _sql
from test_api_trip_writes_db import _api, _usage, _web

pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")
FIXTURES = Path(__file__).parent / "fixtures" / "api_v1"
TZ = ZoneInfo(TZ_NAME)
# Columns whose values identify a row rather than describe the expense.
IDENTITY = {"id", "account_id", "created_at", "updated_at", "idempotency_key", "idempotency_digest"}
INVALID = "This value is not valid."


def _key() -> str:
    return str(uuid.uuid4())


def _without_ids(node):
    if isinstance(node, dict):
        return {key: _without_ids(value) for key, value in node.items()
                if key not in ("id", "vehicle_id", "trip_id")}
    if isinstance(node, list):
        return [_without_ids(value) for value in node]
    return node


async def _seed(state, account=None) -> dict[str, int]:
    async with state.pools[account or state.owner].connection() as conn:
        return await seed_ledger(conn)


async def _row(state, expense_id) -> dict | None:
    async with state.raw.connection() as conn:
        row = await (await conn.execute(
            "SELECT row_to_json(e) FROM expenses e WHERE id = %s", (expense_id,))).fetchone()
    return None if row is None else row[0]


async def _count(state) -> int:
    async with state.raw.connection() as conn:
        return (await (await conn.execute("SELECT count(*) FROM expenses")).fetchone())[0]


async def _last_id(state) -> int:
    async with state.raw.connection() as conn:
        return (await (await conn.execute("SELECT max(id) FROM expenses")).fetchone())[0]


def _durable(row: dict) -> dict:
    return {key: value for key, value in row.items() if key not in IDENTITY}


class _Commits:
    """Requests whose handler reported a committed write."""

    def __init__(self, monkeypatch):
        self.paths: list[str] = []
        original = capacity_routes.mark_committed_write

        def record(request):
            self.paths.append(request.url.path)
            original(request)

        for module in ("app.api_expenses", "app.ui.expenses"):
            monkeypatch.setattr(f"{module}.mark_committed_write", record)

    def take(self) -> list[str]:
        taken, self.paths = self.paths, []
        return taken


def _web_form(**values) -> dict:
    form = {"incurred_on": "2026-04-16", "category": "fuel", "amount": "20.50", "treatment": "",
            "notes": "  Top up ", "trip_id": ""}
    form.update({key: str(value) for key, value in values.items()})
    return form


def _body(**values) -> dict:
    body = {"incurred_on": "2026-04-16", "category": "fuel", "amount": "20.50", "notes": "  Top up "}
    body.update(values)
    return body


async def _create(state, body, key=None, account=None):
    headers = {} if key is False else {"Idempotency-Key": _key() if key is None else key}
    return await _api(state, "POST", "/api/v1/expenses", account, body=body, headers=headers)


def test_create_leaves_the_same_row_and_charge_as_the_web_form(monkeypatch):
    async def check(state):
        commits = _Commits(monkeypatch)
        ids = await _seed(state)
        for extra in ({}, {"trip_id": ids["business"], "category": "tolls"},
                      {"category": "other", "treatment": "fully_business"}):
            usage = await _usage(state)
            response = await _web(state, "/expenses", _web_form(vehicle_id=ids["car"], **extra))
            assert response.status_code == 204, response.text
            web_cost = await _usage(state) - usage
            web_id = await _last_id(state)
            usage = await _usage(state)
            key = _key()
            response = await _create(state, _body(vehicle_id=ids["car"], **extra), key)
            assert response.status_code == 201, response.text
            assert response.headers["cache-control"] == "no-store, private"
            assert "idempotent-replayed" not in response.headers
            body = response.json()
            assert response.headers["location"] == f"/api/v1/expenses/{body['id']}"
            assert await _usage(state) - usage == web_cost
            web_row, api_row = await _row(state, web_id), await _row(state, body["id"])
            assert _durable(api_row) == _durable(web_row)
            assert web_row["idempotency_key"] is None and web_row["idempotency_digest"] is None
            assert api_row["idempotency_key"] == key and api_row["idempotency_digest"] is not None
            assert commits.take() == ["/expenses", "/api/v1/expenses"]
            assert (body["notes"], body["amount"], body["incurred_on"]) == ("Top up", "20.50", "2026-04-16")
        assert body["treatment"] == "fully_business" and body["trip_id"] is None
    _run(check)


def test_create_replays_a_repeat_and_refuses_a_reused_key(monkeypatch):
    async def check(state):
        commits = _Commits(monkeypatch)
        ids = await _seed(state)
        key = _key()
        first = await _create(state, _body(vehicle_id=ids["car"]), key)
        assert first.status_code == 201
        commits.take()
        count = await _count(state)
        await _sql(state, "UPDATE expenses SET notes = 'edited later' WHERE id = %s", (first.json()["id"],))
        usage = await _usage(state)
        # The same request, spelled differently, replays the current record.
        for body in (_body(vehicle_id=ids["car"]),
                     _body(vehicle_id=ids["car"], amount="20.5", notes="Top up",
                           treatment="business_use_allocated", trip_id=None)):
            replay = await _create(state, body, key.upper())
            assert replay.status_code == 200, replay.text
            assert replay.headers["idempotent-replayed"] == "true"
            assert replay.headers["cache-control"] == "no-store, private"
            assert "location" not in replay.headers
            assert replay.json() == {**first.json(), "notes": "edited later"}
        assert await _count(state) == count and await _usage(state) == usage
        assert commits.take() == []
        stored = await _row(state, first.json()["id"])
        for body in (_body(vehicle_id=ids["car"], amount="20.51"), _body(vehicle_id=ids["van"]),
                     _body(vehicle_id=ids["car"], treatment="fully_business"),
                     _body(vehicle_id=ids["car"], trip_id=ids["business"])):
            response = await _create(state, body, key)
            assert (response.status_code, response.json()) == (409, _envelope("idempotency_key_reused"))
        assert await _row(state, first.json()["id"]) == stored and await _count(state) == count
        # Keys are scoped to the account and to the resource type.
        their_car = (await _seed(state, OTHER))["car"]
        assert (await _create(state, _body(vehicle_id=their_car), key, OTHER)).status_code == 201
        trip = await _api(state, "POST", "/api/v1/trips", headers={"Idempotency-Key": key}, body={
            "started_at": "2026-04-20T16:00:00Z", "ended_at": "2026-04-20T16:30:00Z", "distance_m": 100})
        assert trip.status_code == 201
        # Deleting the record frees its key.
        assert (await _api(state, "DELETE", f"/api/v1/expenses/{first.json()['id']}")).status_code == 204
        again = await _create(state, _body(vehicle_id=ids["car"]), key)
        assert again.status_code == 201 and again.json()["id"] != first.json()["id"]
    _run(check)


def test_create_without_a_valid_key_or_body_claims_nothing(monkeypatch):
    async def check(state):
        commits = _Commits(monkeypatch)
        ids = await _seed(state)
        theirs = await _seed(state, OTHER)
        count = await _count(state)
        response = await _create(state, _body(vehicle_id=ids["car"]), False)
        assert response.status_code == 422
        assert response.json()["error"]["fields"] == {"Idempotency-Key": "This field is required."}
        for bad in ("not-a-uuid", "{" + _key() + "}", _key().replace("-", ""), ""):
            response = await _create(state, _body(vehicle_id=ids["car"]), bad)
            assert response.json()["error"]["fields"] == {"Idempotency-Key": INVALID}, bad
        key = _key()
        car = ids["car"]
        cases = [
            (_body(vehicle_id=car, amount="1.234"), {"amount": "Invalid expense amount"}),
            (_body(vehicle_id=car, amount="0"), {"amount": "Invalid expense amount"}),
            (_body(vehicle_id=car, amount="0.00"), {"amount": "Invalid expense amount"}),
            (_body(vehicle_id=car, amount="-1"), {"amount": "Invalid expense amount"}),
            (_body(vehicle_id=car, amount="1e3"), {"amount": "Invalid expense amount"}),
            (_body(vehicle_id=car, amount=" 1"), {"amount": "Invalid expense amount"}),
            (_body(vehicle_id=car, amount="NaN"), {"amount": "Invalid expense amount"}),
            (_body(vehicle_id=car, amount="10000000000.00"), {"amount": "Invalid expense amount"}),
            (_body(vehicle_id=car, amount="9" * 21), {"amount": "Invalid expense amount"}),
            (_body(vehicle_id=car, amount="1." + "0" * 21), {"amount": "Invalid expense amount"}),
            (_body(vehicle_id=car, amount=12), {"amount": INVALID}),
            (_body(vehicle_id=car, amount=None), {"amount": INVALID}),
            (_body(vehicle_id=car, incurred_on="2026-02-30"), {"incurred_on": "Invalid expense date"}),
            (_body(vehicle_id=car, incurred_on="20260416"), {"incurred_on": "Invalid expense date"}),
            (_body(vehicle_id=car, incurred_on="0001-01-01"), {"incurred_on": "Invalid expense date"}),
            (_body(vehicle_id=car, incurred_on="1899-12-31"), {"incurred_on": "Invalid expense date"}),
            (_body(vehicle_id=car, incurred_on="9999-01-01"), {"incurred_on": "Invalid expense date"}),
            (_body(vehicle_id=car, category="other"), {"treatment": "Other expenses require a tax treatment"}),
            (_body(vehicle_id=car, category="bogus"), {"category": INVALID}),
            (_body(vehicle_id=car, treatment="bogus"), {"treatment": INVALID}),
            (_body(vehicle_id=car, treatment=None), {"treatment": INVALID}),
            (_body(vehicle_id=theirs["car"]), {"vehicle_id": "No such vehicle"}),
            (_body(vehicle_id=2**70), {"vehicle_id": "No such vehicle"}),
            (_body(vehicle_id=0), {"vehicle_id": "No such vehicle"}),
            (_body(vehicle_id="1"), {"vehicle_id": INVALID}),
            (_body(vehicle_id=car, trip_id=theirs["business"]), {"trip_id": "No such trip"}),
            (_body(vehicle_id=car, trip_id=0), {"trip_id": "No such trip"}),
            (_body(vehicle_id=car, trip_id=2**63), {"trip_id": "No such trip"}),
            (_body(vehicle_id=car, notes="a\x00b"), {"notes": INVALID}),
            (_body(vehicle_id=car, receipt="x"), {"receipt": INVALID}),
            ({"vehicle_id": car}, {"incurred_on": "This field is required.",
                                   "category": "This field is required.", "amount": "This field is required."}),
            (_body(vehicle_id=theirs["car"], amount="x", trip_id=theirs["business"]),
             {"amount": "Invalid expense amount"}),
            (_body(vehicle_id=theirs["car"], trip_id=theirs["business"]),
             {"vehicle_id": "No such vehicle", "trip_id": "No such trip"}),
        ]
        for body, fields in cases:
            response = await _create(state, body, key)
            assert response.status_code == 422, (body, response.text)
            assert response.json() == {"error": {"code": "validation_failed",
                                                 "message": ERRORS["validation_failed"][1],
                                                 "fields": fields}}, body
        assert await _count(state) == count and commits.take() == []
        # A corrected request may use the key; the boundary years and amounts are accepted.
        for body in (_body(vehicle_id=car), _body(vehicle_id=car, incurred_on="1900-01-01", amount="0.01"),
                     _body(vehicle_id=car, incurred_on="9998-12-31", amount="9999999999.99")):
            response = await _create(state, body, key if body["amount"] == "20.50" else None)
            assert response.status_code == 201, response.text
            assert response.json()["amount"] == body["amount"]
    _run(check)


def test_money_survives_as_exact_decimal_strings():
    async def check(state):
        ids = await _seed(state)
        amounts = ["0.01", "0.10", "1.00", "12.30", "33.33", "1234567.89", "9999999999.99"]
        created = []
        for amount in amounts:
            response = await _create(state, _body(vehicle_id=ids["car"], amount=amount))
            assert response.status_code == 201
            created.append(response.json()["id"])
            assert response.json()["amount"] == amount
        async with state.raw.connection() as conn:
            stored = await (await conn.execute(
                "SELECT amount FROM expenses WHERE id = ANY(%s) ORDER BY id", (created,))).fetchall()
        assert [row[0] for row in stored] == [Decimal(amount) for amount in amounts]
        listed = {item["id"]: item["amount"] for item in
                  (await _get(state, "/api/v1/expenses", year=2026, limit=200)).json()["items"]}
        assert [listed[expense_id] for expense_id in created] == amounts
        response = await _api(state, "PATCH", f"/api/v1/expenses/{created[0]}", body={"amount": "0.1"})
        assert response.json()["amount"] == "0.10"
    _run(check)


def test_concurrent_creates_with_one_key_make_one_expense():
    async def check(state):
        ids = await _seed(state)
        count = await _count(state)
        key = _key()
        responses = await asyncio.gather(*(_create(state, _body(vehicle_id=ids["car"]), key) for _ in range(2)))
        statuses = sorted(r.status_code for r in responses)
        assert statuses in ([200, 201], [201, 503]), statuses
        if statuses == [201, 503]:
            assert (await _create(state, _body(vehicle_id=ids["car"]), key)).status_code == 200
        assert await _count(state) == count + 1

        # In the database, two transactions claiming one key leave one expense.
        expense = Expense(vehicle_id=ids["car"], incurred_on=date(2026, 4, 16), category="fuel",
                          amount=Decimal("20.50"), treatment="business_use_allocated")
        key = uuid.uuid4()
        async with application_role_pools(TEST_DB) as roles:
            pool = AccountPool(roles.runtime, AccountPrincipal(state.owner, True, 1))

            async def claim():
                async with pool.connection() as conn:
                    outcome = await create_expense(conn, expense, key, expense_digest(expense))
                    await asyncio.sleep(0.2)
                    return outcome
            outcomes = await asyncio.gather(claim(), claim())
        assert sorted(created for _, created in outcomes) == [False, True]
        assert len({expense_id for expense_id, _ in outcomes}) == 1
        assert await _count(state) == count + 2

        # A create racing an uncommitted claim waits for it, then replays it
        # once it commits, or inserts once it rolls back.
        for commit, expected in ((True, 200), (False, 201)):
            key = _key()
            holder = await psycopg.AsyncConnection.connect(TEST_DB)
            try:
                await holder.execute(
                    "INSERT INTO expenses (account_id, vehicle_id, incurred_on, category, amount, treatment, "
                    "notes, idempotency_key, idempotency_digest) VALUES (%s, %s, '2026-04-16', 'fuel', 20.50, "
                    "'business_use_allocated', 'Top up', %s, %s)",
                    (state.owner, ids["car"], key, expense_digest(Expense(**{**expense.__dict__, "notes": "Top up"}))))
                task = asyncio.create_task(_create(state, _body(vehicle_id=ids["car"]), key))
                await asyncio.sleep(0.5)
                assert not task.done()
                await (holder.commit() if commit else holder.rollback())
                response = await task
            finally:
                await holder.close()
            assert response.status_code == expected, response.text
            async with state.raw.connection() as conn:
                assert await (await conn.execute(
                    "SELECT count(*) FROM expenses WHERE idempotency_key = %s", (key,))).fetchone() == (1,)
    _run(check)


def test_a_full_account_gets_storage_full_and_claims_nothing(monkeypatch):
    async def check(state):
        commits = _Commits(monkeypatch)
        theirs = await _seed(state, OTHER)
        before = await _row(state, theirs["fuel"])
        count = await _count(state)
        async with state.raw.connection() as conn:
            used = (await (await conn.execute(
                "SELECT actual_bytes + reserved_bytes FROM account_usage WHERE account_id = %s",
                (OTHER,))).fetchone())[0]
            await conn.execute(
                "UPDATE storage_grants SET limit_mode = 'custom', account_limit_bytes = %s, "
                "raw_limit_bytes = NULL, enhancement_limit_bytes = NULL WHERE account_id = %s", (used, OTHER))
        key = _key()
        response = await _create(state, _body(vehicle_id=theirs["car"]), key, OTHER)
        assert (response.status_code, response.json()) == (409, _envelope("storage_full"))
        response = await _api(state, "PATCH", f"/api/v1/expenses/{theirs['fuel']}", OTHER,
                              body={"notes": "x" * 500})
        assert (response.status_code, response.json()) == (409, _envelope("storage_full"))
        assert await _row(state, theirs["fuel"]) == before and await _count(state) == count
        assert commits.take() == []
        async with state.raw.connection() as conn:
            await conn.execute("UPDATE storage_grants SET limit_mode = 'default', account_limit_bytes = NULL "
                               "WHERE account_id = %s", (OTHER,))
        assert (await _create(state, _body(vehicle_id=theirs["car"]), key, OTHER)).status_code == 201
    _run(check)


def test_patch_leaves_the_same_rows_as_the_web_edit(monkeypatch):
    async def check(state):
        commits = _Commits(monkeypatch)
        ids = await _seed(state)
        web, api = ids["parking"], ids["fuel"]
        changes = {"vehicle_id": ids["van"], "incurred_on": "2026-04-15", "category": "tolls",
                   "amount": "7.25", "treatment": "fully_business", "notes": " Bridge ",
                   "trip_id": ids["unclassified"]}
        response = await _web(state, f"/expenses/{web}/update", _web_form(**changes))
        assert response.status_code == 204
        before = await _row(state, api)
        response = await _api(state, "PATCH", f"/api/v1/expenses/{api}", body=changes)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store, private"
        web_row, api_row = await _row(state, web), await _row(state, api)
        assert _durable(api_row) == _durable(web_row)
        assert api_row["updated_at"] != before["updated_at"]
        assert commits.take() == [f"/expenses/{web}/update", f"/api/v1/expenses/{api}"]
        body = response.json()
        assert (body["notes"], body["trip_id"], body["conflicts"]) == ("Bridge", ids["unclassified"], [])

        # Unchanged: the API writes nothing; the web form still saves.
        stored = await _row(state, api)
        for patch in ({}, changes, {"amount": "7.250", "notes": "Bridge"}):
            response = await _api(state, "PATCH", f"/api/v1/expenses/{api}", body=patch)
            assert response.status_code == 200 and response.json() == body
        assert await _row(state, api) == stored and commits.take() == []
        response = await _web(state, f"/expenses/{api}/update", _web_form(**changes))
        assert response.status_code == 204
        saved = await _row(state, api)
        assert saved["updated_at"] != stored["updated_at"]
        assert {**saved, "updated_at": None} == {**stored, "updated_at": None}
    _run(check)


def test_patch_refuses_what_the_expense_cannot_hold():
    async def check(state):
        ids = await _seed(state)
        theirs = await _seed(state, OTHER)
        expense = ids["fuel"]
        before = await _row(state, expense)
        cases = [
            ({"vehicle_id": None}, {"vehicle_id": INVALID}),
            ({"incurred_on": None}, {"incurred_on": INVALID}),
            ({"category": None}, {"category": INVALID}),
            ({"amount": None}, {"amount": INVALID}),
            ({"treatment": None}, {"treatment": INVALID}),
            ({"amount": "0"}, {"amount": "Invalid expense amount"}),
            ({"amount": "12345678901"}, {"amount": "Invalid expense amount"}),
            ({"incurred_on": "0001-01-01"}, {"incurred_on": "Invalid expense date"}),
            ({"incurred_on": "9999-12-31"}, {"incurred_on": "Invalid expense date"}),
            ({"incurred_on": "2026-4-1"}, {"incurred_on": "Invalid expense date"}),
            ({"vehicle_id": theirs["car"]}, {"vehicle_id": "No such vehicle"}),
            ({"trip_id": theirs["business"]}, {"trip_id": "No such trip"}),
            ({"trip_id": -1}, {"trip_id": "No such trip"}),
            ({"notes": "a\x00"}, {"notes": INVALID}),
            ({"id": 5}, {"id": INVALID}),
        ]
        for body, fields in cases:
            response = await _api(state, "PATCH", f"/api/v1/expenses/{expense}", body=body)
            assert response.status_code == 422, (body, response.text)
            assert response.json()["error"]["fields"] == fields, body
        for content in ("[]", "not json"):
            response = await _api(state, "PATCH", f"/api/v1/expenses/{expense}", content=content,
                                  headers={"Content-Type": "application/json"})
            assert response.status_code == 422, content
        assert await _row(state, expense) == before
    _run(check)


def test_writes_never_reach_another_account(monkeypatch):
    async def check(state):
        commits = _Commits(monkeypatch)
        ids = await _seed(state)
        theirs = await _seed(state, OTHER)
        their_expenses = [theirs[name] for name in ("fuel", "parking", "conflicted", "insurance", "old")]
        rows = {expense_id: await _row(state, expense_id) for expense_id in their_expenses}
        for expense_id in their_expenses:
            response = await _api(state, "PATCH", f"/api/v1/expenses/{expense_id}", body={"notes": "mine"})
            assert (response.status_code, response.json()) == (404, _envelope("not_found"))
            response = await _api(state, "PATCH", f"/api/v1/expenses/{expense_id}",
                                  body={"vehicle_id": ids["car"], "trip_id": ids["business"]})
            assert response.status_code == 404
            response = await _api(state, "DELETE", f"/api/v1/expenses/{expense_id}")
            assert (response.status_code, response.json()) == (404, _envelope("not_found"))
        for path in ("/api/v1/expenses/0", f"/api/v1/expenses/{2**63}", "/api/v1/expenses/999999"):
            assert (await _api(state, "PATCH", path, body={"notes": "x"})).status_code == 404
            assert (await _api(state, "DELETE", path)).status_code == 404
        assert {expense_id: await _row(state, expense_id) for expense_id in rows} == rows
        assert commits.take() == []
        listed = (await _get(state, "/api/v1/expenses", year=2026, limit=200)).json()["items"]
        assert not {item["id"] for item in listed} & set(their_expenses)
        response = await _api(state, "PATCH", f"/api/v1/expenses/{theirs['fuel']}", OTHER, body={"notes": "theirs"})
        assert response.status_code == 200 and response.json()["notes"] == "theirs"
    _run(check)


def test_delete_matches_the_web(monkeypatch):
    async def check(state):
        commits = _Commits(monkeypatch)
        ids = await _seed(state)
        twins = []
        for _ in range(2):
            assert (await _web(state, "/expenses", _web_form(vehicle_id=ids["car"]))).status_code == 204
            twins.append(await _last_id(state))
        commits.take()
        web, api = twins
        usage = await _usage(state)
        assert (await _web(state, f"/expenses/{web}/delete", {})).status_code == 204
        web_cost = usage - await _usage(state)
        usage = await _usage(state)
        response = await _api(state, "DELETE", f"/api/v1/expenses/{api}")
        assert (response.status_code, response.content) == (204, b"")
        assert response.headers["cache-control"] == "no-store, private"
        assert usage - await _usage(state) == web_cost > 0
        assert await _row(state, web) is None and await _row(state, api) is None
        assert commits.take() == [f"/expenses/{web}/delete", f"/api/v1/expenses/{api}"]
        assert (await _api(state, "DELETE", f"/api/v1/expenses/{api}")).status_code == 404
    _run(check)


async def _walk(state, account=None, limit=2, **params):
    pages, cursor = [], None
    while True:
        query = {**params, "limit": limit, **({"cursor": cursor} if cursor else {})}
        response = await _get(state, "/api/v1/expenses", account, **query)
        assert response.status_code == 200, response.text
        body = response.json()
        pages.append([item["id"] for item in body["items"]])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages


def test_the_list_pages_newest_first_and_each_expense_once():
    async def check(state):
        ids = await _seed(state)
        await _seed(state, OTHER)
        # Equal dates break ties by id.
        same_day = []
        for amount in ("1.00", "2.00", "3.00"):
            same_day.append((await _create(state, _body(vehicle_id=ids["car"], incurred_on="2026-04-14",
                                                        amount=amount))).json()["id"])
        async with state.pools[state.owner].connection() as conn:
            expected = [row[0] for row in await (await conn.execute(
                "SELECT id FROM expenses WHERE account_id = %s AND incurred_on >= '2026-01-01' "
                "AND incurred_on < '2027-01-01' ORDER BY incurred_on DESC, id DESC",
                (account_id(conn),))).fetchall()]
        pages = await _walk(state, year=2026)
        assert [expense_id for page in pages for expense_id in page] == expected
        assert all(len(page) == 2 for page in pages[:-1])
        assert sorted(same_day, reverse=True) == [i for i in expected if i in same_day]
        assert await _walk(state, year=2025, limit=50) == [[ids["old"]]]
        assert await _walk(state, year=2024) == [[]]
        # Inserts newer than the cursor wait for a restart; older ones appear
        # once; deletions just stop appearing.
        first = await _get(state, "/api/v1/expenses", year=2026, limit=3)
        seen = [item["id"] for item in first.json()["items"]]
        newer = (await _create(state, _body(vehicle_id=ids["car"], incurred_on="2026-12-31"))).json()["id"]
        older = (await _create(state, _body(vehicle_id=ids["car"], incurred_on="2026-01-02"))).json()["id"]
        gone = expected[4]
        assert (await _api(state, "DELETE", f"/api/v1/expenses/{gone}")).status_code == 204
        cursor = first.json()["next_cursor"]
        while cursor:
            body = (await _get(state, "/api/v1/expenses", year=2026, limit=3, cursor=cursor)).json()
            seen += [item["id"] for item in body["items"]]
            cursor = body["next_cursor"]
        assert newer not in seen and seen.count(older) == 1 and gone not in seen
        assert len(seen) == len(set(seen))
    _run(check)


def test_the_list_cursor_is_bound_to_its_year_and_rejects_tampering():
    async def check(state):
        ids = await _seed(state)
        for amount in ("1.00", "2.00"):
            await _create(state, _body(vehicle_id=ids["car"], amount=amount))
        page = (await _get(state, "/api/v1/expenses", year=2026, limit=2)).json()
        cursor = page["next_cursor"]
        assert cursor
        response = await _get(state, "/api/v1/expenses", year=2025, cursor=cursor)
        assert (response.status_code, response.json()) == (400, _envelope("invalid_cursor"))
        trips = (await _get(state, "/api/v1/trips", limit=1)).json()
        assert trips["next_cursor"]
        middle = len(cursor) // 2
        tampered = cursor[:middle] + ("A" if cursor[middle] != "A" else "B") + cursor[middle + 1:]
        for bad in (tampered, "x", cursor + "A", trips["next_cursor"]):
            response = await _get(state, "/api/v1/expenses", year=2026, cursor=bad)
            assert (response.status_code, response.json()) == (400, _envelope("invalid_cursor")), bad
        response = await _get(state, "/api/v1/trips", cursor=cursor)
        assert response.status_code == 400
        # Another account's walk with this cursor stays inside its own rows.
        await _seed(state, OTHER)
        response = await _get(state, "/api/v1/expenses", OTHER, year=2026, cursor=cursor)
        assert response.status_code == 200
        owner_ids = {item["id"] for item in (await _get(state, "/api/v1/expenses", year=2026, limit=200)
                                             ).json()["items"]}
        assert not owner_ids & {item["id"] for item in response.json()["items"]}
    _run(check)


@pytest.mark.parametrize("path", ["/api/v1/expenses", "/api/v1/expenses/comparison"])
def test_years_outside_1900_to_9998_are_refused(path):
    async def check(state):
        for year in ("1", "1899", "9999", "0", "-1", "abc", ""):
            response = await _get(state, path, year=year)
            assert response.status_code == 422, (year, response.text)
            assert response.json()["error"]["fields"] == {"year": INVALID}, year
        for year in (1900, 9998):
            assert (await _get(state, path, year=year)).status_code == 200
        for limit in (0, 201, "x"):
            if path == "/api/v1/expenses":
                assert (await _get(state, path, limit=limit)).status_code == 422
    _run(check)


def test_the_list_defaults_to_the_current_display_year():
    async def check(state):
        ids = await _seed(state)
        from datetime import datetime
        today = datetime.now(TZ).date()
        created = (await _create(state, _body(vehicle_id=ids["car"], incurred_on=today.isoformat()))).json()
        items = (await _get(state, "/api/v1/expenses", limit=200)).json()["items"]
        assert created["id"] in {item["id"] for item in items}
        assert {item["incurred_on"][:4] for item in items} == {str(today.year)}
        response = await _get(state, "/api/v1/expenses/comparison")
        assert response.status_code == 422 and response.json()["error"]["fields"] == {"year": "This field is required."}
    _run(check)


def test_list_items_carry_conflict_codes_and_trip_links():
    async def check(state):
        ids = await _seed(state)
        items = {item["id"]: item for item in
                 (await _get(state, "/api/v1/expenses", year=2026, limit=200)).json()["items"]}
        assert items[ids["conflicted"]]["conflicts"] == ["vehicle", "date", "not_my_vehicle"]
        assert items[ids["conflicted"]]["trip_id"] == ids["not_mine"]
        assert items[ids["fuel"]]["conflicts"] == [] and items[ids["fuel"]]["trip_id"] == ids["business"]
        assert items[ids["parking"]]["trip_id"] is None
        # Deleting a linked trip clears the link, as on the web.
        assert (await _api(state, "DELETE", f"/api/v1/trips/{ids['business']}")).status_code == 204
        items = {item["id"]: item for item in
                 (await _get(state, "/api/v1/expenses", year=2026, limit=200)).json()["items"]}
        assert items[ids["fuel"]]["trip_id"] is None
    _run(check)


async def _reference_comparison(state, year):
    async with state.pools[state.owner].connection() as conn:
        trips, rates = await fetch_range_trips(conn, TZ, date(year, 1, 1), date(year, 12, 31))
        expenses, readings = await fetch_expense_report_inputs(conn, year)
    return build_expense_report(year, trips, expenses, readings, rates, TZ)


def test_the_comparison_matches_the_annual_report():
    async def check(state):
        await _seed(state)
        await _seed(state, OTHER)
        await _sql(state, "DELETE FROM expenses WHERE account_id = %s", (OTHER,), OTHER)
        for year in (2025, 2026, 2027):
            response = await _get(state, "/api/v1/expenses/comparison", year=year)
            assert response.status_code == 200, response.text
            body = response.json()
            report = await _reference_comparison(state, year)
            assert body == api_expenses._comparison(year, report).model_dump(mode="json")
            assert body["ledger_total"] == format(report.ledger_total, ".2f")
        body = (await _get(state, "/api/v1/expenses/comparison", year=2026)).json()
        assert body["ledger_total"] == "1261.11"
        assert [line["vehicle_name"] for line in body["vehicles"]] == ["My Car", "Van"]
        car = body["vehicles"][0]
        assert (car["denominator_source"], car["provisional"]) == ("odometer", False)
        # The other account's ledger is only its own: it has trips but no expenses.
        theirs = (await _get(state, "/api/v1/expenses/comparison", OTHER, year=2026)).json()
        assert theirs["ledger_total"] == "0.00" and theirs["category_totals"] == []
        fixture = json.loads((FIXTURES / "expense_comparison_response.json").read_text())
        assert _without_ids(body) == _without_ids(fixture)
    _run(check)


def test_the_web_report_shows_the_comparison_totals():
    async def check(state):
        await _seed(state)
        body = (await _get(state, "/api/v1/expenses/comparison", year=2026)).json()
        page = (await state.client.get("/report/2026")).text
        for line in body["vehicles"]:
            for field in ("allocated_expenses", "fully_business_expenses", "standard_total", "actual_total"):
                if line[field] is not None:
                    assert f"${Decimal(line[field]):,.2f}" in page, field
    _run(check)


def test_writes_authenticate_before_reading_the_body():
    async def check(state):
        ids = await _seed(state)
        large = json.dumps({"notes": "x" * (2 * 1024 * 1024)})
        bad = {"Authorization": "Bearer not-a-token", "Content-Type": "application/json"}
        expense = ids["fuel"]
        for method, path, extra in (("PATCH", f"/api/v1/expenses/{expense}", {}),
                                    ("POST", "/api/v1/expenses", {"Idempotency-Key": _key()}),
                                    ("DELETE", f"/api/v1/expenses/{expense}", {})):
            response = await state.client.request(method, path, content=large, headers={**bad, **extra})
            assert (response.status_code, response.json()["error"]["code"]) == (401, "invalid_token"), method
            response = await state.client.request(method, path, content="{}", headers={
                "Content-Type": "application/json", **extra})
            assert response.status_code == 401, method
        for method, path, extra in (("PATCH", f"/api/v1/expenses/{expense}", {}),
                                    ("POST", "/api/v1/expenses", {"Idempotency-Key": _key()})):
            response = await _api(state, method, path, content=large,
                                  headers={"Content-Type": "application/json", **extra})
            assert (response.status_code, response.json()["error"]["code"]) == (413, "payload_too_large")
        for path in ("/api/v1/expenses", "/api/v1/expenses/comparison?year=2026"):
            response = await state.client.get(path)
            assert response.status_code == 401
        assert await _row(state, expense) is not None
    _run(check)


def test_portable_export_leaves_expense_idempotency_keys_out():
    async def check(state):
        from app.portable.export import _export_expense, _fetch_export_expenses
        ids = await _seed(state)
        assert (await _create(state, _body(vehicle_id=ids["car"]))).status_code == 201
        async with state.pools[state.owner].connection() as conn:
            expenses = [_export_expense(row) for row in await _fetch_export_expenses(conn)]
        assert expenses and not any({"idempotency_key", "idempotency_digest"} & set(e) for e in expenses)
    _run(check)


async def _vehicle_names(state) -> dict[str, int]:
    async with state.pools[state.owner].connection() as conn:
        rows = await (await conn.execute(
            "SELECT name, id FROM vehicles WHERE account_id = %s", (account_id(conn),))).fetchall()
    return dict(rows)


def _local(request: dict, fixture: dict, names: dict[str, int], trips: dict[str, int]) -> dict:
    local = dict(request)
    if local.get("vehicle_id") is not None:
        local["vehicle_id"] = names[fixture["vehicles"][str(local["vehicle_id"])]]
    if local.get("trip_id") is not None:
        local["trip_id"] = trips[fixture["trips"][str(local["trip_id"])]]
    return local


def test_patch_cases_match_the_synthetic_fixture(monkeypatch):
    async def check(state):
        commits = _Commits(monkeypatch)
        ids = await _seed(state)
        names = await _vehicle_names(state)
        fixture = json.loads((FIXTURES / "expense_patch_cases.json").read_text())
        covered = {(c["field"], c["case"]) for c in fixture["cases"]}
        assert covered == {("all", "omitted")} | {
            (field, case) for field in api_expenses.ExpensePatch.model_fields
            for case in ("unchanged", "modified", "null")}
        expense = ids["fuel"]
        for case in fixture["cases"]:
            before = await _row(state, expense)
            response = await _api(state, "PATCH", f"/api/v1/expenses/{expense}",
                                  body=_local(case["request"], fixture, names, ids))
            label = (case["field"], case["case"])
            assert response.status_code == case["status"], label
            assert _without_ids(response.json()) == _without_ids(case["response"]), label
            after = await _row(state, expense)
            assert (after["updated_at"] != before["updated_at"]) == case["written"], label
            assert commits.take() == ([f"/api/v1/expenses/{expense}"] if case["written"] else []), label
            if not case["written"]:
                assert after == before, label
    _run(check)


def test_create_cases_match_the_synthetic_fixture():
    async def check(state):
        ids = await _seed(state)
        names = await _vehicle_names(state)
        fixture = json.loads((FIXTURES / "expense_create_cases.json").read_text())
        count = await _count(state)
        created = {}
        for case in fixture["cases"]:
            response = await _api(state, "POST", "/api/v1/expenses", headers=case["headers"],
                                  body=_local(case["request"], fixture, names, ids))
            assert response.status_code == case["status"], case["case"]
            body = response.json()
            assert _without_ids(body) == _without_ids(case["response"]), case["case"]
            headers = {k: v for k, v in response.headers.items() if k in ("location", "idempotent-replayed")}
            assert set(headers) == set(case["response_headers"]), case["case"]
            if "location" in headers:
                assert headers["location"] == f"/api/v1/expenses/{body['id']}"
                created[case["case"]] = body["id"]
            if case["case"] == "replayed":
                assert body["id"] == created["created"]
        assert await _count(state) == count + 2
    _run(check)


def test_the_list_matches_the_synthetic_fixture():
    async def check(state):
        await _seed(state)
        response = await _get(state, "/api/v1/expenses", year=2026, limit=3)
        body = response.json()
        fixture = json.loads((FIXTURES / "expenses_page_response.json").read_text())
        assert _without_ids({**body, "next_cursor": body["next_cursor"] is not None}) == _without_ids(
            {**fixture, "next_cursor": fixture["next_cursor"] is not None})
    _run(check)

