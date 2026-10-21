"""pytest plugin: replay CI selections over one collection.

Loaded with ``-p`` by tests/test_ci_workflow.py's collect-only subprocess.
Collection is the expensive part, so after one full collection this runs
the real ``pytest_collection_modifyitems`` hook chain (tier markers, the
ODOGRAPH_DB_SHARD split and ``-m`` deselection) again for each requested
``-m`` expression and shard, and writes the selected node IDs as JSON.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

REQUEST_ENV = "ODOGRAPH_COLLECTION_SELECTIONS"
OUTPUT_ENV = "ODOGRAPH_COLLECTION_SELECTIONS_OUTPUT"
_SHARD_ENV = "ODOGRAPH_DB_SHARD"


def pytest_collection_finish(session) -> None:
    requested = json.loads(os.environ[REQUEST_ENV])
    config = session.config
    saved_markexpr = config.option.markexpr
    saved_shard = os.environ.get(_SHARD_ENV)
    results = []
    try:
        for marker, db_shard in requested:
            items = list(session.items)
            config.option.markexpr = marker
            os.environ[_SHARD_ENV] = db_shard
            config.hook.pytest_collection_modifyitems(session=session, config=config, items=items)
            results.append({"marker": marker, "db_shard": db_shard,
                            "nodeids": [item.nodeid for item in items]})
    finally:
        config.option.markexpr = saved_markexpr
        if saved_shard is None:
            os.environ.pop(_SHARD_ENV, None)
        else:
            os.environ[_SHARD_ENV] = saved_shard
    Path(os.environ[OUTPUT_ENV]).write_text(json.dumps(results))
