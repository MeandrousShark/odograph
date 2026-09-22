"""Tests for the portable export bundle's pure shaping function."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from app.portable import FORMAT, FORMAT_VERSION, build_export_bundle

TZ = timezone.utc


def _bundle(**overrides):
    kwargs = dict(
        vehicles=[{
            "id": 1, "name": "My Car", "make": None, "model": None, "plate": None,
            "is_default": True, "active": True,
        }],
        places=[{
            "id": 5, "name": "Home", "kind": "home", "lat": 47.6, "lon": -122.3, "radius_m": 150.0,
        }],
        tag_rules=[{
            "a_place": None, "a_kind": "home", "b_place": None, "b_kind": "work",
            "category": "personal",
        }],
        mileage_rates=[{
            "year": 2026, "rate_per_mi": 0.725, "rate_h2_per_mi": None, "h2_start_month": None,
        }],
        trips=[{
            "id": 42, "device": "phone1", "source": "detected",
            "started_at": datetime(2026, 6, 15, 15, 0, tzinfo=TZ),
            "ended_at": datetime(2026, 6, 15, 15, 30, tzinfo=TZ),
            "distance_m": 1609.344, "has_gap": False, "category": "business",
            "exclusion": None,
            "purpose": "Meet client", "notes": "client visit",
            "vehicle_id": 1, "start_place_id": 5, "end_place_id": None,
            "tag_source": "human", "start_label": None, "end_label": None,
        }],
        expenses=[{
            "vehicle_id": 1, "incurred_on": date(2026, 6, 1), "category": "fuel",
            "amount": Decimal("45.67"), "treatment": "business_use_allocated", "notes": None,
        }],
        odometer_readings=[{
            "vehicle_id": 1, "recorded_at": datetime(2026, 1, 1, tzinfo=TZ),
            "odometer_m": 160934.4, "note": None,
        }],
        settings={"auto_assign_default_vehicle": True, "display_tz": "Asia/Tokyo"},
        schema_version=18,
        exported_at=datetime(2026, 8, 5, 12, 0, tzinfo=TZ),
    )
    kwargs.update(overrides)
    return build_export_bundle(**kwargs)


def test_top_level_shape():
    bundle = _bundle()
    assert bundle["format"] == FORMAT
    assert bundle["format_version"] == FORMAT_VERSION
    assert bundle["schema_version"] == 18
    assert bundle["exported_at"] == "2026-08-05T12:00:00+00:00"
    for table in (
        "vehicles", "places", "tag_rules", "mileage_rates", "trips", "expenses",
        "odometer_readings",
    ):
        assert isinstance(bundle[table], list)
    assert bundle["settings"] == {"auto_assign_default_vehicle": True, "display_tz": "Asia/Tokyo"}


def test_vehicle_carries_dollar_id_equal_to_source_id():
    bundle = _bundle()
    assert bundle["vehicles"][0]["$id"] == 1
    assert bundle["vehicles"][0]["name"] == "My Car"
    assert bundle["vehicles"][0]["is_default"] is True


def test_place_carries_dollar_id_and_lat_lon():
    bundle = _bundle()
    place = bundle["places"][0]
    assert place["$id"] == 5
    assert place["lat"] == 47.6
    assert place["lon"] == -122.3


def test_tag_rule_references_are_not_dollar_ids_but_source_ids_directly():
    # a_place/b_place are already the source's real ids here, which is
    # exactly what a bundle-local $id is defined to be, so no remapping
    # happens on the export side.
    bundle = _bundle(tag_rules=[{
        "a_place": 5, "a_kind": None, "b_place": None, "b_kind": "work", "category": "business",
    }])
    rule = bundle["tag_rules"][0]
    assert rule["a_place"] == 5
    assert rule["b_kind"] == "work"


def test_trip_references_vehicle_and_places_by_dollar_id():
    bundle = _bundle()
    trip = bundle["trips"][0]
    assert trip["$id"] == 42
    assert trip["vehicle"] == 1
    assert trip["start_place"] == 5
    assert trip["end_place"] is None
    assert trip["started_at"] == "2026-06-15T15:00:00+00:00"
    assert trip["distance_m"] == 1609.344
    assert trip["category"] == "business"
    assert trip["exclusion"] is None
    assert trip["tag_source"] == "human"


def test_trip_carries_its_exclusion_state():
    bundle = _bundle(trips=[{
        "id": 42, "device": "phone1", "source": "detected",
        "started_at": datetime(2026, 6, 15, 15, 0, tzinfo=TZ),
        "ended_at": datetime(2026, 6, 15, 15, 30, tzinfo=TZ),
        "distance_m": 1609.344, "has_gap": False, "category": "personal",
        "exclusion": "not_my_vehicle",
        "purpose": None, "notes": None,
        "vehicle_id": 1, "start_place_id": None, "end_place_id": None,
        "tag_source": None, "start_label": None, "end_label": None,
    }])
    assert bundle["trips"][0]["exclusion"] == "not_my_vehicle"


def test_trip_carries_its_endpoint_labels_or_null_when_unset():
    bundle = _bundle()
    trip = bundle["trips"][0]
    assert trip["start_label"] is None
    assert trip["end_label"] is None

    bundle = _bundle(trips=[{
        "id": 7, "device": "phone1", "source": "manual",
        "started_at": datetime(2026, 6, 15, 15, 0, tzinfo=TZ),
        "ended_at": datetime(2026, 6, 15, 15, 30, tzinfo=TZ),
        "distance_m": 1000.0, "has_gap": False, "category": "personal",
        "exclusion": None,
        "purpose": None, "notes": None,
        "vehicle_id": None, "start_place_id": None, "end_place_id": None,
        "tag_source": None, "start_label": "Grandma's house", "end_label": "Lake cabin",
    }])
    trip = bundle["trips"][0]
    assert trip["start_label"] == "Grandma's house"
    assert trip["end_label"] == "Lake cabin"


def test_expense_amount_serializes_as_exact_decimal_string():
    bundle = _bundle()
    expense = bundle["expenses"][0]
    assert expense["vehicle"] == 1
    assert expense["amount"] == "45.67"
    assert expense["incurred_on"] == "2026-06-01"
    assert expense["trip"] is None


def test_expense_references_linked_trip_by_bundle_id():
    bundle = _bundle(expenses=[{
        "vehicle_id": 1, "trip_id": 42, "incurred_on": date(2026, 6, 1),
        "category": "fuel", "amount": Decimal("45.67"),
        "treatment": "business_use_allocated", "notes": None,
    }])
    assert bundle["expenses"][0]["trip"] == 42


def test_odometer_reading_references_vehicle_by_dollar_id():
    bundle = _bundle()
    reading = bundle["odometer_readings"][0]
    assert reading["vehicle"] == 1
    assert reading["recorded_at"] == "2026-01-01T00:00:00+00:00"
    assert reading["odometer_m"] == 160934.4


def test_mileage_rate_carries_midyear_split_when_present():
    bundle = _bundle(mileage_rates=[{
        "year": 2022, "rate_per_mi": 0.585, "rate_h2_per_mi": 0.625, "h2_start_month": 7,
    }])
    rate = bundle["mileage_rates"][0]
    assert rate["rate_h2_per_mi"] == 0.625
    assert rate["h2_start_month"] == 7


def test_bundle_is_json_serializable():
    import json

    json.dumps(_bundle())
