from __future__ import annotations

import functools
import inspect

from app.auth import require_csrf
from app.ui import make_router


@functools.cache
def _routes():
    return tuple(make_router().routes)


def _route():
    for route in _routes():
        if route.path == "/trips/batch_update":
            return route
    raise AssertionError("batch_update route missing")


def test_batch_endpoint_wire_defaults_and_csrf_dependency():
    route = _route()
    params = inspect.signature(route.endpoint).parameters

    assert params["category"].default.default == "keep"
    assert params["vehicle_id"].default.default == "keep"
    assert params["exclusion"].default.default == "keep"
    assert params["purpose"].default.default == ""
    assert params["set_purpose"].default.default is False
    assert any(dependency.dependency is require_csrf for dependency in route.dependencies)
