import pytest

from grok_search.throttle import BreakerOpen, CircuitBreaker

KEY = ("https://proxy.test/v1", "tier")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(clock, **kw):
    params = dict(threshold=3, window_s=60, cooldown_s=60, max_cooldown_s=300)
    params.update(kw)
    return CircuitBreaker(clock=clock, **params)


def test_opens_after_threshold_within_window():
    clock = Clock()
    b = make(clock)
    assert b.record_failure(KEY) is False
    assert b.record_failure(KEY) is False
    assert b.record_failure(KEY) is True
    with pytest.raises(BreakerOpen) as info:
        b.check(KEY)
    assert 59 <= info.value.retry_after_s <= 60
    assert b.is_open(KEY)


def test_failures_outside_window_do_not_count():
    clock = Clock()
    b = make(clock)
    for _ in range(5):
        b.record_failure(KEY)
        clock.t += 61
    assert not b.is_open(KEY)
    b.check(KEY)


def test_exhausted_pool_opens_immediately_with_retry_after():
    clock = Clock()
    b = make(clock)
    assert b.record_failure(KEY, retry_after=90, exhausted=True) is True
    with pytest.raises(BreakerOpen) as info:
        b.check(KEY)
    assert info.value.retry_after_s == 90
    assert "exhausted" in info.value.reason


def test_retry_after_is_capped_by_max_cooldown():
    clock = Clock()
    b = make(clock)
    b.record_failure(KEY, retry_after=10_000, exhausted=True)
    with pytest.raises(BreakerOpen) as info:
        b.check(KEY)
    assert info.value.retry_after_s == 300


def test_half_open_admits_one_probe_and_success_closes():
    clock = Clock()
    b = make(clock)
    for _ in range(3):
        b.record_failure(KEY)
    clock.t = 60
    b.check(KEY)  # probe admitted
    with pytest.raises(BreakerOpen) as info:
        b.check(KEY)
    assert "probe" in info.value.reason
    b.record_success(KEY)
    b.check(KEY)
    assert b.snapshot() == {}


def test_probe_failure_doubles_cooldown_up_to_cap():
    clock = Clock()
    b = make(clock)
    for _ in range(3):
        b.record_failure(KEY)
    clock.t = 60
    b.check(KEY)
    assert b.record_failure(KEY) is True
    with pytest.raises(BreakerOpen) as info:
        b.check(KEY)
    assert info.value.retry_after_s == 120
    clock.t = 180
    b.check(KEY)
    b.record_failure(KEY)
    clock.t = 420
    b.check(KEY)
    b.record_failure(KEY)
    with pytest.raises(BreakerOpen) as info:
        b.check(KEY)
    assert info.value.retry_after_s == 300
    assert b.snapshot()[f"{KEY[1]} @ {KEY[0]}"]["opened_count"] == 4


def test_release_probe_lets_next_caller_probe():
    clock = Clock()
    b = make(clock)
    b.record_failure(KEY, exhausted=True)
    clock.t = 60
    b.check(KEY)
    b.release_probe(KEY)
    b.check(KEY)


def test_keys_are_independent():
    clock = Clock()
    b = make(clock)
    b.record_failure(KEY, exhausted=True)
    other = (KEY[0], "fast-tier")
    b.check(other)
    assert not b.is_open(other)


def test_snapshot_reports_states():
    clock = Clock()
    b = make(clock)
    b.record_failure(KEY)
    snap = b.snapshot()[f"{KEY[1]} @ {KEY[0]}"]
    assert snap["state"] == "closed" and snap["recent_429"] == 1
    b.record_failure(KEY, exhausted=True)
    assert b.snapshot()[f"{KEY[1]} @ {KEY[0]}"]["state"] == "open"
    clock.t = 61
    assert b.snapshot()[f"{KEY[1]} @ {KEY[0]}"]["state"] == "half-open"
