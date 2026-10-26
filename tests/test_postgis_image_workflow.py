import os
import subprocess
from pathlib import Path

import pytest
import yaml
from workflow_actions import is_pinned, pinned_names


pytestmark = pytest.mark.ops

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "postgis-image.yml"
DOCKERFILE = ROOT / "docker" / "postgis" / "Dockerfile"


def load_workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def steps_by_name(job: dict) -> dict:
    return {step.get("name"): step for step in job["steps"] if step.get("name")}


def test_workflow_only_builds_for_scoped_changes_and_serializes_refs():
    workflow = load_workflow()
    # Pushes to main publish the image, so only changes to the image itself
    # may trigger a publish. Smoke script and test edits are checked on PRs.
    push_paths = [
        "docker/postgis/**",
        ".github/workflows/postgis-image.yml",
    ]
    pull_request_paths = [
        *push_paths,
        "scripts/db_image_identity.sh",
        "scripts/release_preflight_smoke.sh",
        "tests/test_postgis_image_workflow.py",
    ]

    assert workflow["on"]["push"] == {"branches": ["main"], "paths": push_paths}
    assert workflow["on"]["pull_request"] == {"paths": pull_request_paths}
    assert workflow["on"]["workflow_dispatch"] == ""
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "postgis-image-${{ github.ref }}",
        "cancel-in-progress": "false",
    }


def test_dockerfile_pins_base_toolchain_and_verifies_gosu_source():
    source = DOCKERFILE.read_text()

    assert (
        "docker.io/nickblah/postgis:16-bookworm-postgis-3.6.4@"
        "sha256:de90689b3a56831db2b4d520a65b8acee01581d14060749a17b50026c8e37d70"
    ) in source
    assert (
        "docker.io/library/golang:1.27.3-bookworm@"
        "sha256:972b4110ba0f47539a975c31e52c64765e1ff19f2d74469ae4a595c6676e80a5"
    ) in source
    assert "GOSU_COMMIT=6456aaa0f3c854d199d0f037f068eb97515b7513" in source
    assert "git fetch --depth 1 origin \"$GOSU_COMMIT\"" in source
    assert 'test "$(git rev-parse HEAD)" = "$GOSU_COMMIT"' in source
    assert "go mod download" in source
    assert "go mod verify" in source
    assert "ENV CGO_ENABLED=0" in source
    assert "go build -trimpath -buildvcs=false" in source
    assert "-ldflags" not in source
    assert "gosu --version" in source
    assert "gosu nobody true" in source
    assert "apt-get update" in source
    assert "apt-get upgrade -y --no-install-recommends" in source
    assert "rm -rf /var/lib/apt/lists/*" in source
    assert "ARG GIT_REVISION=unknown" in source
    assert "org.opencontainers.image.source" in source
    assert "org.opencontainers.image.revision" in source


