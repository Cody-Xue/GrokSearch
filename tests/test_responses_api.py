"""GROK_API_MODE=responses: the Responses API request, its event stream and the web_search result.

fixtures/responses_stream.sse is a real stream from the CPA gateway (grok-4.7 with web_search and
x_search), shortened: each web_search_call keeps two of its sources and reasoning items lose their
encrypted content. The text deltas, annotations and usage are unchanged.
"""
import json
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from tenacity import wait_none

from grok_search import server
from grok_search.config import Config
from grok_search.models import ModelListCache
from grok_search.providers import grok
from grok_search.providers.grok import GrokSearchProvider, GrokUpstreamError
from grok_search.sources import SourcesCache

FIXTURE = Path(__file__).parent / "fixtures" / "responses_stream.sse"
API = "https://api.example.test/v1"
REAL_CLIENT = httpx.AsyncClient
FIXTURE_QUERIES = [
    "latest stable Rust release version and date",
    "Rust release notes latest stable",
    "site:blog.rust-lang.org latest Rust release",
]


def event(kind: str, **fields) -> str:
    return f"event: {kind}\ndata: " + json.dumps({"type": kind, **fields}) + "\n\n"


def delta(text: str, output_index: int = 1, content_index: int = 0) -> str:
    return event("response.output_text.delta", output_index=output_index, content_index=content_index, delta=text)


def finished(status: str = "completed", **response) -> str:
    return event(f"response.{status}", response={"object": "response", "status": status, **response})


def provider(mode: str = "responses") -> GrokSearchProvider:
    return GrokSearchProvider(API, "test-key", "grok-4.7", api_mode=mode)


async def parse(body: str, mode: str = "responses"):
    return await provider(mode)._parse_streaming_result(httpx.Response(200, text=body))


