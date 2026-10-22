import functools
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

import pytest
import yaml
from workflow_actions import is_pinned

import suite_cost
from suite_cost import SHARD_CAP


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"


def load_workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def shard_matrix() -> list[dict]:
    """The matrix the plan job would publish for this checkout's collection."""
    return _session_plan()["matrix"]["include"]


def test_ci_runs_full_suite_on_main_pushes_and_pull_requests_with_postgis(tmp_path):
    workflow = load_workflow()

    assert workflow["on"] == {"push": {"branches": ["main"]}, "pull_request": ""}
    assert workflow["permissions"] == {"contents": "read"}

    job = workflow["jobs"]["suite"]
    assert job["name"] == "suite (${{ matrix.shard }})"
    assert job["needs"] == "plan"
    assert job["timeout-minutes"] == "20"
    assert job.get("continue-on-error", "false") == "false"
    assert job["strategy"] == {"fail-fast": "false",
                               "matrix": "${{ fromJSON(needs.plan.outputs.matrix) }}"}
    assert job["env"] == {
        "TEST_DATABASE_URL": "postgresql://mileage:testpw@127.0.0.1:5432/mileage",
        "ODOGRAPH_SHARD": "${{ matrix.spec }}",
        "ODOGRAPH_SHARD_DIGEST": "${{ needs.plan.outputs.digest }}",
    }
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
        step for step in job["steps"] if is_pinned(step.get("uses"), "actions/setup-python")
    )
    assert setup_python["with"]["cache-dependency-path"] == "requirements-dev.lock"
    assert steps["Install test dependencies"]["run"] == (
        "python -m pip install -r requirements-dev.lock"
    )
    suite = steps["Run test shard"]
    assert suite.get("continue-on-error", "false") == "false"
    assert suite["env"] == {
        "ODOGRAPH_TIMING_RECORDS": "${{ runner.temp }}/pytest-timing.jsonl",
        "ODOGRAPH_TIMING_SHARD": "${{ matrix.shard }}",
        "ODOGRAPH_TIMING_HEAD": "${{ github.event.pull_request.head.sha || github.sha }}",
    }
    script = suite["run"]
    assert script.splitlines()[0] == "set -euo pipefail"
    supervised = shlex.split(script.replace("\\\n", "").split("; then", 1)[0])
    assert supervised == [
        "set", "-euo", "pipefail", "if",
        "timeout", "--signal=INT", "--kill-after=15s", "15m",
        "python", "-m", "pytest", "-vv", "--durations=25",
        ">", "$RUNNER_TEMP/pytest-shard.log", "2>&1",
    ]
    assert "tee" not in script and "PIPESTATUS" not in script
    preview = script.split("\n  suite_status=$?\nfi\n", 1)[1]
    assert shlex.split(preview.replace("\\\n", "")) == [
        "timeout", "--signal=TERM", "--kill-after=1s", "5s",
        "tail", "-c", "16384", "$RUNNER_TEMP/pytest-shard.log", "||", "true",
        "exit", "$suite_status",
    ]

    retained = steps["Retain test shard log"]
    assert retained.get("continue-on-error", "false") == "false"
    assert retained["if"] == "always()"
    assert retained["timeout-minutes"] == "2"
    assert is_pinned(retained["uses"], "actions/upload-artifact")
    assert retained["with"] == {
        "name": "pytest-log-${{ github.run_id }}-${{ github.run_attempt }}-${{ github.event_name }}-${{ matrix.shard }}",
        "path": "${{ runner.temp }}/pytest-shard.log",
        "retention-days": "7",
        "if-no-files-found": "warn",
    }
    assert job["steps"].index(retained) > job["steps"].index(suite)
    timing = steps["Retain test timing records"]
    assert timing.get("continue-on-error", "false") == "false"
    assert timing["if"] == "always()"
    assert timing["timeout-minutes"] == "2"
    assert is_pinned(timing["uses"], "actions/upload-artifact")
    assert timing["with"] == {
        "name": "pytest-timing-${{ github.run_id }}-${{ github.run_attempt }}-${{ github.event_name }}-${{ matrix.shard }}",
        "path": "${{ runner.temp }}/pytest-timing.jsonl",
        "retention-days": "90",
        "if-no-files-found": "warn",
    }
    assert [
        step for step in job["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
    ] == [retained, timing]

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
        assert (tmp_path / "pytest-shard.log").read_text() == result.stdout

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
    assert (tmp_path / "pytest-shard.log").read_text() == content
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
        assert (tmp_path / "pytest-shard.log").read_text() == "suite stdout\nsuite stderr\n"


def test_ci_cancels_superseded_branch_runs_but_never_main():
    workflow = load_workflow()

    assert workflow["concurrency"] == {
        "group": (
            "${{ github.workflow }}-"
            "${{ github.ref == 'refs/heads/main' && github.run_id || github.ref }}"
        ),
        "cancel-in-progress": "${{ github.ref != 'refs/heads/main' }}",
    }


def test_ci_checks_every_tracked_shell_script_once():
    workflow = load_workflow()
    steps = {step.get("name"): step for step in workflow["jobs"]["plan"]["steps"]}

    assert steps["Check shell script syntax"] == {
        "name": "Check shell script syntax",
        "run": "git ls-files -z -- '*.sh' | xargs -0 -n1 bash -n",
    }
    assert all(step.get("name") != "Check shell script syntax"
               for name, job in workflow["jobs"].items() if name != "plan"
               for step in job["steps"])


def _plan_step() -> dict:
    steps = load_workflow()["jobs"]["plan"]["steps"]
    return next(step for step in steps if step.get("id") == "plan")


def test_ci_plans_shards_in_a_read_only_first_job():
    workflow = load_workflow()
    job = workflow["jobs"]["plan"]

    assert "needs" not in job and "permissions" not in job and "strategy" not in job
    assert int(job["timeout-minutes"]) <= 10
    assert job["outputs"] == {
        "matrix": "${{ steps.plan.outputs.matrix }}",
        "digest": "${{ steps.plan.outputs.digest }}",
        "count": "${{ steps.plan.outputs.count }}",
    }
    steps = {step.get("name"): step for step in job["steps"]}
    setup_python = next(
        step for step in job["steps"] if is_pinned(step.get("uses"), "actions/setup-python")
    )
    suite_setup = next(
        step for step in workflow["jobs"]["suite"]["steps"]
        if is_pinned(step.get("uses"), "actions/setup-python")
    )
    assert setup_python == suite_setup
    assert steps["Install test dependencies"]["run"] == (
        "python -m pip install -r requirements-dev.lock"
    )
    plan = _plan_step()
    assert plan["env"] == {"PYTHONPATH": "tests",
                           "ODOGRAPH_SHARD_PLAN_OUTPUT": "${{ runner.temp }}/shard-plan.json"}
    assert "${{" not in plan["run"]
    assert plan["run"].splitlines()[:8] == [
        "set -euo pipefail",
        'log="$RUNNER_TEMP/shard-plan.log"',
        'if python -m pytest --collect-only -q -p no:cacheprovider -p shard_plan > "$log" 2>&1; then',
        '  tail -n 20 "$log"',
        "else",
        "  plan_status=$?",
        '  tail -c 16384 "$log" || true',
        '  exit "$plan_status"',
    ]
    assert job["steps"].index(steps["Install test dependencies"]) < job["steps"].index(plan)


def _run_plan_step(tmp_path, plan_text: str | None, status: int = 0, lines: int = 1):
    """Run the plan step with pytest replaced by one that writes `plan_text`
    and prints `lines` numbered lines after a header."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    python = bin_dir / "python"
    python.write_text(
        '#!/bin/sh\nif [ "$1" = -m ]; then\n'
        '  if [ -n "${CI_TEST_PLAN:-}" ]; then printf "%s" "$CI_TEST_PLAN" > "$ODOGRAPH_SHARD_PLAN_OUTPUT"; fi\n'
        '  echo "collect-only output"\n  seq "$CI_TEST_LINES"\n  echo "exit message" >&2\n'
        '  exit "$CI_TEST_STATUS"\nfi\n'
        f'exec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o755)
    output = tmp_path / "github-output"
    output.write_text("")
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
           "GITHUB_OUTPUT": str(output), "CI_TEST_STATUS": str(status),
           "RUNNER_TEMP": str(tmp_path), "CI_TEST_LINES": str(lines),
           "ODOGRAPH_SHARD_PLAN_OUTPUT": str(tmp_path / "shard-plan.json"),
           "CI_TEST_PLAN": plan_text or ""}
    result = subprocess.run(["bash", "-c", _plan_step()["run"]], env=env,
                            capture_output=True, text=True, timeout=30)
    return result, output.read_text()


def test_ci_plan_step_publishes_the_plugin_plan_as_job_outputs(tmp_path):
    plan = _session_plan()
    result, outputs = _run_plan_step(tmp_path, json.dumps(plan))

    assert result.returncode == 0, result.stderr
    assert result.stdout == "collect-only output\n1\nexit message\n"
    lines = dict(line.split("=", 1) for line in outputs.splitlines())
    assert set(lines) == {"matrix", "digest", "count"}
    assert json.loads(lines["matrix"]) == plan["matrix"]
    assert "\n" not in lines["matrix"] and " " not in lines["matrix"]
    assert (lines["digest"], int(lines["count"])) == (plan["digest"], plan["count"])
    assert f"{plan['count']} shards of 225.0, cap 8" in result.stderr


def test_ci_plan_step_shows_twenty_lines_on_success_and_a_long_tail_on_failure(tmp_path):
    result, _ = _run_plan_step(tmp_path, json.dumps(_session_plan()), lines=5000)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(n) for n in range(4982, 5001)] + ["exit message"]

    result, outputs = _run_plan_step(tmp_path, None, status=4, lines=5000)
    log = (tmp_path / "shard-plan.log").read_text()
    assert result.returncode == 4 and outputs == ""
    assert len(log) > 16384 and result.stdout == log[-16384:]
    assert result.stdout.endswith("5000\nexit message\n")


def test_ci_plan_step_fails_on_planner_failure_or_unreadable_plan(tmp_path):
    result, outputs = _run_plan_step(tmp_path, None, status=4)
    assert result.returncode == 4 and outputs == ""
    assert result.stdout == "collect-only output\n1\nexit message\n"

    for broken in ("", "{", "[]", json.dumps({"matrix": {"include": []}})):
        result, outputs = _run_plan_step(tmp_path, broken or " ")
        assert result.returncode == 1, broken
        assert "digest=" not in outputs


def _run_plan_plugin(tmp_path, cases: int, env: dict | None = None, files: int = 1):
    """Collect `files` db files of `cases` cases each, plus one other case."""
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    db\n")
    for index in range(files):
        (tmp_path / f"test_sample_{index}_db.py").write_text(
            "import pytest\n\npytestmark = pytest.mark.db\n\n\n"
            f"@pytest.mark.parametrize('n', range({cases}))\ndef test_case(n):\n    pass\n")
    (tmp_path / "test_other.py").write_text("def test_other():\n    pass\n")
    output = tmp_path / "plan.json"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "-p", "shard_plan"],
        cwd=tmp_path,
        env={**os.environ, "ODOGRAPH_SHARD": "", "ODOGRAPH_SHARD_DIGEST": "",
             "PYTHONPATH": str(ROOT / "tests"), "ODOGRAPH_SHARD_PLAN_OUTPUT": str(output),
             **(env or {})},
        capture_output=True, text=True, timeout=60,
    )
    return result, output


def test_plan_plugin_plans_a_small_suite_as_one_shard(tmp_path):
    result, output = _run_plan_plugin(tmp_path, 3)

    assert result.returncode == 0, result.stdout + result.stderr
    plan = json.loads(output.read_text())
    assert plan["matrix"] == {"include": [{"shard": "shard-1", "spec": "1/1"}]}
    assert (plan["count"], plan["cases"], plan["estimate_seconds"]) == (1, 4, 2.21)


def test_plan_plugin_stops_with_the_capacity_message_beyond_the_cap(tmp_path):
    # 2,580 db cases in ten files and one other case are 1,806.11
    # model-seconds, just past 8 shards of 225.
    result, output = _run_plan_plugin(tmp_path, 258, files=10)

    assert result.returncode == 4
    assert "needs 9 shards of 225 model-seconds, but SHARD_CAP allows 8" in result.stdout
    assert "1806 model-seconds (30.1 minutes)" in result.stdout
    assert not output.exists()


@pytest.mark.parametrize("env", [{"ODOGRAPH_SHARD": "1/2"}, {"ODOGRAPH_SHARD_DIGEST": "abc"}])
def test_plan_plugin_refuses_a_partial_collection(tmp_path, env):
    result, output = _run_plan_plugin(tmp_path, 3, env)

    assert result.returncode == 4
    assert "the shard plan needs the full collection" in result.stdout
    assert not output.exists()


def test_plan_plugin_refuses_marker_selection(tmp_path):
    _run_plan_plugin(tmp_path, 3)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "-p", "shard_plan", "-m", "db"],
        cwd=tmp_path,
        env={**os.environ, "ODOGRAPH_SHARD": "", "ODOGRAPH_SHARD_DIGEST": "",
             "PYTHONPATH": str(ROOT / "tests"),
             "ODOGRAPH_SHARD_PLAN_OUTPUT": str(tmp_path / "selected.json")},
        capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 4
    assert "the shard plan needs the full collection" in result.stdout


def _selections() -> list[tuple[str, str]]:
    return [("db", ""), ("not db", "")]


@functools.cache
def _session_run() -> tuple[dict[tuple[str, str], list[str]], dict]:
    """Collect the suite once, plan it, and replay every CI selection over it.

    A full collection costs seconds, and the partition and serial-timeout
    checks need one per shard. tests/shard_plan.py writes the plan the CI plan
    job publishes, and tests/collection_selections.py then runs each shard's
    real selection hooks in the same process.
    """
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "selections.json"
        plan_output = Path(directory) / "plan.json"
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
             "-p", "shard_plan", "-p", "collection_selections"],
            cwd=ROOT,
            # A CI shard runs this suite with its own selection set; the
            # canonical collection must not inherit it.
            env={**os.environ, "ODOGRAPH_SHARD": "", "ODOGRAPH_SHARD_DIGEST": "",
                 "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "tests"), os.environ.get("PYTHONPATH")])),
                 "ODOGRAPH_SHARD_PLAN_OUTPUT": str(plan_output),
                 "ODOGRAPH_COLLECTION_SELECTIONS": json.dumps(_selections()),
                 "ODOGRAPH_COLLECTION_SELECTIONS_OUTPUT": str(output)},
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        selected = json.loads(output.read_text())
        plan = json.loads(plan_output.read_text())
    collection = {("", ""): [line for line in result.stdout.splitlines() if "::" in line]}
    for row in selected:
        collection[(row["marker"], row["shard"])] = row["nodeids"]
    return collection, plan


def _session_collection() -> dict[tuple[str, str], list[str]]:
    return _session_run()[0]


def _session_plan() -> dict:
    return _session_run()[1]


def _collect(marker: str | None, shard: str) -> list[str]:
    return list(_session_collection()[(marker or "", shard)])


def plan_problems(plan) -> list[str]:
    """What a consumer of the plan job's output would trip over."""
    try:
        rows = plan["matrix"]["include"]
        count = plan["count"]
        digest = plan["digest"]
    except (TypeError, KeyError) as error:
        return [f"missing {error}"]
    problems = []
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= SHARD_CAP:
        problems.append(f"count {count!r} is not 1 to {SHARD_CAP}")
    elif rows != [{"shard": f"shard-{k}", "spec": f"{k}/{count}"} for k in range(1, count + 1)]:
        problems.append(f"matrix {rows!r} is not shard-1 to shard-{count}")
    if not (isinstance(digest, str) and len(digest) == 64
            and set(digest) <= set("0123456789abcdef")):
        problems.append(f"digest {digest!r} is not a sha256")
    return problems


def test_ci_plan_matches_the_cost_model_on_the_real_collection():
    plan = _session_plan()
    everything = _collect(None, "")
    db_cases = len(_collect("db", ""))

    assert plan_problems(plan) == []
    assert plan["digest"] == suite_cost.collection_digest(everything)
    assert plan["cases"] == len(everything)
    total = db_cases * suite_cost.DB_CENTISECONDS + (len(everything) - db_cases) * suite_cost.OTHER_CENTISECONDS
    assert plan["estimate_seconds"] == total / 100
    assert plan["count"] == max(1, -(-total // suite_cost.BUDGET))
    assert sum(shard["cases"] for shard in plan["shards"]) == len(everything)


def test_ci_shards_partition_the_real_collection():
    everything = _collect(None, "")
    plan = _session_plan()
    shards = [_collect("", row["spec"]) for row in shard_matrix()]

    assert len(shards) == plan["count"]
    assert all(shards), "every shard selects some cases"
    assert sorted(case for shard in shards for case in shard) == sorted(everything)
    for shard, planned in zip(shards, plan["shards"]):
        assert sorted({case.split("::", 1)[0] for case in shard}) == planned["files"]
        assert len(shard) == planned["cases"]


@pytest.mark.parametrize("mutate, problem", [
    (lambda plan: "not a plan", "missing"),
    (lambda plan: {**plan, "matrix": {}}, "missing"),
    (lambda plan: {**plan, "matrix": {"include": []}}, "matrix"),
    (lambda plan: {**plan, "count": 0, "matrix": {"include": []}}, "count 0"),
    (lambda plan: {**plan, "count": SHARD_CAP + 1}, f"count {SHARD_CAP + 1}"),
    (lambda plan: {**plan, "count": str(plan["count"])}, "count"),
    (lambda plan: {**plan, "count": plan["count"] + 1}, "matrix"),
    (lambda plan: {**plan, "matrix": {"include": [
        {**row, "spec": row["spec"].replace("/", "-")} for row in plan["matrix"]["include"]]}},
     "matrix"),
    (lambda plan: {**plan, "digest": ""}, "digest"),
])
def test_plan_problems_catch_malformed_output(mutate, problem):
    assert plan_problems(_session_plan()) == []
    problems = plan_problems(mutate(dict(_session_plan())))

    assert problems and problem in problems[0]


def _run_aggregate(results: dict) -> int:
    [step] = load_workflow()["jobs"]["test"]["steps"]
    return subprocess.run(
        ["bash", "-c", step["run"]], env={**os.environ, "SHARD_CAP": step["env"]["SHARD_CAP"], **results},
        capture_output=True, text=True, timeout=10,
    ).returncode


def test_ci_aggregate_check_passes_only_when_every_shard_passed():
    job = load_workflow()["jobs"]["test"]

    assert job["if"] == "always()"
    assert job["needs"] == ["plan", "suite"]
    assert job["timeout-minutes"] == "2"
    [step] = job["steps"]
    assert step["env"] == {
        "PLAN_RESULT": "${{ needs.plan.result }}",
        "PLAN_COUNT": "${{ needs.plan.outputs.count }}",
        "PLAN_MATRIX": "${{ needs.plan.outputs.matrix }}",
        "SHARD_CAP": str(SHARD_CAP),
        "SUITE_RESULT": "${{ needs.suite.result }}",
    }
    matrix = json.dumps({"include": [{"shard": f"shard-{k}", "spec": f"{k}/3"} for k in (1, 2, 3)]})
    passing = {"PLAN_RESULT": "success", "PLAN_COUNT": "3", "PLAN_MATRIX": matrix,
               "SUITE_RESULT": "success"}
    assert _run_aggregate(passing) == 0
    capped = json.dumps({"include": [{"shard": f"shard-{k}", "spec": f"{k}/{SHARD_CAP}"}
                                     for k in range(1, SHARD_CAP + 1)]})
    assert _run_aggregate({**passing, "PLAN_COUNT": str(SHARD_CAP), "PLAN_MATRIX": capped}) == 0
    over = json.dumps({"include": [{"shard": f"shard-{k}", "spec": f"{k}/{SHARD_CAP + 1}"}
                                   for k in range(1, SHARD_CAP + 2)]})
    failing = [
        {"PLAN_RESULT": "failure"}, {"PLAN_RESULT": "cancelled"}, {"PLAN_RESULT": "skipped"},
        {"PLAN_RESULT": ""},
        {"PLAN_COUNT": ""}, {"PLAN_COUNT": "0"}, {"PLAN_COUNT": "-1"}, {"PLAN_COUNT": "three"},
        {"PLAN_COUNT": "3 "}, {"PLAN_COUNT": "03"}, {"PLAN_COUNT": "2"},
        {"PLAN_COUNT": str(SHARD_CAP + 1), "PLAN_MATRIX": over},
        {"PLAN_MATRIX": ""}, {"PLAN_MATRIX": "{"}, {"PLAN_MATRIX": "[]"},
        {"PLAN_MATRIX": json.dumps({"include": []})}, {"PLAN_MATRIX": "{}"},
        {"SUITE_RESULT": "failure"}, {"SUITE_RESULT": "cancelled"},
        {"SUITE_RESULT": "skipped"}, {"SUITE_RESULT": ""},
    ]
    for change in failing:
        assert _run_aggregate({**passing, **change}) != 0, change


def test_ci_timing_summary_reports_without_gating(tmp_path):
    workflow = load_workflow()
    job = workflow["jobs"]["timing-summary"]

    # Runs after failed shards, but a superseded, cancelled run spends no runner.
    assert job["if"] == "${{ !cancelled() }}"
    assert job["needs"] == "suite"
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    assert int(job["timeout-minutes"]) <= 15
    assert workflow["jobs"]["test"]["needs"] == ["plan", "suite"]
    assert all("timing-summary" not in str(other.get("needs", ""))
               for name, other in workflow["jobs"].items() if name != "timing-summary")

    steps = {step.get("name"): step for step in job["steps"]}
    current = steps["Download this run's timing records"]
    assert is_pinned(current["uses"], "actions/download-artifact")
    assert current["id"] == "current"
    assert current["continue-on-error"] == "true"
    assert current["with"] == {
        "pattern": "pytest-timing-${{ github.run_id }}-*",
        "path": "${{ runner.temp }}/timing-current",
    }
    baselines = steps["Download main timing baselines"]
    assert baselines["id"] == "baselines"
    assert baselines["continue-on-error"] == "true"
    assert baselines["timeout-minutes"] == "5"
    assert baselines["env"] == {"GH_TOKEN": "${{ github.token }}",
                                "REPOSITORY": "${{ github.repository }}"}
    summary = steps["Summarize test timing"]
    assert summary["env"] == {"CURRENT_DOWNLOAD": "${{ steps.current.outcome }}",
                              "BASELINE_DOWNLOAD": "${{ steps.baselines.outcome }}"}
    assert "${{" not in summary["run"]
    assert shlex.split(summary["run"].replace("\\\n", ""))[3:] == [
        "python", "scripts/ci_timing_summary.py", "--current", "$RUNNER_TEMP/timing-current",
        "--baseline", "$RUNNER_TEMP/timing-baselines",
        "--current-download", "$CURRENT_DOWNLOAD", "--baseline-download", "$BASELINE_DOWNLOAD",
        ">>", "$GITHUB_STEP_SUMMARY",
    ]
    assert '>> "$GITHUB_STEP_SUMMARY"' in summary["run"]
    order = [current, baselines, summary]
    assert sorted(order, key=job["steps"].index) == order

    # Run the baseline download against a fake gh: one run's artifacts are gone.
    log = tmp_path / "gh.log"
    gh = tmp_path / "gh"
    gh.write_text(
        '#!/bin/sh\necho "$*" >> "$CI_TEST_LOG"\n'
        'case "$1" in api) printf "30\\n20\\n" ;; run) [ "$5" != 20 ] ;; esac\n')
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
           "RUNNER_TEMP": str(tmp_path), "CI_TEST_LOG": str(log),
           "REPOSITORY": "owner/repo", "GH_TOKEN": "token"}
    result = subprocess.run(["bash", "-c", baselines["run"]], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "no timing records for run 20\n"
    target = tmp_path / "timing-baselines"
    assert log.read_text().splitlines() == [
        "api --jq .workflow_runs[].id repos/owner/repo/actions/workflows/test.yml/runs"
        "?branch=main&event=push&status=success&per_page=15",
        f"run download --repo owner/repo 30 --pattern pytest-timing-* --dir {target}/30",
        f"run download --repo owner/repo 20 --pattern pytest-timing-* --dir {target}/20",
    ]


def test_ci_installs_pinned_gitleaks_before_pytest():
    workflow = load_workflow()
    job_steps = workflow["jobs"]["suite"]["steps"]
    steps = {step.get("name"): step for step in job_steps}

    install = steps["Install Gitleaks"]
    assert install["env"] == {
        "GITLEAKS_VERSION": "8.30.1",
        "GITLEAKS_SHA256": "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    }
    assert "gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}" in install["run"]
    assert "sha256sum --check -" in install["run"]
    assert 'echo "$install_dir" >> "$GITHUB_PATH"' in install["run"]
    assert "if" not in install
    assert job_steps.index(install) < job_steps.index(steps["Run test shard"])


def test_ci_disables_durability_on_its_own_service_container_before_pytest(tmp_path):
    job_steps = load_workflow()["jobs"]["suite"]["steps"]
    steps = {step.get("name"): step for step in job_steps}
    step = steps["Disable durability on the test database"]

    assert step["env"] == {"POSTGRES_CONTAINER": "${{ job.services.postgres.id }}"}
    assert "if" not in step
    assert job_steps.index(step) < job_steps.index(steps["Run test shard"])

    # Run the extracted shell against a fake docker that records each call.
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        'echo "$*" >> "$CI_TEST_LOG"\n'
        'case "$*" in *"SHOW "*) printf "%s\\n" "${CI_TEST_SHOW:-off}" ;; esac\n'
    )
    fake_docker.chmod(0o755)
    fake_sleep = tmp_path / "sleep"
    fake_sleep.write_text("#!/bin/sh\nexit 0\n")
    fake_sleep.chmod(0o755)
    log = tmp_path / "docker.log"
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
           "POSTGRES_CONTAINER": "service-123", "CI_TEST_LOG": str(log)}

    result = subprocess.run(
        ["bash", "-c", step["run"]], env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    prefix = "exec service-123 psql -v ON_ERROR_STOP=1 -U mileage -d mileage "
    assert log.read_text().splitlines() == [
        prefix + "-c ALTER SYSTEM SET fsync = off",
        prefix + "-c ALTER SYSTEM SET synchronous_commit = off",
        prefix + "-c ALTER SYSTEM SET full_page_writes = off",
        prefix + "-c SELECT pg_reload_conf()",
        prefix + "-Atc SHOW fsync",
        prefix + "-Atc SHOW synchronous_commit",
        prefix + "-Atc SHOW full_page_writes",
    ]

    # A setting that never reads back as off fails the step.
    log.unlink()
    result = subprocess.run(
        ["bash", "-c", step["run"]], env={**env, "CI_TEST_SHOW": "on"},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 1
    assert "fsync did not take effect" in result.stderr


def test_ci_public_tree_is_an_explicit_unprivileged_gate():
    workflow = load_workflow()
    assert set(workflow["jobs"]) == {"plan", "suite", "test", "timing-summary", "public-tree"}
    job = workflow["jobs"]["public-tree"]
    assert job["runs-on"] == "ubuntu-latest"
    steps = {step.get("name"): step for step in job["steps"]}
    assert steps["Check public tree"]["run"] == "python scripts/check_public_tree.py"
    install = steps["Install Gitleaks"]
    suite_steps = {step.get("name"): step for step in workflow["jobs"]["suite"]["steps"]}
    assert install == suite_steps["Install Gitleaks"]
    assert job["steps"].index(install) < job["steps"].index(steps["Check public tree"])
    for candidate in workflow["jobs"].values():
        assert candidate["runs-on"] == "ubuntu-latest"
        for step in candidate["steps"]:
            if is_pinned(step.get("uses"), "actions/checkout"):
                assert step["with"]["persist-credentials"] == "false"
    assert "pull_request_target" not in workflow["on"]
    assert "secrets." not in WORKFLOW.read_text()
