import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import timing_records


ROOT = Path(__file__).resolve().parents[1]

SAMPLE = '''
import os
import time

import pytest

from timing_records import span


@pytest.fixture(scope="module")
def shared():
    return 1


@pytest.fixture
def broken():
    raise RuntimeError("setup fails")


@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown fails")


@pytest.fixture
def expected_setup_failure():
    pytest.xfail("not ready")


@pytest.mark.unit
def test_passes(shared):
    for _ in range(2):
        with span("work"):
            time.sleep(0.01)


@pytest.mark.db
def test_fails(shared):
    assert False


def test_skipped():
    pytest.skip("not here")


@pytest.mark.xfail(strict=False)
def test_expected_failure():
    assert False


def test_setup_error(broken):
    pass


@pytest.mark.xfail(strict=False)
def test_unexpected_pass():
    pass


def test_teardown_error(broken_teardown):
    pass


def test_setup_xfail(expected_setup_failure):
    pass


def test_recording_is_not_inherited():
    assert not {key for key in os.environ if key.startswith("ODOGRAPH_TIMING_")}


def test_shared_setup_spans():
    from contextlib import asynccontextmanager

    import jinja2
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    jinja2.Environment().from_string("{{ x }}").render(x=1)

    @asynccontextmanager
    async def lifespan(app):
        yield

    with TestClient(FastAPI(lifespan=lifespan)):
        pass


def test_deselected():
    pass


def test_stops_the_session():
    pytest.exit("stopping", returncode=3)
'''


def _write_sample(tmp_path: Path, sample: str) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    unit\n    db\n")
    (tmp_path / "conftest.py").write_text(
        "import timing_records\n\n\n"
        "def pytest_configure(config):\n    timing_records.activate(config, 57)\n")
    (tmp_path / "test_sample.py").write_text(sample)


def _run_sample(tmp_path: Path, records: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT / "tests"), str(ROOT))),
           "ODOGRAPH_TIMING_RECORDS": str(records),
           "ODOGRAPH_TIMING_SHARD": "db-2", "ODOGRAPH_TIMING_HEAD": "abc123"}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
         "-k", "not deselected"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)


def test_records_every_case_with_header_collection_and_session(tmp_path):
    _write_sample(tmp_path, SAMPLE)
    records = tmp_path / "out" / "timing.jsonl"
    result = _run_sample(tmp_path, records)
    assert result.returncode == 3, result.stdout + result.stderr

    lines = [json.loads(line) for line in records.read_text().splitlines()]
    assert [line["kind"] for line in lines] == (
        ["header", "collection"] + ["case"] * 11 + ["session"])
    assert all(line["shard"] == "db-2" and line["head"] == "abc123" for line in lines)

    header = lines[0]
    assert header["format"] == timing_records.FORMAT_VERSION
    assert header["schema_version"] == 57
    assert header["markexpr"] == ""
    assert {"python", "logical_cpus", "cpu_model", "image_version"} <= set(header["environment"])

    collection = lines[1]
    assert (collection["collected"], collection["deselected"], collection["selected"]) == (12, 1, 11)
    assert collection["seconds"] >= 0

    cases = {line["nodeid"].split("::")[1]: line for line in lines[2:-1]}
    assert {name: case["outcome"] for name, case in cases.items()} == {
        "test_passes": "passed",
        "test_fails": "failed",
        "test_skipped": "skipped",
        "test_expected_failure": "xfailed",
        "test_setup_error": "error",
        "test_recording_is_not_inherited": "passed",
        "test_shared_setup_spans": "passed",
        "test_unexpected_pass": "xpassed",
        "test_teardown_error": "error",
        "test_setup_xfail": "xfailed",
        "test_stops_the_session": "interrupted",
    }
    assert cases["test_passes"]["tier"] == "unit"
    assert cases["test_fails"]["tier"] == "db"
    assert cases["test_skipped"]["tier"] is None

    passes = cases["test_passes"]
    assert passes["shared_fixtures"] == ["shared"]
    assert "shared_fixtures" not in cases["test_fails"]
    assert all(passes[when] >= 0 for when in ("setup", "call", "teardown"))
    count, seconds = passes["spans"]["work"]
    assert count == 2 and seconds >= 0.02
    assert passes["call"] >= seconds
    assert cases["test_setup_error"]["call"] is None
    assert cases["test_stops_the_session"]["setup"] >= 0
    assert cases["test_stops_the_session"]["call"] is None
    assert lines[-2]["nodeid"].endswith("::test_stops_the_session")
    assert {"template_compile", "template_render", "app_startup", "app_shutdown"} <= set(
        cases["test_shared_setup_spans"]["spans"])

    assert lines[-1]["exitstatus"] == 3
    assert lines[-1]["cases"] == 11


def test_recording_fault_turns_recording_off_without_failing_the_suite(tmp_path):
    _write_sample(tmp_path, "def test_passes():\n    pass\n")
    unwritable = tmp_path / "records-are-a-directory"
    unwritable.mkdir()
    result = _run_sample(tmp_path, unwritable)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "timing records disabled" in result.stderr
    assert list(unwritable.iterdir()) == []


def _lifespan_result(lifespan, body):
    async def run():
        async with timing_records._timed_lifespan(lifespan)(object()) as state:
            return await body(state)
    return asyncio.run(run())


def test_timed_lifespan_keeps_state_suppression_and_errors(monkeypatch):
    monkeypatch.setattr(timing_records, "_spans", {})
    exits = []

    @asynccontextmanager
    async def passing(app):
        yield {"ready": True}

    async def body(state):
        return state
    assert _lifespan_result(passing, body) == {"ready": True}
    assert {"app_startup", "app_shutdown"} <= set(timing_records._spans)

    @asynccontextmanager
    async def suppressing(app):
        try:
            yield None
        except RuntimeError as error:
            exits.append(error)

    async def raises(state):
        raise RuntimeError("in body")
    assert _lifespan_result(suppressing, raises) is None
    assert [str(error) for error in exits] == ["in body"]

    @asynccontextmanager
    async def plain(app):
        yield None
    with pytest.raises(RuntimeError, match="in body"):
        _lifespan_result(plain, raises)

    @asynccontextmanager
    async def refuses(app):
        raise RuntimeError("refused")
        yield
    with pytest.raises(RuntimeError, match="refused"):
        _lifespan_result(refuses, body)

    async def generator(app):
        yield "from generator"
    assert _lifespan_result(generator, body) == "from generator"


def test_span_records_nothing_outside_a_recorded_case(monkeypatch):
    # This suite may itself be recording, so stand in for an idle process.
    monkeypatch.setattr(timing_records, "_spans", None)
    with timing_records.span("idle"):
        pass
    assert timing_records._spans is None
