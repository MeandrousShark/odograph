from pathlib import Path

import yaml
from workflow_actions import is_pinned, pinned_names


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def load_workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def steps_by_name(job: dict) -> dict:
    return {step.get("name"): step for step in job["steps"] if step.get("name")}


def test_release_is_tag_only_repo_scoped_and_serialized_per_tag():
    workflow = load_workflow()

    assert workflow["on"] == {"push": {"tags": ["v*"]}}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "release-${{ github.ref }}",
        "cancel-in-progress": "false",
    }

    prepare = workflow["jobs"]["prepare"]
    assert prepare["steps"][0]["with"]["ref"] == "${{ github.sha }}"
    assert prepare["steps"][0]["with"]["fetch-depth"] == "0"
    validate = steps_by_name(prepare)["Validate tagged release inputs"]
    assert validate["env"]["REPOSITORY"] == "${{ github.repository }}"
    assert 'image="ghcr.io/${REPOSITORY,,}"' in validate["run"]
    assert "git rev-parse HEAD" in validate["run"]
    assert "scripts/check_release_contract.py" in validate["run"]
    assert '--tag "$VERSION" --image "$image"' in validate["run"]
    assert "gh release view" in steps_by_name(prepare)[
        "Refuse to replace an existing GitHub release"
    ]["run"]

    source = WORKFLOW.read_text()
    assert "ghcr.io/meandrousshark/odograph:" not in source.lower()
    assert source.count("ghcr.io/meandrousshark/odograph-postgis@sha256:4dc09eaaabbde791dee120e3fdb5f32cfd7d6c707a08cd38c1f9c7fe16b66982") == 3
    assert ":latest" not in source


