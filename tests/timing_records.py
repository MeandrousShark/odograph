"""Test-only timing records: one JSON line per case, for suite cost baselines.

Off unless ODOGRAPH_TIMING_RECORDS names an output file. Each line is
flushed as it is written, so a shard stopped by its timeout still leaves the
cases it finished. Records:

- ``header``: format, revisions, shard, CI run and runner environment
- ``collection``: collection time and case counts
- ``case``: node id, tier, outcome, setup/call/teardown seconds, spans, and
  any module- or session-scoped fixtures first set up in that case's setup
  (their cost lands in that case's setup time)
- ``session``: exit status and wall time

Spans are inclusive elapsed time, so nested spans (a reset inside a
bootstrap) are not additive, and spans of concurrent coroutines in one case
can sum past its wall time.
"""
from __future__ import annotations

import functools
import inspect
import json
import os
import platform
import subprocess
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import pytest

FORMAT_VERSION = 1
_ENV_RECORDS = "ODOGRAPH_TIMING_RECORDS"
_ENV_SHARD = "ODOGRAPH_TIMING_SHARD"
_ENV_HEAD = "ODOGRAPH_TIMING_HEAD"
_TIERS = ("unit", "ops", "db")

# Spans of the case now running, or None when recording is off or between
# cases. Cases run one at a time in this process.
_spans: dict[str, list] | None = None


@contextmanager
def span(name: str):
    spans = _spans
    if spans is None:
        yield
        return
    start = time.perf_counter()
    try:
        yield
    finally:
        entry = spans.setdefault(name, [0, 0.0])
        entry[0] += 1
        entry[1] += time.perf_counter() - start


def timed(name: str):
    """Decorate a sync or async function so each call records a span."""

    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def run_async(*args, **kwargs):
                with span(name):
                    return await function(*args, **kwargs)
            return run_async

        @functools.wraps(function)
        def run(*args, **kwargs):
            with span(name):
                return function(*args, **kwargs)
        return run

    return decorate


def _git_head(root: Path) -> str | None:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor()


