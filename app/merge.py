"""Pure validation for the trip-list "merge selected" feature.

Merging is only possible between trips already adjacent through a real
detected stay -- an arbitrary, non-contiguous
selection can't be bridged by the suppress-override mechanism
(app/detector/core.py), since a suppress override only removes one real stay
between two trips that are already next to each other. This validates a
selection is exactly a contiguous run before app/ui/merge_split.py inserts
any overrides.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TripSpan:
    id: int
    started_at: datetime
    ended_at: datetime


def plan_merge_selected(
    selected: list[TripSpan], in_range: list[TripSpan]
) -> list[tuple[datetime, datetime]]:
    """`in_range` is every detected trip for the same device whose
    started_at falls within the selected trips' overall span (the caller
    fetches this from the DB). If it doesn't exactly match the selection,
    some other trip sits between two selected ones -- a gap this mechanism
    can't bridge. Returns the suppress range for each consecutive pair in
    the (sorted) selection, ready to insert as trip_boundary_overrides rows.
    """
    if len(selected) < 2:
        raise ValueError("Select at least two trips to merge")
    if {t.id for t in selected} != {t.id for t in in_range}:
        raise ValueError(
            "Selected trips must be a contiguous run with no other trip between them"
        )
    ordered = sorted(selected, key=lambda t: t.started_at)
    return [(a.ended_at, b.started_at) for a, b in zip(ordered, ordered[1:])]
