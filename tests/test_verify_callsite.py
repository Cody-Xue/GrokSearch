"""web_search's verification call site: which switches gate what, and which sources are verified."""
import json
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

from grok_search import server
from grok_search.providers.grok import GrokResponse, GrokSearchProvider
from grok_search.sources import SourcesCache

CITED = "https://cited.example.test/paper"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("GROK_API_URL", "https://api.example.test/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setenv("GROK_MODEL", "test-model")
    for key in ("GUDA_API_KEY", "TAVILY_API_KEY", "FIRECRAWL_API_KEY", "GROK_VERIFY_FAKE_IP", "GROK_VERIFY_DOH_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(server.config, "_cached_model", None)
    monkeypatch.setattr(server, "_SOURCES_CACHE", SourcesCache())
    answer = GrokResponse(f"see arXiv:2211.17192 [[1]]({CITED})", [{"type": "url_citation", "url": CITED, "title": "1"}])
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", AsyncMock(return_value=answer))
    fake = AsyncMock(return_value={"arxiv": [], "doi": [], "urls": [], "unresolved": [], "lookup_failed": []})
    monkeypatch.setattr(server, "verify_answer", fake)
    return fake


async def call(client, name, arguments):
    result = await client.call_tool(name, arguments)
    assert not result.is_error
    return json.loads(next(block.text for block in result.content if block.type == "text"))


@pytest.mark.asyncio
@pytest.mark.parametrize("ids, urls, called", [
    ("true", "true", True),
    ("false", "true", True),
    ("true", "false", True),
    ("false", "false", False),
])
async def test_ids_and_urls_switches_gate_their_own_checks(configured, monkeypatch, ids, urls, called):
    monkeypatch.setenv("GROK_VERIFY_IDS", ids)
    monkeypatch.setenv("GROK_VERIFY_URLS", urls)
    async with Client(server.mcp) as client:
        result = await call(client, "web_search", {"query": "q"})
    assert ("verification" in result) is called
    if called:
        kwargs = configured.await_args.kwargs
        assert kwargs["check_ids"] is (ids == "true") and kwargs["check_urls"] is (urls == "true")
        assert kwargs["fake_ip"] == "doh" and kwargs["doh_url"] == "https://cloudflare-dns.com/dns-query"


@pytest.mark.asyncio
async def test_only_cited_sources_are_verified(configured, monkeypatch):
    monkeypatch.setenv("GROK_VERIFY_IDS", "true")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    extra = [{"url": "https://extra.example.test/hit", "title": "Extra", "description": "d"}]
    monkeypatch.setattr(server, "_call_firecrawl_search", AsyncMock(return_value=extra))
    async with Client(server.mcp) as client:
        result = await call(client, "web_search", {"query": "q", "extra_sources": 1})
    assert "https://extra.example.test/hit" in result["content"]
    answer, sources = configured.await_args.args
    assert [s["url"] for s in sources] == [CITED]
    assert "Extra sources" not in answer


@pytest.mark.asyncio
async def test_description_explains_unresolved_versus_lookup_failed():
    async with Client(server.mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    description = " ".join(tools["web_search"].description.split())
    assert ("lookup_failed (rate limit, 5xx, timeout, network, or a site that refuses scripted requests with "
            "401/403/451) as unknown") in description
    assert "other 4xx except 401/403/451" in description
    assert "Treat unresolved (the registry or site said it does not exist" in description
    verify_param = tools["web_search"].input_schema["properties"]["verify"]["description"]
    assert "GROK_VERIFY_IDS" in verify_param and "GROK_VERIFY_URLS" in verify_param
