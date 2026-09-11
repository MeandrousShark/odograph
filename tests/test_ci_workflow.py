from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"


def load_workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def test_ci_runs_full_suite_on_push_and_pull_requests_with_postgis():
    workflow = load_workflow()

    assert set(workflow["on"]) == {"push", "pull_request"}
    assert workflow["permissions"] == {"contents": "read"}

    job = workflow["jobs"]["test"]
    assert job["env"]["TEST_DATABASE_URL"] == (
        "postgresql://mileage:testpw@127.0.0.1:5432/mileage"
    )
    postgres = job["services"]["postgres"]
    assert postgres["image"] == "postgis/postgis:16-3.4"
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
    assert steps["Run full test suite"]["run"] == "python -m pytest"
    assert "tests/" not in steps["Run full test suite"]["run"]


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
