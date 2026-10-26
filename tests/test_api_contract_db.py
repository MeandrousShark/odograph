"""API v1 behavioral contract: every case in
`tests/fixtures/api_v1/contract_cases.json` replayed against the real
application, one seeded world per test, comparing status, error code, field
presence, null versus value, units and paging with the checked-in fixtures
(see tests/api_contract.py for what is compared).

After a deliberate, compatible change, refresh the cases whose expected
response is inline, and write fixture files that do not exist yet, with

    ODOGRAPH_UPDATE_API_CONTRACT=1 .venv/bin/pytest tests/test_api_contract_db.py

Existing fixture files are never rewritten; edit them by hand.
"""
from __future__ import annotations

import json
import os
import re
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path

import pytest

from app.account_context import account_id, control_connection
from app.api_grants import create_grant
from app.capacity import AdmissionManager, CapacityBusy
from app.ingest import FailedAuthLimiter
from api_contract import contract_mismatches
from api_expense_support import seed_ledger
from api_trip_support import seed_trip_kinds
from conftest import seed_tracking_device
from test_api_trips_db import OTHER, TEST_DB, _run

pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")
FIXTURES = Path(__file__).parent / "fixtures" / "api_v1"
CASES = FIXTURES / "contract_cases.json"
GENERATE = "generate"
UNKNOWN_TOKEN = "A" * 43
_REF = re.compile(r"\{([a-z_]+:[^}]+)\}")


async def _rows(conn, sql, params=()):
    return await (await conn.execute(sql, params)).fetchall()


async def _seed_world(world, state) -> dict[str, object]:
    refs: dict[str, object] = {}
    owner, other = state.pools[state.owner], state.pools[OTHER]
    if world == "trips":
        async with owner.connection() as conn:
            refs.update({f"trip:{kind}": trip for kind, trip in (await seed_trip_kinds(conn)).items()})
            for name, vehicle in await _rows(conn, "SELECT name, id FROM vehicles WHERE account_id = %s",
                                             (account_id(conn),)):
                refs[f"vehicle:{name}"] = vehicle
        async with other.connection() as conn:
            refs.update({f"other_trip:{kind}": trip for kind, trip in (await seed_trip_kinds(conn)).items()})
    elif world == "ledger":
        for pool, prefix in ((owner, ""), (other, "other_")):
            async with pool.connection() as conn:
                for name, row in (await seed_ledger(conn)).items():
                    kind = "vehicle" if name in ("car", "van") else "expense" if name in _EXPENSES else "trip"
                    refs[f"{prefix}{kind}:{name}"] = row
    elif world == "auth":
        async with control_connection(state.app.state.control_pool) as conn:
            spare = await create_grant(conn, state.owner, 1, "Spare phone")
        refs["access:spare"], refs["refresh:spare"] = spare.access_token, spare.refresh_token
    return refs


_EXPENSES = ("fuel", "parking", "conflicted", "insurance", "old")


@asynccontextmanager
async def _setup(name, state, refs, patch):
    """A condition that lasts for one case only."""
    if name == "strict_limiter":
        patch.setattr(state.app.state, "api_auth_limiter", FailedAuthLimiter(1, 900))
        yield
    elif name == "foreground_busy":
        original = AdmissionManager.operation

        def operation(manager, lane, *args, **kwargs):
            if lane == "foreground":
                raise CapacityBusy("contract case")
            return original(manager, lane, *args, **kwargs)
        patch.setattr(AdmissionManager, "operation", operation)
        yield
    elif name == "full_storage_other":
        async with state.raw.connection() as conn:
            used = (await (await conn.execute(
                "SELECT actual_bytes + reserved_bytes FROM account_usage WHERE account_id = %s",
                (OTHER,))).fetchone())[0]
            await conn.execute(
                "UPDATE storage_grants SET limit_mode = 'custom', account_limit_bytes = %s, "
                "raw_limit_bytes = NULL, enhancement_limit_bytes = NULL WHERE account_id = %s", (used, OTHER))
        yield
        async with state.raw.connection() as conn:
            await conn.execute("UPDATE storage_grants SET limit_mode = 'default', account_limit_bytes = NULL "
                               "WHERE account_id = %s", (OTHER,))
    elif name == "trip_in_progress":
        async with state.pools[state.owner].connection() as conn:
            device = await seed_tracking_device(conn, "car")
            await conn.execute(
                "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, received_at, geom) "
                "SELECT %s, %s, 'car', now() - n * interval '1 minute', now(), "
                "ST_SetSRID(ST_MakePoint(-122.3, 47.6), 4326)::geography FROM generate_series(1, 20) n",
                (account_id(conn), device))
            refs["trip:in_progress"] = (await (await conn.execute(
                "INSERT INTO trips (account_id, tracking_device_id, device, source, started_at, ended_at, "
                "distance_m) VALUES (%s, %s, 'car', 'detected', now() - interval '10 minutes', "
                "now() - interval '1 minute', 700) RETURNING id", (account_id(conn), device))).fetchone())[0]
        yield
        async with state.pools[state.owner].connection() as conn:
            await conn.execute("DELETE FROM trips WHERE id = %s", (refs.pop("trip:in_progress"),))
            await conn.execute("DELETE FROM points WHERE tracking_device_id = %s", (device,))
    else:
        raise AssertionError(f"unknown setup {name}")


