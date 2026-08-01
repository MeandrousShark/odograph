"""Pure tests for the "merge selected" trip-list feature."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.merge import TripSpan, plan_merge_selected


def _ts(minutes: float) -> datetime:
    return datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def _span(id: int, start_min: float, end_min: float) -> TripSpan:
    return TripSpan(id=id, started_at=_ts(start_min), ended_at=_ts(end_min))


def test_two_adjacent_trips_merge():
    a, b = _span(1, 0, 10), _span(2, 20, 30)
    assert plan_merge_selected([b, a], [a, b]) == [(_ts(10), _ts(20))]


def test_three_contiguous_trips_merge():
    a, b, c = _span(1, 0, 10), _span(2, 20, 30), _span(3, 40, 50)
    assert plan_merge_selected([a, b, c], [a, b, c]) == [
        (_ts(10), _ts(20)), (_ts(30), _ts(40)),
    ]


def test_order_of_selected_list_does_not_matter():
    a, b, c = _span(1, 0, 10), _span(2, 20, 30), _span(3, 40, 50)
    assert plan_merge_selected([c, a, b], [a, b, c]) == [
        (_ts(10), _ts(20)), (_ts(30), _ts(40)),
    ]


def test_gap_between_selected_trips_rejected():
    a, b, c = _span(1, 0, 10), _span(2, 20, 30), _span(3, 40, 50)
    # b sits between a and c but wasn't selected -- a real gap the merge
    # mechanism can't bridge.
    with pytest.raises(ValueError, match="contiguous"):
        plan_merge_selected([a, c], [a, b, c])


def test_single_trip_rejected():
    a = _span(1, 0, 10)
    with pytest.raises(ValueError, match="at least two"):
        plan_merge_selected([a], [a])


def test_empty_selection_rejected():
    with pytest.raises(ValueError, match="at least two"):
        plan_merge_selected([], [])
