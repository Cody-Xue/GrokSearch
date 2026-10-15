import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from tenacity import wait_none

from grok_search.providers import grok
from grok_search.providers.grok import GrokSearchProvider, GrokUpstreamError, _StopWhenRetryBudgetExhausted, _make_stream_error
from grok_search.throttle import BreakerOpen, breaker

KEY = ("https://api.example.test/v1", "tier-x")


def sse(*deltas):
    return "".join("data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n" for delta in deltas) + "data:[DONE]\n\n"


@pytest.fixture
def provider():
    return GrokSearchProvider(KEY[0], "test-key", KEY[1])


def mock_client(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(grok.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(grok, "_WaitWithRetryAfter", lambda *args: wait_none())


@pytest.mark.asyncio
async def test_error_event_frame_becomes_upstream_error(provider):
    body = 'event: error\ndata: {"error": {"message": "No available accounts for this model tier", "type": "rate_limit"}}\n\n'
    with pytest.raises(GrokUpstreamError) as info:
        await provider._parse_streaming_result(httpx.Response(200, text=body))
    assert info.value.error_type == "rate_limit"
    assert info.value.exhausted and not info.value.retryable


@pytest.mark.asyncio
async def test_error_frame_with_unparseable_payload(provider):
    body = "event: error\ndata: upstream exploded\n\n"
    with pytest.raises(GrokUpstreamError) as info:
        await provider._parse_streaming_result(httpx.Response(200, text=body))
    assert "upstream exploded" in str(info.value)


@pytest.mark.asyncio
async def test_error_object_in_data_without_event(provider):
    body = 'data: {"error": {"message": "upstream timeout", "type": "upstream_error"}}\n\n'
    with pytest.raises(GrokUpstreamError) as info:
        await provider._parse_streaming_result(httpx.Response(200, text=body))
    assert info.value.retryable and info.value.error_type == "upstream_error"


@pytest.mark.asyncio
async def test_event_type_resets_after_blank_line(provider):
    body = "event: ping\ndata: {}\n\n" + sse({"content": "ok"})
    result = await provider._parse_streaming_result(httpx.Response(200, text=body))
    assert result.content == "ok"


def test_auth_errors_are_not_retryable():
    err = _make_stream_error({"message": "Invalid API key", "code": "auth_error"})
    assert not err.retryable and err.error_type == "auth_error"


@pytest.mark.asyncio
async def test_http_429_surfaces_retry_after_and_counts_toward_breaker(provider, monkeypatch):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "7"}, text="rate limited")

    mock_client(monkeypatch, handler)
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("GROK_RETRY_BUDGET_S", "0")
    with pytest.raises(GrokUpstreamError) as info:
        await provider.search_with_sources("q")
    assert calls == 2
    assert info.value.error_type == "rate_limit" and info.value.retry_after == 7
    assert not breaker.is_open(KEY)
    assert breaker.snapshot()[f"{KEY[1]} @ {KEY[0]}"]["recent_429"] == 2


@pytest.mark.asyncio
async def test_retry_after_beyond_budget_stops_immediately(provider, monkeypatch):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "120"}, text="slow down")

    mock_client(monkeypatch, handler)
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("GROK_RETRY_BUDGET_S", "45")
    with pytest.raises(GrokUpstreamError) as info:
        await provider.search_with_sources("q")
    assert calls == 1 and info.value.retry_after == 120


@pytest.mark.asyncio
async def test_exhausted_pool_opens_breaker_and_short_circuits_next_call(provider, monkeypatch):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, text='{"error":{"message":"No available accounts for this model tier"}}')

    mock_client(monkeypatch, handler)
    with pytest.raises(GrokUpstreamError) as info:
        await provider.search_with_sources("q")
    assert calls == 1 and info.value.exhausted
    with pytest.raises(BreakerOpen) as opened:
        await provider.search_with_sources("q")
    assert calls == 1 and opened.value.retry_after_s > 0
    # A different model tier at the same proxy is unaffected.
    assert not breaker.is_open((KEY[0], "other-tier"))


@pytest.mark.asyncio
async def test_repeated_429_opens_breaker_across_calls(provider, monkeypatch):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, text="rate limited")

    mock_client(monkeypatch, handler)
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "0")
    monkeypatch.setenv("GROK_BREAKER_THRESHOLD", "3")
    for _ in range(3):
        with pytest.raises(GrokUpstreamError):
            await provider.search_with_sources("q")
    assert calls == 3 and breaker.is_open(KEY)
    with pytest.raises(BreakerOpen):
        await provider.search_with_sources("q")
    assert calls == 3


@pytest.mark.asyncio
async def test_non_429_http_error_is_typed(provider, monkeypatch):
    async def handler(request):
        return httpx.Response(401, text="unauthorized")

    mock_client(monkeypatch, handler)
    with pytest.raises(GrokUpstreamError) as info:
        await provider.search_with_sources("q")
    assert info.value.error_type == "http_401" and not info.value.retryable


@pytest.mark.asyncio
async def test_empty_stream_is_reported_not_swallowed(provider, monkeypatch):
    async def handler(request):
        return httpx.Response(200, text="data: [DONE]\n\n")

    mock_client(monkeypatch, handler)
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "0")
    with pytest.raises(GrokUpstreamError) as info:
        await provider.search_with_sources("q")
    assert info.value.error_type == "empty_response"


@pytest.mark.asyncio
async def test_success_after_rate_limit_closes_breaker(provider, monkeypatch):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, text=sse({"content": "fine"}))

    mock_client(monkeypatch, handler)
    result = await provider.search_with_sources("q")
    assert result.content == "fine" and calls == 2
    assert breaker.snapshot() == {}


def _state(*, idle_for: float, elapsed: float, retry_after):
    exc = GrokUpstreamError("429", error_type="rate_limit", retry_after=retry_after, status=429)
    outcome = SimpleNamespace(failed=True, exception=lambda: exc)
    return SimpleNamespace(idle_for=idle_for, seconds_since_start=elapsed, outcome=outcome)


def test_retry_budget_counts_sleep_not_request_time():
    stop = _StopWhenRetryBudgetExhausted(45)
    # A 100-second request that then hits a short Retry-After must still be retried.
    assert stop(_state(idle_for=0, elapsed=100, retry_after=7)) is False
    # Accumulated sleep plus the requested wait beyond the budget stops the call.
    assert stop(_state(idle_for=40, elapsed=41, retry_after=7)) is True
    # Budget already spent sleeping: stop regardless of the next wait.
    assert stop(_state(idle_for=45, elapsed=46, retry_after=None)) is True
    # Backoff without Retry-After and budget left: keep retrying.
    assert stop(_state(idle_for=10, elapsed=200, retry_after=None)) is False


@pytest.mark.asyncio
async def test_slow_first_attempt_still_retries_on_transient_error(provider, monkeypatch):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.2)
        if calls == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, text=sse({"content": "recovered"}))

    mock_client(monkeypatch, handler)
    monkeypatch.setenv("GROK_RETRY_BUDGET_S", "0.1")  # far below the request time
    result = await provider.search_with_sources("q")
    assert result.content == "recovered" and calls == 2
