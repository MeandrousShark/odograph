"""Reconciliation tests: late-flush windowing and tag survival across a
reprocess.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.detector.core import Override, Params, detect
from app.detector.reconcile import ExistingTrip, plan_reconcile
from tests.synth import Drive, Stationary, build_track

P = Params()


def _ts(minutes: float) -> datetime:
    return datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def test_late_flush_converges_to_full_run():
    """Case 13: process the first half, then the late remainder via the
    rewind-to-settled-stay window; final trip set must equal a single full run.
    """
    pts = build_track([
        Stationary(duration_s=600),
        Drive(km=3.0),
        Stationary(duration_s=900),
        Drive(km=4.0, bearing_deg=180),
        Stationary(duration_s=1200),
    ])
    full_stays, full_trips = detect(pts, P)
    assert len(full_trips) == 2

    # First run sees everything up to mid-second-drive (a live phone that then
    # goes offline and flushes the rest later).
    cutoff = full_trips[1].started_at + timedelta(seconds=120)
    first_batch = [p for p in pts if p.t <= cutoff]
    stays1, trips1 = detect(first_batch, P)
    assert len(trips1) == 1  # second drive incomplete -> held

    # Late flush arrives. Rewind: start of the newest stay settled before the
    # earliest new point.
    dirty_from = min(p.t for p in pts if p.t > cutoff)
    settled = max((s for s in stays1 if s.ended_at < dirty_from), key=lambda s: s.ended_at)
    t0 = settled.started_at
    window_pts = [p for p in pts if p.t >= t0]
    _, window_trips = detect(window_pts, P)

    # Simulate the DB reconcile: trip1 started before t0 so it's untouched;
    # only in-window detected trips participate.
    existing_in_window = [
        ExistingTrip(id=1, started_at=t.started_at, ended_at=t.ended_at)
        for t in trips1 if t.started_at >= t0
    ]
    plan = plan_reconcile(
        existing_in_window, [(t.started_at, t.ended_at) for t in window_trips]
    )
    final = [t for t in trips1 if t.started_at < t0] + \
            [window_trips[i] for i in plan.inserts] + \
            [window_trips[ni] for _, ni in plan.matches]

    assert sorted((t.started_at, t.ended_at) for t in final) == \
           [(t.started_at, t.ended_at) for t in full_trips]
    got = {t.started_at: t for t in final}
    for t in full_trips:
        assert abs(got[t.started_at].distance_m - t.distance_m) < 1.0


def test_overlap_match_preserves_identity():
    """A reprocessed trip with slightly shifted boundaries updates the same
    row (tags survive); non-overlapping ones are inserted/deleted."""
    existing = [
        ExistingTrip(id=5, started_at=_ts(0), ended_at=_ts(30), category="business"),
        ExistingTrip(id=6, started_at=_ts(120), ended_at=_ts(140), category="unclassified"),
    ]
    new = [
        (_ts(2), _ts(31)),    # same trip, boundaries nudged by late data
        (_ts(200), _ts(220)),  # brand new
    ]
    plan = plan_reconcile(existing, new)
    assert plan.matches == [(5, 0)]
    assert plan.inserts == [1]
    assert [e.id for e in plan.deletes] == [6]


def test_below_half_overlap_is_not_a_match():
    existing = [ExistingTrip(id=1, started_at=_ts(0), ended_at=_ts(60))]
    new = [(_ts(50), _ts(110))]  # 10 min overlap of 60-min trips: < 50%
    plan = plan_reconcile(existing, new)
    assert plan.matches == []
    assert plan.inserts == [0]
    assert [e.id for e in plan.deletes] == [1]


def test_manual_trips_never_enter_reconciliation():
    """Case 15: the runner feeds only source='detected' rows into the plan
    (SQL filter); given that, an overlapping manual trip can never be matched
    or deleted, since reconciliation literally cannot see it. This test pins the
    contract: the plan touches exactly what it was given."""
    detected = [ExistingTrip(id=1, started_at=_ts(0), ended_at=_ts(30), category="personal")]
    # A manual trip covering the same window exists in the DB but is excluded
    # upstream, so it must not appear in matches or deletes.
    new = [(_ts(0), _ts(30))]
    plan = plan_reconcile(detected, new)
    assert plan.matches == [(1, 0)]
    assert plan.deletes == []
    touched_ids = {m[0] for m in plan.matches} | {d.id for d in plan.deletes}
    assert touched_ids == {1}


def test_merge_override_reconcile_keeps_longer_trips_tags():
    """After a suppress-override merge, reconcile's existing
    overlap-based matching keeps whichever original trip was longer (its
    id/tags survive); the shorter original is deleted. Pins the mechanism
    the merge endpoint's explicit tag/notes overwrite (app/ui/merge_split.py)
    then relies on, instead of leaving the result to overlap-luck."""
    pts = build_track([
        Stationary(duration_s=900),
        Drive(km=3.0),
        Stationary(duration_s=1200),
        Drive(km=4.0, bearing_deg=180),
        Stationary(duration_s=900),
    ])
    stays0, trips0 = detect(pts, P)
    assert len(trips0) == 2
    short_trip, long_trip = sorted(trips0, key=lambda t: t.ended_at - t.started_at)
    assert (long_trip.ended_at - long_trip.started_at) > (short_trip.ended_at - short_trip.started_at)

    middle = stays0[1]
    override = Override(kind="suppress", range_start=middle.started_at, range_end=middle.ended_at)
    _, merged_trips = detect(pts, P, overrides=[override])
    assert len(merged_trips) == 1
    merged = merged_trips[0]

    existing = [
        ExistingTrip(id=1, started_at=short_trip.started_at, ended_at=short_trip.ended_at,
                     category="personal", tag_source="rule"),
        ExistingTrip(id=2, started_at=long_trip.started_at, ended_at=long_trip.ended_at,
                     category="business", tag_source="human"),
    ]
    plan = plan_reconcile(existing, [(merged.started_at, merged.ended_at)])
    assert plan.matches == [(2, 0)]
    assert [e.id for e in plan.deletes] == [1]


def test_deletes_carry_tag_source_for_the_human_only_tag_loss_warning():
    """runner.py only warns on a deleted trip if tag_source == 'human'
    (a rule-applied tag disappearing silently is fine, since the rule can
    just re-apply to whatever trip replaces it). ExistingTrip must carry
    tag_source all the way through to the deletes list for that decision to
    be made downstream."""
    existing = [
        ExistingTrip(id=1, started_at=_ts(0), ended_at=_ts(30),
                     category="business", tag_source="human"),
        ExistingTrip(id=2, started_at=_ts(120), ended_at=_ts(140),
                     category="personal", tag_source="rule"),
    ]
    # Neither trip is detected anymore (e.g. boundaries shifted past the
    # 50%-overlap threshold) -> both are deletes.
    plan = plan_reconcile(existing, [])
    deletes_by_id = {d.id: d for d in plan.deletes}
    assert deletes_by_id[1].tag_source == "human"
    assert deletes_by_id[2].tag_source == "rule"