def _environment() -> dict:
    return {
        "python": platform.python_version(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "logical_cpus": os.cpu_count(),
        "cpu_model": _cpu_model(),
        "runner_os": os.environ.get("RUNNER_OS"),
        "runner_arch": os.environ.get("RUNNER_ARCH"),
        "image_os": os.environ.get("ImageOS"),
        "image_version": os.environ.get("ImageVersion"),
    }


def _outcome(reports: dict) -> str:
    call = reports.get("call")
    if call is not None and call.failed:
        return "failed"
    if any(report.failed for report in reports.values()):
        return "error"
    if call is not None and hasattr(call, "wasxfail"):
        return "xfailed" if call.skipped else "xpassed"
    if any(report.skipped for report in reports.values()):
        return "skipped"
    return "passed"


class TimingRecorder:
    def __init__(self, config, path: Path, shard: str, head: str | None):
        self.config = config
        self.started = time.perf_counter()
        self.common = {"shard": shard, "head": head}
        self.tiers: dict[str, str | None] = {}
        self.reports: dict = {}
        self.shared_fixtures: list[str] = []
        self.cases = 0
        self.deselected = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("w", encoding="utf-8")
        root = Path(str(config.rootpath))
        self.write("header", {
            "format": FORMAT_VERSION,
            "checkout": _git_head(root),
            "markexpr": config.option.markexpr,
            "db_shard": os.environ.get("ODOGRAPH_DB_SHARD", ""),
            "event": os.environ.get("GITHUB_EVENT_NAME"),
            "ref": os.environ.get("GITHUB_REF"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "environment": _environment(),
        })

    def write(self, kind: str, fields: dict) -> None:
        record = {"kind": kind, **self.common, **fields}
        self.file.write(json.dumps(record, sort_keys=True) + "\n")
        self.file.flush()

    @pytest.hookimpl(hookwrapper=True)
    def pytest_collection(self, session):
        start = time.perf_counter()
        yield
        for item in session.items:
            tiers = [tier for tier in _TIERS if item.get_closest_marker(tier)]
            self.tiers[item.nodeid] = tiers[0] if len(tiers) == 1 else None
        self.write("collection", {
            "seconds": round(time.perf_counter() - start, 6),
            "collected": len(session.items) + self.deselected,
            "deselected": self.deselected,
            "selected": len(session.items),
        })

    def pytest_deselected(self, items):
        self.deselected += len(items)

    def pytest_runtest_logstart(self, nodeid, location):
        global _spans
        _spans = {}
        self.reports = {}
        self.shared_fixtures = []

    def pytest_fixture_setup(self, fixturedef, request):
        if fixturedef.scope != "function" and _spans is not None:
            self.shared_fixtures.append(fixturedef.argname)

    def pytest_runtest_logreport(self, report):
        self.reports[report.when] = report

    def pytest_runtest_logfinish(self, nodeid, location):
        global _spans
        spans, _spans = _spans or {}, None
        fields = {
            "nodeid": nodeid,
            "tier": self.tiers.get(nodeid),
            "outcome": _outcome(self.reports),
            "spans": {name: [count, round(seconds, 6)]
                      for name, (count, seconds) in sorted(spans.items())},
        }
        for when in ("setup", "call", "teardown"):
            report = self.reports.get(when)
            fields[when] = round(report.duration, 6) if report is not None else None
        if self.shared_fixtures:
            fields["shared_fixtures"] = self.shared_fixtures
        self.write("case", fields)
        self.cases += 1

    def pytest_sessionfinish(self, session, exitstatus):
        self.write("session", {
            "exitstatus": int(exitstatus),
            "cases": self.cases,
            "seconds": round(time.perf_counter() - self.started, 6),
        })
        self.file.close()


def _wrap_method(owner, attribute: str, name: str) -> None:
    setattr(owner, attribute, timed(name)(getattr(owner, attribute)))


def _install_spans() -> None:
    """Span shared setup outside tests/conftest.py, for the session.

    Only names looked up at call time are patched; conftest spans its own
    helpers and the names it binds at import.
    """
    import jinja2
    from fastapi import FastAPI
    from psycopg_pool import AsyncConnectionPool
    import app.main

    _wrap_method(AsyncConnectionPool, "open", "pool_open")
    _wrap_method(AsyncConnectionPool, "close", "pool_close")
    _wrap_method(jinja2.Environment, "compile", "template_compile")
    _wrap_method(jinja2.Template, "render", "template_render")
    _wrap_method(app.main, "create_app", "create_app")

    original_init = FastAPI.__init__

    def init(app, *args, **kwargs):
        lifespan = kwargs.get("lifespan")
        if lifespan is not None:
            kwargs["lifespan"] = _timed_lifespan(lifespan)
        original_init(app, *args, **kwargs)

    FastAPI.__init__ = init


def _timed_lifespan(lifespan):
    if inspect.isasyncgenfunction(lifespan):
        lifespan = asynccontextmanager(lifespan)

    @asynccontextmanager
    async def run(app):
        manager = lifespan(app)
        with span("app_startup"):
            state = await manager.__aenter__()
        try:
            yield state
        except BaseException:
            with span("app_shutdown"):
                if not await manager.__aexit__(*sys.exc_info()):
                    raise
        else:
            with span("app_shutdown"):
                await manager.__aexit__(None, None, None)

    return run


def activate(config) -> None:
    """Record timings when ODOGRAPH_TIMING_RECORDS is set.

    The variables are removed once read so pytest subprocesses that tests
    start do not also record into the same file.
    """
    path = os.environ.pop(_ENV_RECORDS, "")
    shard = os.environ.pop(_ENV_SHARD, "") or "local"
    head = os.environ.pop(_ENV_HEAD, "")
    if not path:
        return
    root = Path(str(config.rootpath))
    recorder = TimingRecorder(config, Path(path), shard, head or _git_head(root))
    _install_spans()
    config.pluginmanager.register(recorder, "odograph-timing-records")
