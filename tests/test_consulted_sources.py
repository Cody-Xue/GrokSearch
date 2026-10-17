"""Cited sources versus pages the search only consulted (v1.13.1 field report on GROK_API_MODE=responses).

With grok-4.7 the Responses API sent, next to the real citations, one url_citation annotation for every page
its search had opened: offsets 0/0 and the URL as title. v1.13.0 took all of them for citations: a Chinese
query reported sources_count 77 (61 of them such pages, cpython/Lib/ssl.py, rclone/commits, eslint issues),
an answer without a single citation reported 40 sources on 27 domains, citation_positions showed 61 checked
and 61 removed, and the 30 URL checks of the verification went to those pages while the 16 links the answer
actually wrote were skipped as over the limit. The answer itself used plain Markdown links, which
normalize_citations could not relocate offsets to.

fixtures/responses_multi_agent.sse is a real stream of the default model (grok-4.20-multi-agent-0309(high))
from the CPA gateway, 2026-10-17, with the encrypted reasoning content removed: it cites with [[n]] markers,
writes two more URLs as bare links, and bills 73 search calls of which the stream shows 5.
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp import Client
from tenacity import wait_none

from grok_search import server
from grok_search.citations import normalize_citations
from grok_search.models import ModelListCache
from grok_search.providers import grok
from grok_search.providers.grok import GrokResponse, GrokSearchProvider
from grok_search.sources import SourcesCache, consulted_identities, sources_from_annotations

MULTI_AGENT = Path(__file__).parent / "fixtures" / "responses_multi_agent.sse"
REAL_CLIENT = httpx.AsyncClient

RELEASE = "https://www.python.org/downloads/release/python-3150/"
WHATSNEW = "https://docs.python.org/3.15/whatsnew/3.15.html"
UNRELATED = [
    "https://github.com/python/cpython/blob/main/Lib/ssl.py",
    "https://github.com/rclone/rclone/commits",
    "https://github.com/eslint/eslint/issues/20607",
    "https://github.com/TypeError/secure/releases",
]
ANSWER = (
    f"Python 3.15.0 是最新稳定版，发布于 2026-10-09（[发布页面]({RELEASE})）。\n\n"
    f"新特性见 {WHATSNEW} 。"
)


def consulted(url: str) -> dict:
    """An annotation as grok-4.7's Responses stream sent it for every page its search opened."""
    return {"type": "url_citation", "url": url, "title": url, "start_index": 0, "end_index": 0}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("GROK_API_URL", "https://api.example.test/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setenv("GROK_MODEL", "test-model")
    for key in ("TAVILY_API_KEY", "FIRECRAWL_API_KEY", "GROK_VERIFY_DETAIL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(server.config, "_cached_model", None)
    monkeypatch.setattr(server, "_SOURCES_CACHE", SourcesCache())
    monkeypatch.setattr(server, "_MODEL_CACHE", ModelListCache())
    return server.mcp


async def search(app, monkeypatch, response: GrokResponse, verify: bool = False):
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", AsyncMock(return_value=response))
    checked = AsyncMock(return_value={"claim_support": "not_checked", "summary": {}})
    monkeypatch.setattr(server, "verify_answer", checked)
    if verify:
        monkeypatch.setenv("GROK_VERIFY_URLS", "true")
    async with Client(app) as client:
        result = await client.call_tool("web_search", {"query": "Python 最新稳定版", "verify": verify})
        data = json.loads(next(block.text for block in result.content if block.type == "text"))
        got = await client.call_tool("get_sources", {"session_id": data["session_id"]})
        cached = json.loads(next(block.text for block in got.content if block.type == "text"))
    return data, cached, checked


# ------------------------------------------------------------------ annotations
def test_an_empty_span_is_no_location_and_a_url_is_no_title():
    sources = sources_from_annotations([consulted(RELEASE), {**consulted(WHATSNEW), "title": "3", "start_index": 7, "end_index": 7}])
    assert sources == [
        {"url": RELEASE, "provider": "grok", "citations": []},
        {"url": WHATSNEW, "provider": "grok", "citations": [{"label": "3"}]},
    ]


def test_consulted_identities():
    annotations = [
        consulted(UNRELATED[0]),
        consulted(UNRELATED[1]), {"type": "url_citation", "url": UNRELATED[1] + "/", "start_index": 3, "end_index": 9},
        {**consulted(UNRELATED[2]), "title": "2"},  # a citation number: cited
        {"type": "url_citation", "url": UNRELATED[3]},  # no offsets at all: cited, as before
    ]
    # Only the first one has nothing but empty spans; the second also has a real span (another spelling).
    assert consulted_identities(annotations) == {"github.com/python/cpython/blob/main/Lib/ssl.py"}


# ------------------------------------------------------------------ web_search and get_sources
@pytest.mark.asyncio
async def test_consulted_pages_are_not_cited_sources(app, monkeypatch):
    annotations = [consulted(url) for url in (UNRELATED[0], RELEASE, UNRELATED[1], WHATSNEW, UNRELATED[2], UNRELATED[3])]
    data, cached, _ = await search(app, monkeypatch, GrokResponse(ANSWER, annotations))
    assert data["content"] == ANSWER
    # The two pages the answer links are cited; the four it never mentions were only consulted.
    assert (data["sources_count"], data["distinct_domains"], data["consulted_sources_count"]) == (2, 2, 4)
    assert data["total_sources_count"] == cached["sources_count"] == 2
    assert [s["url"] for s in cached["sources"]] == [RELEASE, WHATSNEW]
    assert cached["sources"][0]["title"] == "发布页面"  # the link text; the URL-as-title is dropped
    assert cached["consulted"] == [{"url": url, "provider": "grok"} for url in UNRELATED]
    assert cached["consulted_sources_count"] == 4
    # No location was claimed, so none was checked or removed.
    assert data["citation_positions"] == {"markers": 0, "duplicate_markers_removed": 0, "checked": 0, "valid": 0,
                                          "relocated": 0, "removed": 0, "consulted_only": 4}


@pytest.mark.asyncio
async def test_an_answer_without_citations_reports_none(app, monkeypatch):
    # BERT/ResNet in the field report: no citation in the answer, yet sources_count 40 on 27 domains.
    pages = [f"https://site{i}.example.test/page" for i in range(40)]
    data, cached, _ = await search(app, monkeypatch, GrokResponse("BERT 是 2018 年提出的预训练模型。", [consulted(u) for u in pages]))
    assert (data["sources_count"], data["distinct_domains"], data["consulted_sources_count"]) == (0, 0, 40)
    assert cached["sources"] == [] and len(cached["consulted"]) == 40


@pytest.mark.asyncio
async def test_get_sources_lists_at_most_100_consulted_pages(app, monkeypatch):
    pages = [f"https://site{i}.example.test/page" for i in range(130)]
    data, cached, _ = await search(app, monkeypatch, GrokResponse("答案。", [consulted(u) for u in pages]))
    assert data["consulted_sources_count"] == cached["consulted_sources_count"] == 130
    assert [c["url"] for c in cached["consulted"]] == pages[:100]


@pytest.mark.asyncio
async def test_verification_checks_the_links_the_answer_writes_first(app, monkeypatch):
    written = [f"https://docs{i}.example.test/a" for i in range(16)]
    answer = "要点：\n" + "\n".join(f"- 第 {i} 条（[来源 {i}]({url})）" for i, url in enumerate(written))
    annotations = [consulted(f"https://noise{i}.example.test/x") for i in range(61)]
    # One positioned annotation that does not match the text: cited, but checked after the written links.
    annotations.append({"type": "url_citation", "url": "https://elsewhere.example.test/p", "start_index": 5, "end_index": 40})
    data, _, checked = await search(app, monkeypatch, GrokResponse(answer, annotations), verify=True)
    assert data["sources_count"] == 17 and data["consulted_sources_count"] == 61
    verified = [s["url"] for s in checked.await_args.args[1]]
    assert verified == written + ["https://elsewhere.example.test/p"]


@pytest.mark.asyncio
async def test_misplaced_offsets_are_relocated_to_plain_markdown_links(app, monkeypatch):
    links = [("https://one.example.test/a", "第一个来源"), ("https://two.example.test/b", "第二个来源")]
    answer = "".join(f"事实 {i}。[{title}]({url})\n" for i, (url, title) in enumerate(links))
    # Offsets counted in another unit (UTF-16 code units of a longer text): they miss the links.
    annotations = [{"type": "url_citation", "url": url, "title": str(i + 1), "start_index": 100 + 50 * i, "end_index": 140 + 50 * i}
                   for i, (url, _) in enumerate(links)]
    data, cached, _ = await search(app, monkeypatch, GrokResponse(answer, annotations))
    assert data["citation_positions"]["relocated"] == 2 and data["citation_positions"]["removed"] == 0
    for source, (url, title) in zip(cached["sources"], links):
        occurrence = source["citations"][0]
        assert occurrence["position"] == "relocated" and answer[occurrence["start_index"]:occurrence["end_index"]] == f"[{title}]({url})"


def test_relocation_takes_markers_first_then_links_and_bare_urls_in_reading_order():
    url = "https://example.test/doc"
    text = f"See {url}. Also [the doc]({url}) and [[1]]({url})."
    sources = [{"url": url, "citations": [{"start_index": 500 + i, "end_index": 520 + i} for i in range(4)]}]
    _, out, report = normalize_citations(text, sources)
    spans = [text[c["start_index"]:c["end_index"]] for c in out[0]["citations"] if c.get("position") == "relocated"]
    assert spans == [f"[[1]]({url})", url, f"[the doc]({url})"]
    assert report["relocated"] == 3 and report["removed"] == 1


# ------------------------------------------------------------------ the real multi-agent stream
@pytest.mark.asyncio
async def test_real_multi_agent_stream(app, monkeypatch):
    monkeypatch.setenv("GROK_API_MODE", "responses")
    monkeypatch.setattr(grok.httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=MULTI_AGENT.read_text(encoding="utf-8"))), **kwargs))
    monkeypatch.setattr(grok, "_WaitWithRetryAfter", lambda *args: wait_none())
    checked = AsyncMock(return_value={"claim_support": "not_checked", "summary": {}})
    monkeypatch.setattr(server, "verify_answer", checked)
    monkeypatch.setenv("GROK_VERIFY_URLS", "true")
    async with Client(app) as client:
        result = await client.call_tool("web_search", {"query": "Python 最新的稳定版本是哪个？"})
        data = json.loads(next(block.text for block in result.content if block.type == "text"))
    assert data["content"].startswith("**Python 3.15.0**，发布日期为 **2026 年 10 月 9 日**")
    assert data["citation_positions"]["valid"] == data["citation_positions"]["checked"] == 6
    # Three annotated sources and the two python.org URLs the answer writes as bare links.
    assert data["sources_count"] == 5 and "consulted_sources_count" not in data
    assert [s["url"] for s in checked.await_args.args[1]] == [
        "https://blog.python.org/2026/10/python-3150-final-is-here/", "https://www.python.org/downloads/macos/",
        RELEASE, "https://www.python.org/downloads/", "https://devguide.python.org/versions/",
    ]
    # 73 billed search calls, 5 visible in the stream (2 searches, 3 page opens): the query list is partial.
    assert data["usage"]["web_search_calls"] == 73
    assert data["search_queries"] == ["latest stable Python version site:python.org",
                                      "Python release schedule OR latest version official"]
    assert data["search_queries_partial"] is True
