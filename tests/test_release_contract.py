from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_release_contract.py"


def _stage_contract(tmp_path: Path, readme: str | None = None) -> Path:
    (tmp_path / "scripts").mkdir()
    for name in ("check_release_contract.py", "release_notes.py"):
        shutil.copy2(ROOT / "scripts" / name, tmp_path / "scripts" / name)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [1.2.3] - 2026-08-11\n\nA release.\n"
    )
    (tmp_path / "compose.yaml").write_text(
        "services:\n  app:\n    image: ghcr.io/example/odograph:v1.2.3\n"
    )
    (tmp_path / "README.md").write_text(readme or (
        "Run `git checkout vX.Y.Z`; Compose uses the immutable image tag "
        "pinned by the checked-out release.\n"
    ))
    (tmp_path / "Dockerfile").write_text(
        "ARG VERSION=dev\nENV APP_VERSION=$VERSION\n"
    )
    return tmp_path


def _run(root: Path, tag: str = "v1.2.3") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "python3",
            str(root / "scripts" / "check_release_contract.py"),
            "--root",
            str(root),
            "--tag",
            tag,
            "--image",
            "ghcr.io/example/odograph",
        ],
        capture_output=True,
        text=True,
    )


def test_release_contract_accepts_matching_version_surfaces(tmp_path):
    root = _stage_contract(tmp_path)

    result = _run(root)

    assert result.returncode == 0, result.stderr
    assert "ghcr.io/example/odograph:v1.2.3" in result.stdout


def test_release_contract_accepts_a_pinned_clone_install_command(tmp_path):
    root = _stage_contract(
        tmp_path,
        "Run `git clone --branch vX.Y.Z --depth 1 "
        "https://github.com/MeandrousShark/odograph.git`; Compose uses the "
        "immutable image tag pinned by the checked-out release.\n",
    )

    result = _run(root)

    assert result.returncode == 0, result.stderr


def test_actual_readme_has_a_supported_release_install_contract(tmp_path):
    root = _stage_contract(tmp_path, (ROOT / "README.md").read_text())

    result = _run(root)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "readme",
    [
        "Install the application from the repository.\n",
        "Run `git clone https://github.com/MeandrousShark/odograph.git`.\n",
        "Run `git clone --branch v0.10 --depth 1 "
        "https://github.com/MeandrousShark/odograph.git`.\n",
        "Run `git clone --branch main --depth 1 "
        "https://github.com/MeandrousShark/odograph.git`.\n",
        "Run `git clone --branch develop --depth 1 "
        "https://github.com/MeandrousShark/odograph.git`.\n",
    ],
)
def test_release_contract_rejects_missing_or_moving_install_commands(tmp_path, readme):
    root = _stage_contract(
        tmp_path,
        readme + "Compose uses the immutable image tag pinned by the checked-out release.\n",
    )

    result = _run(root)

    assert result.returncode != 0
    assert "release install contract" in result.stderr


def test_release_contract_rejects_a_mismatched_compose_image(tmp_path):
    root = _stage_contract(tmp_path)
    (root / "compose.yaml").write_text(
        "services:\n  app:\n    image: ghcr.io/example/odograph:v1.2.2\n"
    )

    result = _run(root)

    assert result.returncode != 0
    assert "expected 'ghcr.io/example/odograph:v1.2.3'" in result.stderr


def test_release_contract_rejects_a_missing_changelog_version(tmp_path):
    root = _stage_contract(tmp_path)

    result = _run(root, tag="v1.2.4")

    assert result.returncode != 0
    assert "no exact section for 1.2.4" in result.stderr
