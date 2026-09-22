"""Tests for normalize_bundle, the portable import bundle's pure
validation/normalization function. No database needed -- that's the point
of the function being pure -- so these are plain unit tests with no
TEST_DATABASE_URL skip marker and no pool.
"""
from __future__ import annotations

import pytest

from app.portable import FORMAT, FORMAT_VERSION, normalize_bundle

BASE_VEHICLE = {
    "$id": 1, "name": "Car", "make": None, "model": None, "plate": None,
    "is_default": True, "active": True,
}
BASE_PLACE = {
    "$id": 1, "name": "Home", "kind": "home", "lat": 47.6, "lon": -122.3, "radius_m": 150.0,
}
BASE_TRIP = {
    "$id": 1, "device": "phone1", "source": "manual",
    "started_at": "2026-06-15T15:00:00+00:00", "ended_at": "2026-06-15T15:30:00+00:00",
    "distance_m": 1000.0, "has_gap": False, "category": "business",
    "purpose": "Meet client", "notes": "client visit",
    "vehicle": 1, "start_place": 1, "end_place": None, "tag_source": "human",
}


def _bundle(**overrides) -> dict:
    bundle = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "schema_version": 19,
        "exported_at": "2026-08-05T00:00:00+00:00",
        "vehicles": [dict(BASE_VEHICLE)],
        "places": [dict(BASE_PLACE)],
        "tag_rules": [],
        "mileage_rates": [],
        "trips": [],
        "expenses": [],
        "odometer_readings": [],
        "settings": {"auto_assign_default_vehicle": False, "display_tz": "Asia/Tokyo"},
    }
    bundle.update(overrides)
    return bundle


def test_minimal_valid_bundle_with_exactly_one_default_vehicle_is_accepted():
    normalized, issues = normalize_bundle(_bundle())
    assert issues == []
    assert normalized is not None
    assert normalized["vehicles"][0]["is_default"] is True


def test_fully_populated_valid_bundle_is_accepted():
    bundle = _bundle(
        tag_rules=[
            {"a_place": 1, "a_kind": None, "b_place": None, "b_kind": "work", "category": "business"},
        ],
        mileage_rates=[
            {"year": 2026, "rate_per_mi": 0.7, "rate_h2_per_mi": None, "h2_start_month": None},
        ],
        trips=[dict(BASE_TRIP)],
        expenses=[{
            "vehicle": 1, "incurred_on": "2026-06-01", "category": "fuel",
            "amount": "45.67", "treatment": "business_use_allocated", "notes": "receipt",
        }],
        odometer_readings=[{
            "vehicle": 1, "recorded_at": "2026-01-01T00:00:00+00:00",
            "odometer_m": 1000.0, "note": "new year",
        }],
    )
    normalized, issues = normalize_bundle(bundle)
    assert issues == []
    assert normalized is not None
    assert len(normalized["trips"]) == 1
    assert len(normalized["expenses"]) == 1
    assert len(normalized["odometer_readings"]) == 1


def test_expense_trip_reference_must_match_a_bundle_trip_id():
    bundle = _bundle(
        trips=[dict(BASE_TRIP)],
        expenses=[{
            "vehicle": 1, "trip": 1, "incurred_on": "2026-06-01", "category": "fuel",
            "amount": "45.67", "treatment": "business_use_allocated", "notes": None,
        }],
    )
    normalized, issues = normalize_bundle(bundle)
    assert issues == []
    assert normalized["expenses"][0]["trip"] == 1

    bundle["expenses"][0]["trip"] = 999
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("trip references unknown trip" in issue for issue in issues)


def test_version_one_expense_without_trip_remains_unlinked():
    bundle = _bundle(format_version=1, trips=[], expenses=[{
        "vehicle": 1, "incurred_on": "2026-06-01", "category": "fuel",
        "amount": "45.67", "treatment": "business_use_allocated", "notes": None,
    }])
    normalized, issues = normalize_bundle(bundle)
    assert issues == []
    assert normalized["expenses"][0]["trip"] is None


# --- FIX 1: is_default/active/has_gap must be real booleans -----------------

def test_vehicle_is_default_string_is_rejected_not_coerced_truthy():
    bundle = _bundle(vehicles=[{**BASE_VEHICLE, "is_default": "false"}])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("is_default" in issue and "boolean" in issue for issue in issues)