def mock_upstream(monkeypatch, handler):
    monkeypatch.setattr(grok.httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(grok, "_WaitWithRetryAfter", lambda *args: wait_none())


# ------------------------------------------------------------------ setting
def test_api_mode_setting(monkeypatch):
    config = Config()
    monkeypatch.delenv("GROK_API_MODE", raising=False)
    assert config.api_mode == "chat"
    monkeypatch.setenv("GROK_API_MODE", " Responses ")
    assert config.api_mode == "responses"
    monkeypatch.setenv("GROK_API_MODE", "realtime")
    assert config.api_mode == "chat"
    assert any("GROK_API_MODE" in warning for warning in config.config_warnings())
    monkeypatch.setenv("GROK_API_MODE", "chat")
    assert config.api_mode == "chat"
    assert not any("GROK_API_MODE" in warning for warning in config.config_warnings())


def test_provider_follows_the_setting_unless_given_a_mode(monkeypatch):
    monkeypatch.setenv("GROK_API_MODE", "responses")
    assert GrokSearchProvider(API, "k", "m").api_mode == "responses"
    assert GrokSearchProvider(API, "k", "m", api_mode="chat").api_mode == "chat"
    monkeypatch.delenv("GROK_API_MODE")
    assert GrokSearchProvider(API, "k", "m").api_mode == "chat"


# ------------------------------------------------------------------ request
@pytest.mark.asyncio
@pytest.mark.parametrize("mode, path", [("responses", "/v1/responses"), ("chat", "/v1/chat/completions")])
async def test_request_endpoint_and_body(monkeypatch, mode, path):
    seen = []

    def handler(request):
        seen.append(request)
        if mode == "responses":
            return httpx.Response(200, text=delta("ok") + finished())
        return httpx.Response(200, text='data: {"choices": [{"delta": {"content": "ok"}}]}\n\ndata: [DONE]\n\n')

    mock_upstream(monkeypatch, handler)
    result = await provider(mode).search_with_sources("latest Rust release")
    assert result.content == "ok"
    assert seen[0].url.path == path
    body = json.loads(seen[0].content)
    assert body["model"] == "grok-4.7" and body["stream"] is True
    assert body["tools"] == [{"type": "web_search"}, {"type": "x_search"}]
    conversation = body["input"] if mode == "responses" else body["messages"]
    assert [m["role"] for m in conversation] == ["system", "user"]
    assert "latest Rust release" in conversation[1]["content"]
    if mode == "responses":
        assert body["store"] is False and "messages" not in body
    else:
        assert "input" not in body and "store" not in body


# ------------------------------------------------------------------ the real stream
@pytest.mark.asyncio
async def test_real_gateway_stream():
    result = await parse(FIXTURE.read_text(encoding="utf-8"))
    assert result.content.startswith("The latest stable Rust release is **1.99.1**")
    assert len(result.annotations) == 4
    for annotation in result.annotations:
        # Offsets count code points of the returned answer and cover its [[n]](url) marker.
        marker = result.content[annotation["start_index"]:annotation["end_index"]]
        assert marker == f"[[{annotation['title']}]]({annotation['url']})"
    assert result.finish_reason == "stop" and result.done_seen and not result.incomplete
    assert result.search_queries == FIXTURE_QUERIES
    assert result.compact_usage() == {
        "prompt_tokens": 19814, "completion_tokens": 654, "reasoning_tokens": 319, "cached_tokens": 4480,
        "cost_usd": 0.0275, "web_search_calls": 3,
    }
    fields = result.result_fields()
    assert fields["search_queries"] == FIXTURE_QUERIES and "incomplete" not in fields


@pytest.mark.asyncio
async def test_reasoning_and_progress_events_stay_out_of_the_answer():
    body = (
        event("response.created", response={"status": "in_progress", "error": None})
        + event("response.reasoning_summary_text.delta", output_index=0, delta="private thinking")
        + event("response.web_search_call.searching", output_index=1)
        + delta("Answer.", output_index=2)
        + finished()
    )
    result = await parse(body)
    assert result.content == "Answer."


@pytest.mark.asyncio
async def test_annotation_offsets_of_a_second_text_part_are_shifted():
    first, second = "First [[1]](https://a.test/).", "Second [[2]](https://b.test/)."
    body = (
        delta(first, output_index=1)
        + event("response.output_text.annotation.added", output_index=1, content_index=0,
                annotation={"type": "url_citation", "url": "https://a.test/", "title": "1", "start_index": 6, "end_index": 28})
        + delta(second, output_index=3)
        + event("response.output_text.annotation.added", output_index=3, content_index=0,
                annotation={"type": "url_citation", "url": "https://b.test/", "title": "2", "start_index": 7, "end_index": 29})
        + finished()
    )
    result = await parse(body)
    assert result.content == first + "\n\n" + second
    assert [result.content[a["start_index"]:a["end_index"]] for a in result.annotations] == [
        "[[1]](https://a.test/)", "[[2]](https://b.test/)",
    ]


@pytest.mark.asyncio
async def test_non_streaming_response_object():
    text = "Hi [[1]](https://a.test/)"
    body = {
        "object": "response", "status": "completed",
        "output": [
            {"type": "web_search_call", "status": "completed", "action": {"type": "search", "query": "greeting"}},
            {"type": "message", "content": [{"type": "output_text", "text": text, "annotations": [
                {"type": "url_citation", "url": "https://a.test/", "title": "1", "start_index": 3, "end_index": 25}]}]},
        ],
        "usage": {"input_tokens": 5, "output_tokens": 2, "output_tokens_details": {"reasoning_tokens": 1}},
    }
    result = await provider()._parse_streaming_result(httpx.Response(200, json=body))
    assert result.content == text and result.done_seen and result.finish_reason == "stop"
    assert text[result.annotations[0]["start_index"]:result.annotations[0]["end_index"]] == "[[1]](https://a.test/)"
    assert result.search_queries == ["greeting"]
    assert result.compact_usage() == {"prompt_tokens": 5, "completion_tokens": 2, "reasoning_tokens": 1}


# ------------------------------------------------------------------ how the answer ended
@pytest.mark.asyncio
@pytest.mark.parametrize("reason, finish", [("max_output_tokens", "length"), ("content_filter", "content_filter")])
async def test_incomplete_response(reason, finish):
    result = await parse(delta("The answer is cut") + finished("incomplete", incomplete_details={"reason": reason}))
    assert result.incomplete
    assert (result.finish_reason, result.native_finish_reason) == (finish, reason)
    assert result.result_fields()["incomplete"] is True


@pytest.mark.asyncio
async def test_failed_response_is_classified():
    error = {"code": "rate_limit_exceeded", "message": "All credentials for model grok-4.7 are cooling down"}
    with pytest.raises(GrokUpstreamError) as info:
        await parse(delta("partial") + finished("failed", error=error))
    assert info.value.error_type == "rate_limit" and info.value.exhausted


@pytest.mark.asyncio
async def test_error_event_is_raised():
    body = delta("partial") + "event: error\ndata: " + json.dumps(
        {"type": "error", "code": "internal_server_error", "message": "upstream exploded"}) + "\n\n"
    with pytest.raises(GrokUpstreamError) as info:
        await parse(body)
    assert "upstream exploded" in str(info.value)


@pytest.mark.asyncio
async def test_chat_style_error_frame_on_the_responses_endpoint():
    body = delta("partial") + 'data: {"error": {"message": "No available accounts", "type": "rate_limit"}}\n\n'
    with pytest.raises(GrokUpstreamError) as info:
        await parse(body)
    assert info.value.error_type == "rate_limit"


@pytest.mark.asyncio
async def test_stream_without_a_final_event_is_retried_as_truncated(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, text=delta("The answer stops mid"))  # no response.completed
        return httpx.Response(200, text=delta("The whole answer.") + finished())

    mock_upstream(monkeypatch, handler)
    result = await provider().search_with_sources("q")
    assert result.content == "The whole answer." and len(calls) == 2


@pytest.mark.asyncio
async def test_http_error_on_the_responses_endpoint(monkeypatch):
    body = {"error": {"code": "model_not_found", "message": "unknown provider for model grok-x", "type": "invalid_request_error"}}
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, json=body)

    mock_upstream(monkeypatch, handler)
    with pytest.raises(GrokUpstreamError) as info:
        await provider().search_with_sources("q")
    assert info.value.status == 400 and not info.value.retryable and len(calls) == 1


