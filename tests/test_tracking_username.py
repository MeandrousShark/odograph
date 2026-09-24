"""Pure unit tests for the readable device-username slug. DB-backed
collision, rollback, and rotation behavior is covered in
`tests/test_tracking_ownership_db.py`.
"""
from __future__ import annotations

from app.tracking import slugify_label


def test_spaces_and_punctuation_collapse_to_single_dashes():
    assert slugify_label("My Phone!") == "my-phone"
    assert slugify_label("  --Multiple---Spaces!!  ") == "multiple-spaces"


def test_uppercase_is_lowercased():
    assert slugify_label("WORK IPHONE") == "work-iphone"


def test_non_ascii_characters_are_dropped_as_other():
    assert slugify_label("café phone") == "caf-phone"


def test_all_non_slug_characters_fall_back_to_device():
    assert slugify_label("\U0001F600\U0001F600\U0001F600") == "device"
    assert slugify_label("   ") == "device"
    assert slugify_label("") == "device"


def test_length_is_capped_with_trailing_dash_re_trimmed():
    label = "a" * 19 + " " + "b" * 5
    slug = slugify_label(label)
    assert len(slug) <= 20
    assert slug == "a" * 19
    assert not slug.endswith("-")


def test_length_cap_without_a_boundary_dash():
    slug = slugify_label("a" * 30)
    assert slug == "a" * 20


def test_no_leading_trailing_or_doubled_dashes():
    slug = slugify_label("!!!Weird---Label***Name???")
    assert not slug.startswith("-")
    assert not slug.endswith("-")
    assert "--" not in slug
    assert slug == "weird-label-name"
