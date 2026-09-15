from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SECURITY_WORKFLOW = ROOT / ".github" / "workflows" / "security.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def load_workflow(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def steps_by_name(job: dict) -> dict:
    return {step.get("name"): step for step in job["steps"] if step.get("name")}


def test_security_rot_scan_runs_weekly_and_on_manual_request():
    workflow = load_workflow(SECURITY_WORKFLOW)

    assert workflow["on"] == {
        "schedule": [{"cron": "17 11 * * 1"}],
        "workflow_dispatch": "",
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert "maintainer advisory" in workflow["name"]
    assert "red on findings" in workflow["name"]


def test_pip_audit_has_no_tag_acceptance_and_remains_blocking():
    workflow = load_workflow(SECURITY_WORKFLOW)
    job = workflow["jobs"]["python-dependencies"]

    assert "advisory" in job["name"]
    assert "red on findings" in job["name"]
    audit = steps_by_name(job)["Audit locked Python dependencies"]
    assert audit["uses"] == "pypa/gh-action-pip-audit@v1.1.0"
    assert audit["with"] == {
        "inputs": "requirements.lock",
        "no-deps": "true",
    }
    assert "continue-on-error" not in audit


def test_both_architectures_build_locally_without_registry_access():
    workflow = load_workflow(SECURITY_WORKFLOW)
    job = workflow["jobs"]["container-images"]

    assert "advisory" in job["name"]
    assert "red on findings" in job["name"]
    assert job["strategy"] == {
        "fail-fast": "false",
        "matrix": {
            "include": [
                {
                    "platform": "linux/amd64",
                    "archive": "/tmp/odograph-amd64.tar",
                },
                {
                    "platform": "linux/arm64",
                    "archive": "/tmp/odograph-arm64.tar",
                },
            ]
        },
    }

    steps = steps_by_name(job)
    build = steps["Build local image archive"]
    assert build["uses"] == "docker/build-push-action@v7"
    assert build["with"]["platforms"] == "${{ matrix.platform }}"
    assert build["with"]["outputs"] == "type=docker,dest=${{ matrix.archive }}"
    assert "push" not in build["with"]
    assert "tags" not in build["with"]

    all_steps = job["steps"] + workflow["jobs"]["python-dependencies"]["steps"]
    assert not any("docker/login-action" in step.get("uses", "") for step in all_steps)
    assert all("permissions" not in configured_job for configured_job in workflow["jobs"].values())
    assert not any("publish" in step.get("name", "").lower() for step in all_steps)
    source = "\n".join(
        step.get("run", "") for step in job["steps"]
    )
    assert "packages:" not in source
    assert "ghcr.io" not in source
    assert "GITHUB_TOKEN" not in source
    assert "secrets." not in source


def test_scheduled_trivy_gate_matches_release_fixability_and_severity():
    security = load_workflow(SECURITY_WORKFLOW)
    release = load_workflow(RELEASE_WORKFLOW)
    scheduled_scan = steps_by_name(security["jobs"]["container-images"])[
        "Scan local image archive"
    ]
    release_scan = steps_by_name(release["jobs"]["build-amd64"])["Scan amd64 image"]

    assert scheduled_scan["uses"] == release_scan["uses"]
    for setting in ["format", "exit-code", "ignore-unfixed", "vuln-type", "severity", "scanners"]:
        assert scheduled_scan["with"][setting] == release_scan["with"][setting]
    assert scheduled_scan["with"]["input"] == "${{ matrix.archive }}"
    assert "trivyignores" not in scheduled_scan["with"]
    assert "continue-on-error" not in scheduled_scan


def test_scheduled_postgis_scans_use_the_pinned_remote_children_without_emulation():
    workflow = load_workflow(SECURITY_WORKFLOW)
    job = workflow["jobs"]["postgis-images"]

    assert job["strategy"] == {
        "fail-fast": "false",
        "matrix": {
            "include": [
                {"platform": "linux/amd64", "architecture": "amd64"},
                {"platform": "linux/arm64", "architecture": "arm64"},
            ]
        },
    }
    assert not any(
        "setup-qemu" in step.get("uses", "") for step in job["steps"]
    )
    assert not any(
        command in step.get("run", "")
        for step in job["steps"]
        for command in ("docker run", "docker build", "docker pull")
    )
    scan = steps_by_name(job)["Scan published PostGIS image"]
    assert scan["uses"] == "aquasecurity/trivy-action@v0.36.0"
    assert scan["env"] == {"TRIVY_PLATFORM": "${{ matrix.platform }}"}
    assert scan["with"] == {
        "image-ref": "ghcr.io/meandrousshark/odograph-postgis@sha256:89e58d40e04e390d3418f99890dff103972476a5a9d21c70bda4d210cae7a2f6",
        "format": "table",
        "exit-code": "1",
        "ignore-unfixed": "true",
        "vuln-type": "os,library",
        "severity": "HIGH,CRITICAL",
        "scanners": "vuln",
    }
    assert "trivyignores" not in scan["with"]
    assert "continue-on-error" not in scan


def test_security_workflow_uses_only_reputable_versioned_actions():
    workflow = load_workflow(SECURITY_WORKFLOW)
    used = {
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    }

    assert used == {
        "actions/checkout@v6",
        "docker/setup-qemu-action@v4",
        "docker/setup-buildx-action@v4",
        "docker/build-push-action@v7",
        "pypa/gh-action-pip-audit@v1.1.0",
        "aquasecurity/trivy-action@v0.36.0",
    }
