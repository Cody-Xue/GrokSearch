import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp import Client
from tenacity import wait_none

from grok_search import server
from grok_search.providers import grok
from grok_search.providers.grok import GrokResponse, GrokSearchProvider
from grok_search.citations import find_markers, normalize_citations
from grok_search.sources import SourcesCache, sources_from_annotations


@pytest.fixture
def provider():
    return GrokSearchProvider("https://api.example.test/v1", "test-key")


@pytest.fixture
def captured():
    return json.loads((Path(__file__).parent / "fixtures" / "grok_annotations.json").read_text(encoding="utf-8"))


def sse(*deltas):
    return "".join("data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n" for delta in deltas) + "data:[DONE]\n\n"


def citation(url="https://example.test/a", **fields):
    return {"type": "url_citation", "url": url, **fields}


@pytest.mark.asyncio
async def test_captured_annotation_only_chunks_keep_13_locations_and_7_sources(provider, captured):
    # Actual captured annotations, serialized into SSE; the original body was not retained.
    annotations = captured["annotations"]
    body = sse({"content": "answer"}, *({"annotations": [a]} for a in annotations))
    result = await provider._parse_streaming_result(httpx.Response(200, text=body))
    assert result.content == "answer"
    assert result.annotations == annotations
    sources = sources_from_annotations(result.annotations)
    assert len(annotations) == 13
    assert {source["url"] for source in sources} == set(captured["unique_urls"])
    assert len(sources) == 7
    assert sum(len(source["citations"]) for source in sources) == 13
    assert all("title" not in source for source in sources)
    assert sources[0]["citations"][0] == {"label": "1", "start_index": 54, "end_index": 107}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
async def test_null_content_and_nested_citations(provider, stream):
    annotation = {"type": "url_citation", "url_citation": {"url": "https://example.test/a", "title": "Page title", "start_index": 0, "end_index": 6}}
    message = {"content": None, "annotations": [annotation]}
    body = sse(message) if stream else json.dumps({"choices": [{"message": message}]}, indent=2)
    result = await provider._parse_streaming_result(httpx.Response(200, text=body))
    assert result.content == ""
    assert result.annotations == [annotation]
    assert sources_from_annotations(result.annotations) == [{"url": "https://example.test/a", "provider": "grok", "title": "Page title", "citations": [{"start_index": 0, "end_index": 6}]}]


@pytest.mark.asyncio
async def test_nonstream_json_preserves_answer_and_flat_annotations(provider):
    annotation = citation(title="2")
    body = {"choices": [{"message": {"content": "  answer\n", "annotations": [annotation]}}]}
    result = await provider._parse_streaming_result(httpx.Response(200, json=body))
    assert result == GrokResponse("  answer\n", [annotation])


@pytest.mark.asyncio
async def test_malformed_events_do_not_discard_valid_data(provider):
    malformed = [None, [], {"choices": None}, {"choices": [None]}, {"choices": [{"delta": None}]}]
    body = "data: invalid\n\n" + "".join("data:" + json.dumps(item) + "\n\n" for item in malformed)
    body += sse({"content": None, "annotations": "invalid"}, {"content": "OK", "annotations": [None, citation()]})
    result = await provider._parse_streaming_result(httpx.Response(200, text=body))
    assert result == GrokResponse("OK", [citation()])


def test_source_dedup_keeps_distinct_locations_and_rejects_non_url_annotations():
    first = citation(title="1", start_index=0, end_index=10)
    second = citation(title="1", start_index=20, end_index=30)
    annotations = [None, {}, {"type": "file_citation", "url": "https://example.test/file"}, citation(url="javascript:alert(1)"), {"type": "url_citation", "url_citation": None}, first, first, second]
    sources = sources_from_annotations(annotations)
    assert len(sources) == 1
    assert sources[0]["citations"] == [{"label": "1", "start_index": 0, "end_index": 10}, {"label": "1", "start_index": 20, "end_index": 30}]


@pytest.mark.asyncio
async def test_text_only_callers_remain_compatible(provider, monkeypatch):
    execute = AsyncMock(return_value=GrokResponse("Title: Example\nExtracts: Summary", [citation()]))
    monkeypatch.setattr(provider, "_execute_stream_result_with_retry", execute)
    assert await provider.search("query") == "Title: Example\nExtracts: Summary"
    assert execute.call_args.args[1]["tools"] == [{"type": "web_search"}, {"type": "x_search"}]
    assert await provider._parse_streaming_response(httpx.Response(200, text=sse({"content": "text"}))) == "text"


