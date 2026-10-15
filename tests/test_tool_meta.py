import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp import Client, FastMCP

from grok_search import server
from grok_search.providers.grok import GrokResponse, GrokSearchProvider
from grok_search.sources import SourcesCache
from grok_search.utils import build_search_prompt


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("GROK_API_URL", "https://api.example.test/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setenv("GROK_MODEL", "test-model")
    for key in ("GUDA_API_KEY", "TAVILY_API_KEY", "FIRECRAWL_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(server.config, "_cached_model", None)
    monkeypatch.setattr(server, "_SOURCES_CACHE", SourcesCache())
    return server.mcp


async def call(client, name, arguments):
    result = await client.call_tool(name, arguments)
    assert not result.is_error
    return json.loads(next(block.text for block in result.content if block.type == "text"))


@pytest.mark.asyncio
async def test_read_only_tools_are_annotated_and_planning_is_opt_in():
    async with Client(server.mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    for name in ("web_search", "web_fetch", "web_map", "get_sources", "get_config_info"):
        assert tools[name].annotations is not None and tools[name].annotations.readOnlyHint is True, name
    for name in ("switch_model", "toggle_builtin_tools"):
        assert not (tools[name].annotations and tools[name].annotations.readOnlyHint), name
    assert "plan_intent" not in tools
    assert "Before using this tool" not in (tools["web_search"].description or "")


@pytest.mark.asyncio
async def test_planning_tools_can_be_registered_on_demand():
    app = FastMCP("planning-test")
    server.register_planning_tools(app)
    async with Client(app) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert {"plan_intent", "plan_complexity", "plan_sub_query", "plan_search_term", "plan_tool_mapping", "plan_execution"} <= names


@pytest.mark.asyncio
async def test_instructions_are_appended_and_citation_rules_present(monkeypatch):
    provider = GrokSearchProvider("https://api.example.test/v1", "test-key")
    execute = AsyncMock(return_value=GrokResponse("ok"))
    monkeypatch.setattr(provider, "_execute_stream_result_with_retry", execute)
    await provider.search_with_sources("q", instructions="List arXiv IDs only")
    messages = execute.call_args.args[1]["messages"]
    assert "[Additional instructions from the caller]" in messages[1]["content"]
    assert messages[1]["content"].rstrip().endswith("List arXiv IDs only")
    assert "Never combine numbers" in messages[0]["content"]
    await provider.search_with_sources("q")
    assert "[Additional instructions" not in execute.call_args.args[1]["messages"][1]["content"]


def test_concise_style_drops_explanatory_rules_but_keeps_strategy():
    concise = build_search_prompt("concise")
    explanatory = build_search_prompt("explanatory")
    assert "real-world analogies" in explanatory and "Define every technical term" in explanatory
    assert "real-world analogies" not in concise and "Define every technical term" not in concise
    for prompt in (concise, explanatory):
        assert "Breadth-First Search" in prompt and "Depth-First Search" in prompt
        assert "Never combine numbers" in prompt and "Never invent identifiers" in prompt


@pytest.mark.asyncio
async def test_style_env_selects_prompt(monkeypatch):
    provider = GrokSearchProvider("https://api.example.test/v1", "test-key")
    execute = AsyncMock(return_value=GrokResponse("ok"))
    monkeypatch.setattr(provider, "_execute_stream_result_with_retry", execute)
    monkeypatch.setenv("GROK_SEARCH_STYLE", "concise")
    await provider.search_with_sources("q")
    assert "real-world analogies" not in execute.call_args.args[1]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_switch_model_warns_when_env_pins_model(configured, monkeypatch, tmp_path):
    monkeypatch.setattr(server.config, "_config_file", tmp_path / "config.json")
    async with Client(configured) as client:
        data = await call(client, "switch_model", {"model": "other-model"})
    assert data["current_model"] == "other-model"
    assert "GROK_MODEL" in data["warning"] and "test-model" in data["warning"]


@pytest.mark.asyncio
async def test_extra_sources_are_appended_and_domains_counted(configured, monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-extra-key")
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", AsyncMock(return_value=GrokResponse("answer", [{"type": "url_citation", "url": "https://a.example.test/x", "title": "1"}])))
    monkeypatch.setattr(server, "_call_firecrawl_search", AsyncMock(return_value=[{"url": "https://www.b.example.test/y", "title": "Extra", "description": "desc"}]))
    async with Client(configured) as client:
        result = await call(client, "web_search", {"query": "q", "extra_sources": 1})
    assert result["content"].startswith("answer\n\n## Extra sources")
    assert "[Extra](https://www.b.example.test/y) [firecrawl]" in result["content"]
    assert result["sources_count"] == 2 and result["distinct_domains"] == 2


@pytest.mark.asyncio
async def test_web_search_attaches_verification_when_enabled(configured, monkeypatch):
    monkeypatch.setenv("GROK_VERIFY_IDS", "true")
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", AsyncMock(return_value=GrokResponse("see 2607.06065")))
    fake = AsyncMock(return_value={"arxiv": [{"id": "2607.06065", "title": "X"}], "doi": [], "urls": [], "unresolved": []})
    monkeypatch.setattr(server, "verify_answer", fake)
    async with Client(configured) as client:
        with_check = await call(client, "web_search", {"query": "q"})
        without = await call(client, "web_search", {"query": "q", "verify": False})
    assert with_check["verification"]["arxiv"][0]["id"] == "2607.06065"
    assert "verification" not in without
    fake.assert_awaited_once()


@pytest.mark.asyncio
async def test_rate_limit_failure_is_structured(configured, monkeypatch):
    from grok_search.providers.grok import GrokUpstreamError
    error = GrokUpstreamError("HTTP 429: No available accounts for this model tier", error_type="rate_limit", retryable=False, retry_after=42, status=429, exhausted=True)
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", AsyncMock(side_effect=error))
    async with Client(configured) as client:
        result = await call(client, "web_search", {"query": "q"})
    assert result["content"].startswith("[搜索失败]") and "42 秒后重试" in result["content"] and "账号池已耗尽" in result["content"]
    assert result["error_type"] == "rate_limit" and result["retry_after_s"] == 42 and result["exhausted"] is True and result["status"] == 429


@pytest.mark.asyncio
async def test_get_config_info_reports_breaker_and_version(configured, monkeypatch):
    from grok_search.throttle import breaker
    breaker.record_failure(("https://api.example.test/v1", "test-model"), exhausted=True, retry_after=30)

    async def handler(request):
        return httpx.Response(200, json={"data": [{"id": "test-model"}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    async with Client(configured) as client:
        data = await call(client, "get_config_info", {})
    assert data["server_version"]
    assert data["breaker"]["test-model @ https://api.example.test/v1"]["state"] == "open"
    assert data["GROK_MODEL_SOURCE"] == "env"
