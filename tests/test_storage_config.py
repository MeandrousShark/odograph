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
    monkeypatch.setenv("SESSION_SECRET", "synthetic-storage-config")
    for key in STORAGE_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.mark.parametrize("key", STORAGE_KEYS)
@pytest.mark.parametrize("value", ["0", "-1", "1.5", str(2 ** 63)])
def test_invalid_storage_size_refuses_configuration(storage_env, key, value):
    storage_env.setenv(key, value)
    with pytest.raises(RuntimeError, match=key):
        Config.from_env()


@pytest.mark.parametrize("key,value", [
    ("STORAGE_RAW_LIMIT_BYTES", str(3 * 1024 ** 3)),
    ("STORAGE_ENHANCEMENT_LIMIT_BYTES", str(3 * 1024 ** 3)),
    ("STORAGE_INSTANCE_RESERVE_BYTES", str(12 * 1024 ** 3)),
])
def test_unfunded_subset_or_reserve_refuses_configuration(storage_env, key, value):
    storage_env.setenv(key, value)
    with pytest.raises(RuntimeError, match="storage"):
        Config.from_env()
