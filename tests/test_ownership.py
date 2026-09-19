from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.ownership import OwnershipMigrationError, _legacy_location_label, legacy_rate_overrides


def test_effective_legacy_rates_preserve_override_precision_and_ignore_invalid_values():
    assert legacy_rate_overrides({
        "MILEAGE_RATE_2026": "0.725123", "MILEAGE_RATE_2025": "nan",
        "MILEAGE_RATE_2024": "inf", "MILEAGE_RATE_2023": "-1",
        "MILEAGE_RATE_BAD": "1", "UNRELATED": "3",
    }) == {2026: Decimal("0.725123")}


def test_unrepresentable_legacy_rate_year_refuses_migration():
    with pytest.raises(OwnershipMigrationError):
        legacy_rate_overrides({"MILEAGE_RATE_2147483648": "1"})


@pytest.mark.parametrize("label, expected", [(None, "default"), ("", "default"),
                                              (False, "default"), (True, "True"),
                                              (123, "123"), ("phone", "phone")])
def test_legacy_raw_device_labels_preserve_python_metadata_semantics(label, expected):
    payload = {"lat": 10, "lon": 20, "tst": 1704067200, "tid": label}
    assert _legacy_location_label(payload, datetime(2024, 1, 1, tzinfo=timezone.utc)) == expected


def test_rejected_legacy_raw_location_does_not_invent_a_tracker():
    payload = {"lat": 10, "lon": 20, "tst": 1704153600, "tid": "future"}
    assert _legacy_location_label(payload, datetime(2024, 1, 1, tzinfo=timezone.utc)) is None
    payload["tst"] = True
    assert _legacy_location_label(payload, datetime(2024, 1, 1, tzinfo=timezone.utc)) is None