@pytest.mark.parametrize(
    ("job_name", "platform", "runner", "arch"),
    [
        ("build-amd64", "linux/amd64", "ubuntu-24.04", "amd64"),
        ("build-arm64", "linux/arm64", "ubuntu-24.04-arm", "arm64"),
    ],
)
def test_native_build_jobs_build_scan_and_test_the_same_archive(
    job_name: str, platform: str, runner: str, arch: str
):
    job = load_workflow()["jobs"][job_name]
    steps = steps_by_name(job)
    build = steps[f"Build {arch} archive"]
    verify = steps[f"Load and verify {arch} archive"]
    scan = steps[f"Scan {arch} archive"]
    identity = steps[f"Probe {arch} database identity"]
    database = steps[f"Test fresh {arch} PostgreSQL"]
    upload = steps[f"Upload {arch} archive"]

    assert job["runs-on"] == runner
    assert job["permissions"] == {"contents": "read"}
    assert not any("setup-qemu" in step.get("uses", "") for step in job["steps"])
    names = [step.get("name") for step in job["steps"]]
    assert names.index(f"Load and verify {arch} archive") < names.index(
        f"Probe {arch} database identity"
    )
    assert job["outputs"] == {"identity": "${{ steps.identity.outputs.identity }}"}
    assert identity["id"] == "identity"
    assert 'CONTAINER_RUNTIME=docker scripts/db_image_identity.sh "$LOCAL_IMAGE"' in identity["run"]
    assert f"evidence/identity-{arch}.txt" in identity["run"]
    assert 'echo "identity=$identity" >> "$GITHUB_OUTPUT"' in identity["run"]
    assert "GITHUB_STEP_SUMMARY" in identity["run"]
    native = steps[f"Verify native {arch} runner"]
    expected_runner_arch = "X64" if arch == "amd64" else "ARM64"
    expected_uname = "x86_64" if arch == "amd64" else "aarch64"
    assert f'test "$RUNNER_ARCH" = {expected_runner_arch}' in native["run"]
    assert f'test "$(uname -m)" = {expected_uname}' in native["run"]
    assert f"--platform {platform}" in build["run"]
    assert "--pull" in build["run"]
    assert "--no-cache" in build["run"]
    assert "--provenance=false" in build["run"]
    assert "--build-arg GIT_REVISION=\"$GITHUB_SHA\"" in build["run"]
    assert "--output \"type=docker,dest=$RUNNER_TEMP/$ARCHIVE\"" in build["run"]
    assert "docker load --input" in verify["run"]
    assert f'EXPECTED_ARCH: {arch}' in WORKFLOW.read_text()
    assert "org.opencontainers.image.source" in verify["run"]
    assert "org.opencontainers.image.revision" in verify["run"]

    assert is_pinned(scan["uses"], "aquasecurity/trivy-action")
    assert scan["with"] == {
        "image-ref": f"odograph-postgis:test-${{{{ github.sha }}}}-{arch}",
        "format": "table",
        "exit-code": "1",
        "ignore-unfixed": "true",
        "vuln-type": "os,library",
        "severity": "HIGH,CRITICAL",
        "scanners": "vuln",
        "output": f"evidence/trivy-{arch}.txt",
    }
    assert "trivyignores" not in scan["with"]

    assert "pg_isready" in database["run"]
    assert "CREATE EXTENSION postgis;" in database["run"]
    assert "CREATE EXTENSION postgis_topology;" in database["run"]
    assert "CREATE EXTENSION postgis_tiger_geocoder;" in database["run"]
    assert "ST_Transform" in database["run"]
    assert "trap cleanup EXIT" in database["run"]
    assert "pg_isready -h 127.0.0.1" in database["run"]
    assert "docker rm -fv" in database["run"]
    smoke = steps[f"Smoke test v0.11.0 application on {arch} PostGIS"]
    assert "docker pull --platform" in smoke["run"]
    assert "ghcr.io/meandrousshark/odograph@sha256:" in smoke["run"]
    assert "POSTGIS_IMAGE=\"$LOCAL_IMAGE\"" in smoke["run"]
    assert "scripts/release_preflight_smoke.sh" in smoke["run"]
    evidence = steps[f"Upload {arch} scan and smoke evidence"]
    assert evidence["if"] == "always()"
    assert is_pinned(evidence["uses"], "actions/upload-artifact")
    assert evidence["with"]["path"] == "evidence/"
    assert is_pinned(upload["uses"], "actions/upload-artifact")
    assert (
        upload["with"]["name"]
        == f"postgis-image-${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}-linux-{arch}"
    )
    assert upload["with"]["path"] == f"${{{{ runner.temp }}}}/postgis-image-linux-{arch}.tar"


def test_publish_is_main_only_and_consumes_exact_archives_without_rebuilding():
    workflow = load_workflow()
    publish = workflow["jobs"]["publish"]
    source = WORKFLOW.read_text()
    steps = steps_by_name(publish)
    names = [step.get("name") for step in publish["steps"]]

    assert "github.event_name == 'push'" in publish["if"]
    assert "github.ref == 'refs/heads/main'" in publish["if"]
    assert "github.event_name == 'workflow_dispatch'" in publish["if"]
    assert publish["needs"] == ["build-amd64", "build-arm64", "identity"]
    assert publish["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
    }
    assert source.count("docker/setup-qemu") == 0
    assert "docker/build-push-action" not in source

    for arch in ("amd64", "arm64"):
        download = next(
            step
            for step in publish["steps"]
            if is_pinned(step.get("uses"), "actions/download-artifact")
            and f"linux-{arch}" in step["with"]["name"]
        )
        assert download["with"]["name"] == (
            f"postgis-image-${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}-linux-{arch}"
        )

    absent = steps["Refuse to replace an existing commit tag"]["run"]
    assert "docker buildx imagetools inspect" in absent
    assert "Could not establish that image tag is absent" in absent
    push = steps["Push tested architecture images"]["run"]
    assert "docker load" in steps["Load exact tested archives"]["run"]
    assert "docker push \"$IMAGE:$TAG-amd64\"" in push
    assert "docker push \"$IMAGE:$TAG-arm64\"" in push
    assert "imagetools inspect" in push

    manifest = steps["Publish two-architecture index"]["run"]
    assert "imagetools create" in manifest
    assert "linux/amd64" in manifest
    assert "linux/arm64" in manifest
    assert "manifest_digest" in manifest
    assert steps["Publish two-architecture index"]["env"]["IDENTITY"] == (
        "${{ needs.identity.outputs.identity }}"
    )
    assert "database identity: \\`$IDENTITY\\`" in manifest
    assert names.index("Push tested architecture images") < names.index(
        "Publish two-architecture index"
    )


