import json

import httpx
import pytest
from fastmcp import Client

from grok_search import server


async def map_text(arguments):
    async with Client(server.mcp) as client:
        result = await client.call_tool("web_map", arguments)
    return next(block.text for block in result.content if block.type == "text")


@pytest.mark.asyncio
async def test_scope_parameters_reach_tavily(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.delenv("TAVILY_ENABLED", raising=False)
    sent = {}

    async def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"base_url": "docs.example.test", "results": ["https://docs.example.test/a"], "response_time": 0.1})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    text = await map_text({"url": "https://docs.example.test", "allow_external": False, "select_paths": ["/docs/.*"], "exclude_domains": ["^blog\\."]})
    assert json.loads(text)["results"] == ["https://docs.example.test/a"]
    assert sent["allow_external"] is False and sent["select_paths"] == ["/docs/.*"] and sent["exclude_domains"] == ["^blog\\."]
    assert "select_domains" not in sent and "exclude_paths" not in sent


@pytest.mark.asyncio
async def test_external_links_stay_on_by_default(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.delenv("TAVILY_ENABLED", raising=False)
    sent = {}

    async def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"base_url": "x", "results": []})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    await map_text({"url": "https://docs.example.test"})
    assert sent["allow_external"] is True


@pytest.mark.asyncio
async def test_tavily_enabled_false_is_honoured(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_ENABLED", "false")
    assert "TAVILY_ENABLED=false" in await map_text({"url": "https://docs.example.test"})
