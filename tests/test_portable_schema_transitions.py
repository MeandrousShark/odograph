"""The importer's explicit compatibility table must cover the current schema.

Each migration needs its own target block in
`app/portable/importer.py`'s `_COMPATIBLE_SCHEMA_TRANSITIONS`; without one,
every portable import into an upgraded instance is refused. A migration that
does change the bundle must change this test deliberately.
"""
from __future__ import annotations

from app.db import MIGRATIONS_DIR
from app.portable.importer import _COMPATIBLE_SCHEMA_TRANSITIONS

LATEST = max(int(path.name.split("_", 1)[0]) for path in MIGRATIONS_DIR.glob("*.sql"))


def _accepted(target: int) -> set[tuple[int, int, int]]:
    return {t for t in _COMPATIBLE_SCHEMA_TRANSITIONS if t[2] == target}


def test_current_schema_accepts_every_compatible_bundle():
    expected = {(1, 21, LATEST)} | {(2, s, LATEST) for s in range(22, 26)} | {
        (3, s, LATEST) for s in range(26, LATEST + 1)}
    assert _accepted(LATEST) == expected


def test_no_target_beyond_the_current_schema():
    assert max(t[2] for t in _COMPATIBLE_SCHEMA_TRANSITIONS) == LATEST
