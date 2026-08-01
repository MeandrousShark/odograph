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


def test_client_ip_ignores_untrusted_xff_and_uses_normalized_client():
    request = SimpleNamespace(
        headers={"x-forwarded-for": "203.0.113.99"},
        client=SimpleNamespace(host="10.89.0.4"),
    )
    assert client_ip(request) == "10.89.0.4"


def test_client_ip_has_safe_fallback_without_client():
    assert client_ip(SimpleNamespace(client=None)) == "unknown"