def test_vehicle_active_non_bool_is_rejected():
    bundle = _bundle(vehicles=[{**BASE_VEHICLE, "active": 1}])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("active" in issue and "boolean" in issue for issue in issues)


def test_trip_has_gap_non_bool_is_rejected():
    bundle = _bundle(trips=[{**BASE_TRIP, "has_gap": "yes"}])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("has_gap" in issue and "boolean" in issue for issue in issues)


def test_two_vehicles_both_marked_default_are_rejected():
    bundle = _bundle(vehicles=[
        {**BASE_VEHICLE, "$id": 1, "is_default": True},
        {**BASE_VEHICLE, "$id": 2, "name": "Truck", "is_default": True},
    ])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("is_default" in issue and "one vehicle" in issue for issue in issues)


# --- FIX 2: place lat/lon range check ----------------------------------------

def test_place_lon_out_of_range_is_rejected():
    bundle = _bundle(places=[{**BASE_PLACE, "lon": 999.0}])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("lon" in issue and "-180" in issue for issue in issues)


def test_place_lat_out_of_range_is_rejected():
    bundle = _bundle(places=[{**BASE_PLACE, "lat": -91}])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("lat" in issue and "-90" in issue for issue in issues)


def test_place_lat_lon_zero_is_accepted():
    bundle = _bundle(places=[{**BASE_PLACE, "lat": 0, "lon": 0}])
    normalized, issues = normalize_bundle(bundle)
    assert issues == []
    assert normalized["places"][0]["lat"] == 0
    assert normalized["places"][0]["lon"] == 0


# --- FIX 3: optional string fields must be str or null -----------------------

def test_trip_notes_dict_is_rejected():
    bundle = _bundle(trips=[{**BASE_TRIP, "notes": {"x": 1}}])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("notes" in issue and "string" in issue for issue in issues)


def test_expense_notes_int_is_rejected():
    bundle = _bundle(expenses=[{
        "vehicle": 1, "incurred_on": "2026-06-01", "category": "fuel",
        "amount": "45.67", "treatment": "business_use_allocated", "notes": 123,
    }])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("notes" in issue and "string" in issue for issue in issues)


def test_all_optional_string_fields_explicitly_null_are_accepted():
    bundle = _bundle(
        vehicles=[{**BASE_VEHICLE, "make": None, "model": None, "plate": None}],
        trips=[{**BASE_TRIP, "purpose": None, "notes": None}],
        expenses=[{
            "vehicle": 1, "incurred_on": "2026-06-01", "category": "fuel",
            "amount": "45.67", "treatment": "business_use_allocated", "notes": None,
        }],
        odometer_readings=[{
            "vehicle": 1, "recorded_at": "2026-01-01T00:00:00+00:00",
            "odometer_m": 1000.0, "note": None,
        }],
    )
    normalized, issues = normalize_bundle(bundle)
    assert issues == []


# --- FIX 4: bool must not resolve as a $id cross-reference -------------------

def test_trip_vehicle_true_is_rejected_not_resolved_to_dollar_id_one():
    bundle = _bundle(trips=[{**BASE_TRIP, "vehicle": True}])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("vehicle" in issue and "unknown" in issue for issue in issues)


def test_trip_start_place_true_is_rejected_not_resolved_to_dollar_id_one():
    bundle = _bundle(trips=[{**BASE_TRIP, "start_place": True}])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("start_place" in issue and "unknown" in issue for issue in issues)


def test_trip_end_place_true_is_rejected():
    bundle = _bundle(trips=[{**BASE_TRIP, "end_place": True}])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("end_place" in issue and "unknown" in issue for issue in issues)


def test_tag_rule_a_place_true_is_rejected():
    bundle = _bundle(tag_rules=[{
        "a_place": True, "a_kind": None, "b_place": None, "b_kind": "work", "category": "business",
    }])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("a_place" in issue and "unknown" in issue for issue in issues)


def test_tag_rule_b_place_true_is_rejected():
    bundle = _bundle(tag_rules=[{
        "a_place": None, "a_kind": "home", "b_place": True, "b_kind": None, "category": "personal",
    }])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("b_place" in issue and "unknown" in issue for issue in issues)


# --- FIX 5: ended_at must not be before started_at ---------------------------

def test_trip_ended_at_before_started_at_is_rejected():
    bundle = _bundle(trips=[{
        **BASE_TRIP,
        "started_at": "2026-06-15T15:00:00+00:00",
        "ended_at": "2026-06-15T14:59:59+00:00",
    }])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("ended_at" in issue and "started_at" in issue for issue in issues)


