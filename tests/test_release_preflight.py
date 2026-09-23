from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_release_preflight.py"
WORKFLOW = ROOT / ".github" / "workflows" / "release-preflight.yml"
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("check_release_preflight", SCRIPT)
assert SPEC and SPEC.loader
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def load_workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def steps_by_name(job: dict) -> dict:
    return {step.get("name"): step for step in job["steps"] if step.get("name")}


def test_derives_exact_version_from_repository_compose_image():
    compose = "services:\n  app:\n    image: ghcr.io/example/odograph:v1.2.3\n"

    assert preflight.derive_version(compose, "Example/Odograph") == "v1.2.3"


def test_rejects_a_compose_image_from_another_repository():
    compose = "services:\n  app:\n    image: ghcr.io/other/odograph:v1.2.3\n"

    with pytest.raises(preflight.PreflightError, match="expected a tag"):
        preflight.derive_version(compose, "Example/Odograph")


def test_unused_version_requires_explicit_404_for_every_resource(monkeypatch):
    monkeypatch.setattr(preflight, "_github_resource", lambda *args: (404, b"{}"))
    monkeypatch.setattr(preflight, "_registry_manifest_status", lambda *args: 404)

    result = preflight.check_remote_version_state(
        "Example/Odograph", "v1.2.3", "a" * 40, "unused", "token"
    )

    assert result == {
        "mode": "unused",
        "git_tag": 404,
        "github_release": 404,
        "ghcr_manifest": 404,
    }


def test_unused_version_rejects_mixed_absent_and_present_state(monkeypatch):
    def github_resource(_repository, path, _token):
        return (200 if path.startswith("git/ref") else 404), b"{}"

    monkeypatch.setattr(preflight, "_github_resource", github_resource)
    monkeypatch.setattr(preflight, "_registry_manifest_status", lambda *args: 404)

    with pytest.raises(preflight.PreflightError, match="git_tag=HTTP 200"):
        preflight.check_remote_version_state(
            "Example/Odograph", "v1.2.3", "a" * 40, "unused", "token"
        )


def test_remote_http_errors_fail_closed(monkeypatch):
    monkeypatch.setattr(
        preflight, "_http_request", lambda *args, **kwargs: (503, {}, b"unavailable")
    )

    with pytest.raises(preflight.PreflightError, match="HTTP 503"):
        preflight._github_resource("Example/Odograph", "git/ref/tags/v1.2.3", "token")


def test_published_version_requires_exact_tag_revision(monkeypatch):
    revision = "a" * 40

    def github_resource(_repository, path, _token):
        if path.startswith("releases/"):
            return 200, json.dumps({"tag_name": "v1.2.3"}).encode()
        if path.startswith("commits/"):
            return 200, json.dumps({"sha": revision}).encode()
        return 200, b"{}"

    monkeypatch.setattr(preflight, "_github_resource", github_resource)
    monkeypatch.setattr(preflight, "_registry_manifest_status", lambda *args: 200)

    result = preflight.check_remote_version_state(
        "Example/Odograph", "v1.2.3", revision, "published", "token"
    )

    assert result["tag_revision"] == revision


def test_published_version_rejects_a_different_tag_revision(monkeypatch):
    def github_resource(_repository, path, _token):
        if path.startswith("releases/"):
            return 200, b'{"tag_name":"v1.2.3"}'
        if path.startswith("commits/"):
            return 200, b'{"sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}'
        return 200, b"{}"

    monkeypatch.setattr(preflight, "_github_resource", github_resource)
    monkeypatch.setattr(preflight, "_registry_manifest_status", lambda *args: 200)

    with pytest.raises(preflight.PreflightError, match="expected aaaaa"):
        preflight.check_remote_version_state(
            "Example/Odograph", "v1.2.3", "a" * 40, "published", "token"
        )


def test_registry_authenticates_before_accepting_manifest_404(monkeypatch):
    responses = iter(
        (
            (
                401,
                {
                    "WWW-Authenticate": (
                        'Bearer realm="https://ghcr.io/token",service="ghcr.io",'
                        'scope="repository:example/odograph:pull"'
                    )
                },
                b"",
            ),
            (200, {}, b'{"token":"registry-token"}'),
            (404, {}, b""),
        )
    )
    monkeypatch.setattr(preflight, "_http_request", lambda *args, **kwargs: next(responses))

    assert preflight._registry_manifest_status("Example/Odograph", "v1.2.3") == 404


def test_workflow_is_read_only_and_skips_unchanged_compose_versions():
    workflow = load_workflow()

    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["push"]["branches"] == ["main", "release/**"]
    assert "pull_request" in workflow["on"]
    assert not workflow["on"]["pull_request"]
    assert "paths" not in workflow["on"]["push"]
    assert "workflow_dispatch" in workflow["on"]
    gate = steps_by_name(workflow["jobs"]["prepare"])[
        "Select and validate the exact release tree"
    ]["run"]
    assert 'current_version="$(python scripts/check_release_preflight.py version' in gate
    assert 'previous_version="$(python scripts/check_release_preflight.py version' in gate
    assert 'if [ "$current_version" = "$previous_version" ]' in gate
    assert "run-preflight=false" in gate

    source = WORKFLOW.read_text()
    assert "contents: write" not in source
    assert "packages: write" not in source
    assert "push: true" not in source
    assert "cosign sign " not in source
    assert "gh release create" not in source


