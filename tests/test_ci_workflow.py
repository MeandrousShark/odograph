import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"
SNAPSHOT_SCRIPT = ROOT / "scripts" / "make_public_snapshot.sh"


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


def test_public_snapshot_includes_ci_and_release_files(tmp_path):
    snapshot = tmp_path / "snapshot"
    subprocess.run(
        [SNAPSHOT_SCRIPT, snapshot],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    source_workflows = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / ".github" / "workflows").iterdir()
        if path.is_file()
    }
    snapshot_workflows = {
        path.relative_to(snapshot).as_posix()
        for path in (snapshot / ".github" / "workflows").iterdir()
        if path.is_file()
    }
    assert ".github/workflows/test.yml" in snapshot_workflows
    assert snapshot_workflows == source_workflows
    assert (snapshot / ".github" / "workflows" / "test.yml").read_bytes() == (
        WORKFLOW.read_bytes()
    )
    assert (snapshot / "compose.build.override.yml").read_bytes() == (
        ROOT / "compose.build.override.yml"
    ).read_bytes()
    assert (snapshot / "CHANGELOG.md").read_bytes() == (
        ROOT / "CHANGELOG.md"
    ).read_bytes()
    assert (snapshot / "docs" / "releasing.md").read_bytes() == (
        ROOT / "docs" / "releasing.md"
    ).read_bytes()

    snapshot_docs = {
        path.relative_to(snapshot).as_posix()
        for path in (snapshot / "docs").rglob("*")
        if path.is_file()
    }
    assert snapshot_docs == {
        "docs/backups.md",
        "docs/osrm.md",
        "docs/owntracks.md",
        "docs/privacy.md",
        "docs/releasing.md",
        "docs/reverse-proxy.md",
        "docs/security.md",
        "docs/upgrading.md",
    }
