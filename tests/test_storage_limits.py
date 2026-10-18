"""GiB parsing for the administrator storage limit editor."""
from decimal import Decimal

import pytest

from app.application_roles import MIGRATIONS_DIR
from app.storage_limits import (
    GIB,
    MIN_CUSTOM_LIMIT_BYTES,
    limit_gib,
    max_custom_gib,
    parse_custom_gib,
)


def test_sql_minimum_matches_the_python_minimum():
    source = (MIGRATIONS_DIR / "047_storage_limit_editor.sql").read_text()
    assert MIN_CUSTOM_LIMIT_BYTES == 10737418
    assert f"input_account_bytes<{MIN_CUSTOM_LIMIT_BYTES}" in source


@pytest.mark.parametrize("value,expected", [
    ("0.01", 10737418), ("1", GIB), ("1.5", int(1.5 * GIB)), ("1.50", int(1.5 * GIB)),
    ("2.25", int(Decimal("2.25") * GIB)), ("999999999", 999999999 * GIB),
])
def test_valid_sizes_round_down_to_whole_bytes(value, expected):
    assert parse_custom_gib(value) == expected


@pytest.mark.parametrize("value", [
    "", "0", "0.00", "0.001", "0.009", "-1", "+1", "1.234", "1e2", "NaN", "Infinity",
    " 1", "1 ", "1\n", "1_0", "1.", ".5", "1000000000", "\u0661", None,
])
def test_invalid_sizes_are_refused(value):
    assert parse_custom_gib(value) is None


def test_display_values_round_trip_and_never_exceed_the_budget():
    for hundredths in range(1, 5000):
        gib = Decimal(hundredths).scaleb(-2)
        stored = parse_custom_gib(str(gib))
        assert limit_gib(stored) == gib
        assert max_custom_gib(stored) == gib
        assert max_custom_gib(stored - 1) < gib
    assert max_custom_gib(0) == Decimal("0.00")
    assert str(max_custom_gib(10 * GIB)) == "10.00"
