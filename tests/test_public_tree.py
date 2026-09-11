from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_public_tree.py"
spec = importlib.util.spec_from_file_location("public_tree", SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q")
    for relative in checker.PUBLIC_TOP_LEVEL | checker.PUBLIC_DOCS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Public fixture\n")
    (root / ".gitignore").write_text(".venv/\n/.env\n")
    git(root, "add", ".")
    return root


def run(root, env=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        env=env, capture_output=True, text=True, timeout=130,
    )


def write(root, relative, content):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_real_scanner_accepts_public_tree_without_git_changes(source):
    assert shutil.which("gitleaks"), "Gitleaks is required for the public-tree integration tests"
    before = git(source, "status", "--porcelain=v1", "-z")
    result = run(source)
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(source, "status", "--porcelain=v1", "-z") == before


@pytest.mark.parametrize("relative", [
    "docs/notes.md", "notes.md", "tests/test_handoff_contract.py", "app/.claude/settings.json",
    "scripts/.codex/settings.json", "tests/.agents/state.json", "app/AGENTS.md", "app/.env",
    "scripts/.env.local", "scripts/.devsite/state.json",
])
def test_untracked_disallowed_paths_are_rejected(source, relative):
    write(source, relative, "fixture")
    result = run(source)
    assert result.returncode == 1
    assert "outside the public inventory" in result.stdout


def test_tracked_ignored_files_are_still_rejected(source):
    write(source, ".env", "fixture")
    git(source, "add", "-f", ".env")
    assert run(source).returncode == 1


def test_ignored_local_data_and_git_metadata_are_not_scanned(source):
    marker = "AK" + "IAABCDEFGHIJKLMNOP"
    write(source, ".git/leak-marker.txt", marker)
    write(source, ".venv/fixture.txt", marker)
    write(source, ".env", marker)
    assert run(source).returncode == 0


@pytest.mark.parametrize("content", [
    "docs/HAND" + "OFF.md", "DES" + "IGN.md", "PH" + "ASE3.md",
    "PUBLIC" + "-M1.md", "(" + "M6)", "W" + "6",
    "sap" + "poro", "miles." + "hannoncloud.com", "git." + "hannoncloud.com",
])
def test_private_references_in_public_content_are_rejected_and_redacted(source, content):
    write(source, "app/fixture.txt", content)
    result = run(source)
    assert result.returncode == 1
    assert "private reference or hostname" in result.stdout
    assert content not in result.stdout + result.stderr


def test_public_agent_files_are_scanned(source):
    write(source, "AGENTS.md", "docs/HAND" + "OFF.md")
    assert run(source).returncode == 1


@pytest.mark.parametrize("relative", [str(Path("app") / "check_public_tree.py"), "scripts/check_public_tree.py"])
def test_checker_filename_does_not_exempt_content(source, relative):
    write(source, relative, "docs/HAND" + "OFF.md")
    assert run(source).returncode == 1


def test_required_public_document_cannot_disappear(source):
    (source / "docs/usage.md").unlink()
    result = run(source)
    assert result.returncode == 1
    assert "required public file is missing" in result.stdout


def test_symlink_is_rejected(source, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (source / "app").mkdir()
    (source / "app/link.txt").symlink_to(outside)
    result = run(source)
    assert result.returncode == 1
    assert "symlinks are not allowed" in result.stdout


def test_real_scanner_rejects_secret_without_echoing_it(source):
    marker = "AK" + "IAABCDEFGHIJKLMNOP"
    write(source, "app/fixture.txt", marker)
    result = run(source)
    assert result.returncode == 1
    assert "Gitleaks rejected" in result.stdout
    assert marker not in result.stdout + result.stderr


def test_missing_scanner_fails_closed(source, monkeypatch, capsys):
    monkeypatch.setattr(checker.shutil, "which", lambda name: None)
    assert not checker.scan_secrets(source, checker.source_files(source))
    assert "Gitleaks is required" in capsys.readouterr().out


def test_scanner_failure_and_configuration_cannot_silently_bypass_scan(source, monkeypatch, capsys):
    monkeypatch.setenv("GITLEAKS_CONFIG", "/untrusted/config")
    monkeypatch.setenv("GITLEAKS_CONFIG_TOML", "untrusted")
    monkeypatch.setattr(checker.shutil, "which", lambda name: "/scanner")
    paths = checker.source_files(source)

    def failed_scan(args, **kwargs):
        assert args[:2] == ["/scanner", "dir"]
        assert "--redact=100" in args
        assert "--ignore-gitleaks-allow" in args
        assert not any(key.startswith("GITLEAKS_") for key in kwargs["env"])
        assert not (kwargs["cwd"] / ".git").exists()
        return subprocess.CompletedProcess(args, 2, b"sensitive output", b"sensitive output")

    monkeypatch.setattr(checker.subprocess, "run", failed_scan)
    assert not checker.scan_secrets(source, paths)
    assert "sensitive output" not in capsys.readouterr().out


def test_checkout_public_inventory_matches_doc_validation():
    from test_public_docs import PUBLIC_FILES, PUBLIC_DIRECTORIES

    assert checker.PUBLIC_TOP_LEVEL | checker.PUBLIC_DOCS == PUBLIC_FILES
    assert checker.PUBLIC_DIRECTORIES == set(PUBLIC_DIRECTORIES)


@pytest.mark.parametrize("relative", [str(Path("app") / "fixture.py"), "docs/usage.md", "app/fixture.png"])
def test_invalid_utf8_cannot_hide_private_content_in_source(source, relative):
    path = source / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff" + b"sap" + b"poro")
    result = run(source)
    assert result.returncode == 1
    assert "must be valid UTF-8" in result.stdout
    assert "sap" + "poro" not in result.stdout + result.stderr


def test_binary_assets_still_reject_private_hostnames(source):
    path = source / "static/fixture.png"
    path.parent.mkdir()
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"sap" + b"poro")
    result = run(source)
    assert result.returncode == 1
    assert "private reference or hostname" in result.stdout


def test_existing_png_assets_pass_validation(source):
    relative = "docs/images/usage-dashboard.png"
    shutil.copyfile(ROOT / relative, source / relative)
    assert run(source).returncode == 0