@pytest.mark.asyncio
async def test_retry_discards_partial_content_and_citations(provider, monkeypatch):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse({"content": "partial", "annotations": [citation("https://example.test/failed")]}).encode()
            raise httpx.RemoteProtocolError("interrupted response")

    attempts = 0

    async def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(200, stream=BrokenStream())
        return httpx.Response(200, text=sse({"content": "complete", "annotations": [citation()]}))

    real_client = httpx.AsyncClient
    monkeypatch.setattr(grok.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(grok, "_WaitWithRetryAfter", lambda *args: wait_none())
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "1")
    result = await provider.search_with_sources("query")
    assert attempts == 2
    assert result == GrokResponse("complete", [citation()])


@pytest.fixture
def configured_server(monkeypatch):
    monkeypatch.setenv("GROK_API_URL", "https://api.example.test/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setenv("GROK_MODEL", "test-model")
    monkeypatch.setenv("GROK_DEBUG", "false")
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
async def test_offsets_that_miss_the_answer_lose_their_coordinates(configured_server, captured, monkeypatch):
    # The captured offsets belong to an answer that was not retained; this body has no markers at all.
    body = "  " + "answer " * 300 + "\n\nSources:\n- [extra](https://example.test/extra)\n"
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", AsyncMock(return_value=GrokResponse(body, captured["annotations"])))
    async with Client(configured_server) as client:
        result = await call(client, "web_search", {"query": "test"})
        cached = await call(client, "get_sources", {"session_id": result["session_id"]})
    assert result["content"] == body
    assert result["sources_count"] == cached["sources_count"] == 8
    assert {item["url"] for item in cached["sources"][:7]} == set(captured["unique_urls"])
    occurrences = [occ for item in cached["sources"] for occ in item.get("citations", [])]
    assert len(occurrences) == 13
    assert all(occ["position"] == "removed" and "start_index" not in occ for occ in occurrences)
    assert (occurrences[0]["upstream_start_index"], occurrences[0]["upstream_end_index"]) == (54, 107)
    assert result["citation_positions"] == {"markers": 0, "duplicate_markers_removed": 0, "checked": 13, "valid": 0, "relocated": 0, "removed": 13}


U1, U2 = "https://example.test/one", "https://example.test/two"
MARKED = f"[[1]]({U1})[[1]]({U1})[[2]]({U2}) First fact.[[1]]({U1})[[1]]({U1}) Second fact.[[2]]({U2})"
CLEANED = f"[[1]]({U1})[[2]]({U2}) First fact.[[1]]({U1}) Second fact.[[2]]({U2})"


async def search_with(server_app, monkeypatch, content, annotations):
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", AsyncMock(return_value=GrokResponse(content, annotations)))
    async with Client(server_app) as client:
        result = await call(client, "web_search", {"query": "test"})
        cached = await call(client, "get_sources", {"session_id": result["session_id"]})
    return result, cached


@pytest.mark.asyncio
async def test_valid_offsets_follow_collapsed_duplicate_markers(configured_server, monkeypatch):
    annotations = [citation(m.url, title=m.label, start_index=m.start, end_index=m.end) for m in find_markers(MARKED)]
    result, cached = await search_with(configured_server, monkeypatch, MARKED, annotations)
    assert result["content"] == CLEANED
    report = result["citation_positions"]
    assert report["duplicate_markers_removed"] == 2 and report["valid"] == 6 and report["relocated"] == report["removed"] == 0
    occurrences = [(source["url"], occ) for source in cached["sources"] for occ in source["citations"]]
    assert len(occurrences) == 4  # one per remaining marker
    for url, occ in occurrences:
        assert "position" not in occ
        assert CLEANED[occ["start_index"]:occ["end_index"]] == f"[[{occ['label']}]]({url})"


@pytest.mark.asyncio
async def test_scaled_offsets_are_relocated_to_their_markers(configured_server, monkeypatch):
    annotations = [citation(m.url, title=m.label, start_index=m.start * 2, end_index=m.end * 2) for m in find_markers(MARKED)]
    result, cached = await search_with(configured_server, monkeypatch, MARKED, annotations)
    assert result["content"] == CLEANED and result["citation_positions"]["relocated"] == 6
    occurrences = [(source["url"], occ) for source in cached["sources"] for occ in source["citations"]]
    assert len(occurrences) == 4
    for url, occ in occurrences:
        assert occ["position"] == "relocated" and "upstream_start_index" in occ
        assert CLEANED[occ["start_index"]:occ["end_index"]] == f"[[{occ['label']}]]({url})"


def test_markdown_link_spans_are_accepted():
    text = "Fact ([example.test](https://example.test/a)). More."
    start, end = text.index("("), text.index(")).") + 2
    cleaned, sources, report = normalize_citations(text, [{"url": "https://example.test/a", "citations": [{"start_index": start, "end_index": end}]}])
    assert cleaned == text and report["valid"] == 1
    assert sources[0]["citations"] == [{"start_index": start, "end_index": end}]


def test_markers_without_annotations_are_deduplicated():
    cleaned, _, report = normalize_citations(MARKED, [])
    assert cleaned == CLEANED and report["duplicate_markers_removed"] == 2 and report["checked"] == 0


@pytest.mark.asyncio
async def test_legacy_sources_still_work_without_annotations(configured_server, monkeypatch):
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", AsyncMock(return_value=GrokResponse("Answer\n\nSources:\n- [Example](https://example.test/a)")))
    async with Client(configured_server) as client:
        result = await call(client, "web_search", {"query": "test"})
        cached = await call(client, "get_sources", {"session_id": result["session_id"]})
    assert result["content"] == "Answer"
    assert result["sources_count"] == cached["sources_count"] == 1
    assert cached["sources"][0]["url"] == "https://example.test/a"


@pytest.mark.asyncio
async def test_extra_sources_merge_with_native_citations(configured_server, monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-extra-key")
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", AsyncMock(return_value=GrokResponse("answer", [citation(title="1")])))
    monkeypatch.setattr(server, "_call_firecrawl_search", AsyncMock(return_value=[{"url": "https://example.test/a", "title": "Duplicate"}, {"url": "https://example.test/b", "title": "Extra"}]))
    async with Client(configured_server) as client:
        result = await call(client, "web_search", {"query": "test", "extra_sources": 2})
        cached = await call(client, "get_sources", {"session_id": result["session_id"]})
    # sources_count counts what Grok cited; the uncited extra hit is counted separately.
    assert (result["sources_count"], result["extra_sources_count"], result["total_sources_count"]) == (1, 1, 2)
    assert [item["provider"] for item in cached["sources"]] == ["grok", "firecrawl"]
    assert (cached["sources_count"], cached["cited_sources_count"], cached["extra_sources_count"]) == (2, 1, 1)
    assert cached["sources"][0]["citations"] == [{"label": "1"}]


@pytest.mark.asyncio
async def test_parallel_searches_keep_sources_in_their_own_sessions(configured_server, monkeypatch):
    async def search(self, query, platform=""):
        await asyncio.sleep(0)
        return GrokResponse(query, [citation("https://example.test/" + query)])

    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", search)
    async with Client(configured_server) as client:
        results = await asyncio.gather(*(call(client, "web_search", {"query": query}) for query in ("first", "second")))
        assert results[0]["session_id"] != results[1]["session_id"]
        for result in results:
            cached = await call(client, "get_sources", {"session_id": result["session_id"]})
            assert cached["sources_count"] == 1
            assert cached["sources"][0]["url"] == "https://example.test/" + result["content"]


@pytest.mark.asyncio
async def test_failed_search_does_not_reuse_previous_sources(configured_server, monkeypatch):
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", AsyncMock(side_effect=[GrokResponse("answer", [citation()]), RuntimeError("upstream failed")]))
    async with Client(configured_server) as client:
        first = await call(client, "web_search", {"query": "first"})
        second = await call(client, "web_search", {"query": "second"})
        cached = await call(client, "get_sources", {"session_id": second["session_id"]})
    assert first["sources_count"] == 1
    assert second["sources_count"] == cached["sources_count"] == 0
    assert second["content"].startswith("[搜索失败]")
    assert second["error_type"] == "unknown"
    assert "upstream failed" in second["error"]
