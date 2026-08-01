import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_notes.py"
SPEC = importlib.util.spec_from_file_location("release_notes", SCRIPT)
assert SPEC and SPEC.loader
release_notes = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_notes
SPEC.loader.exec_module(release_notes)


CHANGELOG = """\
# Changelog

## [Unreleased]

- Work in progress.

## [0.6.0-rc.2] - 2026-07-21

### Added

- Tagged release artifacts.
- **Security scan acceptance (pip-audit):** `GHSA-abcd-1234-wxyz` — No patched dependency release exists; exposure is not reachable in this deployment.
- **Security scan acceptance (pip-audit):** `PYSEC-2026-42` — The affected optional code path is not installed.
- **Security scan acceptance (pip-audit):** `CVE-2026-12345` — The vulnerable extra is not enabled.
- **Security scan acceptance (Trivy linux/amd64):** `CVE-2026-22222` — The vulnerable binary is not invoked by the application.
- **Security scan acceptance (Trivy all):** `CVE-2026-33333` — The base-image vendor fix is pending and the service is not exposed.

## [0.5.1] - 2026-07-01

- **Security scan acceptance (Trivy linux/arm64):** `CVE-2026-99999` — Applies only to the older release.
"""


def test_extracts_exact_tag_notes_and_scoped_acceptances():
    result = release_notes.extract_release_notes(CHANGELOG, "v0.6.0-rc.2")

    assert result.body.startswith("### Added\n")
    assert "0.5.1" not in result.body
    assert result.pip_audit_ignores == (
        "GHSA-abcd-1234-wxyz",
        "PYSEC-2026-42",
        "CVE-2026-12345",
    )
    assert result.trivy_ignores("linux/amd64") == (
        "CVE-2026-22222",
        "CVE-2026-33333",
    )
    assert result.trivy_ignores("linux/arm64") == ("CVE-2026-33333",)


def test_no_acceptances_keeps_every_gate_fully_blocking():
    changelog = "# Changelog\n\n## [0.6.0] - 2026-07-21\n\n- Initial release.\n"

    result = release_notes.extract_release_notes(changelog, "v0.6.0")

    assert result.pip_audit_ignores == ()
    assert result.trivy_ignores("linux/amd64") == ()
    assert result.trivy_ignores("linux/arm64") == ()


@pytest.mark.parametrize(
    "line",
    [
        "- **Security scan acceptance (pip-audit):** `CVE-2026-12345` —",
        "- **Security scan acceptance (Trivy):** `CVE-2026-12345` — A reason.",
        "- **Security scan acceptance (Trivy linux/s390x):** `CVE-2026-12345` — A reason.",
        "- **Security scan acceptance (pip-audit):** `NOT-A-NATIVE-ID` — A reason.",
    ],
)
def test_malformed_acceptances_fail_closed(line):
    changelog = f"# Changelog\n\n## [0.6.0]\n\n{line}\n"

    with pytest.raises(release_notes.ReleaseNotesError, match="scan acceptance"):
        release_notes.extract_release_notes(changelog, "v0.6.0")


def test_acceptance_from_wrong_version_is_not_applied():
    result = release_notes.extract_release_notes(CHANGELOG, "v0.5.1")

    assert result.pip_audit_ignores == ()
    assert result.trivy_ignores("linux/amd64") == ()
    assert result.trivy_ignores("linux/arm64") == ("CVE-2026-99999",)


@pytest.mark.parametrize("tag", ["0.6.0", "vnext", "v0.6"])
def test_non_semver_tags_are_rejected(tag):
    with pytest.raises(release_notes.ReleaseNotesError, match="semantic-version"):
        release_notes.extract_release_notes(CHANGELOG, tag)


def test_cli_writes_release_notes_and_scanner_ignore_files(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG)
    notes = tmp_path / "notes.md"
    pip_ignore = tmp_path / "pip.ignore"
    amd64_ignore = tmp_path / "amd64.ignore"
    arm64_ignore = tmp_path / "arm64.ignore"

    subprocess.run(
        [
            sys.executable,
            SCRIPT,
            "--tag",
            "v0.6.0-rc.2",
            "--changelog",
            changelog,
            "--notes-output",
            notes,
            "--pip-audit-output",
            pip_ignore,
            "--trivy-amd64-output",
            amd64_ignore,
            "--trivy-arm64-output",
            arm64_ignore,
        ],
        check=True,
        cwd=ROOT,
    )

    assert notes.read_text().startswith("### Added\n")
    assert pip_ignore.read_text().splitlines() == [
        "GHSA-abcd-1234-wxyz",
        "PYSEC-2026-42",
        "CVE-2026-12345",
    ]
    assert amd64_ignore.read_text().splitlines() == [
        "CVE-2026-22222",
        "CVE-2026-33333",
    ]
    assert arm64_ignore.read_text().splitlines() == ["CVE-2026-33333"]
