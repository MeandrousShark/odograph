"""Binary storage sizes for templates."""
from __future__ import annotations

from app.storage_limits import GIB, limit_gib

KIB = 2 ** 10
MIB = 2 ** 20


def storage_unit(value: int) -> str:
    """The unit storage_size picks for `value` when no unit is forced."""
    # Promote when one-place rounding would print 1024.0 of the smaller unit.
    if value < KIB:
        return "bytes"
    if round(value / KIB, 1) < 1024:
        return "KiB"
    if round(value / MIB, 1) < 1024:
        return "MiB"
    return "GiB"


def storage_size(value: int, unit: str | None = None) -> str:
    """Format a byte count in binary units.

    Without `unit` the unit follows the value. A forced `unit` lets paired
    values share one, such as a usage and its limit. GiB uses the same
    two-place rounding as the storage limit editor.
    """
    if unit is None:
        unit = storage_unit(value)
    if unit == "bytes":
        return f"{value} bytes"
    if unit == "KiB":
        return f"{value / KIB:.1f} KiB"
    if unit == "MiB":
        return f"{value / MIB:.1f} MiB"
    if unit == "GiB":
        return f"{limit_gib(value)} GiB"
    raise ValueError(f"unknown storage unit: {unit!r}")
