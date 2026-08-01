"""Missing-trip detection: purely read-time, computed from `TRIP_COLUMNS`'
`prev_*`/`missing_trip_covered` correlated subselects (app/ui.py) — no
migration, no persistence, no detector change.

Kept as a pure function (same "pure core, thin I/O wrapper" split as
app/snap.py, app/rates.py, app/export.py) so the threshold-and-suppression
logic is unit-testable without a database, and is the one place the flag's
actual truth condition lives — `app/main.py` wires it in as a Jinja global,
`_trip_card.html` is the only template that calls it (deliberately the list
page only; not review or detail).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app.places_desc import describe_endpoint


@dataclass(frozen=True)
class MissingTripBadge:
    gap_m: float
    prefill_url: str


def missing_trip_badge(
    trip: dict, threshold_m: float, tz: ZoneInfo,
) -> MissingTripBadge | None:
    """`None` means no badge. `threshold_m <= 0` disables the feature
    entirely without needing every caller to check it separately.

    `trip["prev_end_gap_m"]` is `None` for manual rows (no `start_geom` to
    measure from), a device's first detected trip (no predecessor), and a
    detected trip whose immediate predecessor lacks `end_geom` — all three
    are TRIP_COLUMNS' job, not this function's (`ST_Distance` against a NULL
    input is NULL, not a false "far away"). `missing_trip_covered` means a
    manual trip already bridges the gap — checked last so a covered flag
    never even needs the distance compared.
    """
    if threshold_m <= 0:
        return None
    gap_m = trip.get("prev_end_gap_m")
    prev_ended_at: datetime | None = trip.get("prev_trip_ended_at")
    if gap_m is None or prev_ended_at is None:
        return None
    if gap_m <= threshold_m:
        return None
    if trip.get("missing_trip_covered"):
        return None

    local_end = prev_ended_at.astimezone(tz)
    # No geocoded address for the *previous* trip's end is fetched into
    # TRIP_COLUMNS (would need yet another correlated subselect for a value
    # that only ever appears in this free-text, user-editable hint) --
    # describe_endpoint's coordinate fallback is honest and legible enough
    # for a starting point the user is expected to edit anyway.
    prev_end_label = describe_endpoint(
        trip.get("prev_trip_end_place_name"), trip.get("prev_trip_end_lat"),
        trip.get("prev_trip_end_lon"), None,
    )
    this_start_label = describe_endpoint(
        trip.get("start_place_name"), trip.get("start_lat"), trip.get("start_lon"),
        trip.get("start_address"),
    )
    params = {
        "manual_date": local_end.strftime("%Y-%m-%d"),
        "manual_start": local_end.strftime("%H:%M"),
        "manual_notes": f"bridge: {prev_end_label} → {this_start_label}",
    }
    trip_id = trip.get("id")
    if trip_id is not None:
        # Carries which trip the badge belongs to purely so trips_archive()
        # can resolve the OSRM road-distance suggestion without putting raw
        # coordinates in the URL — add_manual_trip (the POST this form
        # submits to) never reads this param.
        params["bridge_trip"] = str(trip_id)
    return MissingTripBadge(
        gap_m=gap_m,
        prefill_url="/trips?" + urlencode(params) + "#manual-trip",
    )
