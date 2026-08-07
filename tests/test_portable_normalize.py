"""Tests for normalize_bundle, the portable import bundle's pure
validation/normalization function. No database needed -- that's the point
of the function being pure -- so these are plain unit tests with no
TEST_DATABASE_URL skip marker and no pool.
"""
from __future__ import annotations

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
        "settings": {"auto_assign_default_vehicle": False},
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
