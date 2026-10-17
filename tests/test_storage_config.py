import pytest

from app.config import Config

STORAGE_KEYS = (
    "STORAGE_ACCOUNT_LIMIT_BYTES", "STORAGE_RAW_LIMIT_BYTES",
    "STORAGE_ENHANCEMENT_LIMIT_BYTES", "STORAGE_INSTANCE_BUDGET_BYTES",
    "STORAGE_INSTANCE_RESERVE_BYTES",
)


@pytest.fixture
def storage_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/unused")
    monkeypatch.setenv("SESSION_SECRET", "synthetic-storage-config-long-enough-for-config")
    for key in STORAGE_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.mark.parametrize("key", STORAGE_KEYS)
@pytest.mark.parametrize("value", ["0", "-1", "1.5", str(2 ** 63)])
def test_invalid_storage_size_refuses_configuration(storage_env, key, value):
    if key in ("STORAGE_RAW_LIMIT_BYTES", "STORAGE_ENHANCEMENT_LIMIT_BYTES"):
        storage_env.setenv("STORAGE_ACCOUNT_LIMIT_BYTES", str(2 * 1024 ** 3))
    storage_env.setenv(key, value)
    with pytest.raises(RuntimeError, match=key):
        Config.from_env()


@pytest.mark.parametrize("key,value", [
    ("STORAGE_RAW_LIMIT_BYTES", str(3 * 1024 ** 3)),
    ("STORAGE_ENHANCEMENT_LIMIT_BYTES", str(3 * 1024 ** 3)),
    ("STORAGE_INSTANCE_RESERVE_BYTES", str(12 * 1024 ** 3)),
])
def test_unfunded_subset_or_reserve_refuses_configuration(storage_env, key, value):
    storage_env.setenv("STORAGE_ACCOUNT_LIMIT_BYTES", str(2 * 1024 ** 3))
    storage_env.setenv(key, value)
    with pytest.raises(RuntimeError, match="storage"):
        Config.from_env()


@pytest.mark.parametrize("empty", [False, True])
def test_unset_account_limit_means_no_limits(storage_env, empty):
    if empty:
        storage_env.setenv("STORAGE_ACCOUNT_LIMIT_BYTES", "")
    cfg = Config.from_env()
    assert (cfg.storage_account_limit_bytes, cfg.storage_raw_limit_bytes,
            cfg.storage_enhancement_limit_bytes) == (None, None, None)
    assert (cfg.storage_instance_budget_bytes, cfg.storage_instance_reserve_bytes) == (
        12 * 1024 ** 3, 2 * 1024 ** 3)


def test_account_limit_keeps_default_sub_limits(storage_env):
    storage_env.setenv("STORAGE_ACCOUNT_LIMIT_BYTES", str(2 * 1024 ** 3))
    cfg = Config.from_env()
    assert (cfg.storage_account_limit_bytes, cfg.storage_raw_limit_bytes,
            cfg.storage_enhancement_limit_bytes) == (2 * 1024 ** 3, 256 * 1024 ** 2, 128 * 1024 ** 2)
    storage_env.setenv("STORAGE_RAW_LIMIT_BYTES", "1000")
    storage_env.setenv("STORAGE_ENHANCEMENT_LIMIT_BYTES", "")
    cfg = Config.from_env()
    assert (cfg.storage_raw_limit_bytes, cfg.storage_enhancement_limit_bytes) == (1000, 128 * 1024 ** 2)


@pytest.mark.parametrize("key", ["STORAGE_RAW_LIMIT_BYTES", "STORAGE_ENHANCEMENT_LIMIT_BYTES"])
def test_sub_limit_without_account_limit_refuses_configuration(storage_env, key):
    storage_env.setenv(key, "1024")
    with pytest.raises(RuntimeError, match=f"{key} requires STORAGE_ACCOUNT_LIMIT_BYTES"):
        Config.from_env()


def test_direct_config_refuses_sub_limit_without_account_limit():
    from dataclasses import fields
    required = {f.name: None for f in fields(Config) if f.default is f.default_factory}
    with pytest.raises(RuntimeError, match="sub-limits require"):
        Config(**{**required, "storage_raw_limit_bytes": 1024})
