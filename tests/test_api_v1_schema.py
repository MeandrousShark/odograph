"""API v1 schema contract.

The OpenAPI document is generated from the application without serving it
(docs routes stay off), limited to `/api/v1` paths and the component schemas
they reference, and compared with the checked-in copy in
`tests/fixtures/api_v1/openapi.json`. v1 changes are additive only: the check
fails on a removed field, parameter or media type, a type change, a newly
required request field, a response field that becomes nullable or optional,
a changed `format` (or one newly required of a request field), a request
enum value removed or a request field newly limited to an enum, and a
request bound tightened (a higher minimum, a lower maximum, a new or changed
pattern). Added
request values and added response enum values are additive. After an additive change,
regenerate the copy with

    ODOGRAPH_UPDATE_API_SCHEMA=1 .venv/bin/pytest tests/test_api_v1_schema.py

The synthetic request and response fixtures beside it are for client tests;
they are checked against the same models here.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from app.api_errors import ERRORS, ApiErrorEnvelope
from app.api_expenses import Expense, ExpenseComparison, ExpenseCreate, ExpensePage, ExpensePatch
from app.api_stats import StatsResponse, WeekDashboardResponse
from app.api_trips import (
    PurposeList, ReviewResponse, TripCreate, TripDetailResponse, TripPage, TripPatch, VehicleList,
)
from app.api_v1 import CapabilitiesResponse, MeResponse, TokenRequest, TokenResponse

FIXTURES = Path(__file__).parent / "fixtures" / "api_v1"
SNAPSHOT = FIXTURES / "openapi.json"
API_PREFIX = "/api/v1"
METHODS = ("get", "put", "post", "delete", "patch")


def api_openapi_document() -> dict:
    from app.main import create_app
    from tests.auth_db_fixtures import auth_config

    app = create_app(auth_config("postgresql://schema-check@127.0.0.1:1/unused"))
    assert app.openapi_url is None and app.docs_url is None and app.redoc_url is None
    document = app.openapi()
    paths = {path: item for path, item in document["paths"].items()
             if path == API_PREFIX or path.startswith(API_PREFIX + "/")}
    schemas = document.get("components", {}).get("schemas", {})
    wanted: set[str] = set()

    def collect(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[1]
                if name not in wanted:
                    wanted.add(name)
                    collect(schemas[name])
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(paths)
    return _without_labels(
        {"paths": paths, "components": {"schemas": {name: schemas[name] for name in sorted(wanted)}}})


# Generated from handler names and HTTP reason phrases, not part of the
# contract, and free to change with a framework upgrade.
_LABELS = ("summary", "operationId", "description", "title")
# Maps keyed by names a contract defines (fields, paths, statuses, media
# types), where a key such as "title" is data rather than a label.
_NAMED_MAPS = ("properties", "schemas", "paths", "responses", "content")


def _without_labels(node, named=False):
    if isinstance(node, list):
        return [_without_labels(item) for item in node]
    if not isinstance(node, dict):
        return node
    return {key: _without_labels(value, key in _NAMED_MAPS and not named)
            for key, value in node.items() if named or key not in _LABELS}


def _resolve(document, schema):
    while isinstance(schema, dict) and "$ref" in schema:
        schema = document["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    return schema or {}


def _types(document, schema) -> set[str]:
    schema = _resolve(document, schema)
    found = set()
    for key in ("anyOf", "oneOf"):
        for member in schema.get(key, ()):
            found |= _types(document, member)
    kind = schema.get("type")
    if isinstance(kind, list):
        found.update(kind)
    elif kind:
        found.add(kind)
    elif "properties" in schema:
        found.add("object")
    if schema.get("nullable"):
        found.add("null")
    return found


def _members(document, schema):
    schema = _resolve(document, schema)
    members = [schema]
    for key in ("anyOf", "oneOf"):
        members += [_resolve(document, member) for member in schema.get(key, ())]
    return members


def _leaves(document, schema):
    """The non-null alternatives a value may take."""
    schema = _resolve(document, schema)
    alternatives = [*schema.get("anyOf", ()), *schema.get("oneOf", ())]
    if not alternatives:
        return [] if schema.get("type") == "null" else [schema]
    return [leaf for member in alternatives for leaf in _leaves(document, member)]


def _enum(document, schema) -> set | None:
    """The values an enumerated field allows, or None when any value of its
    type is allowed. A `const` is a one-value enum."""
    values: set = set()
    for leaf in _leaves(document, schema):
        if "enum" in leaf:
            values.update(leaf["enum"])
        elif "const" in leaf:
            values.add(leaf["const"])
        else:
            return None
    return values or None


def _formats(document, schema) -> set[str]:
    return {leaf["format"] for leaf in _leaves(document, schema) if "format" in leaf}


_LOWER_BOUNDS = ("minimum", "exclusiveMinimum", "minLength", "minItems")
_UPPER_BOUNDS = ("maximum", "exclusiveMaximum", "maxLength", "maxItems")


def _bound(document, schema, keyword):
    """The loosest `keyword` across a field's alternatives, or None when
    some alternative does not set it."""
    values = [leaf.get(keyword) for leaf in _leaves(document, schema)]
    if not values or None in values:
        return None
    return min(values) if keyword in _LOWER_BOUNDS else max(values)


def _compare_bounds(old_doc, new_doc, old, new, where, problems):
    """A request may never accept less than before: a raised lower bound, a
    lowered upper bound or a new or changed pattern is breaking."""
    for keyword in (*_LOWER_BOUNDS, *_UPPER_BOUNDS):
        before, after = _bound(old_doc, old, keyword), _bound(new_doc, new, keyword)
        if after is None or before == after:
            continue
        if before is None or (after > before if keyword in _LOWER_BOUNDS else after < before):
            problems.append(f"{where}: request {keyword} tightened from {before} to {after}")
    old_patterns = {leaf["pattern"] for leaf in _leaves(old_doc, old) if "pattern" in leaf}
    new_patterns = {leaf["pattern"] for leaf in _leaves(new_doc, new) if "pattern" in leaf}
    if new_patterns and new_patterns != old_patterns:
        problems.append(f"{where}: request pattern changed from {sorted(old_patterns)} to {sorted(new_patterns)}")


def _compare_values(old_doc, new_doc, old, new, where, request, problems):
    """The freeze's enum and format rules. A request may never lose an
    accepted value; a response may gain values clients must tolerate."""
    old_enum, new_enum = _enum(old_doc, old), _enum(new_doc, new)
    if request and old_enum is None and new_enum is not None:
        problems.append(f"{where}: request field is newly restricted to {sorted(map(str, new_enum))}")
    elif request and old_enum is not None and new_enum is not None:
        for value in sorted(map(str, old_enum - new_enum)):
            problems.append(f"{where}: request enum value {value} removed")
    if request:
        _compare_bounds(old_doc, new_doc, old, new, where, problems)
    old_formats, new_formats = _formats(old_doc, old), _formats(new_doc, new)
    if old_formats and old_formats != new_formats:
        problems.append(f"{where}: format changed from {sorted(old_formats)} to {sorted(new_formats)}")
    elif request and new_formats and not old_formats:
        problems.append(f"{where}: request field newly requires format {sorted(new_formats)}")


def _compare_schema(old_doc, new_doc, old, new, where, request, problems):
    old_types, new_types = _types(old_doc, old), _types(new_doc, new)
    if old_types - {"null"} != new_types - {"null"}:
        problems.append(f"{where}: type changed from {sorted(old_types)} to {sorted(new_types)}")
        return
    _compare_values(old_doc, new_doc, old, new, where, request, problems)
    if not request and "null" in new_types and "null" not in old_types:
        problems.append(f"{where}: response field became nullable")
    if request and "null" in old_types and "null" not in new_types:
        problems.append(f"{where}: request field no longer accepts null")
    old_objects = [m for m in _members(old_doc, old) if "properties" in m]
    new_objects = [m for m in _members(new_doc, new) if "properties" in m]
    if old_objects:
        old_object = old_objects[0]
        new_object = new_objects[0] if new_objects else {"properties": {}}
        old_required = set(old_object.get("required", ()))
        new_required = set(new_object.get("required", ()))
        for name, old_field in old_object["properties"].items():
            if name not in new_object["properties"]:
                problems.append(f"{where}.{name}: field removed")
                continue
            if not request and name in old_required and name not in new_required:
                problems.append(f"{where}.{name}: response field is no longer always present")
            _compare_schema(old_doc, new_doc, old_field, new_object["properties"][name],
                            f"{where}.{name}", request, problems)
        if request:
            for name in sorted(new_required - old_required):
                problems.append(f"{where}.{name}: request field is newly required")
    for key in ("items", "additionalProperties"):
        old_members = [m for m in _members(old_doc, old) if isinstance(m.get(key), dict)]
        new_members = [m for m in _members(new_doc, new) if isinstance(m.get(key), dict)]
        if old_members and new_members:
            _compare_schema(old_doc, new_doc, old_members[0][key], new_members[0][key],
                            f"{where}[{key}]", request, problems)


def _json_schema(body):
    return ((body or {}).get("content", {}).get("application/json", {}) or {}).get("schema")


def breaking_changes(old: dict, new: dict) -> list[str]:
    """Incompatible differences between two filtered API documents."""
    problems: list[str] = []
    for path, old_item in old["paths"].items():
        for method in METHODS:
            if method not in old_item:
                continue
            where = f"{method.upper()} {path}"
            new_operation = new["paths"].get(path, {}).get(method)
            if new_operation is None:
                problems.append(f"{where}: operation removed")
                continue
            old_operation = old_item[method]
            old_parameters = {(p["in"], p["name"]): p for p in old_operation.get("parameters", ())}
            new_parameters = {(p["in"], p["name"]): p for p in new_operation.get("parameters", ())}
            for parameter in new_operation.get("parameters", ()):
                key = (parameter["in"], parameter["name"])
                if parameter.get("required") and not old_parameters.get(key, {}).get("required"):
                    problems.append(f"{where}: parameter {parameter['name']} is newly required")
            for key, parameter in old_parameters.items():
                if key not in new_parameters:
                    problems.append(f"{where}: parameter {parameter['name']} removed")
                elif "schema" in parameter:
                    _compare_schema(old, new, parameter["schema"], new_parameters[key].get("schema", {}),
                                    f"{where} parameter {parameter['name']}", True, problems)
            old_body, new_body = old_operation.get("requestBody"), new_operation.get("requestBody")
            if new_body and new_body.get("required") and not (old_body and old_body.get("required")):
                problems.append(f"{where}: request body is newly required")
            if _json_schema(old_body) is not None:
                if _json_schema(new_body) is None:
                    problems.append(f"{where}: request body removed")
                else:
                    _compare_schema(old, new, _json_schema(old_body), _json_schema(new_body),
                                    f"{where} request", True, problems)
            for status, old_response in old_operation.get("responses", {}).items():
                new_response = new_operation.get("responses", {}).get(status)
                if new_response is None:
                    problems.append(f"{where}: response {status} removed")
                    continue
                for media_type in old_response.get("content", {}):
                    if media_type not in new_response.get("content", {}):
                        problems.append(f"{where}: response {status} media type {media_type} removed")
                if _json_schema(old_response) is not None:
                    if _json_schema(new_response) is None:
                        problems.append(f"{where}: response {status} body removed")
                    else:
                        _compare_schema(old, new, _json_schema(old_response), _json_schema(new_response),
                                        f"{where} {status}", False, problems)
    return problems


def _snapshot_text(document: dict) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def test_api_schema_matches_the_checked_in_contract():
    generated = api_openapi_document()
    assert set(generated["paths"]) == {
        "/api/v1/auth/revoke", "/api/v1/auth/token", "/api/v1/capabilities", "/api/v1/me",
        "/api/v1/trips", "/api/v1/trips/{trip_id}", "/api/v1/review", "/api/v1/vehicles",
        "/api/v1/purposes/recent", "/api/v1/dashboard/week", "/api/v1/stats", "/api/v1/expenses",
        "/api/v1/expenses/{expense_id}", "/api/v1/expenses/comparison", "/api/v1/exports/trips",
        "/api/v1/reports/range", "/api/v1/reports/{year}",
    }
    if os.environ.get("ODOGRAPH_UPDATE_API_SCHEMA") == "1" and SNAPSHOT.exists():
        problems = breaking_changes(json.loads(SNAPSHOT.read_text()), generated)
        assert not problems, "\n".join(problems)
        SNAPSHOT.write_text(_snapshot_text(generated))
    checked_in = json.loads(SNAPSHOT.read_text())
    problems = breaking_changes(checked_in, generated)
    assert not problems, "API v1 changed incompatibly:\n" + "\n".join(problems)
    assert _snapshot_text(generated) == SNAPSHOT.read_text(), (
        "API v1 changed compatibly; regenerate tests/fixtures/api_v1/openapi.json "
        "(see this module's docstring)")


def _document():
    return json.loads(SNAPSHOT.read_text())


def _schema(document, name):
    return document["components"]["schemas"][name]


def test_comparator_accepts_added_optional_fields():
    old = _document()
    new = copy.deepcopy(old)
    _schema(new, "TokenRequest")["properties"]["new_optional"] = {
        "anyOf": [{"type": "string"}, {"type": "null"}], "title": "New Optional"}
    _schema(new, "MeResponse")["properties"]["new_field"] = {"type": "string"}
    _schema(new, "MeResponse")["required"].append("new_field")
    new["paths"]["/api/v1/me"]["get"]["responses"]["404"] = {"description": "Not Found"}
    assert breaking_changes(old, new) == []
    assert breaking_changes(old, old) == []


@pytest.mark.parametrize("mutate,expected", [
    (lambda d: _schema(d, "MeResponse")["properties"].pop("grant_id"),
     "GET /api/v1/me 200.grant_id: field removed"),
    (lambda d: _schema(d, "MeResponse")["properties"].update(account_id={"type": "string"}),
     "GET /api/v1/me 200.account_id: type changed"),
    (lambda d: _schema(d, "MeResponse")["properties"].update(
        server_version={"anyOf": [{"type": "string"}, {"type": "null"}]}),
     "GET /api/v1/me 200.server_version: response field became nullable"),
    (lambda d: _schema(d, "MeResponse")["required"].remove("display_name"),
     "GET /api/v1/me 200.display_name: response field is no longer always present"),
    (lambda d: _schema(d, "TokenRequest")["required"].append("refresh_token"),
     "POST /api/v1/auth/token request.refresh_token: request field is newly required"),
    (lambda d: _schema(d, "ApiErrorBody")["properties"].pop("fields"),
     "POST /api/v1/auth/token 400.error.fields: field removed"),
    (lambda d: _schema(d, "CapabilitiesResponse")["properties"].update(
        api_versions={"type": "array", "items": {"type": "integer"}}),
     "GET /api/v1/capabilities 200.api_versions[items]: type changed"),
    (lambda d: d["paths"].pop("/api/v1/capabilities"),
     "GET /api/v1/capabilities: operation removed"),
])
def test_comparator_rejects_incompatible_changes(mutate, expected):
    old = _document()
    new = copy.deepcopy(old)
    mutate(new)
    problems = breaking_changes(old, new)
    assert any(problem.startswith(expected) for problem in problems), problems


def _enumerate(schema, values):
    schema.pop("const")
    schema["enum"] = values


def _parameter(document, path, name):
    return next(p for p in document["paths"][path]["get"]["parameters"] if p["name"] == name)


@pytest.mark.parametrize("mutate,expected", [
    (lambda d: _parameter(d, "/api/v1/exports/trips", "format")["schema"]["enum"].remove("csv"),
     "GET /api/v1/exports/trips parameter format: request enum value csv removed"),
    (lambda d: _parameter(d, "/api/v1/trips", "exclusion")["schema"]["anyOf"][0]["enum"].remove("none"),
     "GET /api/v1/trips parameter exclusion: request enum value none removed"),
    (lambda d: _schema(d, "ExpenseCreate")["properties"]["treatment"]["enum"].remove("fully_business"),
     "POST /api/v1/expenses request.treatment: request enum value fully_business removed"),
    (lambda d: _parameter(d, "/api/v1/reports/{year}", "format")["schema"].update(const="csv"),
     "GET /api/v1/reports/{year} parameter format: request enum value xlsx removed"),
    (lambda d: _parameter(d, "/api/v1/exports/trips", "q")["schema"]["anyOf"][0].update(enum=["a"]),
     "GET /api/v1/exports/trips parameter q: request field is newly restricted"),
    (lambda d: _schema(d, "TripCreate")["properties"]["started_at"].update(format="date"),
     "POST /api/v1/trips request.started_at: format changed"),
    (lambda d: _schema(d, "TripPatch")["properties"]["ended_at"].pop("format"),
     "PATCH /api/v1/trips/{trip_id} request.ended_at: format changed"),
    (lambda d: _schema(d, "TripPatch")["properties"]["purpose"]["anyOf"][0].update(format="uuid"),
     "PATCH /api/v1/trips/{trip_id} request.purpose: request field newly requires format"),
    (lambda d: d["paths"]["/api/v1/exports/trips"]["get"]["parameters"].pop(),
     "GET /api/v1/exports/trips: parameter exclusion removed"),
    (lambda d: d["paths"]["/api/v1/exports/trips"]["get"]["responses"]["200"]["content"].pop(
        "text/csv; charset=utf-8"),
     "GET /api/v1/exports/trips: response 200 media type text/csv; charset=utf-8 removed"),
    (lambda d: _schema(d, "TripCreate")["required"].append("purpose"),
     "POST /api/v1/trips request.purpose: request field is newly required"),
    (lambda d: _schema(d, "TripSummary")["properties"].update(category={"anyOf": [{"type": "string"}, {"type": "null"}]}),
     "GET /api/v1/trips 200.items[items].category: response field became nullable"),
])
def test_comparator_applies_the_freeze_rules(mutate, expected):
    old = _document()
    new = copy.deepcopy(old)
    mutate(new)
    problems = breaking_changes(old, new)
    assert any(problem.startswith(expected) for problem in problems), problems


def _year(document):
    return _parameter(document, "/api/v1/reports/{year}", "year")["schema"]


def _with_labels(document):
    """The snapshot with an array request field, which v1 does not have yet."""
    _schema(document, "TripPatch")["properties"]["labels"] = {"type": "array", "items": {"type": "string"}}
    return document


def _labels(document):
    return _schema(document, "TripPatch")["properties"]["labels"]


def _purpose(document):
    return _schema(document, "TripPatch")["properties"]["purpose"]["anyOf"][0]


@pytest.mark.parametrize("mutate,expected", [
    (lambda d: _year(d).update(minimum=1950), "request minimum tightened from 1900 to 1950"),
    (lambda d: _year(d).update(maximum=2100), "request maximum tightened from 9998 to 2100"),
    (lambda d: _year(d).update(exclusiveMinimum=1900), "request exclusiveMinimum tightened from None to 1900"),
    (lambda d: _year(d).update(exclusiveMaximum=9999), "request exclusiveMaximum tightened from None to 9999"),
    (lambda d: _purpose(d).update(maxLength=10), "request maxLength tightened from None to 10"),
    (lambda d: _purpose(d).update(minLength=1), "request minLength tightened from None to 1"),
    (lambda d: _labels(d).update(minItems=1), "request minItems tightened from None to 1"),
    (lambda d: _labels(d).update(maxItems=5), "request maxItems tightened from None to 5"),
    (lambda d: _purpose(d).update(pattern="^[a-z]+$"), "request pattern changed from [] to ['^[a-z]+$']"),
])
def test_comparator_rejects_tightened_request_bounds(mutate, expected):
    old = _with_labels(_document())
    new = copy.deepcopy(old)
    mutate(new)
    problems = breaking_changes(old, new)
    assert any(expected in problem for problem in problems), problems


@pytest.mark.parametrize("tighten", [
    lambda d: _year(d).update(minimum=1950),
    lambda d: _year(d).update(maximum=2100),
    lambda d: _purpose(d).update(maxLength=10, minLength=2, pattern="^[a-z]+$"),
    lambda d: _labels(d).update(minItems=2, maxItems=5),
])
def test_comparator_accepts_loosened_request_bounds(tighten):
    # The reverse of each tightening: the old document is the strict one.
    strict = _with_labels(_document())
    tighten(strict)
    loose = _with_labels(_document())
    assert breaking_changes(strict, loose) == []
    assert breaking_changes(strict, copy.deepcopy(strict)) == []


def test_comparator_accepts_a_pattern_dropped_or_a_bound_change_on_responses():
    old = _document()
    _schema(old, "MeResponse")["properties"]["display_time_zone"].update(maxLength=64, pattern="^.+$")
    new = copy.deepcopy(old)
    _schema(new, "MeResponse")["properties"]["display_time_zone"].update(maxLength=8, pattern="^x$")
    assert breaking_changes(old, new) == []


@pytest.mark.parametrize("mutate", [
    # Added request values and added response values are additive.
    lambda d: _parameter(d, "/api/v1/exports/trips", "format")["schema"]["enum"].append("pdf"),
    lambda d: _enumerate(_parameter(d, "/api/v1/reports/range", "format")["schema"], ["xlsx", "csv"]),
    lambda d: _schema(d, "TripCreate")["properties"]["category"]["enum"].append("medical"),
    lambda d: _schema(d, "MultiLineString")["properties"]["type"].update(enum=["MultiLineString", "LineString"]),
    lambda d: _schema(d, "TripSummary")["properties"]["source"].update(enum=["detected", "manual"]),
    lambda d: d["paths"]["/api/v1/exports/trips"]["get"]["responses"]["200"]["content"].update(
        {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}),
    # A newly declared response format only narrows what clients receive.
    lambda d: _schema(d, "TripSummary")["properties"]["started_at"].update(format="date-time"),
])
def test_comparator_accepts_additive_value_changes(mutate):
    old = _document()
    new = copy.deepcopy(old)
    mutate(new)
    assert breaking_changes(old, new) == []


@pytest.mark.parametrize("filename,model", [
    ("token_request.json", TokenRequest),
    ("token_request_authorization_code.json", TokenRequest),
    ("error_grant_limit_reached.json", ApiErrorEnvelope),
    ("error_signin_disabled.json", ApiErrorEnvelope),
    ("token_response.json", TokenResponse),
    ("capabilities_response.json", CapabilitiesResponse),
    ("me_response.json", MeResponse),
    ("error_invalid_token.json", ApiErrorEnvelope),
    ("error_validation_failed.json", ApiErrorEnvelope),
    ("error_invalid_cursor.json", ApiErrorEnvelope),
    ("trips_page_response.json", TripPage),
    ("trip_detail_gapped.json", TripDetailResponse),
    ("trip_detail_snapped_only.json", TripDetailResponse),
    ("trip_detail_raw_only.json", TripDetailResponse),
    ("trip_detail_geometry_free.json", TripDetailResponse),
    ("trip_detail_imported.json", TripDetailResponse),
    ("trip_detail_manual.json", TripDetailResponse),
    ("trip_detail_routed_manual.json", TripDetailResponse),
    ("review_response.json", ReviewResponse),
    ("vehicles_response.json", VehicleList),
    ("purposes_response.json", PurposeList),
    ("trip_patch_request.json", TripPatch),
    ("trip_create_request.json", TripCreate),
    ("trip_create_response.json", TripDetailResponse),
    ("error_idempotency_key_reused.json", ApiErrorEnvelope),
    ("error_storage_full.json", ApiErrorEnvelope),
    ("error_trip_in_progress.json", ApiErrorEnvelope),
    ("dashboard_week_response.json", WeekDashboardResponse),
    ("stats_response.json", StatsResponse),
    ("expenses_page_response.json", ExpensePage),
    ("expense_comparison_response.json", ExpenseComparison),
    ("expense_create_request.json", ExpenseCreate),
    ("expense_patch_request.json", ExpensePatch),
    ("error_busy.json", ApiErrorEnvelope),
    ("error_export_format_invalid.json", ApiErrorEnvelope),
    ("error_report_range_reversed.json", ApiErrorEnvelope),
])
def test_synthetic_fixtures_match_their_models(filename, model):
    data = json.loads((FIXTURES / filename).read_text())
    parsed = model.model_validate(data)
    if "_request" not in filename:
        # Every documented response field is present, null rather than omitted.
        assert parsed.model_dump(mode="json") == data


@pytest.mark.parametrize("filename,request_model,record_model", [
    ("trip_patch_cases.json", TripPatch, TripDetailResponse),
    ("trip_create_cases.json", TripCreate, TripDetailResponse),
    ("expense_patch_cases.json", ExpensePatch, Expense),
    ("expense_create_cases.json", ExpenseCreate, Expense),
])
def test_write_case_fixtures_match_their_models(filename, request_model, record_model):
    for case in json.loads((FIXTURES / filename).read_text())["cases"]:
        response_model = record_model if case["status"] < 400 else ApiErrorEnvelope
        assert response_model.model_validate(case["response"]).model_dump(mode="json") == case["response"]
        if case["status"] < 400:
            request_model.model_validate(case["request"])


def test_every_error_code_has_a_fixture_with_its_registry_message():
    codes = set()
    for path in sorted(FIXTURES.glob("error_*.json")):
        data = json.loads(path.read_text())
        assert ApiErrorEnvelope.model_validate(data).model_dump(mode="json") == data, path.name
        error = data["error"]
        assert error["message"] == ERRORS[error["code"]][1], path.name
        codes.add(error["code"])
    assert codes == set(ERRORS)
    assert all((FIXTURES / f"error_{code}.json").exists() for code in ERRORS)
