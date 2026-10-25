"""API v1 schema contract.

The OpenAPI document is generated from the application without serving it
(docs routes stay off), limited to `/api/v1` paths and the component schemas
they reference, and compared with the checked-in copy in
`tests/fixtures/api_v1/openapi.json`. v1 changes are additive only: the check
fails on a removed field, a type change, a newly required request field or a
response field that becomes nullable or optional. After an additive change,
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

from app.api_errors import ApiErrorEnvelope
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
    return {"paths": paths, "components": {"schemas": {name: schemas[name] for name in sorted(wanted)}}}


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


def _compare_schema(old_doc, new_doc, old, new, where, request, problems):
    old_types, new_types = _types(old_doc, old), _types(new_doc, new)
    if old_types - {"null"} != new_types - {"null"}:
        problems.append(f"{where}: type changed from {sorted(old_types)} to {sorted(new_types)}")
        return
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
            for parameter in new_operation.get("parameters", ()):
                key = (parameter["in"], parameter["name"])
                if parameter.get("required") and not old_parameters.get(key, {}).get("required"):
                    problems.append(f"{where}: parameter {parameter['name']} is newly required")
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
    assert set(generated["paths"]) == {"/api/v1/auth/token", "/api/v1/capabilities", "/api/v1/me"}
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
    _schema(new, "TokenRequest")["properties"]["code"] = {
        "anyOf": [{"type": "string"}, {"type": "null"}], "title": "Code"}
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


@pytest.mark.parametrize("filename,model", [
    ("token_request.json", TokenRequest),
    ("token_response.json", TokenResponse),
    ("capabilities_response.json", CapabilitiesResponse),
    ("me_response.json", MeResponse),
    ("error_invalid_token.json", ApiErrorEnvelope),
    ("error_validation_failed.json", ApiErrorEnvelope),
])
def test_synthetic_fixtures_match_their_models(filename, model):
    data = json.loads((FIXTURES / filename).read_text())
    parsed = model.model_validate(data)
    if filename != "token_request.json":
        # Every documented response field is present, null rather than omitted.
        assert parsed.model_dump(mode="json") == data
