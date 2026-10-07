import asyncio
from contextlib import asynccontextmanager

from app import diagnose


def test_instance_storage_diagnostics_use_only_aggregate_control_metadata(monkeypatch):
    calls = []

    class Connection:
        async def execute(self, query):
            calls.append(query)
            return self

        async def fetchone(self):
            return (12 << 30, 2 << 30, 10 << 30, 5, 2, 1)

    @asynccontextmanager
    async def control(pool):
        assert pool == "control"
        yield Connection()

    monkeypatch.setattr(diagnose, "control_connection", control)
    report = asyncio.run(diagnose._check_storage("control"))
    assert report.ok
    assert calls == ["SELECT * FROM public.storage_instance_status()"]
    assert report.stats == {
        "instance_budget_bytes": 12 << 30, "instance_reserve_bytes": 2 << 30,
        "total_grants_bytes": 10 << 30, "account_count": 5,
        "warning_count": 2, "blocked_count": 1,
    }


def test_storage_diagnostics_failure_reports_class_without_exception_content(monkeypatch):
    @asynccontextmanager
    async def control(pool):
        raise RuntimeError("sensitive connection details")
        yield

    monkeypatch.setattr(diagnose, "control_connection", control)
    report = asyncio.run(diagnose._check_storage(object()))
    assert not report.ok
    assert report.error_type == "RuntimeError"
    assert "sensitive" not in repr(report)