def test_preflight_runs_full_policy_and_native_local_image_matrix():
    jobs = load_workflow()["jobs"]
    quality = jobs["quality"]
    quality_steps = steps_by_name(quality)

    assert quality["services"]["postgres"]["image"] == (
        "ghcr.io/meandrousshark/odograph-postgis@sha256:b352024dd6f9ca2ba0f1e7125dcfdcf78b824f2cbe86edf89559e1ddf4d80241"
    )
    assert quality_steps["Install pinned Gitleaks"]["env"]["GITLEAKS_VERSION"] == "8.30.1"
    assert "python -m pytest -q" in quality_steps["Run full locked test suite"]["run"]
    assert quality_steps["Audit locked Python dependencies"]["uses"] == (
        "pypa/gh-action-pip-audit@v1.1.0"
    )

    native = jobs["native-images"]
    matrix = native["strategy"]["matrix"]["include"]
    assert [(item["runner"], item["platform"]) for item in matrix] == [
        ("ubuntu-24.04", "linux/amd64"),
        ("ubuntu-24.04-arm", "linux/arm64"),
    ]
    steps = steps_by_name(native)
    build = steps["Build native local image archive"]
    assert build["uses"] == "docker/build-push-action@v7"
    assert build["with"]["outputs"].startswith("type=docker,dest=")
    assert "push" not in build["with"]
    scan = steps["Scan native image"]
    assert scan["uses"] == "aquasecurity/trivy-action@v0.36.0"
    assert scan["with"]["ignore-unfixed"] == "true"
    assert scan["with"]["severity"] == "HIGH,CRITICAL"
    assert scan["with"]["vuln-type"] == "os,library"
    assert steps["Generate native SPDX SBOM"]["uses"] == "anchore/sbom-action@v0.24.0"
    assert "release_preflight_smoke.sh" in steps["Smoke-test native application image"]["run"]
    assert not any(
        step.get("uses", "").startswith("docker/setup-qemu")
        for step in native["steps"]
    )
    postgis_scan = steps["Scan ${{ matrix.architecture }} PostGIS image"]
    assert postgis_scan["uses"] == "aquasecurity/trivy-action@v0.36.0"
    assert postgis_scan["env"]["TRIVY_PLATFORM"] == "${{ matrix.platform }}"
    assert postgis_scan["with"] == {
        "image-ref": "ghcr.io/meandrousshark/odograph-postgis@sha256:b352024dd6f9ca2ba0f1e7125dcfdcf78b824f2cbe86edf89559e1ddf4d80241",
        "format": "table",
        "output": "evidence/trivy-${{ matrix.architecture }}-postgis.txt",
        "exit-code": "1",
        "ignore-unfixed": "true",
        "vuln-type": "os,library",
        "severity": "HIGH,CRITICAL",
        "scanners": "vuln",
    }
    assert "trivyignores" not in postgis_scan["with"]
    smoke = steps["Smoke-test native application image"]
    assert smoke["env"]["ARCHITECTURE"] == "${{ matrix.architecture }}"
    assert smoke["env"]["POSTGIS_IMAGE"] == (
        "ghcr.io/meandrousshark/odograph-postgis@sha256:b352024dd6f9ca2ba0f1e7125dcfdcf78b824f2cbe86edf89559e1ddf4d80241"
    )
    assert '"linux/${ARCHITECTURE}"' in smoke["run"]


def test_preflight_and_published_drills_use_supported_base_and_exact_candidate():
    jobs = load_workflow()["jobs"]
    for job_name in ("source-upgrade-drill", "published-upgrade-drill"):
        job = jobs[job_name]
        python_setup = next(
            step for step in job["steps"]
            if step.get("uses") == "actions/setup-python@v6"
        )
        assert python_setup["with"]["python-version"] == "3.13"
        assert steps_by_name(job)["Install locked upgrade drill dependencies"]["run"] == (
            "python -m pip install -r requirements-dev.lock"
        )

    source_drill = steps_by_name(jobs["source-upgrade-drill"])[
        "Run v0.10.2 source upgrade and rollback drill"
    ]["run"]
    assert '--base v0.10.2 --candidate "$REVISION"' in source_drill
    assert "--database-image-migration" in source_drill

    published_drill = steps_by_name(jobs["published-upgrade-drill"])[
        "Run immutable-index upgrade and rollback drill"
    ]["run"]
    assert '--base v0.10.2 --candidate "$REVISION"' in published_drill
    assert '--candidate-image "$IMAGE@$INDEX_DIGEST"' in published_drill
    assert "--database-image-migration" in published_drill


def test_published_mode_verifies_exact_signing_identity_and_native_children():
    jobs = load_workflow()["jobs"]
    resolve = steps_by_name(jobs["resolve-published"])
    verify = resolve["Verify release signatures and SPDX attestations"]["run"]

    assert "/.github/workflows/release.yml@refs/tags/${VERSION}" in verify
    assert 'issuer="https://token.actions.githubusercontent.com"' in verify
    assert 'cosign verify --certificate-identity "$identity"' in verify
    assert "cosign verify-attestation --type spdxjson" in verify
    assert '.predicate.packages | type == "array" and length > 0' in verify

    published = jobs["published-native-smoke"]
    matrix = published["strategy"]["matrix"]["include"]
    assert [item["architecture"] for item in matrix] == ["amd64", "arm64"]
    smoke = steps_by_name(published)["Pull and verify the native published child"]["run"]
    assert 'published_image="$IMAGE@$DIGEST"' in smoke
    assert "release_preflight_smoke.sh" in smoke
    assert '"linux/${ARCHITECTURE}"' in smoke
    assert not any(
        step.get("uses", "").startswith("docker/setup-qemu")
        for step in published["steps"]
    )
