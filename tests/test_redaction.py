import json
import logging

import httpx
import pytest
from fastmcp import Client
from tenacity import wait_none

from grok_search import server
from grok_search.providers import grok
from grok_search.providers.grok import GrokSearchProvider, GrokUpstreamError
from grok_search.sources import SourcesCache
from grok_search.throttle import breaker

URL = "https://api.example.test/v1"
SECRET = "sk-echoed-0123456789abcdef"


def mock_client(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(grok.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(grok, "_WaitWithRetryAfter", lambda *args: wait_none())


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("GROK_API_URL", URL)
    monkeypatch.setenv("GROK_API_KEY", SECRET)
    monkeypatch.setenv("GROK_MODEL", "test-model")
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "1")
    for key in ("TAVILY_API_KEY", "FIRECRAWL_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(server.config, "_cached_model", None)
    monkeypatch.setattr(server, "_SOURCES_CACHE", SourcesCache())
    return server.mcp


def test_upstream_error_messages_are_redacted(monkeypatch):
    monkeypatch.setenv("GROK_API_KEY", SECRET)
    error = GrokUpstreamError(f"HTTP 401: invalid key {SECRET} (Authorization: Bearer {SECRET})")
    assert SECRET not in str(error) and str(error).startswith("HTTP 401: invalid key ***")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 502])
async def test_echoed_key_never_reaches_tool_output_or_logs(configured, monkeypatch, caplog, status):
    sent = []

    async def handler(request):
        sent.append(request.headers["Authorization"])
        return httpx.Response(status, text=f"rejected key {SECRET}; Authorization: Bearer {SECRET}")

    mock_client(monkeypatch, handler)
    caplog.set_level(logging.INFO, logger="grok_search")
    async with Client(configured) as client:
        result = await client.call_tool("web_search", {"query": "q"})
    text = next(block.text for block in result.content if block.type == "text")
    data = json.loads(text)
    assert sent and all(value == f"Bearer {SECRET}" for value in sent)  # the key is still sent upstream
    assert data["error_type"] == f"http_{status}" and "rejected key ***" in data["error"]
    assert SECRET not in text
    messages = [record.getMessage() for record in caplog.records]
    assert any("rejected key ***" in message for message in messages)
    assert all(SECRET not in message for message in messages)
    if status == 502:  # retried once; the retry warning is redacted too
        assert len(sent) == 2 and any("retrying" in message for message in messages)


@pytest.mark.asyncio
async def test_stream_error_frames_are_redacted(monkeypatch):
    monkeypatch.setenv("GROK_API_KEY", SECRET)
    provider = GrokSearchProvider(URL, SECRET)
    body = 'event: error\ndata: {"error": {"message": "bad key ' + SECRET + '", "type": "auth_error"}}\n\n'
    with pytest.raises(GrokUpstreamError) as info:
        await provider._parse_streaming_result(httpx.Response(200, text=body))
    assert SECRET not in str(info.value) and "bad key ***" in str(info.value)


@pytest.mark.asyncio
async def test_breaker_reason_is_redacted(monkeypatch):
    monkeypatch.setenv("GROK_API_KEY", SECRET)

    async def handler(request):
        return httpx.Response(429, text=f"No available accounts (key {SECRET})")

    mock_client(monkeypatch, handler)
    with pytest.raises(GrokUpstreamError) as info:
        await GrokSearchProvider(URL, SECRET, "tier(high)").search_with_sources("q")
    assert info.value.exhausted and SECRET not in str(info.value)
    snapshot = breaker.snapshot()
    assert list(snapshot) == [f"tier @ {URL}"] and SECRET not in json.dumps(snapshot)
