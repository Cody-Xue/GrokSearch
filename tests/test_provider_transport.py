"""Wall-clock limit, timeouts, transport errors, breaker admission of queued calls, URL normalisation."""
import asyncio
import json
import time

import httpx
import pytest
from fastmcp import Client
from tenacity import wait_none

from grok_search import server
from grok_search.providers import grok
from grok_search.providers.grok import GrokSearchProvider, GrokUpstreamError
from grok_search.sources import SourcesCache
from grok_search.throttle import BreakerOpen, breaker, get_semaphore

URL = "https://api.example.test/v1"
KEY = (URL, "tier")
REAL_CLIENT = httpx.AsyncClient


def sse_ok(text="ok"):
    return ("data: " + json.dumps({"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}) + "\n\n"
            + "data: [DONE]\n\n")


def mock_client(monkeypatch, handler):
    monkeypatch.setattr(grok.httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(grok, "_WaitWithRetryAfter", lambda *args: wait_none())


class KeepAlive(httpx.AsyncByteStream):
    """HTTP 200, then only SSE comments: no read timeout ever fires."""

    async def __aiter__(self):
        while True:
            yield b": keep-alive\n\n"
            await asyncio.sleep(0.02)


# -- settings ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_timeout_setting_reaches_the_http_client(monkeypatch):
    seen = {}

    def factory(**kwargs):
        seen.update(kwargs)
        return REAL_CLIENT(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=sse_ok())), **kwargs)

    monkeypatch.setattr(grok.httpx, "AsyncClient", factory)
    monkeypatch.setenv("GROK_READ_TIMEOUT_S", "42")
    await GrokSearchProvider(URL, "k", "tier").search_with_sources("q")
    assert seen["timeout"].read == 42.0


# -- wall-clock limit ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_keep_alive_stall_is_cut_by_the_search_timeout(monkeypatch):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=KeepAlive())

    mock_client(monkeypatch, handler)
    monkeypatch.setenv("GROK_SEARCH_TIMEOUT_S", "0.4")
    started = time.monotonic()
    with pytest.raises(GrokUpstreamError) as info:
        await GrokSearchProvider(URL, "k", "tier").search_with_sources("q")
    assert time.monotonic() - started < 2
    assert info.value.error_type == "timeout" and not info.value.retryable and calls == 1
    assert "GROK_SEARCH_TIMEOUT_S" in str(info.value) and "1 attempt(s)" in str(info.value)


@pytest.mark.asyncio
async def test_time_queued_for_a_slot_counts_and_frees_nothing_it_does_not_hold(monkeypatch):
    monkeypatch.setenv("GROK_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("GROK_SEARCH_TIMEOUT_S", "0.3")
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=sse_ok())

    mock_client(monkeypatch, handler)
    async with get_semaphore():
        with pytest.raises(GrokUpstreamError) as info:
            await GrokSearchProvider(URL, "k", "tier").search_with_sources("q")
    assert calls == 0 and info.value.error_type == "timeout" and "slot" in str(info.value)
    # The slot is usable again afterwards.
    monkeypatch.setenv("GROK_SEARCH_TIMEOUT_S", "300")
    assert (await GrokSearchProvider(URL, "k", "tier").search_with_sources("q")).content == "ok"


@pytest.mark.asyncio
async def test_read_timeout_is_retried_at_most_once(monkeypatch):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("", request=request)

    mock_client(monkeypatch, handler)
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("GROK_READ_TIMEOUT_S", "60")
    with pytest.raises(GrokUpstreamError) as info:
        await GrokSearchProvider(URL, "k", "tier").search_with_sources("q")
    assert calls == 2
    assert info.value.error_type == "timeout" and "ReadTimeout" in str(info.value) and "60s" in str(info.value)
    assert "2 attempt(s)" in str(info.value)


@pytest.mark.asyncio
async def test_read_timeout_is_not_retried_without_deadline_for_another_wait(monkeypatch):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("", request=request)

    mock_client(monkeypatch, handler)
    monkeypatch.setenv("GROK_READ_TIMEOUT_S", "180")
    monkeypatch.setenv("GROK_SEARCH_TIMEOUT_S", "100")
    with pytest.raises(GrokUpstreamError) as info:
        await GrokSearchProvider(URL, "k", "tier").search_with_sources("q")
    assert calls == 1 and info.value.error_type == "timeout"


@pytest.mark.asyncio
async def test_no_retry_is_started_when_the_deadline_is_nearly_spent(monkeypatch):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(502, text="bad gateway")

    mock_client(monkeypatch, handler)
    monkeypatch.setenv("GROK_SEARCH_TIMEOUT_S", "3")  # below the minimum window for another attempt
    with pytest.raises(GrokUpstreamError) as info:
        await GrokSearchProvider(URL, "k", "tier").search_with_sources("q")
    assert calls == 1 and info.value.error_type == "http_502"


# -- transport failures --------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_type, expected", [
    (httpx.ConnectError, "proxy tunnel closed or upstream unreachable (ConnectError)"),
    (httpx.ReadError, "connection dropped while reading the response (ReadError)"),
    (httpx.ProxyError, "the proxy refused or failed the tunnel (ProxyError)"),
])
async def test_transport_failures_are_network_errors_with_a_message(monkeypatch, exc_type, expected):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        raise exc_type("", request=request)

    mock_client(monkeypatch, handler)
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "2")
    with pytest.raises(GrokUpstreamError) as info:
        await GrokSearchProvider(URL, "k", "tier").search_with_sources("q")
    assert calls == 3 and info.value.error_type == "network" and info.value.retryable
    assert str(info.value).startswith(expected) and "3 attempt(s)" in str(info.value)


