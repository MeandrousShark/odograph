"""Trip list warning acknowledgements. Pure core + thin account-scoped I/O,
the same split as `app/missing_trip.py` and `app/rates.py`.

Only the two list warnings can be acknowledged: a stored recording gap
(`trips.has_gap`) and the read-time possible missing trip
(`app/missing_trip.py`). An acknowledgement stores the condition's
fingerprint and applies only while that fingerprint still matches the trip:

- recording gap: the trip's `started_at` and `ended_at`
- possible missing trip: the trip's `started_at` plus the predecessor's ID
  and `ended_at`

A merge, split, boundary change or new predecessor therefore shows the
warning again, while an in-place re-detect that keeps the span does not. A
stale row is ignored on read and replaced by the next acknowledgement. The
stored values come from `TRIP_COLUMNS`' `*_ack_*` subselects
(app/ui/_common.py); a trip dict without them is never acknowledged.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.account_context import account_id
from app.missing_trip import MissingTripBadge

RECORDING_GAP = "recording_gap"
MISSING_TRIP = "missing_trip"
WARNING_KINDS = (RECORDING_GAP, MISSING_TRIP)


@dataclass(frozen=True)
class WarningFingerprint:
    trip_started_at: datetime
    trip_ended_at: datetime
    prev_trip_id: int | None = None
    prev_ended_at: datetime | None = None


@dataclass(frozen=True)
class TripWarnings:
    """Which list warnings a trip has now, and which of them are acknowledged."""
    gap: bool
    gap_acknowledged: bool
    missing: MissingTripBadge | None
    missing_acknowledged: bool

    @property
    def listed_gap(self) -> bool:
        return self.gap and not self.gap_acknowledged

    @property
    def listed_missing(self) -> MissingTripBadge | None:
        return None if self.missing_acknowledged else self.missing

    @property
    def any_listed(self) -> bool:
        return self.listed_gap or self.listed_missing is not None


def current_fingerprints(
    trip: dict, missing: MissingTripBadge | None,
) -> dict[str, WarningFingerprint]:
    """Fingerprints of the warnings the trip shows now, keyed by kind.

    `missing` is the trip's `missing_trip_badge` result, computed by the
    caller with the account's threshold and timezone. A badge without a
    predecessor ID has nothing to match against, so it gets no fingerprint.
    """
    fingerprints = {}
    if trip.get("has_gap"):
        fingerprints[RECORDING_GAP] = WarningFingerprint(trip["started_at"], trip.get("ended_at"))
    prev_trip_id = trip.get("prev_trip_id")
    prev_ended_at = trip.get("prev_trip_ended_at")
    if missing is not None and prev_trip_id is not None and prev_ended_at is not None:
        fingerprints[MISSING_TRIP] = WarningFingerprint(
            trip["started_at"], trip.get("ended_at"), prev_trip_id, prev_ended_at,
        )
    return fingerprints


def _acknowledged(trip: dict, kind: str, current: WarningFingerprint | None) -> bool:
    if current is None:
        return False
    if kind == RECORDING_GAP:
        started_at = trip.get("gap_ack_trip_started_at")
        ended_at = trip.get("gap_ack_trip_ended_at")
        return (started_at is not None and started_at == current.trip_started_at
                and ended_at == current.trip_ended_at)
    started_at = trip.get("missing_ack_trip_started_at")
    return (started_at is not None and started_at == current.trip_started_at
            and trip.get("missing_ack_prev_trip_id") == current.prev_trip_id
            and trip.get("missing_ack_prev_ended_at") == current.prev_ended_at)


def trip_warnings(trip: dict, missing: MissingTripBadge | None) -> TripWarnings:
    fingerprints = current_fingerprints(trip, missing)
    return TripWarnings(
        gap=bool(trip.get("has_gap")),
        gap_acknowledged=_acknowledged(trip, RECORDING_GAP, fingerprints.get(RECORDING_GAP)),
        missing=missing,
        missing_acknowledged=_acknowledged(trip, MISSING_TRIP, fingerprints.get(MISSING_TRIP)),
    )


async def acknowledge(conn, trip_id: int, fingerprints: dict[str, WarningFingerprint]) -> None:
    """Upsert one acknowledgement per fingerprint. A row that already matches
    is left alone; a stale one is replaced."""
    for kind, fingerprint in fingerprints.items():
        await conn.execute(
            "INSERT INTO trip_warning_acks "
            "(account_id, trip_id, kind, trip_started_at, trip_ended_at, prev_trip_id, prev_ended_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (account_id, trip_id, kind) DO UPDATE SET "
            "trip_started_at = EXCLUDED.trip_started_at, trip_ended_at = EXCLUDED.trip_ended_at, "
            "prev_trip_id = EXCLUDED.prev_trip_id, prev_ended_at = EXCLUDED.prev_ended_at, "
            "acknowledged_at = now() "
            "WHERE (trip_warning_acks.trip_started_at, trip_warning_acks.trip_ended_at, "
            "trip_warning_acks.prev_trip_id, trip_warning_acks.prev_ended_at) IS DISTINCT FROM "
            "(EXCLUDED.trip_started_at, EXCLUDED.trip_ended_at, EXCLUDED.prev_trip_id, EXCLUDED.prev_ended_at)",
            (account_id(conn), trip_id, kind, fingerprint.trip_started_at, fingerprint.trip_ended_at,
             fingerprint.prev_trip_id, fingerprint.prev_ended_at),
        )


async def unacknowledge(conn, trip_id: int, kind: str) -> None:
    await conn.execute(
        "DELETE FROM trip_warning_acks WHERE account_id = %s AND trip_id = %s AND kind = %s",
        (account_id(conn), trip_id, kind),
    )
