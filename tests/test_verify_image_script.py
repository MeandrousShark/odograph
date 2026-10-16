"""scripts/verify_image.sh and the compose settings it depends on."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_image.sh"
COMPOSE = ROOT / "compose.yaml"
REPO = "ghcr.io/meandrousshark/odograph"
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
ISSUER = "https://token.actions.githubusercontent.com"

_FAKE_ENGINE = r"""#!/bin/sh
printf '%s %s\n' "${0##*/}" "$*" >> "$FAKE_LOG"
case "$1 $2" in
    "pull "*) exit "${FAKE_PULL_EXIT:-0}" ;;
    "image inspect") printf '%b' "$FAKE_INSPECT"; exit "${FAKE_INSPECT_EXIT:-0}" ;;
esac
exit 0
"""
_FAKE_COSIGN = r"""#!/bin/sh
printf 'cosign %s\n' "$*" >> "$FAKE_LOG"
exit "${FAKE_COSIGN_EXIT:-0}"
"""


def _tools(tmp_path: Path, *, cosign: bool = True, engines=("docker",)) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in engines:
        (bin_dir / name).write_text(_FAKE_ENGINE)
        (bin_dir / name).chmod(0o755)
    if cosign:
        (bin_dir / "cosign").write_text(_FAKE_COSIGN)
        (bin_dir / "cosign").chmod(0o755)
    for tool in ("awk", "cat"):
        (bin_dir / tool).symlink_to(shutil.which(tool))
    return bin_dir


def _install(tmp_path: Path, image: str | None) -> Path:
    install = tmp_path / "install"
    install.mkdir()
    text = COMPOSE.read_text()
    if image is not None:
        text = text.replace(
            next(line for line in text.splitlines() if line.strip().startswith("image: ghcr.io/meandrousshark/odograph:")),
            f"    image: {image}",
        )
    (install / "compose.yaml").write_text(text)
    return install


def _run(tmp_path, image, *args, cosign=True, engines=("docker",), env=None, inspect=None):
    bin_dir = _tools(tmp_path, cosign=cosign, engines=engines)
    install = _install(tmp_path, image)
    log = tmp_path / "log"
    log.touch()
    full_env = {
        "PATH": str(bin_dir),
        "FAKE_LOG": str(log),
        "FAKE_INSPECT": inspect if inspect is not None else f"{REPO}@{DIGEST}\\n",
        **(env or {}),
    }
    result = subprocess.run(
        [shutil.which("bash"), str(SCRIPT), *args],
        cwd=install, env=full_env, capture_output=True, text=True, timeout=30,
    )
    return result, log.read_text().splitlines()


def test_canonical_compose_app_never_pulls():
    app = yaml.safe_load(COMPOSE.read_text())["services"]["app"]

    assert app["pull_policy"] == "never"
    assert "build" not in app


def test_help_documents_usage_without_touching_anything(tmp_path):
    result, log = _run(tmp_path, None, "--help", cosign=False, engines=())

    assert result.returncode == 0
    assert "usage: scripts/verify_image.sh" in result.stderr
    assert log == []


def test_verifies_the_pulled_digest_with_the_exact_release_identity(tmp_path):
    result, log = _run(tmp_path, None)

    assert result.returncode == 0, result.stderr
    ref = yaml.safe_load(COMPOSE.read_text())["services"]["app"]["image"]
    version = ref.rsplit(":", 1)[1]
    assert log[0] == f"docker pull {ref}"
    assert log[1].startswith("docker image inspect")
    assert log[2] == (
        "cosign verify "
        f"--certificate-identity https://github.com/MeandrousShark/odograph/.github/workflows/release.yml@refs/tags/{version} "
        f"--certificate-oidc-issuer {ISSUER} {REPO}@{DIGEST}"
    )
    assert len(log) == 3
    assert f"verified: {REPO}@{DIGEST}" in result.stdout


def test_verifies_every_repository_digest_and_ignores_other_repositories(tmp_path):
    inspect = f"{REPO}@{DIGEST}\\nexample.test/other@{OTHER_DIGEST}\\n{REPO}@{OTHER_DIGEST}\\n"

    result, log = _run(tmp_path, f"{REPO}:v1.2.3", inspect=inspect)

    assert result.returncode == 0, result.stderr
    verified = [line.rsplit(" ", 1)[1] for line in log if line.startswith("cosign verify")]
    assert verified == [f"{REPO}@{DIGEST}", f"{REPO}@{OTHER_DIGEST}"]


def test_prerelease_tag_is_an_exact_release_tag(tmp_path):
    result, log = _run(tmp_path, f"{REPO}:v1.0.0-rc.1")

    assert result.returncode == 0, result.stderr
    assert "refs/tags/v1.0.0-rc.1 " in log[2]


@pytest.mark.parametrize(
    "image",
    [f"{REPO}:v1", f"{REPO}:v0.11", f"{REPO}:latest", f"{REPO}:1.2.3", f"{REPO}:v1.2.3.4", REPO],
)
def test_floating_or_missing_tags_are_refused_before_any_pull(tmp_path, image):
    result, log = _run(tmp_path, image)

    assert result.returncode != 0
    assert "exact release tag" in result.stderr
    assert log == []


@pytest.mark.parametrize(
    "image",
    ["example.test/odograph:v1.2.3", "ghcr.io/someone/odograph:v1.2.3", f"{REPO}-other@{DIGEST}"],
)
def test_other_image_repositories_are_refused(tmp_path, image):
    result, log = _run(tmp_path, image)

    assert result.returncode != 0
    assert "is not " + REPO in result.stderr
    assert log == []


def test_digest_pinned_image_requires_the_release_version(tmp_path):
    result, log = _run(tmp_path, f"{REPO}@{DIGEST}")

    assert result.returncode != 0
    assert "--version" in result.stderr
    assert log == []


def test_digest_pinned_image_is_verified_for_the_named_version_only(tmp_path):
    inspect = f"{REPO}@{OTHER_DIGEST}\\n{REPO}@{DIGEST}\\n"

    result, log = _run(tmp_path, f"{REPO}@{DIGEST}", "--version", "v1.2.3", inspect=inspect)

    assert result.returncode == 0, result.stderr
    verify = [line for line in log if line.startswith("cosign verify")]
    assert len(verify) == 1
    assert "refs/tags/v1.2.3 " in verify[0]
    assert verify[0].endswith(f"{REPO}@{DIGEST}")


def test_malformed_digest_and_mismatched_version_are_refused(tmp_path):
    result, log = _run(tmp_path, f"{REPO}@sha256:1234")
    assert result.returncode != 0 and log == []

    other = tmp_path / "second"
    other.mkdir()
    result, log = _run(other, f"{REPO}:v1.2.3", "--version", "v1.2.4")
    assert result.returncode != 0
    assert "does not match" in result.stderr
    assert log == []


def test_failed_signature_check_is_a_failure(tmp_path):
    result, _ = _run(tmp_path, f"{REPO}:v1.2.3", env={"FAKE_COSIGN_EXIT": "1"})

    assert result.returncode != 0
    assert "verification FAILED" in result.stderr
    assert "verified:" not in result.stdout


def test_pull_failure_and_missing_repository_digest_are_failures(tmp_path):
    result, log = _run(tmp_path, f"{REPO}:v1.2.3", env={"FAKE_PULL_EXIT": "1"})
    assert result.returncode != 0
    assert not any(line.startswith("cosign") for line in log)

    other = tmp_path / "second"
    other.mkdir()
    result, log = _run(other, f"{REPO}:v1.2.3", inspect="")
    assert result.returncode != 0
    assert "no " + REPO + " repository digest" in result.stderr
    assert not any(line.startswith("cosign") for line in log)


def test_missing_cosign_is_refused_before_any_pull(tmp_path):
    result, log = _run(tmp_path, f"{REPO}:v1.2.3", cosign=False)

    assert result.returncode != 0
    assert "cosign not found" in result.stderr
    assert log == []


def test_engine_falls_back_to_podman_and_can_be_overridden(tmp_path):
    result, log = _run(tmp_path, f"{REPO}:v1.2.3", engines=("podman",))
    assert result.returncode == 0, result.stderr
    assert log[0] == f"podman pull {REPO}:v1.2.3"

    other = tmp_path / "second"
    other.mkdir()
    result, log = _run(
        other, f"{REPO}:v1.2.3", engines=("docker", "podman"), env={"CONTAINER_RUNTIME": "podman"}
    )
    assert result.returncode == 0, result.stderr
    assert log[0] == f"podman pull {REPO}:v1.2.3"


def test_unknown_argument_is_a_usage_error(tmp_path):
    result, log = _run(tmp_path, None, "--bogus")

    assert result.returncode == 2
    assert log == []


@pytest.mark.parametrize("name", ["verify_image.sh", "upgrade_check.sh", "rehearse_upgrade.sh"])
def test_scripts_pass_shellcheck(name):
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck is not installed")

    result = subprocess.run(
        ["shellcheck", str(ROOT / "scripts" / name)], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stdout
    assert os.access(ROOT / "scripts" / name, os.X_OK)


def test_tag_plus_digest_reference_is_refused_with_an_accurate_message(tmp_path):
    result, log = _run(tmp_path, f"{REPO}:v1.2.3@{DIGEST}")

    assert result.returncode != 0
    assert "either a tag or a digest" in result.stderr
    assert log == []


def test_inspect_failure_is_a_failure(tmp_path):
    result, log = _run(tmp_path, f"{REPO}:v1.2.3", env={"FAKE_INSPECT_EXIT": "1"})

    assert result.returncode != 0
    assert "could not inspect" in result.stderr
    assert not any(line.startswith("cosign") for line in log)
