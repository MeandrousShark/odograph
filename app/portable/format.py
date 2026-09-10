"""Bundle format constants: the format/version tags `normalize_bundle` checks
on the way in and `build_export_bundle` stamps on the way out, the enums a
bundle's category/kind/source fields are restricted to, and the exact rows a
fresh migration seeds (`_check_clean_target` in importer.py compares a
target's vehicles/tag_rules against these, not just table counts, so an
edited default is refused rather than silently accepted).
"""
from __future__ import annotations

FORMAT = "odograph-portable"
# Version 2 is the version that carries fields optional in the bundle and
# absent from a version-1 export, not a version tied to any one of them:
# trip_exclusion is the first, with more expected to land in this same
# version 2 without a further bump. normalize_bundle accepts both 1 and 2 so
# a pre-existing v1 backup still imports, with every version-2-only field
# treated as absent (falling back to its default) on a bundle that predates
# it.
FORMAT_VERSION = 2

TRIP_SOURCES = ("detected", "manual")
TRIP_CATEGORIES = ("unclassified", "business", "personal")
TRIP_EXCLUSIONS = ("not_my_vehicle", "not_deductible")
TAG_RULE_CATEGORIES = ("business", "personal")
TAG_SOURCES = ("human", "rule")

# Must match app.ui._common.TRIP_LABEL_MAX_LENGTH, which in turn matches the
# database's char_length cap on trips.start_label/end_label
# (migrations/025_manual_trip_labels.sql). Defined separately rather than
# imported: app.ui is a FastAPI router package pulling in auth, page
# rendering, and the rest of the authenticated web app, which would make this
# pure bundle validator (see normalize.py's module docstring) depend on all
# of it just to read one integer.
TRIP_LABEL_MAX_LENGTH = 100

# 008_vehicles.sql / 003_places.sql seed these exact rows on every fresh
# migration. The clean-target precondition compares against this content
# (not just table counts), so a target with e.g. an edited default tag_rule
# is refused rather than silently accepted.
SEEDED_VEHICLE = {
    "name": "My Car", "make": None, "model": None, "plate": None,
    "is_default": True, "active": True,
}
SEEDED_TAG_RULES = (
    {"a_place": None, "a_kind": "home", "b_place": None, "b_kind": "work", "category": "personal"},
    {"a_place": None, "a_kind": "work", "b_place": None, "b_kind": "work", "category": "business"},
)
