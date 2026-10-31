from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_release_contract.py"


def _try_compose(
    db: str = "registry.example.test/db@sha256:0",
    app: str = "ghcr.io/example/odograph:v1.2.3",
    seed: str = "ghcr.io/example/odograph:v1.2.3",
) -> str:
    return (
        f"services:\n  db:\n    image: {db}\n  app:\n    image: {app}\n"
        f"  seed:\n    image: {seed}\n"
    )


def _stage_contract(tmp_path: Path, readme: str | None = None) -> Path:
    (tmp_path / "scripts").mkdir()
    for name in ("check_release_contract.py", "release_notes.py"):
        shutil.copy2(ROOT / "scripts" / name, tmp_path / "scripts" / name)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [1.2.3] - 2026-08-11\n\nA release.\n"
    )
    (tmp_path / "compose.yaml").write_text(
        "services:\n  db:\n    image: registry.example.test/db@sha256:0\n"
        "  app:\n    image: ghcr.io/example/odograph:v1.2.3\n    pull_policy: never\n"
    )
    (tmp_path / "compose.try.yaml").write_text(_try_compose())
    (tmp_path / "README.md").write_text(readme or (
        "Run `git checkout vX.Y.Z`; Compose uses the exact release tag "
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
        "exact release tag pinned by the checked-out release.\n",
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
        readme + "Compose uses the exact release tag pinned by the checked-out release.\n",
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


def test_release_contract_requires_the_try_file(tmp_path):
    root = _stage_contract(tmp_path)
    (root / "compose.try.yaml").unlink()

    result = _run(root)

    assert result.returncode != 0
    assert "compose.try.yaml is missing" in result.stderr


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"app": "ghcr.io/example/odograph:v1.2.2"}, "compose.try.yaml app image"),
        ({"seed": "ghcr.io/example/odograph:v1.2.2"}, "compose.try.yaml seed image"),
        ({"db": "registry.example.test/db@sha256:1"}, "compose.try.yaml db image"),
    ],
)
def test_release_contract_rejects_a_stale_try_file(tmp_path, override, message):
    root = _stage_contract(tmp_path)
    (root / "compose.try.yaml").write_text(_try_compose(**override))

    result = _run(root)

    assert result.returncode != 0
    assert message in result.stderr


def test_release_contract_rejects_a_try_file_without_a_service(tmp_path):
    root = _stage_contract(tmp_path)
    (root / "compose.try.yaml").write_text(
        "services:\n  app:\n    image: ghcr.io/example/odograph:v1.2.3\n"
    )

    result = _run(root)

    assert result.returncode != 0
    assert "compose.try.yaml has no seed image" in result.stderr


def test_release_contract_rejects_a_missing_changelog_version(tmp_path):
    root = _stage_contract(tmp_path)

    result = _run(root, tag="v1.2.4")

    assert result.returncode != 0
    assert "no exact section for 1.2.4" in result.stderr


def _compose(db_image: str) -> str:
    return (
        "services:\n"
        f"  db:\n    image: {db_image}\n"
        "  app:\n    image: ghcr.io/example/odograph:v1.2.3\n"
    )


def _stage_db_image_release(
    tmp_path: Path, previous_db: str, current_db: str, notes: str, *, tag_previous: bool = True
) -> Path:
    root = _stage_contract(tmp_path)
    (root / "compose.yaml").write_text(_compose(previous_db))
    git = ["git", "-C", str(root), "-c", "user.name=Release Test", "-c", "user.email=release@example.test"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "previous"], check=True)
    if tag_previous:
        subprocess.run([*git, "tag", "v1.2.2"], check=True)
    (root / "compose.yaml").write_text(_compose(current_db))
    (root / "compose.try.yaml").write_text(_try_compose(db=current_db))
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [1.2.3] - 2026-08-11\n\n"
        f"{notes}\n\n## [1.2.2-rc.1] - 2026-08-01\n\nA candidate.\n\n"
        "## [1.2.2] - 2026-08-01\n\nThe previous release.\n"
    )
    return root


@pytest.mark.parametrize(
    "notes",
    [
        "- The database image was rebuilt with unchanged versions and upgrades in place.",
        "- The database image was\n  Rebuilt With unchanged\n  versions.",
        "- Every installation upgrades through the database-image migration.",
    ],
)
def test_release_contract_accepts_a_db_image_change_that_names_its_kind(tmp_path, notes):
    root = _stage_db_image_release(tmp_path, "registry.example.test/db@sha256:" + "1" * 64,
                                   "registry.example.test/db@sha256:" + "2" * 64, notes)

    result = _run(root)

    assert result.returncode == 0, result.stderr


def test_release_contract_rejects_a_db_image_change_without_its_kind(tmp_path):
    root = _stage_db_image_release(tmp_path, "registry.example.test/db@sha256:" + "1" * 64,
                                   "registry.example.test/db@sha256:" + "2" * 64,
                                   "- The database image moved to a new digest.")

    result = _run(root)

    assert result.returncode != 0
    assert "the db image changed since v1.2.2" in result.stderr
    assert "'rebuilt with unchanged versions'" in result.stderr
    assert "'database-image migration'" in result.stderr


def test_release_contract_needs_no_statement_for_an_unchanged_db_image(tmp_path):
    image = "registry.example.test/db@sha256:" + "1" * 64
    root = _stage_db_image_release(tmp_path, image, image, "- A release.")

    result = _run(root)

    assert result.returncode == 0, result.stderr


def test_release_contract_fails_closed_without_the_previous_release_tag(tmp_path):
    image = "registry.example.test/db@sha256:" + "1" * 64
    root = _stage_db_image_release(tmp_path, image, image, "- A release.", tag_previous=False)

    result = _run(root)

    assert result.returncode != 0
    assert "cannot read compose.yaml at the previous release tag v1.2.2" in result.stderr
