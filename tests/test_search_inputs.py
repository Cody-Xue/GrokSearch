"""web_search input checks: a blank query is refused locally, before any upstream request (E7)."""
import json

import httpx
import pytest
from fastmcp import Client

from grok_search import server
from grok_search.models import ModelListCache
from grok_search.sources import SourcesCache

_REAL_ASYNC_CLIENT = httpx.AsyncClient

SSE_OK = (
    'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":null}]}\n\n'
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    "data: [DONE]\n\n"
)


@pytest.fixture
def upstream(monkeypatch):
    """Every httpx request of the server (Grok, /models, Tavily, Firecrawl) is recorded and answered locally."""
    monkeypatch.setenv("GROK_API_URL", "https://api.example.test/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setenv("GROK_MODEL", "test-model")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    monkeypatch.setenv("TAVILY_API_URL", "https://tavily.example.test")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setenv("FIRECRAWL_API_URL", "https://firecrawl.example.test/v2")
    monkeypatch.setenv("GROK_VERIFY_URLS", "false")
    monkeypatch.setenv("GROK_VERIFY_IDS", "false")
    monkeypatch.delenv("TAVILY_ENABLED", raising=False)
    monkeypatch.setattr(server.config, "_cached_model", None)
    monkeypatch.setattr(server, "_SOURCES_CACHE", SourcesCache())
    monkeypatch.setattr(server, "_MODEL_CACHE", ModelListCache())
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/chat/completions"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=SSE_OK.encode())
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "test-model"}, {"id": "other-model"}]})
        if path.endswith("/search") and request.url.host.startswith("tavily"):
            return httpx.Response(200, json={"results": [{"url": "https://t.example.test/1", "title": "t"}]})
        if path.endswith("/search"):
            return httpx.Response(200, json={"success": True, "data": {"web": [{"url": "https://f.example.test/1", "title": "f"}]}})
        return httpx.Response(404, json={})

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


async def call_search(arguments):
    async with Client(server.mcp) as client:
        result = await client.call_tool("web_search", arguments)
        assert not result.is_error
        data = json.loads(next(block.text for block in result.content if block.type == "text"))
        got = await client.call_tool("get_sources", {"session_id": data["session_id"]})
        sources = json.loads(next(block.text for block in got.content if block.type == "text"))
    return data, sources


@pytest.mark.parametrize("blank", [
    "",
    "   ",
    " \t\n  ",
    "　  ",          # ideographic, no-break and em spaces
    "​﻿‍⁠",    # zero-width space / BOM / joiner / word joiner
    "\x00\x01\x1f\x7f",            # control characters
    "　 ​﻿\r\n\x0b",
])
@pytest.mark.asyncio
async def test_blank_query_is_refused_without_any_upstream_request(upstream, blank):
    data, sources = await call_search({"query": blank, "platform": "GitHub", "extra_sources": 4, "model": "other-model"})
    assert upstream == []  # no Grok call, no /models lookup, no Tavily or Firecrawl search
    assert data["content"].startswith("[搜索失败]") and "查询为空" in data["content"]
    assert data["error_type"] == "bad_request" and data["error"] == "empty query"
    assert (data["sources_count"], data["extra_sources_count"], data["total_sources_count"]) == (0, 0, 0)
    assert "usage" not in data and "extra_sources_report" not in data
    assert sources["sources"] == [] and "error" not in sources  # the session exists and is empty


@pytest.mark.parametrize("query", ["a", "中", "🙂", "​x", " q ", "\x00?"])
@pytest.mark.asyncio
async def test_queries_with_any_visible_character_are_searched(upstream, query):
    data, _ = await call_search({"query": query})
    assert "error_type" not in data and data["content"] == "answer"
    assert [r.url.path for r in upstream] == ["/v1/chat/completions"]


@pytest.mark.asyncio
async def test_whitespace_only_platform_is_ignored(upstream):
    data, _ = await call_search({"query": "httpx", "platform": " 　​ ", "extra_sources": 2})
    assert "error_type" not in data
    grok_body = json.loads(next(r for r in upstream if r.url.path.endswith("/chat/completions")).content)
    user = next(m["content"] for m in grok_body["messages"] if m["role"] == "user")
    assert "focus on these platform" not in user
    assert "platform" not in data["extra_sources_report"]
    engine_bodies = [json.loads(r.content) for r in upstream if r.url.path.endswith("/search")]
    assert [body["query"] for body in engine_bodies] == ["httpx", "httpx"]


def test_blank_predicate():
    assert server._is_blank(None) and server._is_blank("") and server._is_blank("​\x7f　")
    assert not server._is_blank("x") and not server._is_blank("​x​")
