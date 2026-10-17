"""Cross-group behaviour of the merged web_search result and get_config_info (D2, D4-D7)."""
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp import Client

from grok_search import config as config_module
from grok_search import server
from grok_search.config import Config
from grok_search.models import ModelListCache
from grok_search.providers.grok import GrokResponse, GrokSearchProvider
from grok_search.sources import SourcesCache
from grok_search.throttle import BreakerOpen

_REAL_ASYNC_CLIENT = httpx.AsyncClient
CITED = "https://cited.example.test/paper"
ANSWER = "Version 2 is current [[1]](https://cited.example.test/paper)."


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("GROK_API_URL", "https://api.example.test/v1/")  # trailing slash is normalised (D1)
    monkeypatch.setenv("GROK_API_KEY", " test-key ")
    monkeypatch.setenv("GROK_MODEL", "test-model")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_ENABLED", raising=False)
    monkeypatch.setattr(server.config, "_cached_model", None)
    monkeypatch.setattr(server, "_SOURCES_CACHE", SourcesCache())
    monkeypatch.setattr(server, "_MODEL_CACHE", ModelListCache())
    return server.mcp


def grok_answers(monkeypatch, response: GrokResponse):
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", AsyncMock(return_value=response))


def tavily_finds(monkeypatch, n: int):
    async def tavily(query, limit):
        return [{"url": f"https://extra{i}.example.test/p", "title": f"extra {i}", "content": "preview"} for i in range(min(n, limit))]

    async def none(query, limit):
        return []

    monkeypatch.setattr(server, "_call_tavily_search", tavily)
    monkeypatch.setattr(server, "_call_firecrawl_search", none)


async def call(name, arguments):
    async with Client(server.mcp) as client:
        result = await client.call_tool(name, arguments)
        assert not result.is_error
        data = json.loads(next(block.text for block in result.content if block.type == "text"))
        sources = None
        if name == "web_search":
            got = await client.call_tool("get_sources", {"session_id": data["session_id"]})
            sources = json.loads(next(block.text for block in got.content if block.type == "text"))
    return data, sources


@pytest.mark.asyncio
async def test_incomplete_answer_with_extra_sources_keeps_note_counts_and_usage(app, monkeypatch):
    usage = {"prompt_tokens": 1000, "completion_tokens": 50, "completion_tokens_details": {"reasoning_tokens": 20}}
    grok_answers(monkeypatch, GrokResponse(ANSWER, finish_reason="length", usage=usage))
    tavily_finds(monkeypatch, 3)
    data, sources = await call("web_search", {"query": "q", "extra_sources": 3, "verify": False})

    assert data["incomplete"] is True and data["finish_reason"] == "length"
    assert data["usage"] == {"prompt_tokens": 1000, "completion_tokens": 50, "reasoning_tokens": 20}
    # D5: cited sources only in sources_count / distinct_domains; extras counted separately.
    assert (data["sources_count"], data["distinct_domains"], data["extra_sources_count"], data["total_sources_count"]) == (1, 1, 3, 4)
    assert (sources["sources_count"], sources["cited_sources_count"], sources["extra_sources_count"]) == (4, 1, 3)
    assert all(s.get("provider") == "tavily" for s in sources["sources"][1:])
    # D4: the note directly follows Grok's answer; the Extra sources section comes after it.
    content = data["content"]
    note = content.index("[回答不完整]")
    assert content.startswith(ANSWER) and note == len(ANSWER) + 2
    assert content.index("https://extra0.example.test/p") > note


@pytest.mark.asyncio
@pytest.mark.parametrize("ids, urls, attached", [
    ("true", "true", True), ("false", "true", True), ("true", "false", True), ("false", "false", False),
])
async def test_verification_gate_and_scope(app, monkeypatch, ids, urls, attached):
    monkeypatch.setenv("GROK_VERIFY_IDS", ids)
    monkeypatch.setenv("GROK_VERIFY_URLS", urls)
    monkeypatch.setenv("GROK_VERIFY_FAKE_IP", "allow")
    grok_answers(monkeypatch, GrokResponse(ANSWER))
    tavily_finds(monkeypatch, 2)
    seen = {}

    async def fake_verify(answer, sources, **kwargs):
        seen.update(answer=answer, urls=[s["url"] for s in sources], **kwargs)
        return {"summary": {"lookup_failed": 1}, "unresolved": [], "lookup_failed": [{"url": CITED, "reason": "HTTP 503"}]}

    monkeypatch.setattr(server, "verify_answer", fake_verify)
    data, _ = await call("web_search", {"query": "q", "extra_sources": 2})
    if not attached:
        assert "verification" not in data and not seen
        return
    # D5/D6: only Grok-cited sources are verified; IDs and URLs are gated independently.
    assert seen["urls"] == [CITED] and seen["answer"] == ANSWER
    assert seen["check_ids"] is (ids == "true") and seen["check_urls"] is (urls == "true")
    assert seen["fake_ip"] == "allow" and seen["doh_url"] == "https://cloudflare-dns.com/dns-query"
    assert data["verification"]["lookup_failed"][0]["reason"] == "HTTP 503"


def test_breaker_message_hides_url_credentials():
    error = BreakerOpen(("https://user:secret-pw@gw.example.test/v1?key=abc", "grok-4"), 30)
    assert "secret-pw" not in str(error) and "abc" not in str(error)
    assert "https://***@gw.example.test/v1?key=***" in str(error)


@pytest.mark.asyncio
async def test_connection_test_names_cloudflare_pages(app, monkeypatch):
    page = "<!DOCTYPE html><html><head><title>gw.example.test | 524: A timeout occurred</title></head>" + "x" * 5000

    async def handler(request):
        assert request.url.path == "/v1/models"  # no double slash from the trailing '/'
        return httpx.Response(524, text=page, headers={"content-type": "text/html", "server": "cloudflare"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs))
    data, _ = await call("get_config_info", {})
    assert data["connection_test"]["message"] == "HTTP 524: Cloudflare 524: origin timed out (no response within ~100 s)"
    assert data["GROK_API_URL"] == "https://api.example.test/v1"


def test_config_info_lists_timeouts_and_fake_ip_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Config, "_instance", None)
    monkeypatch.setattr(config_module, "_ENV_WARNINGS", {})
    monkeypatch.setenv("GROK_READ_TIMEOUT_S", "120")
    monkeypatch.setenv("GROK_SEARCH_TIMEOUT_S", "200")
    monkeypatch.setenv("GROK_VERIFY_FAKE_IP", "sometimes")
    monkeypatch.setenv("GROK_VERIFY_DOH_URL", "https://doh.example.test/resolve?token=doh-secret")
    info = Config().get_config_info()
    assert (info["GROK_READ_TIMEOUT_S"], info["GROK_SEARCH_TIMEOUT_S"]) == (120.0, 200.0)
    assert info["GROK_VERIFY_FAKE_IP"] == "doh"  # unknown value: default, with a warning
    assert any(w.startswith("GROK_VERIFY_FAKE_IP='sometimes'") for w in info["config_warnings"])
    assert info["GROK_VERIFY_DOH_URL"] == "https://doh.example.test/resolve?token=***"
