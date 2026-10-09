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

from pathlib import Path

import pytest

from conftest import pytest_collection_modifyitems

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


class _FakeItem:
    def __init__(self, nodeid, *markers):
        self.nodeid = nodeid
        self.path = Path(nodeid.split("::", 1)[0])
        self.markers = [getattr(pytest.mark, name).mark for name in markers]

    def iter_markers(self):
        return iter(self.markers)

    def add_marker(self, marker):
        self.markers.append(marker.mark)


def test_case_with_two_explicit_tiers_is_refused_at_collection(monkeypatch):
    # CI shards run `-m "not db"` and `-m db` separately, so the session-wide
    # check above never sees every case; the collection hook must refuse it.
    monkeypatch.delenv("ODOGRAPH_DB_SHARD", raising=False)
    items = [_FakeItem("tests/test_x.py::test_ok"), _FakeItem("tests/test_x.py::test_both", "unit", "db")]

    with pytest.raises(pytest.exit.Exception, match="test_both has more than one tier marker"):
        pytest_collection_modifyitems(None, items)
