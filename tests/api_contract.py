"""Behavioral contract comparison for API v1 responses.

A response matches its fixture when it has exactly the fixture's fields,
`null` exactly where the fixture has `null`, the same types, and the same
values, except for values that identify a row or a moment: ids, opaque
cursors and tokens are compared by type only, and instants by their RFC
3339 shape only. Numbers compare to within a millionth, so a distance sent
in another unit fails. Every mismatch is returned as `path: reason`.
"""
from __future__ import annotations

import re

INSTANT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")
OPAQUE_KEYS = frozenset({"next_cursor", "access_token", "refresh_token", "grant_id"})


def _identifies(key: str | None) -> bool:
    return key is not None and (key == "id" or key.endswith("_id") or key in OPAQUE_KEYS)


def _kind(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def contract_mismatches(expected, actual, path: str = "$", key: str | None = None,
                        ignore: frozenset[str] = frozenset()) -> list[str]:
    expected_kind, actual_kind = _kind(expected), _kind(actual)
    if expected_kind != actual_kind:
        if "null" in (expected_kind, actual_kind):
            return [f"{path}: expected {'null' if expected is None else 'a value'}, got "
                    f"{'null' if actual is None else 'a value'}"]
        return [f"{path}: expected {expected_kind}, got {actual_kind}"]
    if expected_kind == "object":
        problems = [f"{path}.{name}: field missing" for name in expected if name not in actual]
        problems += [f"{path}.{name}: field not in the fixture" for name in actual if name not in expected]
        for name in expected:
            if name in actual:
                problems += contract_mismatches(expected[name], actual[name], f"{path}.{name}", name, ignore)
        return problems
    if expected_kind == "array":
        if len(expected) != len(actual):
            return [f"{path}: expected {len(expected)} items, got {len(actual)}"]
        return [problem for index, (want, got) in enumerate(zip(expected, actual))
                for problem in contract_mismatches(want, got, f"{path}[{index}]", key, ignore)]
    if expected is None or _identifies(key) or key in ignore:
        return []
    if expected_kind == "string" and INSTANT.fullmatch(expected):
        return [] if INSTANT.fullmatch(actual) else [f"{path}: expected an RFC 3339 UTC instant, got {actual!r}"]
    if expected_kind == "number":
        if abs(expected - actual) <= 1e-6 * max(1.0, abs(expected)):
            return []
        return [f"{path}: expected {expected!r}, got {actual!r}"]
    return [] if expected == actual else [f"{path}: expected {expected!r}, got {actual!r}"]
