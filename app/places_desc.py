from __future__ import annotations


def describe_endpoint(
    name: str | None, lat: float | None, lon: float | None, address: str | None = None
) -> str:
    """Human label for a trip endpoint. Fallback order: named place →
    reverse-geocoded address → rounded coordinate → em dash.

    Kept as the one place the full-label decision lives: review, detail,
    exports, the annual report's audit appendix, and the trip list's tooltip
    all call this instead of re-deriving the fallback. The dense trip-list
    label deliberately calls :func:`describe_compact_endpoint` instead; that
    presentation-only shortening must never leak into audit-oriented views.
    """
    if name:
        return name
    if address:
        return address
    if lat is not None and lon is not None:
        return f"{lat:.4f},{lon:.4f}"
    return "—"


def describe_compact_endpoint(
    name: str | None, lat: float | None, lon: float | None, address: str | None = None
) -> str:
    """Short endpoint label for the dense trip-list table.

    The full reverse-geocoded address remains the defensible audit/export
    value and is still returned by :func:`describe_endpoint`. The list only
    needs the street portion to make a route recognizable, however, and using
    the full locality/region/postcode made every row several lines tall. Named
    places remain untouched (including names containing commas), while an
    unnamed address is shortened at its first comma. Malformed addresses with
    no text before that comma retain the full value rather than rendering an
    empty endpoint.
    """
    if name:
        return name
    if address:
        street = address.partition(",")[0].strip()
        return street or address
    return describe_endpoint(None, lat, lon)
