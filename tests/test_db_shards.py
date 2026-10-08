"""tests/conftest.py's db shard split, which the CI test matrix relies on."""
import pytest

from conftest import db_shard_files, db_shard_from_env

WEIGHTS = {
    "tests/test_a_db.py": 9,
    "tests/test_b_db.py": 7,
    "tests/test_c_db.py": 4,
    "tests/test_d_db.py": 4,
    "tests/test_e_db.py": 1,
}


@pytest.mark.parametrize("count", [1, 2, 3, 4, 7])
def test_shards_partition_every_file_exactly_once(count):
    shards = db_shard_files(WEIGHTS, count)

    assert len(shards) == count
    assert sorted(path for shard in shards for path in shard) == sorted(WEIGHTS)


def test_split_does_not_depend_on_collection_order():
    reordered = dict(reversed(list(WEIGHTS.items())))

    assert db_shard_files(reordered, 3) == db_shard_files(WEIGHTS, 3)


def test_heaviest_files_are_spread_first():
    loads = sorted(sum(WEIGHTS[path] for path in shard) for shard in db_shard_files(WEIGHTS, 3))

    assert loads == [8, 8, 9]


@pytest.mark.parametrize("value, expected", [("", None), ("1/1", (1, 1)), ("2/3", (2, 3))])
def test_shard_env_accepts_k_of_n(monkeypatch, value, expected):
    monkeypatch.setenv("ODOGRAPH_DB_SHARD", value)

    assert db_shard_from_env() == expected


def test_unset_shard_env_selects_everything(monkeypatch):
    monkeypatch.delenv("ODOGRAPH_DB_SHARD", raising=False)

    assert db_shard_from_env() is None


@pytest.mark.parametrize("value", ["0/3", "4/3", "1/0", "3", "1/3/5", "a/b", "-1/3", " 1/3", "\u00b2/3"])
def test_shard_env_refuses_malformed_values(monkeypatch, value):
    monkeypatch.setenv("ODOGRAPH_DB_SHARD", value)

    with pytest.raises(pytest.exit.Exception, match="ODOGRAPH_DB_SHARD"):
        db_shard_from_env()
