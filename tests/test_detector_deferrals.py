"""In-memory scheduling state for DETECTOR_TURN_MAX_POINTS deferrals."""
from datetime import datetime, timezone

from app.detector.runner import DEFERRAL_RECHECK_S, DetectorDeferrals, WindowDeferred
from app.worker import WorkerStatus

CHECKPOINT = datetime(2026, 7, 1, tzinfo=timezone.utc)


class _Clock:
    now = 50.0

    def __call__(self):
        return self.now


def _deferrals():
    clock = _Clock()
    return DetectorDeferrals(clock=clock), clock


def test_deferred_device_is_skipped_until_its_recheck():
    deferrals, clock = _deferrals()
    assert not deferrals.should_skip(1, 2, CHECKPOINT, 2, 100)
    assert deferrals.defer(1, 2, WindowDeferred(100, CHECKPOINT, 2)) is True
    assert deferrals.should_skip(1, 2, CHECKPOINT, 2, 100)
    assert not deferrals.should_skip(1, 3, CHECKPOINT, 2, 100)
    assert not deferrals.should_skip(9, 2, CHECKPOINT, 2, 100)
    clock.now += DEFERRAL_RECHECK_S - 1
    assert deferrals.should_skip(1, 2, CHECKPOINT, 2, 100)
    clock.now += 1
    assert not deferrals.should_skip(1, 2, CHECKPOINT, 2, 100)
    # A recheck still over the limit is not a new deferral for logging.
    assert deferrals.defer(1, 2, WindowDeferred(100, CHECKPOINT, 2)) is False
    assert deferrals.should_skip(1, 2, CHECKPOINT, 2, 100)


def test_checkpoint_or_limit_change_clears_early():
    for changed in [(None, 2, 100), (CHECKPOINT, 1, 100), (CHECKPOINT, 2, 500)]:
        deferrals, _clock = _deferrals()
        deferrals.defer(1, 2, WindowDeferred(100, CHECKPOINT, 2))
        assert not deferrals.should_skip(1, 2, *changed)
        assert deferrals.devices() == ()


def test_prune_drops_only_unseen_devices_after_the_cursor():
    deferrals, _clock = _deferrals()
    status = WorkerStatus(label="detector")
    deferrals.bind(status)
    for owner, device in [(1, 2), (1, 5), (1, 8), (3, 9)]:
        deferrals.defer(owner, device, WindowDeferred(100, None, 2))
    assert status.deferred_devices == ((1, 2), (1, 5), (1, 8), (3, 9))
    deferrals.prune(1, 2, {5})
    assert status.deferred_devices == ((1, 2), (1, 5), (3, 9))


def test_disabled_accounts_drop_out_at_the_round_boundary():
    deferrals, _clock = _deferrals()
    status = WorkerStatus(label="detector")
    deferrals.bind(status)
    for owner, device in [(1, 2), (3, 9)]:
        deferrals.defer(owner, device, WindowDeferred(100, None, 2))
    deferrals.retain_accounts({3, 4})
    assert status.deferred_devices == ((3, 9),)
