import json
import re
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

from grok_search import models, server
from grok_search.citations import find_markers
from grok_search.errors import ProviderError
from grok_search.providers.grok import GrokResponse, GrokSearchProvider, GrokUpstreamError
from grok_search.sources import SourcesCache

GROK_URL = "https://grok.example.test/a"
COUNT_FIELDS = ("sources_count", "distinct_domains", "extra_sources_count", "total_sources_count")


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("GROK_API_URL", "https://api.example.test/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setenv("GROK_MODEL", "test-model")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.delenv("TAVILY_ENABLED", raising=False)
    monkeypatch.setattr(server.config, "_cached_model", None)
    monkeypatch.setattr(server, "_SOURCES_CACHE", SourcesCache())
    return server.mcp


def grok_returns(monkeypatch, value):
    mock = AsyncMock(side_effect=value) if isinstance(value, Exception) else AsyncMock(return_value=value)
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", mock)


def engines(monkeypatch, tavily=None, firecrawl=None):
    async def none(query, limit):
        return []
    monkeypatch.setattr(server, "_call_tavily_search", tavily or none)
    monkeypatch.setattr(server, "_call_firecrawl_search", firecrawl or none)


def hits(prefix, n, **fields):
    return [{"url": f"https://{prefix}{i}.example.test/p", "title": f"{prefix} {i}", **fields} for i in range(n)]


async def run(app, arguments, *, sources=True):
    async with Client(app) as client:
        result = await client.call_tool("web_search", {"query": "q", **arguments})
        assert not result.is_error
        data = json.loads(next(block.text for block in result.content if block.type == "text"))
        cached = None
        if sources:
            got = await client.call_tool("get_sources", {"session_id": data["session_id"]})
            cached = json.loads(next(block.text for block in got.content if block.type == "text"))
    return data, cached


# --- answers without native annotations ---------------------------------------------------------------

LINK_LIST = (
    "The official homepages of the Python, Rust and Go programming languages are as follows.\n"
    "- [Python](https://www.python.org/): A versatile, high-level language.\n"
    "- [Rust](https://www.rust-lang.org/): A performant systems language.\n"
    "- [Go](https://go.dev/): An efficient compiled language."
)


@pytest.mark.asyncio
async def test_link_list_answer_without_annotations_is_returned_whole(app, monkeypatch):
    grok_returns(monkeypatch, GrokResponse(LINK_LIST))
    data, cached = await run(app, {})
    assert data["content"] == LINK_LIST and "error" not in data
    assert [data[k] for k in COUNT_FIELDS] == [3, 3, 0, 3]
    assert [s["url"] for s in cached["sources"]] == ["https://www.python.org/", "https://www.rust-lang.org/", "https://go.dev/"]
    assert cached["sources"][0] == {"url": "https://www.python.org/", "provider": "grok", "title": "Python"}


@pytest.mark.asyncio
async def test_answer_that_is_only_a_linked_list_is_not_a_failure(app, monkeypatch):
    only = "- [PEP 703](https://peps.python.org/pep-0703/) makes the GIL optional.\n- [PEP 744](https://peps.python.org/pep-0744/) adds a JIT."
    grok_returns(monkeypatch, GrokResponse(only))
    data, _ = await run(app, {}, sources=False)
    assert data["content"] == only and "error_type" not in data and data["sources_count"] == 2


@pytest.mark.asyncio
async def test_inline_markers_without_annotations_become_sources_with_offsets(app, monkeypatch):
    a, b = "https://a.example.test/x", "https://b.example.test/y"
    body = f"Fact one.[[1]]({a})[[1]]({a}) Fact two.[[2]]({b}) Fact three.[[1]]({a})"
    grok_returns(monkeypatch, GrokResponse(body))
    data, cached = await run(app, {})
    content = data["content"]
    assert content == f"Fact one.[[1]]({a}) Fact two.[[2]]({b}) Fact three.[[1]]({a})"
    assert (data["sources_count"], data["distinct_domains"]) == (2, 2)
    assert "citation_positions" not in data  # those describe native offsets only
    occurrences = [(s["url"], c) for s in cached["sources"] for c in s["citations"]]
    assert len(occurrences) == len(find_markers(content)) == 3
    for url, c in occurrences:
        assert content[c["start_index"]:c["end_index"]] == f"[[{c['label']}]]({url})"


@pytest.mark.asyncio
async def test_pure_source_list_under_heading_is_still_split_off(app, monkeypatch):
    grok_returns(monkeypatch, GrokResponse("Answer.\n\n## Sources\n- https://a.example.test/1\n- [B](https://b.example.test/2)"))
    data, cached = await run(app, {})
    assert data["content"] == "Answer."
    assert [s["url"] for s in cached["sources"]] == ["https://a.example.test/1", "https://b.example.test/2"]
    assert all(s["provider"] == "grok" for s in cached["sources"])


# --- counts: cited vs extra ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_counts_cover_cited_sources_and_extras_are_reported_separately(app, monkeypatch):
    grok_returns(monkeypatch, GrokResponse(f"answer [[1]]({GROK_URL})", [{"type": "url_citation", "url": GROK_URL, "title": "1"}]))
    engines(monkeypatch, tavily=AsyncMock(return_value=hits("t", 5)), firecrawl=AsyncMock(return_value=hits("f", 5)))
    data, cached = await run(app, {"extra_sources": 6})
    assert [data[k] for k in COUNT_FIELDS] == [1, 1, 6, 7]
    assert data["extra_sources_report"]["returned"] == 6
    assert (cached["sources_count"], cached["cited_sources_count"], cached["extra_sources_count"]) == (7, 1, 6)
    assert [s["provider"] for s in cached["sources"]] == ["grok"] + ["tavily", "firecrawl"] * 3


@pytest.mark.asyncio
async def test_answer_with_no_citations_reports_zero_cited(app, monkeypatch):
    grok_returns(monkeypatch, GrokResponse("Plain answer without any link."))
    engines(monkeypatch, tavily=AsyncMock(return_value=hits("t", 3)), firecrawl=AsyncMock(return_value=hits("f", 3)))
    data, _ = await run(app, {"extra_sources": 2}, sources=False)
    assert [data[k] for k in COUNT_FIELDS] == [0, 0, 2, 2]


# --- Extra sources section ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extra_section_is_one_plain_line_per_source(app, monkeypatch):
    grok_returns(monkeypatch, GrokResponse("answer"))
    tavily = [
        {"url": "https://t.example.test/clients", "title": "# Clients - HTTPX\n[docs]",
         "content": "# Clients\n\nIf you are coming from Requests ... ## Usage\n```python\nwith httpx.Client() as client:\n```\n"
                    "See [persistent connections](https://en.wikipedia.org/wiki/HTTP_persistent_connection) and https://stray.example.test/x <b>bold</b>"},
        {"url": "https://t.example.test/init", "title": "Client.__init__() got an unexpected keyword argument 'proxies'", "content": "### Community\n__init__ changed"},
    ]
    engines(monkeypatch, tavily=AsyncMock(return_value=tavily))
    monkeypatch.delenv("FIRECRAWL_API_KEY")
    data, cached = await run(app, {"extra_sources": 2})
    section = data["content"].split("## Extra sources (independent search engines)\n", 1)[1]
    lines = section.split("\n")
    assert len(lines) == 2 and all(re.match(r"\d+\. \[", line) for line in lines)
    assert lines[0].startswith(r"1. [Clients - HTTPX \[docs\]](https://t.example.test/clients) [tavily] — ")
    assert r"Client.\_\_init\_\_() got" in lines[1]
    assert "#" not in section and "```" not in section and "<b>" not in section
    assert "persistent connections" in lines[0]
    listed = {s["url"] for s in cached["sources"]}
    assert set(re.findall(r"https?://[^\s)\]]+", data["content"])) <= listed


@pytest.mark.asyncio
async def test_extra_descriptions_are_capped_for_get_sources(app, monkeypatch):
    grok_returns(monkeypatch, GrokResponse("answer"))
    long_text = ("word " * 3000).strip()
    engines(monkeypatch, tavily=AsyncMock(return_value=hits("t", 3, content=long_text)))
    monkeypatch.delenv("FIRECRAWL_API_KEY")
    data, cached = await run(app, {"extra_sources": 3})
    for source in cached["sources"]:
        assert len(source["description"]) <= 501 and source["description"].endswith("…")
    assert len(data["content"]) < 2000


# --- failure contract -------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_grok_failure_still_shows_the_extra_sources(app, monkeypatch):
    grok_returns(monkeypatch, GrokUpstreamError("HTTP 400: bad request", error_type="http_400", retryable=False, status=400))
    engines(monkeypatch, tavily=AsyncMock(return_value=hits("t", 3)), firecrawl=AsyncMock(return_value=hits("f", 3)))
    data, cached = await run(app, {"extra_sources": 4})
    assert data["content"].startswith("[搜索失败] 上游搜索失败: HTTP 400")
    assert "4 条结果" in data["content"] and "get_sources" in data["content"]
    assert "## Extra sources" in data["content"] and data["content"].count("example.test/p") == 4
    assert [data[k] for k in COUNT_FIELDS] == [0, 0, 4, 4]
    assert data["error_type"] == "http_400" and cached["extra_sources_count"] == 4


@pytest.mark.asyncio
async def test_config_and_invalid_model_failures_use_the_common_prefix(app, monkeypatch):
    monkeypatch.delenv("GROK_API_KEY")
    data, _ = await run(app, {}, sources=False)
    assert data["content"].startswith("[搜索失败] 配置错误: ") and data["error_type"] == "config"
    assert [data[k] for k in COUNT_FIELDS] == [0, 0, 0, 0]

    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setattr(server, "_MODEL_CACHE", models.ModelListCache())

    async def fetch(api_url, api_key, timeout_s=10.0):
        return ["test-model", "grok-4.7"]

    monkeypatch.setattr(models, "fetch_available_models", fetch)
    data, _ = await run(app, {"model": "nope-model"}, sources=False)
    assert data["content"].startswith("[搜索失败] 无效模型: nope-model")
    assert data["error"] == "无效模型: nope-model" and data["error_type"] == "invalid_model"
    assert data["model_check"]["status"] == "invalid"


@pytest.mark.asyncio
async def test_model_check_is_kept_on_upstream_failure(app, monkeypatch):
    monkeypatch.setattr(server, "_MODEL_CACHE", models.ModelListCache())

    async def fetch(api_url, api_key, timeout_s=10.0):
        raise RuntimeError("/models down")

    monkeypatch.setattr(models, "fetch_available_models", fetch)
    grok_returns(monkeypatch, GrokUpstreamError("HTTP 400: unknown model", error_type="http_400", retryable=False, status=400))
    data, _ = await run(app, {"model": "grok-9-typo"}, sources=False)
    assert data["content"].startswith("[搜索失败]") and data["error_type"] == "http_400"
    assert data["model_check"]["status"] == "unverified"


# --- extra_sources backfill ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backfill_keeps_a_duplicate_margin(app, monkeypatch):
    calls = []

    async def fake(engine, query, limit):
        calls.append((engine, limit))
        if engine == "tavily":
            raise ProviderError("tavily", "upstream_error", "boom")
        return server._engine_items(engine, [{"url": f"https://f{i}.example/p"} for i in range(1, 21)][:limit])

    monkeypatch.setattr(server, "_run_engine", fake)
    job = server._ExtraSources("q", 1)
    await job.first_round()
    picked, report = await job.finish(["https://f1.example/p"])  # Firecrawl's first hit is already cited
    assert [item["url"] for item in picked] == ["https://f2.example/p"]
    assert report["returned"] == 1 and calls == [("tavily", 3), ("firecrawl", 3)]


# --- schema and descriptions -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extra_sources_schema_is_bounded(app):
    async with Client(app) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        schema = tools["web_search"].inputSchema["properties"]["extra_sources"]
        assert (schema["minimum"], schema["maximum"], schema["default"]) == (0, 20, 0)
        for bad in (-5, 21):
            result = await client.call_tool("web_search", {"query": "q", "extra_sources": bad}, raise_on_error=False)
            assert result.is_error


@pytest.mark.asyncio
async def test_descriptions_document_offsets_counts_and_error_types(app):
    async with Client(app) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    search, sources = tools["web_search"].description, tools["get_sources"].description
    assert "upstream offsets into the original" not in sources
    assert "code-point offsets" in sources and "content returned by web_search" in sources
    assert "500 characters" in sources
    for word in ("extra_sources_count", "total_sources_count", "lookup_failed", "Every failure starts with"):
        assert word in search
    for error_type in ("config", "invalid_model", "rate_limit", "circuit_open", "empty_response", "timeout",
                       "network", "truncated_stream", "http_<status>", "unknown", "model_not_found"):
        assert error_type in search
