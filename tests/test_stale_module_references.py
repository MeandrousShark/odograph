"""Guards against comments/docstrings naming an app/**.py file that does not
exist. A module split (a single file becoming a package of the same name)
silently invalidates every path a comment mentions, and nothing else in the
suite notices: comments aren't imported, so a renamed or deleted module can
leave stale prose behind indefinitely. This is exactly what happened across
this tree twice before anyone caught it, which is the reason this test
exists.

Scans app/, tests/, scripts/, and migrations/ only. docs/ is deliberately
excluded: several files there are point-in-time records that intentionally
describe a layout as it was, and scanning them would mean maintaining an
allowlist of blessed historical mentions inside this test. Keeping the scan
to source and tooling means every match found here is a real path that
should exist, with no allowlist and no exceptions.
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("app", "tests", "scripts", "migrations")

# A repo-root-relative app/**.py path, e.g. app/ui/trips.py or app/main.py.
# The lookbehind stops this from matching the tail of some longer path
# (e.g. it won't fire on ".../vendor/app/foo.py").
PATH_PATTERN = re.compile(r"(?<![\w./-])app/[a-z0-9_]+(?:/[a-z0-9_]+)*\.py")


def _referenced_app_paths() -> dict[str, set[Path]]:
    """Map each matched path string to the files that mention it."""
    references: dict[str, set[Path]] = {}
    for scan_dir in SCAN_DIRS:
        for path in (ROOT / scan_dir).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            for match in PATH_PATTERN.finditer(text):
                references.setdefault(match.group(), set()).add(path)
    return references


def test_app_py_path_references_point_at_files_that_exist():
    """Every app/**.py path named in a comment, docstring, or string literal
    under app/, tests/, scripts/, or migrations/ must be a file that
    actually exists. This is what catches the failure mode a module split
    causes: a comment keeps naming the pre-split file forever because
    nothing re-checks it once written.
    """
    references = _referenced_app_paths()
    missing = {
        path_str: sorted(str(f.relative_to(ROOT)) for f in files)
        for path_str, files in references.items()
        if not (ROOT / path_str).is_file()
    }
    assert not missing, (
        "Found comments/strings referencing app/**.py paths that do not "
        f"exist on disk: {missing}"
    )
