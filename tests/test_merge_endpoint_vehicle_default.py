"""Confirms the merge endpoints' `vehicle_id` form field defaults to "keep"
at the wire level.

`tests/test_ui_merge_db.py` calls the merge endpoint functions directly,
bypassing FastAPI's own request parsing -- so it can exercise explicit
"keep"/""/digit values but can't exercise a genuinely *missing* form field
(a `Form(...)` default is only resolved into a plain value by FastAPI's
dependency injection, not by a bare Python call). This test instead reads
the declared default straight off the route, the same value FastAPI
substitutes when a real client (including any cached/old client that
predates this field) posts a merge request without `vehicle_id` at all.
"""
from __future__ import annotations

import inspect

from app.ui import make_router

MERGE_PATHS = {
    "/trips/{trip_id}/merge_next",
    "/trips/{trip_id}/merge_prev",
    "/trips/merge_selected",
}


def test_merge_endpoints_default_vehicle_id_to_keep():
    router = make_router()
    seen = set()
    for route in router.routes:
        if route.path not in MERGE_PATHS:
            continue
        seen.add(route.path)
        param = inspect.signature(route.endpoint).parameters["vehicle_id"]
        assert param.default.default == "keep"
    assert seen == MERGE_PATHS
