"""The CI shard planner in tests/suite_cost.py and conftest's shard selection."""
from pathlib import Path
from types import SimpleNamespace

import pytest

import suite_cost
from conftest import pytest_collection_modifyitems, shard_from_env
from suite_cost import BUDGET, SHARD_CAP, CapacityError, assign, file_weights, shard_count

WEIGHTS = {
    "tests/test_a_db.py": 9,
    "tests/test_b_db.py": 7,
    "tests/test_c_db.py": 4,
    "tests/test_d_db.py": 4,
    "tests/test_e_db.py": 1,
}


def test_costs_match_the_calibrated_model():
    assert (suite_cost.DB_CENTISECONDS, suite_cost.OTHER_CENTISECONDS) == (65, 10)
    assert (BUDGET, SHARD_CAP) == (22500, 8)


def test_file_weights_sum_case_costs_per_file():
    cases = [("tests/test_a_db.py::test_x", True), ("tests/test_a_db.py::test_y[1]", True),
             ("tests/test_b.py::test_z", False), ("tests/test_b.py::C::test_w", False)]

    assert file_weights(cases) == {"tests/test_a_db.py": 130, "tests/test_b.py": 20}


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5])
def test_shards_partition_every_file_exactly_once(count):
    shards = assign(WEIGHTS, count)

    assert len(shards) == count and all(shards)
    assert sorted(path for shard in shards for path in shard) == sorted(WEIGHTS)


def test_split_does_not_depend_on_collection_order():
    reordered = dict(reversed(list(WEIGHTS.items())))

    assert assign(reordered, 3) == assign(WEIGHTS, 3)


def test_heaviest_files_are_spread_first():
    loads = sorted(sum(WEIGHTS[path] for path in shard) for shard in assign(WEIGHTS, 3))

    assert loads == [8, 8, 9]


@pytest.mark.parametrize("count", [0, 6])
def test_assign_refuses_a_count_that_would_leave_a_shard_empty(count):
    with pytest.raises(ValueError, match="5 test files"):
        assign(WEIGHTS, count)


def _synthetic(total: int, files: int = 40) -> dict[str, int]:
    share, extra = divmod(total, files)
    return {f"tests/test_{index:02}.py": share + (index < extra) for index in range(files)}


def test_todays_suite_size_plans_six_shards():
    assert shard_count(_synthetic(1201 * 70 + 3413 * 11)) == 6


@pytest.mark.parametrize("budgets", range(1, SHARD_CAP + 1))
def test_a_larger_suite_gets_more_shards_up_to_the_cap(budgets):
    assert shard_count(_synthetic(budgets * BUDGET)) == budgets
    if budgets < SHARD_CAP:
        assert shard_count(_synthetic(budgets * BUDGET + 1)) == budgets + 1


