"""pytest plugin: replay CI selections over one collection.

Loaded with ``-p`` by tests/test_ci_workflow.py's collect-only subprocess.
Collection is the expensive part, so after one full collection this runs
the real ``pytest_collection_modifyitems`` hook chain (tier markers, the
ODOGRAPH_SHARD split and ``-m`` deselection) again for each requested
``-m`` expression and shard, and writes the selected node IDs as JSON.

When tests/shard_plan.py is loaded too, its plan is written first and every
shard in its matrix is replayed as well, with the plan's digest set as CI
sets it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

REQUEST_ENV = "ODOGRAPH_COLLECTION_SELECTIONS"
OUTPUT_ENV = "ODOGRAPH_COLLECTION_SELECTIONS_OUTPUT"
_PLAN_ENV = "ODOGRAPH_SHARD_PLAN_OUTPUT"
_SHARD_ENV = "ODOGRAPH_SHARD"
_DIGEST_ENV = "ODOGRAPH_SHARD_DIGEST"


@pytest.hookimpl(trylast=True)
def pytest_collection_finish(session) -> None:
    requested = [(marker, shard, "") for marker, shard in json.loads(os.environ[REQUEST_ENV])]
    plan_path = os.environ.get(_PLAN_ENV)
    if plan_path and session.config.pluginmanager.has_plugin("shard_plan"):
        plan = json.loads(Path(plan_path).read_text())
        requested += [("", row["spec"], plan["digest"]) for row in plan["matrix"]["include"]]
    config = session.config
    saved_markexpr = config.option.markexpr
    saved_env = {name: os.environ.get(name) for name in (_SHARD_ENV, _DIGEST_ENV)}
    results = []
    try:
        for marker, shard, digest in requested:
            items = list(session.items)
            config.option.markexpr = marker
            os.environ[_SHARD_ENV] = shard
            os.environ[_DIGEST_ENV] = digest
            config.hook.pytest_collection_modifyitems(session=session, config=config, items=items)
            results.append({"marker": marker, "shard": shard,
                            "nodeids": [item.nodeid for item in items]})
    finally:
        config.option.markexpr = saved_markexpr
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    Path(os.environ[OUTPUT_ENV]).write_text(json.dumps(results))