def test_trip_ended_at_equal_to_started_at_is_accepted():
    bundle = _bundle(trips=[{
        **BASE_TRIP,
        "started_at": "2026-06-15T15:00:00+00:00",
        "ended_at": "2026-06-15T15:00:00+00:00",
    }])
    normalized, issues = normalize_bundle(bundle)
    assert issues == []


def test_trip_distance_m_and_odometer_reading_zero_are_accepted():
    bundle = _bundle(
        trips=[{**BASE_TRIP, "distance_m": 0}],
        odometer_readings=[{
            "vehicle": 1, "recorded_at": "2026-01-01T00:00:00+00:00",
            "odometer_m": 0, "note": None,
        }],
    )
    normalized, issues = normalize_bundle(bundle)
    assert issues == []
    assert normalized["trips"][0]["distance_m"] == 0
    assert normalized["odometer_readings"][0]["odometer_m"] == 0


# --- FIX 6: an empty vehicles array is rejected -------------------------------

def test_empty_vehicles_array_is_rejected():
    bundle = _bundle(vehicles=[])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("vehicles" in issue and "at least one" in issue for issue in issues)


# --- trip exclusion -----------------------------------------------------------

def test_trip_exclusion_not_my_vehicle_and_not_deductible_are_accepted():
    bundle = _bundle(trips=[
        {**BASE_TRIP, "$id": 1, "exclusion": "not_my_vehicle"},
        {**BASE_TRIP, "$id": 2, "exclusion": "not_deductible"},
    ])
    normalized, issues = normalize_bundle(bundle)
    assert issues == []
    exclusions = {row["$id"]: row["exclusion"] for row in normalized["trips"]}
    assert exclusions == {1: "not_my_vehicle", 2: "not_deductible"}


def test_trip_exclusion_explicit_null_normalizes_to_none():
    bundle = _bundle(trips=[{**BASE_TRIP, "exclusion": None}])
    normalized, issues = normalize_bundle(bundle)
    assert issues == []
    assert normalized["trips"][0]["exclusion"] is None


def test_trip_exclusion_absent_normalizes_to_none():
    # BASE_TRIP itself carries no "exclusion" key, exactly what a
    # format_version 1 bundle looks like -- this is also covered end to end
    # by test_format_version_1_bundle_imports_trips_as_normal below.
    assert "exclusion" not in BASE_TRIP
    bundle = _bundle(trips=[dict(BASE_TRIP)])
    normalized, issues = normalize_bundle(bundle)
    assert issues == []
    assert normalized["trips"][0]["exclusion"] is None


def test_trip_exclusion_bogus_value_is_rejected():
    bundle = _bundle(trips=[{**BASE_TRIP, "exclusion": "not_a_real_state"}])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("exclusion" in issue and "null" in issue for issue in issues)


# --- format_version -------------------------------------------------------

def test_format_version_2_is_accepted():
    bundle = _bundle(format_version=2)
    normalized, issues = normalize_bundle(bundle)
    assert issues == []
    assert normalized is not None
    assert normalized["format_version"] == 2


def test_format_version_1_bundle_imports_trips_as_normal():
    # A pre-existing v1 backup has no "exclusion" field on any trip at all;
    # it must still import, with every trip arriving as a normal
    # (non-excluded) trip rather than being rejected outright.
    bundle = _bundle(format_version=1, trips=[dict(BASE_TRIP)])
    normalized, issues = normalize_bundle(bundle)
    assert issues == []
    assert normalized is not None
    assert normalized["trips"][0]["exclusion"] is None


def test_unsupported_format_version_is_rejected_with_a_useful_message():
    bundle = _bundle(format_version=4)
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("format_version" in issue and "1" in issue and "2" in issue for issue in issues)


@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_format_version_must_be_an_integer_not_a_coercible_value(value):
    normalized, issues = normalize_bundle(_bundle(format_version=value))
    assert normalized is None
    assert any("format_version" in issue for issue in issues)


# --- trip endpoint labels (schema 25) ----------------------------------------

def test_trip_endpoint_labels_accept_string_or_explicit_null():
    bundle = _bundle(trips=[{
        **BASE_TRIP, "start_place": None, "end_place": None,
        "start_label": "Grandma's house", "end_label": None,
    }])
    normalized, issues = normalize_bundle(bundle)
    assert issues == []
    assert normalized["trips"][0]["start_label"] == "Grandma's house"
    assert normalized["trips"][0]["end_label"] is None


