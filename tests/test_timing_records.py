import json
import os
from pathlib import Path
import subprocess
import sys

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


def test_recording_is_not_inherited():
    assert not {key for key in os.environ if key.startswith("ODOGRAPH_TIMING_")}


def test_deselected():
    pass
'''


def test_records_every_case_with_header_collection_and_session(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    unit\n    db\n")
    (tmp_path / "conftest.py").write_text(
        "import timing_records\n\n\n"
        "def pytest_configure(config):\n    timing_records.activate(config)\n")
    (tmp_path / "test_sample.py").write_text(SAMPLE)
    records = tmp_path / "out" / "timing.jsonl"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "tests"),
           "ODOGRAPH_TIMING_RECORDS": str(records),
           "ODOGRAPH_TIMING_SHARD": "db-2", "ODOGRAPH_TIMING_HEAD": "abc123"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
         "-k", "not deselected"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 1, result.stdout + result.stderr

    lines = [json.loads(line) for line in records.read_text().splitlines()]
    assert [line["kind"] for line in lines] == (
        ["header", "collection"] + ["case"] * 6 + ["session"])
    assert all(line["shard"] == "db-2" and line["head"] == "abc123" for line in lines)

    header = lines[0]
    assert header["format"] == timing_records.FORMAT_VERSION
    assert header["markexpr"] == ""
    assert {"python", "logical_cpus", "cpu_model", "image_version"} <= set(header["environment"])

    collection = lines[1]
    assert (collection["collected"], collection["deselected"], collection["selected"]) == (7, 1, 6)
    assert collection["seconds"] >= 0

    cases = {line["nodeid"].split("::")[1]: line for line in lines[2:-1]}
    assert {name: case["outcome"] for name, case in cases.items()} == {
        "test_passes": "passed",
        "test_fails": "failed",
        "test_skipped": "skipped",
        "test_expected_failure": "xfailed",
        "test_setup_error": "error",
        "test_recording_is_not_inherited": "passed",
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

    assert lines[-1]["exitstatus"] == 1
    assert lines[-1]["cases"] == 6


def test_span_records_nothing_outside_a_recorded_case(monkeypatch):
    # This suite may itself be recording, so stand in for an idle process.
    monkeypatch.setattr(timing_records, "_spans", None)
    with timing_records.span("idle"):
        pass
    assert timing_records._spans is None
