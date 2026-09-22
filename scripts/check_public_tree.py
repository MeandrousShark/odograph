#!/usr/bin/env python3
"""Validate public source paths and content without changing the checkout."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


PUBLIC_DIRECTORIES = frozenset({".github", "app", "tests", "static", "migrations", "scripts"})
PUBLIC_TOP_LEVEL = frozenset({
    "AGENTS.md", "CLAUDE.md", "README.md", "LICENSE", "SECURITY.md",
    "CONTRIBUTING.md", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md", "compose.yaml",
    "compose.build.override.yml", "Dockerfile", ".dockerignore", ".env.example",
    ".gitignore", "requirements.txt", "requirements-dev.txt",
    "requirements-dev.lock", "requirements.lock", "pyproject.toml",
})
PUBLIC_DOCS = frozenset({
    "docs/backups.md", "docs/configuration.md", "docs/install-compose.md",
    "docs/known-issues.md", "docs/osrm.md", "docs/owntracks.md", "docs/privacy.md", "docs/releasing.md",
    "docs/reverse-proxy.md", "docs/security.md", "docs/upgrading.md", "docs/usage.md",
    "docs/images/usage-dashboard.png", "docs/images/usage-review.png",
})
PRIVATE_FILES = frozenset({"tests/test_handoff_contract.py"})
LOCAL_STATE = frozenset({".git", ".claude", ".codex", ".agents", ".venv", "__pycache__", ".pytest_cache", ".devsite"})
# These patterns retain the established private-reference boundary. Root agent
# instructions are public contributor documentation and receive the same scan.
PRIVATE_REFERENCE = re.compile(
    r"docs/DES" r"IGN|docs/HAND" r"OFF|docs/PH" r"ASE|docs/PUB" r"LIC|\(M[0-9]+\)|\bW[0-9]+\b|"
    r"HANDOFF\.md|DESIGN\.md|PHASE[0-9]|PUBLIC\.md|PUBLIC" r"-M"
)
PRIVATE_HOSTNAME = re.compile(r"sap" r"poro|miles\.hannoncloud\.com|git\.hannoncloud\.com", re.IGNORECASE)


def source_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root, check=True, capture_output=True,
    )
    return sorted(set(os.fsdecode(result.stdout).split("\0")) - {""})


def validate(root: Path, paths: list[str]) -> list[str]:
    errors = []
    present = set()
    for relative in paths:
        path = root / relative
        parts = Path(relative).parts
        if path.is_symlink():
            errors.append(f"{relative!r}: symlinks are not allowed")
            continue
        if not path.exists():
            # A tracked deletion is absent from the proposed working tree.
            continue
        present.add(relative)
        if (
            relative in PRIVATE_FILES
            or any(part.lower() in LOCAL_STATE for part in parts)
            or any(part.lower() in {"agents.md", "claude.md"} for part in parts[1:])
            or (relative != ".env.example" and any(part.lower() == ".env" or part.lower().startswith(".env.") for part in parts))
            or not (relative in PUBLIC_TOP_LEVEL or relative in PUBLIC_DOCS or parts[0] in PUBLIC_DIRECTORIES)
        ):
            errors.append(f"{relative!r}: path is outside the public inventory")
            continue
        if not path.is_file():
            errors.append(f"{relative!r}: expected a regular source file")
            continue
        data = path.read_bytes()
        binary_asset = False
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            binary_asset = (
                path.suffix.lower() == ".png"
                and (parts[0] == "static" or relative in PUBLIC_DOCS)
                and (
                    data.startswith(b"\x89PNG\r\n\x1a\n")
                    # This existing screenshot has a legacy PNG filename.
                    or (relative == "docs/images/usage-dashboard.png" and data.startswith(b"\xff\xd8\xff"))
                )
            )
            if not binary_asset:
                errors.append(f"{relative!r}: source file must be valid UTF-8")
                continue
            # Compressed bytes can resemble short milestone markers. Keep the
            # hostname check and secret scan for supported binary assets.
            content = data.decode("utf-8", errors="replace")
        for line_number, line in enumerate(content.splitlines(), 1):
            if PRIVATE_HOSTNAME.search(line) or (not binary_asset and PRIVATE_REFERENCE.search(line)):
                errors.append(f"{relative!r}:{line_number}: private reference or hostname")
    for relative in sorted((PUBLIC_TOP_LEVEL | PUBLIC_DOCS) - present):
        errors.append(f"{relative!r}: required public file is missing")
    return errors


def scan_secrets(root: Path, paths: list[str]) -> bool:
    scanner = shutil.which("gitleaks")
    if scanner is None:
        print("error: Gitleaks is required on PATH")
        return False
    with tempfile.TemporaryDirectory(prefix="odograph-public-tree-") as temporary:
        target = Path(temporary)
        for relative in paths:
            source = root / relative
            if source.is_file():
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
        # Repository or shell configuration must not disable default rules.
        env = {key: value for key, value in os.environ.items() if not key.startswith("GITLEAKS_")}
        result = subprocess.run(
            [scanner, "dir", str(target), "--redact=100", "--no-banner",
             "--log-level", "error", "--ignore-gitleaks-allow"],
            cwd=target, env=env, capture_output=True, timeout=120,
        )
        if result.returncode:
            print("error: Gitleaks rejected the public tree or could not complete; scanner output withheld")
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        paths = source_files(root)
        errors = validate(root, paths)
        if errors:
            for error in errors:
                print(f"error: {error}")
            return 1
        if not scan_secrets(root, paths):
            return 1
    except (OSError, subprocess.SubprocessError):
        print("error: public-tree validation could not complete")
        return 1
    print("Public tree passed inventory, private-reference and Gitleaks checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