def test_release_test_gate_runs_full_postgis_suite_and_pip_audit():
    test = load_workflow()["jobs"]["test"]

    assert test["needs"] == "prepare"
    assert test["env"]["TEST_DATABASE_URL"].startswith("postgresql://")
    postgres = test["services"]["postgres"]
    assert postgres["image"] == (
        "ghcr.io/meandrousshark/odograph-postgis@sha256:4dc09eaaabbde791dee120e3fdb5f32cfd7d6c707a08cd38c1f9c7fe16b66982"
    )
    assert "pg_isready" in postgres["options"]

    steps = steps_by_name(test)
    setup_python = next(
        step for step in test["steps"] if is_pinned(step.get("uses"), "actions/setup-python")
    )
    assert setup_python["with"]["cache-dependency-path"] == "requirements-dev.lock"
    assert steps["Install test dependencies"]["run"] == (
        "python -m pip install -r requirements-dev.lock"
    )
    install = steps["Install Gitleaks"]
    assert install["env"] == {
        "GITLEAKS_VERSION": "8.30.1",
        "GITLEAKS_SHA256": "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    }
    assert "gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}" in install["run"]
    assert "sha256sum --check -" in install["run"]
    assert 'echo "$install_dir" >> "$GITHUB_PATH"' in install["run"]
    assert test["steps"].index(install) < test["steps"].index(steps["Run full test suite"])
    assert steps["Run full test suite"]["run"] == "python -m pytest"
    assert steps["Check shell script syntax"]["run"] == (
        "git ls-files -z -- '*.sh' | xargs -0 -n1 bash -n"
    )
    assert steps["Read tag-scoped pip-audit acceptances"]["env"]["VERSION"] == (
        "${{ needs.prepare.outputs.version }}"
    )
    audit = steps["Audit locked Python dependencies"]
    assert is_pinned(audit["uses"], "pypa/gh-action-pip-audit")
    assert audit["with"] == {
        "inputs": "requirements.lock",
        "no-deps": "true",
        "ignore-vulns": "${{ steps.pip_acceptances.outputs.ids }}",
    }


def test_each_architecture_is_built_from_tagged_tree_and_scanned_by_digest():
    jobs = load_workflow()["jobs"]
    expectations = {
        "build-amd64": ("linux/amd64", "trivy-amd64.ignore", None),
        "build-arm64": ("linux/arm64", "trivy-arm64.ignore", "arm64"),
    }

    for job_name, (platform, ignore_file, qemu_platform) in expectations.items():
        job = jobs[job_name]
        assert job["needs"] == ["prepare", "test"]
        assert job["permissions"] == {"contents": "read", "packages": "write"}
        assert job["outputs"]["digest"] == "${{ steps.build.outputs.digest }}"

        checkout = dict(job["steps"][0])
        assert is_pinned(checkout.pop("uses"), "actions/checkout")
        assert checkout == {
            "with": {
                "ref": "${{ needs.prepare.outputs.revision }}",
                "persist-credentials": "false",
            },
        }
        steps = steps_by_name(job)
        build_name = f"Build and push untagged {platform.removeprefix('linux/')} image"
        build = steps[build_name]
        assert is_pinned(build["uses"], "docker/build-push-action")
        assert build["with"]["context"] == "."
        assert build["with"]["platforms"] == platform
        assert "VERSION=${{ needs.prepare.outputs.version }}" in build["with"]["build-args"]
        assert "GIT_REVISION=${{ needs.prepare.outputs.revision }}" in build["with"]["build-args"]
        assert build["with"]["labels"] == (
            "org.opencontainers.image.source=https://github.com/${{ github.repository }}"
        )
        assert build["with"]["outputs"] == (
            "type=image,name=${{ needs.prepare.outputs.image }},"
            "push-by-digest=true,name-canonical=true,push=true"
        )
        assert build["with"]["provenance"] == "false"
        assert build["with"]["sbom"] == "false"

        scan = steps[f"Scan {platform.removeprefix('linux/')} image"]
        assert is_pinned(scan["uses"], "aquasecurity/trivy-action")
        assert scan["with"]["image-ref"].endswith("@${{ steps.build.outputs.digest }}")
        assert scan["with"]["exit-code"] == "1"
        assert scan["with"]["ignore-unfixed"] == "true"
        assert scan["with"]["severity"] == "HIGH,CRITICAL"
        assert scan["with"]["trivyignores"] == ignore_file
        assert scan["with"]["scanners"] == "vuln"

        postgis_scan = steps[
            f"Scan {platform.removeprefix('linux/')} PostGIS image"
        ]
        assert is_pinned(postgis_scan["uses"], "aquasecurity/trivy-action")
        assert postgis_scan["env"] == {"TRIVY_PLATFORM": platform}
        assert postgis_scan["with"] == {
            "image-ref": "ghcr.io/meandrousshark/odograph-postgis@sha256:4dc09eaaabbde791dee120e3fdb5f32cfd7d6c707a08cd38c1f9c7fe16b66982",
            "format": "table",
            "exit-code": "1",
            "ignore-unfixed": "true",
            "vuln-type": "os,library",
            "severity": "HIGH,CRITICAL",
            "scanners": "vuln",
        }
        assert "trivyignores" not in postgis_scan["with"]

        qemu_steps = [step for step in job["steps"] if step.get("uses", "").startswith("docker/setup-qemu")]
        if qemu_platform:
            assert len(qemu_steps) == 1
            qemu = dict(qemu_steps[0])
            assert is_pinned(qemu.pop("uses"), "docker/setup-qemu-action")
            assert qemu == {"with": {"platforms": qemu_platform}}
        else:
            assert qemu_steps == []


def test_publish_happens_only_after_both_scan_gates_and_emits_all_digests():
    publish = load_workflow()["jobs"]["publish"]

    assert publish["needs"] == ["prepare", "build-amd64", "build-arm64"]
    assert publish["permissions"] == {
        "contents": "write",
        "packages": "write",
        "id-token": "write",
    }
    steps = steps_by_name(publish)
    assert "repos/$REPOSITORY/commits/$VERSION" in steps[
        "Verify tag still identifies the built revision"
    ]["run"]
    assert "imagetools inspect" in steps["Refuse to replace an existing image tag"]["run"]

    verify = steps["Verify architecture manifests"]["run"]
    assert 'architecture == "amd64"' in verify
    assert 'architecture == "arm64"' in verify
    manifest = steps["Publish multi-architecture manifest"]["run"]
    assert "docker buildx imagetools create" in manifest
    assert '"$IMAGE@$AMD64_DIGEST"' in manifest
    assert '"$IMAGE@$ARM64_DIGEST"' in manifest
    assert '["linux/amd64", "linux/arm64"]' in manifest
    assert "manifest_digest" in manifest
    assert "AMD64_DIGEST" in manifest
    assert "ARM64_DIGEST" in manifest

    release = steps["Create GitHub release from changelog"]["run"]
    assert "python scripts/release_notes.py" in release
    assert "--verify-tag" in release
    assert "--notes-file release-notes.md" in release
    assert "--prerelease" in release


def test_publish_generates_and_attests_a_per_architecture_sbom():
    publish = load_workflow()["jobs"]["publish"]
    steps = steps_by_name(publish)

    amd64_sbom = steps["Generate linux/amd64 SBOM"]
    assert is_pinned(amd64_sbom["uses"], "anchore/sbom-action")
    assert amd64_sbom["with"]["image"] == (
        "${{ needs.prepare.outputs.image }}@${{ needs.build-amd64.outputs.digest }}"
    )
    assert amd64_sbom["with"]["format"] == "spdx-json"
    assert amd64_sbom["with"]["output-file"] == "sbom-linux-amd64.spdx.json"
    assert amd64_sbom["with"]["upload-artifact"] == "false"
    assert amd64_sbom["with"]["upload-release-assets"] == "false"

    arm64_sbom = steps["Generate linux/arm64 SBOM"]
    assert is_pinned(arm64_sbom["uses"], "anchore/sbom-action")
    assert arm64_sbom["with"]["image"] == (
        "${{ needs.prepare.outputs.image }}@${{ needs.build-arm64.outputs.digest }}"
    )
    assert arm64_sbom["with"]["output-file"] == "sbom-linux-arm64.spdx.json"

    attest = steps["Attest each architecture's SBOM"]["run"]
    assert "cosign attest" in attest
    assert "--type spdxjson" in attest
    assert "--predicate sbom-linux-amd64.spdx.json" in attest
    assert '"$IMAGE@$AMD64_DIGEST"' in attest
    assert "--predicate sbom-linux-arm64.spdx.json" in attest
    assert '"$IMAGE@$ARM64_DIGEST"' in attest

    install_cosign_index = [step.get("name") for step in publish["steps"]].index(
        "Install cosign"
    )
    assert is_pinned(
        publish["steps"][install_cosign_index]["uses"], "sigstore/cosign-installer"
    )
    attest_index = [step.get("name") for step in publish["steps"]].index(
        "Attest each architecture's SBOM"
    )
    assert install_cosign_index < attest_index


def test_publish_signs_the_manifest_and_each_architecture_digest():
    publish = load_workflow()["jobs"]["publish"]
    steps = steps_by_name(publish)

    sign = steps["Sign the manifest and each architecture digest"]
    assert sign["env"]["MANIFEST_DIGEST"] == "${{ steps.manifest.outputs.digest }}"
    run = sign["run"]
    assert 'cosign sign --yes "$IMAGE@$MANIFEST_DIGEST"' in run
    assert 'cosign sign --yes "$IMAGE@$AMD64_DIGEST"' in run
    assert 'cosign sign --yes "$IMAGE@$ARM64_DIGEST"' in run

    step_names = [step.get("name") for step in publish["steps"]]
    assert step_names.index("Publish multi-architecture manifest") < step_names.index(
        "Sign the manifest and each architecture digest"
    )
    assert step_names.index(
        "Sign the manifest and each architecture digest"
    ) < step_names.index("Create GitHub release from changelog")


def test_external_actions_are_reputable_versioned_releases():
    expected = {
        "actions/checkout",
        "actions/setup-python",
        "docker/login-action",
        "docker/setup-qemu-action",
        "docker/setup-buildx-action",
        "docker/build-push-action",
        "pypa/gh-action-pip-audit",
        "aquasecurity/trivy-action",
        "anchore/sbom-action",
        "sigstore/cosign-installer",
    }
    used = {
        step["uses"]
        for job in load_workflow()["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    }
    assert pinned_names(used) == expected


def test_release_attaches_the_rendered_trial_asset_after_signing():
    publish = load_workflow()["jobs"]["publish"]
    steps = steps_by_name(publish)
    names = [step.get("name") for step in publish["steps"]]
    render = steps["Render the trial compose asset"]

    assert render["env"]["MANIFEST_DIGEST"] == "${{ steps.manifest.outputs.digest }}"
    assert "--output" in render["run"] and "--verify" in render["run"]
    assert render["run"].count('--digest "$MANIFEST_DIGEST"') == 2
    assert render["run"].count("--source compose.try.yaml") == 2
    assert '"$RUNNER_TEMP/compose.try.yaml"' in steps["Create GitHub release from changelog"]["run"]
    assert names.index("Sign the manifest and each architecture digest") < names.index(
        "Render the trial compose asset"
    ) < names.index("Create GitHub release from changelog") < names.index(
        "Verify the attached trial compose asset"
    )
    verify = steps["Verify the attached trial compose asset"]
    assert "gh release download" in verify["run"]
    assert "--verify" in verify["run"]
    assert verify["env"]["MANIFEST_DIGEST"] == "${{ steps.manifest.outputs.digest }}"
    assert '--digest "$MANIFEST_DIGEST"' in verify["run"]
    assert "--source compose.try.yaml" in verify["run"]
    assert "continue-on-error" not in verify
    assert "continue-on-error" not in render
    checkout = next(step for step in publish["steps"] if "actions/checkout" in step.get("uses", ""))
    assert checkout["with"]["ref"] == "${{ needs.prepare.outputs.revision }}"
