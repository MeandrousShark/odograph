"""Every migration from 045 on declares its operator-impact class.

The first line of each such file is `-- migration-class: <class>` so release
notes can state what an upgrade does to existing data. Migrations up to and
including LAST_UNCLASSIFIED_MIGRATION predate the convention and are exempt.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
LAST_UNCLASSIFIED_MIGRATION = 44
MIGRATION_CLASSES = ("additive", "rewrite", "destructive")
HEADER_RE = re.compile(
    r"^-- migration-class: (" + "|".join(MIGRATION_CLASSES) + r")$"
)
NUMBER_RE = re.compile(r"^(\d+)_")


def migration_class_problem(path: Path) -> str | None:
    """Return a failure message for a migration file, or None if it is fine."""
    match = NUMBER_RE.match(path.name)
    if match is None:
        return f"{path.name}: filename does not start with a numeric NNN_ prefix"
    if int(match.group(1)) <= LAST_UNCLASSIFIED_MIGRATION:
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    first = lines[0] if lines else ""
    if HEADER_RE.match(first):
        return None
    allowed = ", ".join(MIGRATION_CLASSES)
    return (
        f"{path.name}: the first line must be '-- migration-class: <class>' "
        f"with <class> one of: {allowed}; found {first!r}"
    )


def test_every_new_migration_declares_a_class():
    problems = [
        problem
        for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
        if (problem := migration_class_problem(path)) is not None
    ]
    assert not problems, "\n".join(problems)


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("migration_class", MIGRATION_CLASSES)
def test_valid_header_passes(tmp_path, migration_class):
    path = _write(
        tmp_path,
        "045_example.sql",
        f"-- migration-class: {migration_class}\nSELECT 1;\n",
    )
    assert migration_class_problem(path) is None


def test_missing_header_fails(tmp_path):
    path = _write(tmp_path, "045_example.sql", "-- Adds a table.\nSELECT 1;\n")
    problem = migration_class_problem(path)
    assert problem is not None
    assert "045_example.sql" in problem
    for migration_class in MIGRATION_CLASSES:
        assert migration_class in problem


def test_empty_file_fails(tmp_path):
    path = _write(tmp_path, "045_example.sql", "")
    assert migration_class_problem(path) is not None


@pytest.mark.parametrize(
    "line",
    [
        "-- migration-class: cosmetic",
        "-- migration-class:",
        "-- migration-class: additive rewrite",
        "-- migration-class: Additive",
        "--migration-class: additive",
        "-- migration-class: additive  ",
    ],
)
def test_unknown_or_malformed_class_fails(tmp_path, line):
    path = _write(tmp_path, "046_example.sql", f"{line}\nSELECT 1;\n")
    assert migration_class_problem(path) is not None


def test_header_not_on_first_line_fails(tmp_path):
    path = _write(
        tmp_path,
        "045_example.sql",
        "-- A leading comment.\n-- migration-class: additive\nSELECT 1;\n",
    )
    assert migration_class_problem(path) is not None


def test_exempt_old_migration_needs_no_header(tmp_path):
    path = _write(tmp_path, "044_example.sql", "SELECT 1;\n")
    assert migration_class_problem(path) is None


def test_cutoff_is_inclusive_of_the_last_unclassified_number(tmp_path):
    exempt = _write(
        tmp_path, f"{LAST_UNCLASSIFIED_MIGRATION:03d}_a.sql", "SELECT 1;\n"
    )
    required = _write(
        tmp_path, f"{LAST_UNCLASSIFIED_MIGRATION + 1:03d}_b.sql", "SELECT 1;\n"
    )
    assert migration_class_problem(exempt) is None
    assert migration_class_problem(required) is not None
