import json
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

from grok_search import server
from grok_search.errors import ProviderError
from grok_search.providers.grok import GrokResponse, GrokSearchProvider
from grok_search.sources import SourcesCache

GROK_URL = "https://grok.example.test/a"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("GROK_API_URL", "https://api.example.test/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setenv("GROK_MODEL", "test-model")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.delenv("TAVILY_ENABLED", raising=False)
    monkeypatch.setattr(server.config, "_cached_model", None)
    monkeypatch.setattr(server, "_SOURCES_CACHE", SourcesCache())
    answer = GrokResponse(f"answer [[1]]({GROK_URL})", [{"type": "url_citation", "url": GROK_URL, "title": "1"}])
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", AsyncMock(return_value=answer))
    return server.mcp


def results(prefix, n, start=1):
    return [{"url": f"https://{prefix}.example.test/{i}", "title": f"{prefix} {i}"} for i in range(start, start + n)]


def engine(fn):
    async def run(query, limit):
        run.calls.append(limit)
        return fn(limit)
    run.calls = []
    return run


async def search(app, **arguments):
    async with Client(app) as client:
        result = await client.call_tool("web_search", {"query": "q", **arguments})
    assert not result.is_error
    return json.loads(next(block.text for block in result.content if block.type == "text"))


@pytest.mark.asyncio
async def test_both_engines_split_the_quota(configured, monkeypatch):
    tavily, firecrawl = engine(lambda n: results("t", n)), engine(lambda n: results("f", n))
    monkeypatch.setattr(server, "_call_tavily_search", tavily)
    monkeypatch.setattr(server, "_call_firecrawl_search", firecrawl)
    out = await search(configured, extra_sources=5)
    report = out["extra_sources_report"]
    assert report["returned"] == 5
    assert (report["engines"]["tavily"]["share"], report["engines"]["firecrawl"]["share"]) == (3, 2)
    assert (report["engines"]["tavily"]["selected"], report["engines"]["firecrawl"]["selected"]) == (3, 2)
    assert tavily.calls == [5] and firecrawl.calls == [4]  # share + 2 candidates for deduplication
    assert out["content"].count("[tavily]") == 3 and out["content"].count("[firecrawl]") == 2


@pytest.mark.asyncio
async def test_failed_engine_is_backfilled_by_the_other(configured, monkeypatch):
    tavily = engine(lambda n: results("t", n))

    async def broken(query, limit):
        raise ProviderError("firecrawl", "quota", "Insufficient credits", status=402)

    monkeypatch.setattr(server, "_call_tavily_search", tavily)
    monkeypatch.setattr(server, "_call_firecrawl_search", broken)
    out = await search(configured, extra_sources=8)
    report = out["extra_sources_report"]
    assert report["returned"] == 8 and report["engines"]["tavily"]["selected"] == 8
    assert report["engines"]["firecrawl"]["status"] == "error"
    assert report["engines"]["firecrawl"]["error"] == {"provider": "firecrawl", "category": "quota", "status": 402, "detail": "Insufficient credits"}
    assert tavily.calls == [6, 8]  # first round: share 4 + 2; backfill: 6 + shortfall 2


@pytest.mark.asyncio
async def test_duplicates_with_grok_and_across_engines_are_skipped(configured, monkeypatch):
    shared = [{"url": "https://www.grok.example.test/a/", "title": "dup of grok"}, {"url": "https://same.example.test/x", "title": "both"}]
    monkeypatch.setattr(server, "_call_tavily_search", engine(lambda n: shared + results("t", n - 2)))
    monkeypatch.setattr(server, "_call_firecrawl_search", engine(lambda n: [{"url": "http://same.example.test/x?utm_source=feed#top", "title": "both"}] + results("f", n - 1)))
    out = await search(configured, extra_sources=4)
    report = out["extra_sources_report"]
    assert report["returned"] == 4 and report["duplicates_skipped"] == 2
    assert "dup of grok" not in out["content"] and out["content"].count("same.example.test/x") == 1


@pytest.mark.asyncio
async def test_tavily_enabled_false_is_honoured(configured, monkeypatch):
    monkeypatch.setenv("TAVILY_ENABLED", "false")
    tavily, firecrawl = engine(lambda n: results("t", n)), engine(lambda n: results("f", n))
    monkeypatch.setattr(server, "_call_tavily_search", tavily)
    monkeypatch.setattr(server, "_call_firecrawl_search", firecrawl)
    out = await search(configured, extra_sources=3)
    assert tavily.calls == [] and firecrawl.calls == [5]
    assert set(out["extra_sources_report"]["engines"]) == {"firecrawl"}


@pytest.mark.asyncio
async def test_no_active_engine_is_reported(configured, monkeypatch):
    monkeypatch.setenv("TAVILY_ENABLED", "false")
    monkeypatch.delenv("FIRECRAWL_API_KEY")
    out = await search(configured, extra_sources=3)
    report = out["extra_sources_report"]
    assert report["returned"] == 0 and report["note"] == "no extra-source engine is active"
    assert report["engines"] == {"tavily": "disabled (TAVILY_ENABLED=false)", "firecrawl": "not configured (FIRECRAWL_API_KEY)"}


@pytest.mark.asyncio
async def test_tavily_search_raises_classified_errors(monkeypatch):
    monkeypatch.setenv("TAVILY_ENABLED", "false")
    with pytest.raises(ProviderError) as info:
        await server._call_tavily_search("q", 3)
    assert info.value.category == "disabled"