def _resolve(text: str, refs) -> str:
    return _REF.sub(lambda match: str(refs[match.group(1)]), text)


def _request(case, state, refs):
    auth = case["auth"]
    token = {"owner": state.tokens[state.owner], "other": state.tokens[OTHER], "none": None,
             "unknown": UNKNOWN_TOKEN}.get(auth, refs.get(auth))
    headers = dict(case.get("headers", {}))
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    kwargs = {"params": case.get("query", {}), "headers": headers}
    if "body_bytes" in case:
        kwargs["content"] = json.dumps({"notes": "x" * case["body_bytes"]})
        headers["Content-Type"] = "application/json"
    elif "body" in case or "body_fixture" in case:
        body = case.get("body")
        if body is None:
            body = json.loads((FIXTURES / case["body_fixture"]).read_text())
        body = {**body, **{field: refs[ref] for field, ref in case.get("bind", {}).items()},
                **case.get("body_changes", {})}
        kwargs["json"] = body
    return case["operation"].split(" ", 1)[0], _resolve(case["path"], refs), kwargs


def _ids(body) -> list:
    if "items" in body:
        return [item["id"] for item in body["items"]]
    return [] if body.get("trip") is None else [body["trip"]["id"]]


async def _replay(case, state, refs) -> tuple[list[str], object]:
    method, path, kwargs = _request(case, state, refs)
    for _ in range(case.get("prior_failures", 0)):
        prior = await state.client.request(method, _resolve(case["path"], refs), **kwargs)
        if prior.status_code != 401:
            return [f"prior request status {prior.status_code}, expected 401"], None
    template = re.escape(case["operation"].split(" ", 1)[1])
    assert re.fullmatch(re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", template), path) or case["status"] == 404, path
    response = await state.client.request(method, path, **kwargs)
    problems = []
    if response.status_code != case["status"]:
        return [f"status {response.status_code}, expected {case['status']}: {response.text[:300]}"], None
    for name, value in case.get("response_headers", {}).items():
        got = response.headers.get(name)
        pattern = re.escape(value).replace(re.escape("{id}"), "[1-9][0-9]*")
        if got is None or not re.fullmatch(pattern, got):
            problems.append(f"header {name}: expected {value!r}, got {got!r}")
    if case["status"] == 204 or case.get("binary"):
        if case["status"] == 204 and response.content:
            problems.append("204 with a body")
        if case.get("binary") and not (response.content and response.headers.get("content-length")
                                       == str(len(response.content))):
            problems.append("binary response without a complete body and Content-Length")
        return problems, None
    body = response.json()
    if "walk" in case:
        pages = [_ids(body)]
        while body["next_cursor"] is not None:
            next_page = await state.client.request(method, path, **{
                **kwargs, "params": {**kwargs["params"], "cursor": body["next_cursor"]}})
            body = next_page.json()
            pages.append(_ids(body))
        expected = [[refs[ref] for ref in page] for page in case["walk"]]
        if pages != expected:
            problems.append(f"pages {pages}, expected {expected}")
        return problems, None
    expected = case.get("response")
    updating = os.environ.get("ODOGRAPH_UPDATE_API_CONTRACT") == "1"
    if "response_fixture" in case and not (FIXTURES / case["response_fixture"]).exists():
        if not updating:
            return problems + [f"missing fixture {case['response_fixture']}"], None
        (FIXTURES / case["response_fixture"]).write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n")
        return problems, None
    if expected == GENERATE or (updating and "response_fixture" not in case):
        return problems, body
    if "response_fixture" in case:
        expected = json.loads((FIXTURES / case["response_fixture"]).read_text())
    problems += contract_mismatches(expected, body, ignore=frozenset(case.get("ignore", ())))
    return problems, None


def _replay_world(world, monkeypatch):
    document = json.loads(CASES.read_text())
    cases = [case for case in document["cases"] if case["world"] == world]
    assert cases
    failures, generated = [], {}

    async def check(state):
        refs = await _seed_world(world, state)
        for case in cases:
            with monkeypatch.context() as patch:
                async with (_setup(case["setup"], state, refs, patch) if "setup" in case else nullcontext()):
                    problems, body = await _replay(case, state, refs)
            failures.extend(f"{case['name']}: {problem}" for problem in problems)
            if body is not None:
                generated[case["name"]] = body

    _run(check)
    if generated and os.environ.get("ODOGRAPH_UPDATE_API_CONTRACT") == "1" and not failures:
        for case in document["cases"]:
            if case["name"] in generated:
                case["response"] = generated[case["name"]]
        CASES.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    elif generated:
        failures.extend(f"{name}: no expected response" for name in generated)
    assert not failures, "API v1 contract mismatches:\n" + "\n".join(failures)


@pytest.mark.parametrize("world", ["auth", "trips", "ledger"])
def test_contract_cases_replay_against_the_application(world, monkeypatch):
    _replay_world(world, monkeypatch)
