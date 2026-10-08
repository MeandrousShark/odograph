import os
from pathlib import Path
import shlex
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"


def load_workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def test_ci_runs_full_suite_on_push_and_pull_requests_with_postgis(tmp_path):
    workflow = load_workflow()

    assert set(workflow["on"]) == {"push", "pull_request"}
    assert workflow["permissions"] == {"contents": "read"}

    job = workflow["jobs"]["test"]
    assert job["timeout-minutes"] == "35"
    assert job.get("continue-on-error", "false") == "false"
    assert job["env"]["TEST_DATABASE_URL"] == (
        "postgresql://mileage:testpw@127.0.0.1:5432/mileage"
    )
    postgres = job["services"]["postgres"]
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert postgres["image"] == compose["services"]["db"]["image"]
    assert postgres["env"] == {
        "POSTGRES_DB": "mileage",
        "POSTGRES_USER": "mileage",
        "POSTGRES_PASSWORD": "testpw",
    }
    assert "pg_isready -h 127.0.0.1 -U mileage -d mileage" in postgres["options"]

    steps = {step.get("name"): step for step in job["steps"]}
    setup_python = next(
        step for step in job["steps"] if step.get("uses") == "actions/setup-python@v6"
    )
    assert setup_python["with"]["cache-dependency-path"] == "requirements-dev.lock"
    assert steps["Install test dependencies"]["run"] == (
        "python -m pip install -r requirements-dev.lock"
    )
    suite = steps["Run full test suite"]
    assert suite.get("continue-on-error", "false") == "false"
    script = suite["run"]
    assert script.splitlines()[0] == "set -euo pipefail"
    supervised = shlex.split(script.replace("\\\n", "").split("; then", 1)[0])
    assert supervised == [
        "set", "-euo", "pipefail", "if",
        "timeout", "--signal=INT", "--kill-after=15s", "30m",
        "python", "-m", "pytest", "-vv", "--durations=25",
        "-o", "faulthandler_timeout=120", ">", "$RUNNER_TEMP/pytest-full-suite.log", "2>&1",
    ]
    assert "tee" not in script and "PIPESTATUS" not in script
    preview = script.split("\n  suite_status=$?\nfi\n", 1)[1]
    assert shlex.split(preview.replace("\\\n", "")) == [
        "timeout", "--signal=TERM", "--kill-after=1s", "5s",
        "tail", "-c", "16384", "$RUNNER_TEMP/pytest-full-suite.log", "||", "true",
        "exit", "$suite_status",
    ]

    retained = steps["Retain full suite log"]
    assert retained.get("continue-on-error", "false") == "false"
    assert retained["if"] == "always()"
    assert retained["timeout-minutes"] == "2"
    assert retained["uses"] == "actions/upload-artifact@v4"
    assert retained["with"] == {
        "name": "pytest-log-${{ github.run_id }}-${{ github.run_attempt }}-${{ github.event_name }}",
        "path": "${{ runner.temp }}/pytest-full-suite.log",
        "retention-days": "7",
        "if-no-files-found": "warn",
    }
    assert job["steps"].index(retained) > job["steps"].index(suite)
    assert [
        step for step in job["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
    ] == [retained]

    # Exercise the extracted shell with synthetic suite output and exit status.
    runner = tmp_path / "timeout"
    runner.write_text('#!/bin/sh\nshift 3\nexec "$@"\n')
    runner.chmod(0o755)
    python = tmp_path / "python"
    python.write_text(
        '#!/bin/sh\nif [ "${CI_TEST_LARGE:-0}" = 1 ]; then printf "%20000s" x; fi\n'
        'printf "suite stdout\\n"\n'
        'printf "suite stderr\\n" >&2\nexit "$CI_TEST_STATUS"\n'
    )
    python.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
           "RUNNER_TEMP": str(tmp_path)}
    for status in (0, 7, 124, 137):
        result = subprocess.run(
            ["bash", "-c", script],
            env={**env, "CI_TEST_STATUS": str(status)},
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == status
        assert result.stdout == "suite stdout\nsuite stderr\n"
        assert (tmp_path / "pytest-full-suite.log").read_text() == result.stdout

    for status in (0, 7, 124, 137):
        result = subprocess.run(
            ["bash", "-c", script],
            env={**env, "CI_TEST_STATUS": str(status),
                 "RUNNER_TEMP": str(tmp_path / "absent")},
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 1

    result = subprocess.run(
        ["bash", "-c", script], env={**env, "CI_TEST_STATUS": "0", "CI_TEST_LARGE": "1"},
        capture_output=True, text=True, timeout=10,
    )
    content = " " * 19999 + "xsuite stdout\nsuite stderr\n"
    assert result.returncode == 0
    assert (tmp_path / "pytest-full-suite.log").read_text() == content
    assert result.stdout == content[-16384:]

    preview_failure = tmp_path / "tail"
    preview_failure.write_text('#!/bin/sh\nexit 9\n')
    preview_failure.chmod(0o755)
    for status in (0, 7):
        result = subprocess.run(
            ["bash", "-c", script], env={**env, "CI_TEST_STATUS": str(status)},
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == status
        assert (tmp_path / "pytest-full-suite.log").read_text() == "suite stdout\nsuite stderr\n"


def test_ci_checks_every_tracked_shell_script():
    workflow = load_workflow()
    steps = {step.get("name"): step for step in workflow["jobs"]["test"]["steps"]}

    assert steps["Check shell script syntax"]["run"] == (
        "git ls-files -z -- '*.sh' | xargs -0 -n1 bash -n"
    )


def test_ci_installs_pinned_gitleaks_before_pytest():
    workflow = load_workflow()
    job_steps = workflow["jobs"]["test"]["steps"]
    steps = {step.get("name"): step for step in job_steps}

    install = steps["Install Gitleaks"]
    assert install["env"] == {
        "GITLEAKS_VERSION": "8.30.1",
        "GITLEAKS_SHA256": "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    }
    assert "gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}" in install["run"]
    assert "sha256sum --check -" in install["run"]
    assert 'echo "$install_dir" >> "$GITHUB_PATH"' in install["run"]
    assert job_steps.index(install) < job_steps.index(steps["Run full test suite"])


def test_ci_public_tree_is_an_explicit_unprivileged_gate():
    workflow = load_workflow()
    assert set(workflow["jobs"]) == {"test", "public-tree"}
    job = workflow["jobs"]["public-tree"]
    assert job["runs-on"] == "ubuntu-latest"
    steps = {step.get("name"): step for step in job["steps"]}
    assert steps["Check public tree"]["run"] == "python scripts/check_public_tree.py"
    install = steps["Install Gitleaks"]
    test_steps = {step.get("name"): step for step in workflow["jobs"]["test"]["steps"]}
    assert install == test_steps["Install Gitleaks"]
    assert job["steps"].index(install) < job["steps"].index(steps["Check public tree"])
    for candidate in workflow["jobs"].values():
        assert candidate["runs-on"] == "ubuntu-latest"
        for step in candidate["steps"]:
            if step.get("uses") == "actions/checkout@v6":
                assert step["with"]["persist-credentials"] == "false"
    assert "pull_request_target" not in workflow["on"]
    assert "secrets." not in WORKFLOW.read_text()
