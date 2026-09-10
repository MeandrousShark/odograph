"""Portable data export/import: a versioned JSON snapshot of the ledger core
(vehicles, places, tag_rules, mileage_rates, trips, expenses,
odometer_readings, and the app_settings singleton) that lets an operator move
data between instances instead of the Postgres schema being the only way out.
Route/path geometry, raw points, and trip_boundary_overrides are deliberately
out of scope for this bundle format. Cross-schema import is refused except
for explicitly enumerated additive transitions that the importer can fill
without losing data.

`build_export_bundle` is pure -- already-fetched DB rows in, a JSON-shaped
dict out, no DB/IO -- so it's unit-testable the same way `build_export_rows`
is in `app/export.py`. `normalize_bundle` is pure too: it validates an
uploaded bundle's shape and in-bundle references without touching the
database, so a malformed upload is rejected before any query runs. Import
itself needs the database (id remapping, the clean-target precondition), and
that split between pure and DB-facing code is what this package is organized
around: format.py holds the bundle constants every other module here shares,
export.py the pure shaping functions above plus the DB fetch helpers that
feed them, normalize.py the pure validation, importer.py the DB-facing
validation and insert path, and routes.py the two HTTP routes that tie
fetch, shape, validate, and insert together.

Every row another row can reference (vehicles, places, trips) carries a
bundle-local `$id` -- the source row's real database id, reused only as a
cross-reference key inside the file. Import never reuses a source id: it
inserts fresh rows and builds its own `$id -> new id` map per table, rewriting
every reference through that map before the dependent rows are inserted.
That's what makes import safe against a target whose sequences are at
different values than the source.
"""
from __future__ import annotations

# Re-exports: keeps `from app.portable import ...` working for callers and
# tests after this package split (this package was a single module until
# these names moved one level down).
from app.portable.export import build_export_bundle  # noqa: F401
from app.portable.format import (  # noqa: F401
    FORMAT,
    FORMAT_VERSION,
    SEEDED_TAG_RULES,
    SEEDED_VEHICLE,
)
from app.portable.importer import _tag_rule_sort_key  # noqa: F401
from app.portable.normalize import normalize_bundle  # noqa: F401
from app.portable.routes import make_router  # noqa: F401

__all__ = [
    "FORMAT",
    "FORMAT_VERSION",
    "build_export_bundle",
    "normalize_bundle",
    "make_router",
    "SEEDED_VEHICLE",
    "SEEDED_TAG_RULES",
]