@pytest.mark.asyncio
async def test_connect_timeout_is_a_timeout(monkeypatch):
    async def handler(request):
        raise httpx.ConnectTimeout("", request=request)

    mock_client(monkeypatch, handler)
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "0")
    with pytest.raises(GrokUpstreamError) as info:
        await GrokSearchProvider(URL, "k", "tier").search_with_sources("q")
    assert info.value.error_type == "timeout" and "connection timed out (ConnectTimeout)" in str(info.value)


# -- breaker admission ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_queued_calls_recheck_the_breaker_after_getting_a_slot(monkeypatch):
    monkeypatch.setenv("GROK_MAX_CONCURRENCY", "1")
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=sse_ok())

    mock_client(monkeypatch, handler)
    provider = GrokSearchProvider(URL, "k", "tier")
    sem = get_semaphore()
    await sem.acquire()
    queued = [asyncio.create_task(provider.search_with_sources(f"q{i}")) for i in range(3)]
    await asyncio.sleep(0.05)  # all three passed the first check and wait for the slot
    breaker.record_failure(KEY, retry_after=60, exhausted=True, reason="account pool exhausted")
    sem.release()
    results = await asyncio.gather(*queued, return_exceptions=True)
    assert all(isinstance(r, BreakerOpen) for r in results) and calls == 0


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.mark.asyncio
async def test_failing_non_probe_call_does_not_admit_a_second_probe(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(breaker, "clock", clock)
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "0")
    release_old = asyncio.Event()
    release_probe = asyncio.Event()
    seen = []

    async def handler(request):
        query = json.loads(request.content)["messages"][1]["content"]
        tag = query.strip().split()[-1]
        seen.append(tag)
        if tag == "old":
            await release_old.wait()
            return httpx.Response(500, text="boom")
        await release_probe.wait()
        return httpx.Response(200, text=sse_ok("probe ok"))

    mock_client(monkeypatch, handler)
    provider = GrokSearchProvider(URL, "k", "tier")
    old = asyncio.create_task(provider.search_with_sources("old"))  # admitted while closed
    await asyncio.sleep(0.05)
    breaker.record_failure(KEY, exhausted=True)  # another call opened the breaker ...
    clock.t += 3600  # ... and its cooldown is over: half-open
    probe = asyncio.create_task(provider.search_with_sources("probe"))
    await asyncio.sleep(0.05)
    release_old.set()
    with pytest.raises(GrokUpstreamError):
        await old  # http_500: not a rate limit, the probe slot stays taken
    with pytest.raises(BreakerOpen) as info:
        await provider.search_with_sources("third")
    assert "probe" in info.value.reason and "third" not in seen
    release_probe.set()
    assert (await probe).content == "probe ok"
    assert breaker.snapshot() == {}


# -- URL / key normalisation and odd input -----------------------------------------------


@pytest.mark.asyncio
async def test_trailing_slash_url_reaches_the_same_chat_path(monkeypatch):
    paths = []

    async def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, text=sse_ok())

    mock_client(monkeypatch, handler)
    provider = GrokSearchProvider(URL + "/", " k ", "tier")
    await provider.search_with_sources("q")
    assert paths == ["/v1/chat/completions"] and provider._breaker_key == KEY


@pytest.mark.asyncio
async def test_web_search_with_trailing_slash_url_works_end_to_end(monkeypatch):
    paths, auth = [], []

    async def handler(request):
        paths.append(request.url.path)
        auth.append(request.headers["Authorization"])
        return httpx.Response(200, text=sse_ok("answer"))

    mock_client(monkeypatch, handler)
    monkeypatch.setenv("GROK_API_URL", URL + "/")
    monkeypatch.setenv("GROK_API_KEY", "test-key\n")
    monkeypatch.setenv("GROK_MODEL", "test-model")
    monkeypatch.setattr(server.config, "_cached_model", None)
    monkeypatch.setattr(server, "_SOURCES_CACHE", SourcesCache())
    async with Client(server.mcp) as client:
        result = await client.call_tool("web_search", {"query": "q"})
    data = json.loads(next(block.text for block in result.content if block.type == "text"))
    assert data["content"] == "answer" and paths == ["/v1/chat/completions"] and auth == ["Bearer test-key"]


@pytest.mark.asyncio
async def test_lone_surrogates_in_the_query_are_replaced_not_crashing(monkeypatch):
    bodies = []

    async def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, text=sse_ok())

    mock_client(monkeypatch, handler)
    result = await GrokSearchProvider(URL, "k", "tier").search_with_sources("a\ud800b 😀", platform="x\udfff", instructions="\ud800")
    assert result.content == "ok"
    user = bodies[0]["messages"][1]["content"]
    assert "a�b \U0001F600" in user and "x�" in user


@pytest.mark.asyncio
async def test_web_search_reports_transport_and_timeout_error_types(monkeypatch):
    async def handler(request):
        raise httpx.ConnectError("", request=request)

    mock_client(monkeypatch, handler)
    monkeypatch.setenv("GROK_API_URL", URL)
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setenv("GROK_MODEL", "test-model")
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "0")
    monkeypatch.setattr(server.config, "_cached_model", None)
    monkeypatch.setattr(server, "_SOURCES_CACHE", SourcesCache())
    async with Client(server.mcp) as client:
        result = await client.call_tool("web_search", {"query": "q"})
    data = json.loads(next(block.text for block in result.content if block.type == "text"))
    assert data["error_type"] == "network"
    assert "proxy tunnel closed or upstream unreachable (ConnectError)" in data["content"]