# ------------------------------------------------------------------ web_search end to end
@pytest.mark.asyncio
async def test_web_search_in_responses_mode(monkeypatch):
    monkeypatch.setenv("GROK_API_MODE", "responses")
    monkeypatch.setenv("GROK_API_URL", API)
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setenv("GROK_MODEL", "grok-4.7")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setattr(server.config, "_cached_model", None)
    monkeypatch.setattr(server, "_SOURCES_CACHE", SourcesCache())
    monkeypatch.setattr(server, "_MODEL_CACHE", ModelListCache())
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, text=FIXTURE.read_text(encoding="utf-8"))

    mock_upstream(monkeypatch, handler)
    async with Client(server.mcp) as client:
        result = await client.call_tool("web_search", {"query": "What is the latest stable Rust release?"})
        data = json.loads(next(block.text for block in result.content if block.type == "text"))
        got = await client.call_tool("get_sources", {"session_id": data["session_id"]})
        sources = json.loads(next(block.text for block in got.content if block.type == "text"))
    assert paths == ["/v1/responses"]
    assert "error" not in data and data["content"].startswith("The latest stable Rust release is **1.99.1**")
    assert data["usage"]["cost_usd"] == 0.0275 and data["usage"]["web_search_calls"] == 3
    assert data["search_queries"] == FIXTURE_QUERIES and "search_queries_partial" not in data
    # Four annotated citations, plus the [GitHub tags](...) link the answer writes without an annotation.
    assert data["sources_count"] == 5 and "consulted_sources_count" not in data
    assert data["citation_positions"]["valid"] == data["citation_positions"]["checked"] == 4
    urls = [s["url"] for s in sources["sources"]]
    assert urls[0] == "https://blog.rust-lang.org/2026/10/15/Rust-1.99.1/"
    assert urls[-1] == "https://github.com/rust-lang/rust/tags" and sources["sources"][-1]["title"] == "GitHub tags"
    # The annotated sources take their title from the answer's link text.
    assert sources["sources"][0]["title"] == "official announcement"


