"""Binary storage size formatting shared by Settings and administration."""
from decimal import Decimal

import pytest

from app.storage_format import storage_size, storage_unit
from app.storage_limits import GIB, parse_custom_gib

KIB = 2 ** 10
MIB = 2 ** 20


@pytest.mark.parametrize("value,expected", [
    (0, "0 bytes"),
    (1023, "1023 bytes"),
    (1024, "1.0 KiB"),
    (1048575, "1.0 MiB"),
    (1048576, "1.0 MiB"),
    (1073741823, "1.00 GiB"),
    (1073741824, "1.00 GiB"),
])
def test_automatic_unit_switches_at_binary_boundaries(value, expected):
    assert storage_size(value) == expected


@pytest.mark.parametrize("value,unit", [
    (0, "bytes"),
    (1023, "bytes"),
    (1024, "KiB"),
    (1048575, "MiB"),
    (1048576, "MiB"),
    (1073741823, "GiB"),
    (1073741824, "GiB"),
])
def test_storage_unit_names_the_unit_storage_size_picks(value, unit):
    assert storage_unit(value) == unit


def test_automatic_unit_promotes_instead_of_printing_1024():
    assert storage_size(1048524) == "1023.9 KiB"
    assert storage_size(1048525) == "1.0 MiB"


def test_automatic_formatting_rounds_ties_to_even():
    assert storage_size(1280) == "1.2 KiB"
    assert storage_size(GIB + GIB // 8) == "1.12 GiB"
    assert storage_size(1536) == "1.5 KiB"
    assert storage_size(1037968) == "1013.6 KiB"
    assert storage_size(50 * MIB) == "50.0 MiB"
    assert storage_size(int(2.5 * GIB)) == "2.50 GiB"


@pytest.mark.parametrize("value,unit,expected", [
    (768, "KiB", "0.8 KiB"),
    (MIB, "MiB", "1.0 MiB"),
    (2 * GIB, "MiB", "2048.0 MiB"),
    (3 * MIB, "GiB", "0.00 GiB"),
    (50 * MIB, "GiB", "0.05 GiB"),
    (0, "GiB", "0.00 GiB"),
    (512, "bytes", "512 bytes"),
])
def test_forced_unit_formats_paired_values_in_one_unit(value, unit, expected):
    assert storage_size(value, unit) == expected


def test_unknown_unit_is_refused():
    with pytest.raises(ValueError):
        storage_size(1, "TiB")


def test_gib_text_reads_back_the_saved_editor_entry():
    for hundredths in range(1, 3000):
        entry = str(Decimal(hundredths).scaleb(-2))
        assert storage_size(parse_custom_gib(entry), "GiB") == f"{entry} GiB"
