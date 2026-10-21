"""Pure reconciliation logic for dirty-window reprocessing.

Matches newly detected trips against existing detected trips by time overlap
so human tags survive a reprocess. Manual trips must never be passed in here;
the runner filters on source = 'detected'.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class ExistingTrip:
    id: int
    started_at: datetime
    ended_at: datetime
    category: str = "unclassified"
    tag_source: str | None = None


@dataclass
class ReconcilePlan:
    # (existing trip id, index into new trips): update in place, keep tags
    matches: list[tuple[int, int]]
    inserts: list[int]  # indices into new trips
    deletes: list[ExistingTrip]
    # Unmatched trips that start in the open segment after the window's last
    # stay. They wait for that segment to end rather than being deleted.
    kept: list[ExistingTrip] = field(default_factory=list)


def _overlap_s(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> float:
    return max(0.0, (min(a_end, b_end) - max(a_start, b_start)).total_seconds())


def plan_reconcile(
    existing: list[ExistingTrip], new_trips: list[tuple[datetime, datetime]],
    open_tail_from: datetime | None = None,
) -> ReconcilePlan:
    """Greedy one-to-one matching by descending overlap.

    A pair qualifies when the overlap covers >= 50% of the shorter trip's
    duration. `new_trips` is a list of (started_at, ended_at).

    `open_tail_from` is the start of the window's last stay. An unmatched trip
    starting at or after it ended at a provisional arrival stay that newer
    points no longer show as the end: the drive went on and has no destination
    yet, so the trip is kept for the run that sees one to match in place,
    keeping its id, tags and expenses.
    """
    candidates: list[tuple[float, int, int]] = []  # (overlap, existing idx, new idx)
    for ei, e in enumerate(existing):
        e_dur = (e.ended_at - e.started_at).total_seconds()
        for ni, (n_start, n_end) in enumerate(new_trips):
            ov = _overlap_s(e.started_at, e.ended_at, n_start, n_end)
            n_dur = (n_end - n_start).total_seconds()
            shorter = max(min(e_dur, n_dur), 1.0)  # avoid zero-division on degenerate trips
            if ov > 0 and ov >= 0.5 * shorter:
                candidates.append((ov, ei, ni))

    candidates.sort(key=lambda c: c[0], reverse=True)
    matched_existing: set[int] = set()
    matched_new: set[int] = set()
    matches: list[tuple[int, int]] = []
    for _, ei, ni in candidates:
        if ei in matched_existing or ni in matched_new:
            continue
        matched_existing.add(ei)
        matched_new.add(ni)
        matches.append((existing[ei].id, ni))

    inserts = [ni for ni in range(len(new_trips)) if ni not in matched_new]
    deletes: list[ExistingTrip] = []
    kept: list[ExistingTrip] = []
    for ei, e in enumerate(existing):
        if ei in matched_existing:
            continue
        if open_tail_from is not None and e.started_at >= open_tail_from:
            kept.append(e)
        else:
            deletes.append(e)
    return ReconcilePlan(matches=matches, inserts=inserts, deletes=deletes, kept=kept)