def test_shard_count_grows_monotonically_to_the_cap():
    totals = range(BUDGET // 2, SHARD_CAP * BUDGET + 1, BUDGET // 4)
    counts = [shard_count(_synthetic(total)) for total in totals]

    assert counts == sorted(counts)
    assert counts[0] == 1 and counts[-1] == SHARD_CAP
    for total, count in zip(totals, counts):
        assert all(assign(_synthetic(total), count))


def test_one_budget_past_the_cap_fails_naming_the_estimate():
    weights = _synthetic(SHARD_CAP * BUDGET + BUDGET)

    with pytest.raises(CapacityError) as raised:
        shard_count(weights)

    message = str(raised.value)
    assert f"{(SHARD_CAP + 1) * BUDGET // 100} model-seconds" in message
    assert f"({(SHARD_CAP + 1) * BUDGET / 6000:.1f} minutes)" in message
    assert f"needs {SHARD_CAP + 1} shards of {BUDGET // 100} model-seconds" in message
    assert f"SHARD_CAP allows {SHARD_CAP}" in message
    assert "SHARD_CAP in tests/suite_cost.py" in message
    assert "SHARD_CAP in the test job of .github/workflows/test.yml" in message


def test_just_past_the_cap_already_fails():
    with pytest.raises(CapacityError):
        shard_count(_synthetic(SHARD_CAP * BUDGET + 1))


def test_shard_count_never_exceeds_the_file_count():
    weights = {"tests/test_big.py": 3 * BUDGET, "tests/test_small.py": 1}

    assert shard_count(weights) == 2
    assert [sorted(shard) for shard in assign(weights, 2)] == [["tests/test_big.py"], ["tests/test_small.py"]]


def test_one_huge_file_plans_one_shard_without_a_capacity_error():
    assert shard_count({"tests/test_huge_db.py": 10 * BUDGET}) == 1


def test_capacity_counts_only_achievable_shards():
    weights = _synthetic(20 * BUDGET, files=SHARD_CAP + 1)

    with pytest.raises(CapacityError, match=f"needs {SHARD_CAP + 1} shards"):
        shard_count(weights)


def test_empty_collection_is_refused():
    with pytest.raises(ValueError, match="no test files"):
        shard_count({})


@pytest.mark.parametrize("value, expected", [("", None), ("1/1", (1, 1)), ("2/3", (2, 3))])
def test_shard_env_accepts_k_of_n(monkeypatch, value, expected):
    monkeypatch.setenv("ODOGRAPH_SHARD", value)

    assert shard_from_env() == expected


def test_unset_shard_env_selects_everything(monkeypatch):
    monkeypatch.delenv("ODOGRAPH_SHARD", raising=False)

    assert shard_from_env() is None


@pytest.mark.parametrize("value", ["0/3", "4/3", "1/0", "3", "1/3/5", "a/b", "-1/3", " 1/3",
                                   "²/3", "١/3"])
def test_shard_env_refuses_malformed_values(monkeypatch, value):
    monkeypatch.setenv("ODOGRAPH_SHARD", value)

    with pytest.raises(pytest.exit.Exception, match="ODOGRAPH_SHARD"):
        shard_from_env()


class _FakeItem:
    def __init__(self, nodeid):
        self.nodeid = nodeid
        self.path = Path(nodeid.split("::", 1)[0])
        self.markers = []

    def iter_markers(self):
        return iter(self.markers)

    def add_marker(self, marker):
        self.markers.append(marker.mark)

    def get_closest_marker(self, name):
        return next((mark for mark in self.markers if mark.name == name), None)


FAKE_NODEIDS = [
    "tests/test_a_db.py::test_1", "tests/test_a_db.py::test_2", "tests/test_a_db.py::test_3",
    "tests/test_b_db.py::test_1", "tests/test_b_db.py::test_2",
    "tests/test_c.py::test_1", "tests/test_c.py::test_2", "tests/test_d.py::test_1",
]


def _select(monkeypatch, spec, digest=None):
    monkeypatch.setenv("ODOGRAPH_SHARD", spec)
    if digest is None:
        monkeypatch.delenv("ODOGRAPH_SHARD_DIGEST", raising=False)
    else:
        monkeypatch.setenv("ODOGRAPH_SHARD_DIGEST", digest)
    deselected = []
    config = SimpleNamespace(hook=SimpleNamespace(
        pytest_deselected=lambda items: deselected.extend(items)))
    items = [_FakeItem(nodeid) for nodeid in FAKE_NODEIDS]
    pytest_collection_modifyitems(config, items)
    return [item.nodeid for item in items], [item.nodeid for item in deselected]


def test_selection_covers_every_tier_and_follows_the_plan(monkeypatch):
    digest = suite_cost.collection_digest(FAKE_NODEIDS)
    weights = file_weights((nodeid, "_db.py" in nodeid) for nodeid in FAKE_NODEIDS)
    assert shard_count(weights) == 1

    selected, deselected = _select(monkeypatch, "1/1", digest)
    assert (selected, deselected) == (FAKE_NODEIDS, [])

    plan = assign(weights, 3)
    shards = [_select(monkeypatch, f"{index}/3")[0] for index in (1, 2, 3)]
    assert [sorted({suite_cost.file_of(nodeid) for nodeid in shard}) for shard in shards] == plan
    assert sorted(nodeid for shard in shards for nodeid in shard) == sorted(FAKE_NODEIDS)
    # The db files are heaviest, so each leads its own shard.
    assert plan[0] == ["tests/test_a_db.py"] and plan[1] == ["tests/test_b_db.py"]


def test_selection_without_a_digest_allows_any_count_up_to_the_files(monkeypatch):
    assert len(_select(monkeypatch, "4/4")[0]) >= 1

    with pytest.raises(pytest.exit.Exception, match="asks for 5 shards but only 4 test files"):
        _select(monkeypatch, "1/5")


def test_selection_stops_when_the_digest_differs(monkeypatch):
    digest = suite_cost.collection_digest(FAKE_NODEIDS)

    with pytest.raises(pytest.exit.Exception) as raised:
        _select(monkeypatch, "1/1", "0" * 64)

    assert "0" * 64 in str(raised.value) and digest in str(raised.value)


def test_selection_stops_when_the_shard_count_differs_from_the_plan(monkeypatch):
    digest = suite_cost.collection_digest(FAKE_NODEIDS)

    with pytest.raises(pytest.exit.Exception, match="asks for 2 shards but this collection plans 1"):
        _select(monkeypatch, "1/2", digest)


def test_selection_stops_on_capacity_when_planning_against_a_digest(monkeypatch):
    monkeypatch.setattr(suite_cost, "BUDGET", 1)
    monkeypatch.setattr(suite_cost, "SHARD_CAP", 2)
    digest = suite_cost.collection_digest(FAKE_NODEIDS)

    with pytest.raises(pytest.exit.Exception, match="needs 4 shards .* SHARD_CAP allows 2"):
        _select(monkeypatch, "1/2", digest)