def test_trip_endpoint_labels_missing_keys_default_to_null():
    # BASE_TRIP carries no start_label/end_label key at all, the same shape
    # an older bundle (schema 21-24) has for both.
    assert "start_label" not in BASE_TRIP and "end_label" not in BASE_TRIP
    bundle = _bundle(trips=[{**BASE_TRIP, "start_place": None}])
    normalized, issues = normalize_bundle(bundle)
    assert issues == []
    assert normalized["trips"][0]["start_label"] is None
    assert normalized["trips"][0]["end_label"] is None


def test_trip_start_label_non_string_is_rejected():
    bundle = _bundle(trips=[{**BASE_TRIP, "start_place": None, "start_label": 123}])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("start_label" in issue and "string" in issue for issue in issues)


def test_trip_end_label_non_string_is_rejected():
    bundle = _bundle(trips=[{**BASE_TRIP, "end_place": None, "end_label": 123}])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("end_label" in issue and "string" in issue for issue in issues)


def test_trip_start_label_at_one_hundred_characters_is_accepted():
    label = "x" * 100
    bundle = _bundle(trips=[{**BASE_TRIP, "start_place": None, "start_label": label}])
    normalized, issues = normalize_bundle(bundle)
    assert issues == []
    assert normalized["trips"][0]["start_label"] == label


def test_trip_start_label_over_one_hundred_characters_is_rejected():
    label = "x" * 101
    bundle = _bundle(trips=[{**BASE_TRIP, "start_place": None, "start_label": label}])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("start_label" in issue and "100" in issue for issue in issues)


def test_trip_label_on_a_detected_trip_is_refused_at_normalization():
    # Migration 025's trips_start_label_manual_only constraint would refuse
    # this at the database too; catching it here means the whole import is
    # rejected with a named issue up front instead of failing mid-insert.
    bundle = _bundle(trips=[{
        **BASE_TRIP, "source": "detected", "start_place": None, "start_label": "Depot",
    }])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("start_label" in issue and "manual" in issue for issue in issues)


def test_trip_label_alongside_start_place_is_refused_at_normalization():
    # BASE_TRIP already references start_place $id 1.
    bundle = _bundle(trips=[{**BASE_TRIP, "start_label": "Depot"}])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("start_label" in issue and "start_place" in issue for issue in issues)


def test_trip_label_alongside_end_place_is_refused_at_normalization():
    bundle = _bundle(trips=[{
        **BASE_TRIP, "start_place": None, "end_place": 1, "end_label": "Depot",
    }])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("end_label" in issue and "end_place" in issue for issue in issues)


def test_trip_start_label_with_surrounding_whitespace_is_rejected():
    # Migration 025's trips_start_label_trimmed_nonblank constraint would
    # refuse this at the database too, since " Depot " isn't equal to its
    # own btrim; catching it here gives a named issue instead of an opaque
    # insert failure.
    bundle = _bundle(trips=[{**BASE_TRIP, "start_place": None, "start_label": " Depot "}])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("start_label" in issue and "whitespace" in issue for issue in issues)


def test_trip_end_label_with_surrounding_whitespace_is_rejected():
    bundle = _bundle(trips=[{**BASE_TRIP, "end_place": None, "end_label": " Depot "}])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("end_label" in issue and "whitespace" in issue for issue in issues)


def test_trip_start_label_empty_string_is_rejected():
    bundle = _bundle(trips=[{**BASE_TRIP, "start_place": None, "start_label": ""}])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("start_label" in issue and "non-empty" in issue for issue in issues)


def test_trip_start_label_whitespace_only_is_rejected():
    bundle = _bundle(trips=[{**BASE_TRIP, "start_place": None, "start_label": "   "}])
    normalized, issues = normalize_bundle(bundle)
    assert normalized is None
    assert any("start_label" in issue and "non-empty" in issue for issue in issues)


def test_trip_endpoint_labels_already_trimmed_values_are_accepted():
    bundle = _bundle(trips=[{
        **BASE_TRIP, "start_place": None, "end_place": None,
        "start_label": "Depot", "end_label": "Grandma's house",
    }])
    normalized, issues = normalize_bundle(bundle)
    assert issues == []
    assert normalized["trips"][0]["start_label"] == "Depot"
    assert normalized["trips"][0]["end_label"] == "Grandma's house"
