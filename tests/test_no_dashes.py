import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHIPPING_DIRECTORIES = (".github/", "app/", "tests/", "static/", "migrations/", "scripts/")
SHIPPING_TOP_LEVEL_FILES = {
    "AGENTS.md",
    "CLAUDE.md",
    ".dockerignore",
    ".env.example",
    ".gitignore",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "Dockerfile",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "compose.build.override.yml",
    "compose.yaml",
    "pyproject.toml",
    "requirements-dev.lock",
    "requirements-dev.txt",
    "requirements.lock",
    "requirements.txt",
}
FORBIDDEN_DASHES = ("\u2013", "\u2014")


def _shipping_files() -> list[Path]:
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode().split("\0")
    return [
        ROOT / relative
        for relative in tracked
        if (relative in SHIPPING_TOP_LEVEL_FILES or relative.startswith(SHIPPING_DIRECTORIES))
        and (ROOT / relative).is_file()
    ]


def test_shipping_surfaces_contain_no_em_or_en_dashes():
    violations = []
    for path in _shipping_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if any(dash in line for dash in FORBIDDEN_DASHES):
                violations.append(f"{path.relative_to(ROOT)}:{line_number}: {line.strip()}")

    assert not violations, "Forbidden dash characters found:\n" + "\n".join(violations)
