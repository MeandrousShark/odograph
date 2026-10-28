"""Suite cost model and the CI shard planner.

Costs are integer centiseconds so sums and tie-breaks reproduce exactly in
every job that plans from its own collection. Stdlib only: the planner runs
in a collect-only job and inside every shard's collection.

To grow CI capacity, change BUDGET or SHARD_CAP here. SHARD_CAP is also
written in the `test` job of .github/workflows/test.yml, and a test keeps the
two equal.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from math import ceil

# Calibrated from the slower v1.3.0 serial run (preflight `quality`): 1,351 db
# and 4,033 other cases, suite 1,282 s; these constants estimate 1,281 s.
# Recheck them when a release run's measured time differs from the estimate by
# more than 20 percent.
DB_CENTISECONDS = 65
OTHER_CENTISECONDS = 10

# Model cost each CI shard is planned to hold, and the most shards one run
# may queue.
BUDGET = 22500
SHARD_CAP = 8


class CapacityError(Exception):
    """The suite needs more shards than SHARD_CAP allows."""


def case_cost(is_db: bool) -> int:
    """Model cost of one case in centiseconds."""
    return DB_CENTISECONDS if is_db else OTHER_CENTISECONDS


def file_of(nodeid: str) -> str:
    """The test file path a node id belongs to."""
    return nodeid.split("::", 1)[0]


def file_weights(cases: Iterable[tuple[str, bool]]) -> dict[str, int]:
    """Model cost per test file from (node id, is db) pairs."""
    weights: dict[str, int] = {}
    for nodeid, is_db in cases:
        path = file_of(nodeid)
        weights[path] = weights.get(path, 0) + case_cost(is_db)
    return weights


def collection_digest(nodeids: Iterable[str]) -> str:
    """sha256 of the node ids, sorted so collection order never changes it."""
    return hashlib.sha256("\n".join(sorted(nodeids)).encode()).hexdigest()


def shard_count(weights: dict[str, int]) -> int:
    """Shards needed to keep each near BUDGET, never more than there are files."""
    if not weights:
        raise ValueError("no test files to plan")
    total = sum(weights.values())
    # Whole files cannot split, so more shards than files cannot help.
    needed = min(max(1, ceil(total / BUDGET)), len(weights))
    if needed > SHARD_CAP:
        raise CapacityError(
            f"the suite is estimated at {total / 100:.0f} model-seconds "
            f"({total / 6000:.1f} minutes), which needs {needed} shards of "
            f"{BUDGET / 100:.0f} model-seconds, but SHARD_CAP allows {SHARD_CAP}. "
            "If the CI account has room, raise BUDGET or SHARD_CAP in "
            "tests/suite_cost.py, and keep SHARD_CAP in the test job of "
            ".github/workflows/test.yml equal to it; otherwise make the suite cheaper.")
    return needed


def assign(weights: dict[str, int], count: int) -> list[list[str]]:
    """Split whole files into `count` shards, each sorted by path.

    Heaviest file first onto the lightest shard, ties broken by path and then
    shard index, so every job computes the same partition.
    """
    if not 1 <= count <= len(weights):
        raise ValueError(f"cannot split {len(weights)} test files into {count} shards")
    loads = [0] * count
    shards: list[list[str]] = [[] for _ in range(count)]
    for path in sorted(weights, key=lambda path: (-weights[path], path)):
        index = loads.index(min(loads))
        loads[index] += weights[path]
        shards[index].append(path)
    return [sorted(shard) for shard in shards]


def parse_spec(value: str) -> tuple[int, int]:
    """Parse K/N with 1 <= K <= N; raise ValueError otherwise."""
    index, _, count = value.partition("/")
    if not (index.isdecimal() and count.isdecimal() and index.isascii() and count.isascii()
            and 1 <= int(index) <= int(count)):
        raise ValueError(f"expected K/N with 1 <= K <= N, got {value!r}")
    return int(index), int(count)
