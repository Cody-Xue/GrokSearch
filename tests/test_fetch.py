import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp import Client

from grok_search import fetching, server, verify


@pytest.fixture(autouse=True)
def _fast_arxiv(monkeypatch):
    monkeypatch.setattr(verify, "ARXIV_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(verify, "ARXIV_RETRY_DELAY_S", 0.0)
    verify._ARXIV_CACHE.clear()
    verify._arxiv_gate.update(loop=None, lock=None, last_start=0.0)

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2607.20852v1</id>
    <title>Code Monitor Red Teaming for
  Public-Test-Passing Code</title>
    <summary>Visible tests are a common gate for LLM-generated code,
  but passing them does not certify specification correctness.</summary>
    <published>2026-07-23T02:23:09Z</published>
    <updated>2026-07-23T02:23:09Z</updated>
    <author><name>Junchi Liao</name></author>
    <author><name>Jiawen Deng</name></author>
    <arxiv:primary_category term="cs.AI"/>
    <category term="cs.AI"/>
  </entry>
</feed>"""


@pytest.mark.asyncio
async def test_short_tavily_result_falls_back_to_longer_firecrawl(monkeypatch):
    monkeypatch.setenv("GROK_FETCH_MIN_CHARS", "2000")
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value="skeleton " * 50))
    firecrawl = AsyncMock(return_value="full " * 1000)
    monkeypatch.setattr(fetching, "_call_firecrawl_scrape", firecrawl)
    out = await fetching.fetch_page("https://example.test/page")
    assert out.startswith("[web_fetch] source=firecrawl total_chars=5000")
    assert firecrawl.await_count == 1


@pytest.mark.asyncio
async def test_short_tavily_result_is_kept_when_firecrawl_fails(monkeypatch):
    monkeypatch.setenv("GROK_FETCH_MIN_CHARS", "2000")
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value="skeleton"))
    monkeypatch.setattr(fetching, "_call_firecrawl_scrape", AsyncMock(return_value=None))
    out = await fetching.fetch_page("https://example.test/page")
    assert out.startswith("[web_fetch] source=tavily total_chars=8")


@pytest.mark.asyncio
async def test_long_tavily_result_skips_firecrawl(monkeypatch):
    monkeypatch.setenv("GROK_FETCH_MIN_CHARS", "2000")
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value="x" * 5000))
    firecrawl = AsyncMock(return_value="y" * 9000)
    monkeypatch.setattr(fetching, "_call_firecrawl_scrape", firecrawl)
    out = await fetching.fetch_page("https://example.test/page")
    assert out.startswith("[web_fetch] source=tavily total_chars=5000")
    assert firecrawl.await_count == 0


@pytest.mark.asyncio
async def test_paging_header_and_slice(monkeypatch):
    text = "".join(str(i % 10) for i in range(10000))
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value=text))
    monkeypatch.setattr(fetching, "_call_firecrawl_scrape", AsyncMock(return_value=None))
    out = await fetching.fetch_page("https://example.test/page", max_chars=4000, offset=4000)
    header, body = out.split("\n\n", 1)
    assert header == "[web_fetch] source=tavily total_chars=10000 offset=4000 returned=4000 truncated=true next_offset=8000"
    assert body == text[4000:8000]
    tail = await fetching.fetch_page("https://example.test/page", max_chars=4000, offset=8000)
    assert tail.split("\n\n", 1)[0].endswith("returned=2000 truncated=false")


@pytest.mark.asyncio
async def test_default_max_chars_comes_from_config(monkeypatch):
    monkeypatch.setenv("GROK_FETCH_MAX_CHARS", "1000")
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value="z" * 3000))
    out = await fetching.fetch_page("https://example.test/page")
    assert "returned=1000 truncated=true next_offset=1000" in out.split("\n\n", 1)[0]


@pytest.mark.asyncio
async def test_arxiv_abs_uses_api_and_skips_extractors(monkeypatch):
    async def handler(request):
        assert request.url.host == "export.arxiv.org"
        assert request.url.params["id_list"] == "2607.20852"
        return httpx.Response(200, text=ATOM)

    real = httpx.AsyncClient
    monkeypatch.setattr(fetching.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    tavily = AsyncMock(return_value="skeleton")
    monkeypatch.setattr(fetching, "_call_tavily_extract", tavily)
    out = await fetching.fetch_page("https://arxiv.org/abs/2607.20852v1")
    assert out.startswith("[web_fetch] source=arxiv-api")
    assert "# Code Monitor Red Teaming for Public-Test-Passing Code" in out
    assert "**Authors:** Junchi Liao, Jiawen Deng" in out
    assert "Visible tests are a common gate for LLM-generated code, but passing them" in out
    assert tavily.await_count == 0


@pytest.mark.asyncio
async def test_arxiv_abs_recovers_from_one_406(monkeypatch):
    from grok_search import verify
    monkeypatch.setattr(verify, "ARXIV_RETRY_DELAY_S", 0)
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(406, text="Not Acceptable") if calls["n"] == 1 else httpx.Response(200, text=ATOM)

    real = httpx.AsyncClient
    monkeypatch.setattr(fetching.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    tavily = AsyncMock(return_value="skeleton")
    monkeypatch.setattr(fetching, "_call_tavily_extract", tavily)
    out = await fetching.fetch_page("https://arxiv.org/abs/2607.20852")
    assert out.startswith("[web_fetch] source=arxiv-api") and calls["n"] == 2 and tavily.await_count == 0


@pytest.mark.asyncio
async def test_arxiv_api_failure_falls_back_to_extractors(monkeypatch):
    from grok_search import verify
    monkeypatch.setattr(verify, "ARXIV_RETRY_DELAY_S", 0)

    async def handler(request):
        return httpx.Response(503, text="down")

    real = httpx.AsyncClient
    monkeypatch.setattr(fetching.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value="p" * 3000))
    out = await fetching.fetch_page("https://arxiv.org/abs/2607.20852")
    assert out.startswith("[web_fetch] source=tavily")


@pytest.mark.asyncio
async def test_mcp_web_fetch_passes_paging_arguments(monkeypatch):
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value="k" * 500))
    monkeypatch.setattr(fetching, "_call_firecrawl_scrape", AsyncMock(return_value=None))
    async with Client(server.mcp) as client:
        result = await client.call_tool("web_fetch", {"url": "https://example.test/p", "max_chars": 100, "offset": 50})
    text = next(block.text for block in result.content if block.type == "text")
    assert text.startswith("[web_fetch] source=tavily total_chars=500 offset=50 returned=100 truncated=true next_offset=150")