_IDENTITY = "postgresql=16.15 postgis=3.6.4 geos=3.11.1 proj=9.8.1 glibc=2.36 icu=72.1"


def _run_identity_comparison(tmp_path: Path, amd64: str, arm64: str):
    job = load_workflow()["jobs"]["identity"]
    step = steps_by_name(job)["Require one database identity on both architectures"]
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        capture_output=True,
        text=True,
        timeout=10,
        env={
            **os.environ,
            "AMD64_IDENTITY": amd64,
            "ARM64_IDENTITY": arm64,
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
    )
    return result, output.read_text() if output.exists() else ""


def test_identity_job_runs_on_every_event_before_publish():
    job = load_workflow()["jobs"]["identity"]

    assert "if" not in job
    assert job["needs"] == ["build-amd64", "build-arm64"]
    assert job["permissions"] == {"contents": "read"}
    assert job["outputs"] == {"identity": "${{ steps.compare.outputs.identity }}"}
    step = steps_by_name(job)["Require one database identity on both architectures"]
    assert step["env"] == {
        "AMD64_IDENTITY": "${{ needs.build-amd64.outputs.identity }}",
        "ARM64_IDENTITY": "${{ needs.build-arm64.outputs.identity }}",
    }


def test_identity_job_records_an_identity_both_architectures_share(tmp_path):
    result, output = _run_identity_comparison(tmp_path, _IDENTITY, _IDENTITY)

    assert result.returncode == 0, result.stderr
    assert output == f"identity={_IDENTITY}\n"
    assert _IDENTITY in (tmp_path / "summary").read_text()


@pytest.mark.parametrize(
    ("amd64", "arm64", "message"),
    [
        (_IDENTITY, _IDENTITY.replace("proj=9.8.1", "proj=9.8.2"), "different database identities"),
        (_IDENTITY, "", "no complete database identity"),
        ("", "", "no complete database identity"),
        (_IDENTITY.replace(" icu=72.1", ""), _IDENTITY.replace(" icu=72.1", ""), "no complete database identity"),
    ],
)
def test_identity_job_refuses_differing_or_incomplete_identities(tmp_path, amd64, arm64, message):
    result, output = _run_identity_comparison(tmp_path, amd64, arm64)

    assert result.returncode != 0
    assert message in result.stderr
    assert output == ""


def test_publish_attaches_per_child_sboms_and_signs_every_digest():
    publish = load_workflow()["jobs"]["publish"]
    steps = steps_by_name(publish)
    names = [step.get("name") for step in publish["steps"]]

    for arch in ("amd64", "arm64"):
        sbom = steps[f"Generate linux/{arch} SPDX SBOM"]
        assert is_pinned(sbom["uses"], "anchore/sbom-action")
        assert sbom["with"]["image"] == (
            f"${{{{ env.IMAGE }}}}@${{{{ steps.children.outputs.{arch}_digest }}}}"
        )
        assert sbom["with"]["format"] == "spdx-json"
        assert sbom["with"]["output-file"] == f"sbom-linux-{arch}.spdx.json"
        assert sbom["with"]["upload-artifact"] == "false"
        assert sbom["with"]["upload-release-assets"] == "false"

    attest = steps["Attest each architecture SPDX SBOM"]["run"]
    assert "cosign attest --yes --type spdxjson" in attest
    assert "sbom-linux-amd64.spdx.json" in attest
    assert "sbom-linux-arm64.spdx.json" in attest

    sign = steps["Sign index and architecture images"]["run"]
    assert 'cosign sign --yes "$IMAGE@$MANIFEST_DIGEST"' in sign
    assert 'cosign sign --yes "$IMAGE@$AMD64_DIGEST"' in sign
    assert 'cosign sign --yes "$IMAGE@$ARM64_DIGEST"' in sign
    assert names.index("Publish two-architecture index") < names.index(
        "Generate linux/amd64 SPDX SBOM"
    )
    assert names.index("Install cosign") < names.index(
        "Attest each architecture SPDX SBOM"
    )


def test_workflow_uses_versioned_actions_and_never_passes_token_to_build_jobs():
    workflow = load_workflow()
    expected = {
        "actions/checkout",
        "actions/download-artifact",
        "actions/upload-artifact",
        "anchore/sbom-action",
        "aquasecurity/trivy-action",
        "docker/login-action",
        "docker/setup-buildx-action",
        "sigstore/cosign-installer",
    }
    used = {
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    }
    assert pinned_names(used) == expected
    for job_name in ("build-amd64", "build-arm64"):
        text = "\n".join(
            step.get("run", "") for step in workflow["jobs"][job_name]["steps"]
        )
        assert "GITHUB_TOKEN" not in text
        assert "secrets." not in text
