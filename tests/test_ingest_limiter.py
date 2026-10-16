from hashlib import sha256
from types import SimpleNamespace

from app.ingest import FailedAuthLimiter, client_ip


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_limiter_blocks_at_threshold_and_window_expiry_unblocks():
    clock = Clock()
    limiter = FailedAuthLimiter(2, 10, clock=clock, prune_interval_s=5)
    limiter.record_failure("one")
    limiter.record_failure("one")
    assert limiter.blocked("one")
    clock.now = 11
    assert not limiter.blocked("one")


def test_limiter_periodically_prunes_inactive_ips_globally():
    clock = Clock()
    limiter = FailedAuthLimiter(2, 10, clock=clock, prune_interval_s=5)
    for i in range(100):
        limiter.record_failure(f"192.0.2.{i}")
    assert len(limiter._failures) == 100
    clock.now = 11
    limiter.blocked("198.51.100.1")
    assert limiter._failures == {}



def test_limiter_does_not_block_fresh_key_when_table_is_full():
    clock = Clock()
    limiter = FailedAuthLimiter(2, 100, clock=clock, max_keys=4)
    for index in range(4):
        clock.now += 1
        limiter.record_failure(f"192.0.2.{index}")
    assert len(limiter._failures) == 4
    assert not limiter.blocked("198.51.100.1")
    clock.now += 1
    limiter.record_failure("198.51.100.1")
    limiter.record_failure("198.51.100.1")
    assert limiter.blocked("198.51.100.1")
    assert len(limiter._failures) == 4


def test_limiter_keeps_blocked_key_while_newer_keys_fill_table():
    clock = Clock()
    limiter = FailedAuthLimiter(2, 100, clock=clock, max_keys=4)
    limiter.record_failure("blocked")
    limiter.record_failure("blocked")
    assert limiter.blocked("blocked")
    for index in range(3):
        clock.now += 1
        limiter.record_failure(f"192.0.2.{index}")
        assert len(limiter._failures) <= 4
    assert limiter.blocked("blocked")
    assert len(limiter._failures) == 4


def test_limiter_evicts_least_recently_failed_key():
    clock = Clock()
    limiter = FailedAuthLimiter(3, 100, clock=clock, max_keys=4)
    limiter.record_failure("first")
    for name in ("second", "third", "fourth"):
        clock.now += 1
        limiter.record_failure(name)
    # A newer failure for "first" makes "second" the least recently failed.
    clock.now += 1
    limiter.record_failure("first")
    clock.now += 1
    limiter.record_failure("fifth")
    kept = {_key(name) for name in ("first", "third", "fourth", "fifth")}
    assert set(limiter._failures) == kept
    assert len(limiter._failures[_key("first")]) == 2
    for index in range(10):
        clock.now += 1
        limiter.record_failure(f"198.51.100.{index}")
        assert len(limiter._failures) <= 4


def _key(ip):
    return sha256(ip.encode()).hexdigest()


def test_client_ip_ignores_untrusted_xff_and_uses_normalized_client():
    request = SimpleNamespace(
        headers={"x-forwarded-for": "203.0.113.99"},
        client=SimpleNamespace(host="10.89.0.4"),
    )
    assert client_ip(request) == "10.89.0.4"


def test_client_ip_has_safe_fallback_without_client():
    assert client_ip(SimpleNamespace(client=None)) == "unknown"
