"""Guards tests/conftest.py's automatic unit/ops/db tier assignment
(pytest_collection_modifyitems): every collected case must carry exactly
one of the three tier markers, never zero and never more than one, so a
`-m unit`/`-m ops`/`-m db` selection is a partition of the suite rather than
something a new file can silently fall outside of or land in twice.

Only meaningful against the full collected set, which is what
request.session.items holds during a bare `pytest` run (the cadence this
suite relies on before every merge). Run under a `-m` selection of its own,
it can only see the already-filtered subset, so it still passes but proves
less; that is expected, not a bug in this test.
"""
from __future__ import annotations

_TIER_MARKER_NAMES = {"unit", "ops", "db"}


def test_every_collected_case_has_exactly_one_tier_marker(request):
    offenders = {}
    for item in request.session.items:
        tiers = sorted(_TIER_MARKER_NAMES & {mark.name for mark in item.iter_markers()})
        if len(tiers) != 1:
            offenders[item.nodeid] = tiers

    assert not offenders, (
        f"cases without exactly one tier marker: {offenders}"
    )
