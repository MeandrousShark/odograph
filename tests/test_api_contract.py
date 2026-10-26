"""The API v1 behavioral contract's own checks: the comparator fails on the
changes the freeze forbids, every operation has replayed cases, and every
checked-in fixture is used."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from api_contract import contract_mismatches

FIXTURES = Path(__file__).parent / "fixtures" / "api_v1"
CASES = json.loads((FIXTURES / "contract_cases.json").read_text())["cases"]
SCHEMA = json.loads((FIXTURES / "openapi.json").read_text())
METHODS = ("get", "put", "post", "delete", "patch")


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


def _set(node, path, value):
    *parents, last = path
    for part in parents:
        node = node[part]
    node[last] = value


@pytest.mark.parametrize("name", ["trip_detail_gapped.json", "stats_response.json", "token_response.json",
                                  "error_validation_failed.json", "expenses_page_response.json"])
def test_a_fixture_matches_itself(name):
    assert contract_mismatches(_fixture(name), _fixture(name)) == []


@pytest.mark.parametrize("name,mutate,expected", [
    ("error_not_found.json", lambda d: _set(d, ("error", "code"), "missing"),
     "$.error.code: expected 'not_found', got 'missing'"),
    ("error_validation_failed.json", lambda d: _set(d, ("error", "fields"), None),
     "$.error.fields: expected a value, got null"),
    ("trip_detail_gapped.json", lambda d: d.pop("has_gap"), "$.has_gap: field missing"),
    ("trip_detail_gapped.json", lambda d: d["start"].pop("description"), "$.start.description: field missing"),
    ("trip_detail_gapped.json", lambda d: _set(d, ("vehicle_name",), None),
     "$.vehicle_name: expected a value, got null"),
    ("trip_detail_manual.json", lambda d: _set(d, ("route_raw",), {"type": "MultiLineString", "coordinates": []}),
     "$.route_raw: expected null, got a value"),
    ("trip_detail_gapped.json", lambda d: _set(d, ("distance_m",), d["distance_m"] / 1609.344),
     "$.distance_m: expected 2310.25"),
    ("trip_detail_gapped.json", lambda d: _set(d, ("started_at",), "2026-04-14T15:00:00Z"),
     "$.started_at: expected an RFC 3339 UTC instant"),
    ("trip_detail_gapped.json", lambda d: _set(d, ("expenses", 0, "amount"), "12.3"),
     "$.expenses[0].amount: expected '12.34'"),
    ("trip_detail_gapped.json", lambda d: _set(d, ("point_count",), "42"), "$.point_count: expected number"),
    ("trip_detail_gapped.json", lambda d: _set(d, ("has_gap",), 1), "$.has_gap: expected boolean"),
    ("trip_detail_gapped.json", lambda d: d["stays"].pop(), "$.stays: expected 2 items, got 1"),
    ("trip_detail_gapped.json", lambda d: _set(d, ("tag_source",), "human"),
     "$.tag_source: field not in the fixture"),
    ("stats_response.json", lambda d: _set(d, ("year",), 2024), "$.year: expected 2025, got 2024"),
    ("expenses_page_response.json", lambda d: _set(d, ("next_cursor",), None),
     "$.next_cursor: expected a value, got null"),
])
def test_the_comparator_fails_on_forbidden_changes(name, mutate, expected):
    fixture = _fixture(name)
    actual = copy.deepcopy(fixture)
    mutate(actual)
    problems = contract_mismatches(fixture, actual)
    assert any(problem.startswith(expected) for problem in problems), problems


def test_ids_cursors_tokens_and_instant_values_are_not_compared():
    detail = _fixture("trip_detail_gapped.json")
    actual = copy.deepcopy(detail)
    actual.update(id=99, vehicle_id=98, started_at="2030-01-01T00:00:00.000000Z")
    actual["expenses"][0]["id"] = 97
    assert contract_mismatches(detail, actual) == []
    page = _fixture("expenses_page_response.json")
    assert contract_mismatches(page, {**page, "next_cursor": "other"}) == []
    tokens = _fixture("token_response.json")
    assert contract_mismatches(tokens, {**tokens, "access_token": "x", "refresh_token": "y"}) == []
    me = _fixture("me_response.json")
    assert contract_mismatches(me, {**me, "server_version": "9.9.9"}, ignore=frozenset({"server_version"})) == []
    assert contract_mismatches(me, {**me, "server_version": "9.9.9"}) != []


def _operations() -> dict[str, dict]:
    return {f"{method.upper()} {path}": item[method]
            for path, item in SCHEMA["paths"].items() for method in METHODS if method in item}


def test_every_operation_has_replayed_cases_with_documented_statuses():
    operations = _operations()
    covered = set()
    names = [case["name"] for case in CASES]
    assert len(names) == len(set(names))
    for case in CASES:
        assert case["world"] in ("auth", "trips", "ledger"), case["name"]
        operation = operations.get(case["operation"])
        if operation is None:
            # Framework answers for paths and methods the API does not have.
            assert case["status"] in (404, 405), case["name"]
            continue
        covered.add(case["operation"])
        assert str(case["status"]) in operation["responses"], case["name"]
        template = re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", re.escape(case["operation"].split(" ", 1)[1]))
        assert re.fullmatch(template, re.sub(r"\{[a-z_]+:[^}]+\}", "1", case["path"])), case["name"]
        has_body = any(key in case for key in ("response", "response_fixture", "walk", "binary"))
        assert has_body == (case["status"] != 204), case["name"]
    assert covered == set(operations)


def test_every_error_code_a_client_can_reach_is_replayed():
    replayed = {_fixture(case["response_fixture"])["error"]["code"] for case in CASES
                if case.get("response_fixture", "").startswith("error_")}
    replayed |= {case["response"]["error"]["code"] for case in CASES
                 if isinstance(case.get("response"), dict) and "error" in case["response"]}
    codes = {json.loads(path.read_text())["error"]["code"] for path in FIXTURES.glob("error_*.json")}
    # A grant limit needs the sign-in flow (the end-to-end test enables it),
    # and an internal error needs a server fault.
    assert codes - replayed == {"grant_limit_reached", "internal_error"}


def test_every_fixture_is_replayed_or_checked_against_its_model():
    schema_test = (Path(__file__).parent / "test_api_v1_schema.py").read_text()
    used = {case[key] for case in CASES for key in ("response_fixture", "body_fixture") if key in case}
    for path in FIXTURES.iterdir():
        if path.name in ("openapi.json", "contract_cases.json") or path.name.startswith("error_"):
            continue
        assert path.name in used or f'"{path.name}"' in schema_test, path.name
