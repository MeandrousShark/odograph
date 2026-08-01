"""Tests for the shared trip-endpoint label fallback. Proves the `address`
fallback works without reverse-geocoded data, so the seam is verified
independently of reverse geocoding actually landing.
"""
from __future__ import annotations

from app.places_desc import describe_compact_endpoint, describe_endpoint


def test_named_place_wins_over_everything():
    assert describe_endpoint("Home", 47.6, -122.3, "123 Main St") == "Home"


def test_address_used_when_name_absent():
    assert describe_endpoint(None, 47.6, -122.3, "123 Main St") == "123 Main St"


def test_coord_fallback_when_no_name_or_address():
    assert describe_endpoint(None, 47.6, -122.3, None) == "47.6000,-122.3000"


def test_dash_when_nothing_available():
    assert describe_endpoint(None, None, None, None) == "—"


def test_compact_named_place_wins_without_truncating_its_comma():
    assert describe_compact_endpoint("Office, downtown", 47.6, -122.3, "123 Main St, Seattle") == (
        "Office, downtown"
    )


def test_compact_address_uses_street_before_first_comma():
    assert describe_compact_endpoint(None, 47.6, -122.3, "123 Main St, Seattle, WA") == (
        "123 Main St"
    )


def test_compact_address_without_comma_is_unchanged():
    assert describe_compact_endpoint(None, 47.6, -122.3, "Rural Route 7") == "Rural Route 7"


def test_compact_malformed_address_does_not_hide_coordinate_or_address():
    assert describe_compact_endpoint(None, 47.6, -122.3, ", Seattle") == ", Seattle"


def test_compact_coordinate_and_dash_fallbacks_match_full_description():
    assert describe_compact_endpoint(None, 47.6, -122.3) == "47.6000,-122.3000"
    assert describe_compact_endpoint(None, None, None) == "—"
