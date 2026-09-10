"""Runs the archive controller's JavaScript behavioral tests.

tests/js/ exercises static/archive_state.js directly with Node's built-in
test runner, which is the only way the request-generation, debounce, and
filter-comparison behavior gets proven rather than asserted about as source
text. This wrapper exists so those cases run in the same command as the rest
of the suite; it is `ops` tier because it shells out. Node is not a runtime
dependency of the application, so a machine without it skips instead of
failing.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
JS_TESTS = REPO_ROOT / "tests" / "js"


def test_archive_state_module_passes_its_node_tests():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; install Node to run the JavaScript tests")

    test_paths = sorted(JS_TESTS.glob("*.test.js"))
    assert test_paths, "no JavaScript test files found"

    result = subprocess.run(
        [node, "--test", *(str(path) for path in test_paths)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"node --test {' '.join(str(path) for path in test_paths)} failed:\n"
        f"{result.stdout}\n{result.stderr}"
    )