# ------------------------------------------------------------------ robustness (review findings F1-F6)
def ann(url: str, title: str, start: int, end: int) -> dict:
    return {"type": "url_citation", "url": url, "title": title, "start_index": start, "end_index": end}


def message(text: str, annotations=()) -> dict:
    return {"type": "message", "content": [{"type": "output_text", "text": text, "annotations": list(annotations)}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [
    finished("failed", error={"code": "invalid_prompt", "message": "Invalid prompt: flagged by usage policy"}),
    'event: error\ndata: {"type": "error", "error": {"code": "invalid_prompt", "message": "flagged by usage policy"}}\n\n',
])
async def test_a_refused_prompt_is_not_resent(monkeypatch, frame):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=delta("partial") + frame)

    mock_upstream(monkeypatch, handler)
    with pytest.raises(GrokUpstreamError) as info:
        await provider().search_with_sources("q")
    assert not info.value.retryable and len(calls) == 1


@pytest.mark.asyncio
async def test_the_final_object_supplies_annotations_the_stream_did_not_send():
    text = "See [[1]](https://a.test/)"
    result = await parse(delta(text) + finished(output=[message(text, [ann("https://a.test/", "1", 4, 26)])]))
    assert result.content == text
    assert [text[a["start_index"]:a["end_index"]] for a in result.annotations] == ["[[1]](https://a.test/)"]


@pytest.mark.asyncio
async def test_the_final_object_repairs_a_lost_delta_frame():
    body = delta("Alpha ") + "data: {not json\n\n" + delta("gamma.") + finished(output=[message("Alpha beta gamma.")])
    result = await parse(body)
    assert result.content == "Alpha beta gamma." and result.dropped_events == 1


@pytest.mark.asyncio
async def test_text_only_in_output_text_done():
    body = event("response.output_text.done", output_index=1, content_index=0, text="Done text.") + finished()
    result = await parse(body)
    assert result.content == "Done text."


@pytest.mark.asyncio
async def test_annotations_are_not_counted_twice_when_the_final_object_repeats_them():
    text = "Hi [[1]](https://a.test/)"
    citation = ann("https://a.test/", "1", 3, 25)
    body = (event("response.output_text.annotation.added", output_index=1, content_index=0, annotation=citation)
            + finished(output=[message(text, [citation])]))
    result = await parse(body)
    assert result.content == text and len(result.annotations) == 1


@pytest.mark.asyncio
async def test_an_annotation_event_never_changes_the_text():
    # No output_index/content_index on the annotation event: it belongs to the part being written.
    citation = ann("https://a.test/", "1", 12, 34)
    body = (delta("Hello ") + event("response.output_text.annotation.added", item_id="msg_1", annotation=citation)
            + delta("world [[1]](https://a.test/).") + finished())
    result = await parse(body)
    assert result.content == "Hello world [[1]](https://a.test/)."
    assert result.annotations == [citation]
    assert result.content[12:34] == "[[1]](https://a.test/)"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "in_progress", "queued"])
async def test_a_final_status_other_than_completed_is_not_a_whole_answer(status):
    terminal = event("response.completed", response={"object": "response", "status": status})
    result = await parse(delta("partial answ") + terminal)
    assert result.incomplete and result.native_finish_reason == status


@pytest.mark.asyncio
async def test_an_error_at_the_top_of_a_failed_event_is_raised(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        frame = event("response.failed", error={"code": "invalid_request_error", "message": "bad tool config"})
        return httpx.Response(200, text=delta("partial") + frame)

    mock_upstream(monkeypatch, handler)
    with pytest.raises(GrokUpstreamError) as info:
        await provider().search_with_sources("q")
    assert "bad tool config" in str(info.value) and not info.value.retryable and len(calls) == 1


@pytest.mark.parametrize("ticks", [10 ** 400, float("inf"), float("nan"), -5, "275228800", True])
def test_unusable_cost_values_are_left_out(ticks):
    from grok_search.providers.grok import GrokResponse
    usage = GrokResponse(usage={"input_tokens": 1, "output_tokens": 1, "cost_in_usd_ticks": ticks}).compact_usage()
    assert usage == {"prompt_tokens": 1, "completion_tokens": 1}
