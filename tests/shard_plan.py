"""pytest plugin: plan the CI shards from one collect-only run.

Loaded with ``-p shard_plan`` (``tests`` on PYTHONPATH) together with
``--collect-only``. After the full collection it writes the plan from
tests/suite_cost.py as JSON to the file named by ODOGRAPH_SHARD_PLAN_OUTPUT:
the GitHub Actions matrix, the collection digest every shard checks, the
estimate, the shard count and each shard's files. A suite past the shard cap
stops pytest with the capacity message instead.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import suite_cost

OUTPUT_ENV = "ODOGRAPH_SHARD_PLAN_OUTPUT"
_SELECTION_ENVS = ("ODOGRAPH_SHARD", "ODOGRAPH_SHARD_DIGEST")
_deselected = 0


def pytest_deselected(items) -> None:
    global _deselected
    _deselected += len(items)


def build_plan(items) -> dict:
    """The shard plan for collected items, as the plan job publishes it."""
    cases = [(item.nodeid, item.get_closest_marker("db") is not None) for item in items]
    weights = suite_cost.file_weights(cases)
    count = suite_cost.shard_count(weights)
    files = suite_cost.assign(weights, count)
    case_counts: dict[str, int] = {}
    for nodeid, _ in cases:
        path = suite_cost.file_of(nodeid)
        case_counts[path] = case_counts.get(path, 0) + 1
    shards = [{
        "spec": f"{index}/{count}",
        "files": shard,
        "cases": sum(case_counts[path] for path in shard),
        "model_seconds": sum(weights[path] for path in shard) / 100,
    } for index, shard in enumerate(files, 1)]
    return {
        "matrix": {"include": [{"shard": f"shard-{index}", "spec": shard["spec"]}
                               for index, shard in enumerate(shards, 1)]},
        "digest": suite_cost.collection_digest(nodeid for nodeid, _ in cases),
        "count": count,
        "cases": len(cases),
        "estimate_seconds": sum(weights.values()) / 100,
        "budget_seconds": suite_cost.BUDGET / 100,
        "cap": suite_cost.SHARD_CAP,
        "shards": shards,
    }


@pytest.hookimpl(tryfirst=True)
def pytest_collection_finish(session) -> None:
    config = session.config
    if any(os.environ.get(name) for name in _SELECTION_ENVS) or config.option.markexpr \
            or config.option.keyword or _deselected:
        pytest.exit("the shard plan needs the full collection: run it without "
                    "ODOGRAPH_SHARD, ODOGRAPH_SHARD_DIGEST, -m or -k",
                    returncode=pytest.ExitCode.USAGE_ERROR)
    try:
        plan = build_plan(session.items)
    except (suite_cost.CapacityError, ValueError) as error:
        pytest.exit(f"shard plan: {error}", returncode=pytest.ExitCode.USAGE_ERROR)
    Path(os.environ[OUTPUT_ENV]).write_text(json.dumps(plan, indent=1) + "\n")
