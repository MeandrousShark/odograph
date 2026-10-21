import os
from pathlib import Path
import subprocess
import sys

import case_time_limit


ROOT = Path(__file__).resolve().parents[1]

SAMPLE = '''
import time

import pytest


@pytest.fixture
def slow_setup():
    time.sleep(0.7)


def test_passes():
    pass


def test_hangs():
    time.sleep(30)


def test_phases_share_one_limit(slow_setup):
    time.sleep(0.7)
'''


def _run(tmp_path: Path, *args: str, limit: str = "1") -> subprocess.CompletedProcess:
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "conftest.py").write_text(
        "import case_time_limit\n\n\n"
        "def pytest_configure(config):\n    case_time_limit.activate(config)\n")
    (tmp_path / "test_sample.py").write_text(SAMPLE)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "tests"), "ODOGRAPH_CASE_TIME_LIMIT": limit}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *args],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)


def test_hung_case_dumps_its_stack_and_exits(tmp_path):
    result = _run(tmp_path, "-k", "passes or hangs")

    assert result.returncode != 0
    assert "Timeout (0:00:01)!" in result.stderr
    assert "most recent call first" in result.stderr
    line = SAMPLE.splitlines().index("    time.sleep(30)") + 1
    assert f'test_sample.py", line {line} in test_hangs' in result.stderr


def test_one_limit_covers_setup_and_call_together(tmp_path):
    result = _run(tmp_path, "-k", "phases")

    assert result.returncode != 0
    assert "in test_phases_share_one_limit" in result.stderr


def test_cases_inside_the_limit_pass(tmp_path):
    result = _run(tmp_path, "-k", "passes or phases", limit="5")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


def test_pytest_faulthandler_timeout_is_refused(tmp_path):
    result = _run(tmp_path, "-o", "faulthandler_timeout=120", "-k", "passes")

    assert result.returncode == 4
    assert "faulthandler_timeout would replace the per-case hang limit" in result.stderr

    disabled = _run(tmp_path, "-p", "no:faulthandler", "-o", "faulthandler_timeout=120",
                    "-k", "passes")
    assert disabled.returncode == 0, disabled.stdout + disabled.stderr


def test_invalid_limit_is_refused(tmp_path):
    result = _run(tmp_path, "-k", "passes", limit="soon")

    assert result.returncode == 4
    assert "ODOGRAPH_CASE_TIME_LIMIT must be a positive number" in result.stderr


def test_suite_runs_under_the_default_limit(request):
    plugin = request.config.pluginmanager.get_plugin("odograph-case-time-limit")

    assert case_time_limit.LIMIT_SECONDS == 60
    assert plugin.seconds == case_time_limit.LIMIT_SECONDS
