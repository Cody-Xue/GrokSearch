"""SSE parsing and stream integrity: line splitting, multi-line events, finish_reason, usage."""
import json

import httpx
import pytest
from fastmcp import Client
from tenacity import wait_none

from grok_search import server
from grok_search.providers import grok
from grok_search.providers.grok import GrokResponse, GrokSearchProvider, GrokUpstreamError, _SSELineSplitter
from grok_search.sources import SourcesCache

URL = "https://api.example.test/v1"


def frame(delta=None, finish=None, native=None, **extra) -> str:
    choice = {"index": 0, "delta": delta or {}, "finish_reason": finish}
    if native is not None or finish is not None:
        choice["native_finish_reason"] = native if native is not None else finish
    payload = {"choices": [choice], **extra}
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def complete(*deltas, finish="stop", native=None, usage=None) -> str:
    body = "".join(frame(d) for d in deltas)
    extra = {"usage": usage} if usage else {}
    return body + frame({}, finish=finish, native=native, **extra) + "data: [DONE]\n\n"


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


async def parse(body) -> GrokResponse:
    provider = GrokSearchProvider(URL, "test-key", "m")
    if isinstance(body, Chunks):
        return await provider._parse_streaming_result(httpx.Response(200, stream=body))
    return await provider._parse_streaming_result(httpx.Response(200, text=body))


REAL_CLIENT = httpx.AsyncClient


def mock_client(monkeypatch, handler):
    monkeypatch.setattr(grok.httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(grok, "_WaitWithRetryAfter", lambda *args: wait_none())


# -- line splitting -------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("sep", [" ", " ", "\x85", "\x0b", "\x1c"])
async def test_unicode_line_separators_inside_json_strings_are_kept(sep):
    body = complete({"content": "Alpha "}, {"content": f"Beta{sep}Gamma"}, {"content": " Delta."})
    result = await parse(body)
    assert result.content == f"Alpha Beta{sep}Gamma Delta."
    assert result.dropped_events == 0


@pytest.mark.asyncio
async def test_annotation_title_with_raw_nel_keeps_the_source():
    annotation = {"type": "url_citation", "url": "https://github.com/encode/httpx", "title": "httpx\x85docs", "start_index": 0, "end_index": 4}
    result = await parse(complete({"content": "text"}, {"annotations": [annotation]}))
    assert result.annotations == [annotation]


@pytest.mark.asyncio
async def test_multi_line_data_event_is_joined_per_sse_spec():
    body = 'data: {"choices": [{"delta":\ndata: {"content": "A [SPLIT]B"}}]}\n\n' + complete()
    result = await parse(body)
    assert result.content == "A [SPLIT]B"


@pytest.mark.asyncio
async def test_events_without_blank_lines_are_still_decoded_line_by_line():
    lines = [frame({"content": "one "}).strip(), frame({"content": "two"}).strip(), frame({}, finish="stop").strip(), "data: [DONE]"]
    result = await parse("\n".join(lines) + "\n")
    assert result.content == "one two" and result.finish_reason == "stop" and result.done_seen


@pytest.mark.asyncio
async def test_crlf_and_cr_line_endings_and_chunk_boundaries():
    crlf = complete({"content": "x"}).replace("\n", "\r\n")
    assert (await parse(crlf)).content == "x"
    cr = complete({"content": "y"}).replace("\n", "\r")
    assert (await parse(cr)).content == "y"
    # A CRLF split across two chunks is one line break, and a UTF-8 character split across chunks survives.
    first = frame({"content": "é"}).encode()
    cut = first.index("é".encode()) + 1
    body = Chunks(first[:cut], first[cut:-1] + b"\r", b"\n" + complete({"content": "!"}).encode())
    result = await parse(body)
    assert result.content == "é!" and result.dropped_events == 0


def test_line_splitter_treats_split_crlf_as_one_break():
    splitter = _SSELineSplitter()
    assert splitter.feed(b"a\r") == ["a"]
    assert splitter.feed(b"\nb\r\n\r\n") == ["b", ""]
    assert splitter.feed(b"tail", final=True) == ["tail"]


@pytest.mark.asyncio
async def test_undecodable_frames_are_counted_not_silently_lost():
    body = "data: {broken\n\n" + complete({"content": "ok"})
    result = await parse(body)
    assert result.content == "ok" and result.dropped_events == 1


# -- completion state and usage -------------------------------------------------


@pytest.mark.asyncio
async def test_finish_reason_native_reason_and_usage_are_recorded():
    usage = {"prompt_tokens": 1065603, "completion_tokens": 27894, "total_tokens": 1093497,
             "completion_tokens_details": {"reasoning_tokens": 21206}}
    result = await parse(complete({"content": "done"}, usage=usage))
    assert (result.finish_reason, result.native_finish_reason, result.done_seen) == ("stop", "stop", True)
    assert not result.incomplete
    assert result.result_fields() == {"usage": {"prompt_tokens": 1065603, "completion_tokens": 27894, "reasoning_tokens": 21206}}


@pytest.mark.asyncio
@pytest.mark.parametrize("finish, native", [("length", "max_output_tokens"), ("content_filter", None), ("stop", "max_output_tokens")])
async def test_cut_short_answers_are_flagged(finish, native):
    result = await parse(complete({"content": "cut because"}, finish=finish, native=native))
    fields = result.result_fields()
    assert fields["incomplete"] is True and fields["finish_reason"] == finish
    assert fields["native_finish_reason"] == (native if native is not None else finish)
    assert "finish_reason=" in result.incomplete_note()


@pytest.mark.asyncio
async def test_nonstream_json_body_counts_as_complete():
    body = {"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
    result = await parse(json.dumps(body))
    assert result.done_seen and result.finish_reason == "stop" and result.compact_usage() == {"prompt_tokens": 3, "completion_tokens": 2}


@pytest.mark.asyncio
async def test_stream_closed_without_finish_reason_or_done_is_retried_as_truncated(monkeypatch):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, text=frame({"content": "half a sent"}))  # clean close mid-answer
        return httpx.Response(200, text=complete({"content": "full answer"}))

    mock_client(monkeypatch, handler)
    result = await GrokSearchProvider(URL, "k", "m").search_with_sources("q")
    assert calls == 2 and result.content == "full answer"

    async def always_truncated(request):
        return httpx.Response(200, text=frame({"content": "half"}))

    mock_client(monkeypatch, always_truncated)
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "1")
    with pytest.raises(GrokUpstreamError) as info:
        await GrokSearchProvider(URL, "k", "m").search_with_sources("q")
    assert info.value.error_type == "truncated_stream" and info.value.retryable
    assert "2 attempt(s)" in str(info.value)


@pytest.mark.asyncio
async def test_done_without_finish_reason_is_accepted(monkeypatch):
    async def handler(request):
        return httpx.Response(200, text=frame({"content": "ok"}) + "data: [DONE]\n\n")

    mock_client(monkeypatch, handler)
    result = await GrokSearchProvider(URL, "k", "m").search_with_sources("q")
    assert result.content == "ok" and not result.incomplete


# -- web_search result fields ---------------------------------------------------


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("GROK_API_URL", URL)
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setenv("GROK_MODEL", "test-model")
    for key in ("TAVILY_API_KEY", "FIRECRAWL_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(server.config, "_cached_model", None)
    monkeypatch.setattr(server, "_SOURCES_CACHE", SourcesCache())
    return server.mcp


async def web_search(app, **args):
    async with Client(app) as client:
        result = await client.call_tool("web_search", {"query": "q", **args})
    return json.loads(next(block.text for block in result.content if block.type == "text"))


@pytest.mark.asyncio
async def test_web_search_reports_truncation_and_usage(configured, monkeypatch):
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "completion_tokens_details": {"reasoning_tokens": 3}}

    async def handler(request):
        return httpx.Response(200, text=complete({"content": "The answer recommends pinning below 1.0 because"},
                                                 finish="length", native="max_output_tokens", usage=usage))

    mock_client(monkeypatch, handler)
    data = await web_search(configured)
    assert data["incomplete"] is True
    assert (data["finish_reason"], data["native_finish_reason"]) == ("length", "max_output_tokens")
    assert data["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 3}
    assert data["content"].startswith("The answer recommends pinning below 1.0 because\n\n[回答不完整]")
    assert data["content"].rstrip().endswith("可能被截断或过滤。")


@pytest.mark.asyncio
async def test_complete_web_search_has_no_incomplete_flag(configured, monkeypatch):
    async def handler(request):
        return httpx.Response(200, text=complete({"content": "Fine."}))

    mock_client(monkeypatch, handler)
    data = await web_search(configured)
    assert data["content"] == "Fine." and "incomplete" not in data and "usage" not in data


# -- a stream that the upstream ended before any content -------------------------


@pytest.mark.parametrize("reason,native", [("length", "max_output_tokens"), ("content_filter", "content_filter")])
@pytest.mark.asyncio
async def test_empty_answer_ended_by_the_upstream_is_not_retried(monkeypatch, reason, native):
    # Output cap spent on reasoning, or the filter fired before the first token: resending the same
    # prompt costs another full request and, for content_filter, can get the model blocked on the gateway.
    calls = 0
    usage = {"prompt_tokens": 1000, "completion_tokens": 64, "completion_tokens_details": {"reasoning_tokens": 64}}

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=complete({"role": "assistant"}, finish=reason, native=native, usage=usage))

    mock_client(monkeypatch, handler)
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "3")
    with pytest.raises(GrokUpstreamError) as info:
        await GrokSearchProvider(URL, "k", "m").search_with_sources("q")
    assert calls == 1
    assert info.value.error_type == "incomplete" and not info.value.retryable
    assert f"finish_reason={reason}" in str(info.value)
    assert info.value.result_fields == {
        "incomplete": True, "finish_reason": reason, "native_finish_reason": native,
        "usage": {"prompt_tokens": 1000, "completion_tokens": 64, "reasoning_tokens": 64},
    }


@pytest.mark.asyncio
async def test_web_search_reports_why_the_upstream_returned_nothing(configured, monkeypatch):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=complete({"role": "assistant"}, finish="content_filter",
                                                 usage={"prompt_tokens": 7, "completion_tokens": 0}))

    mock_client(monkeypatch, handler)
    data = await web_search(configured, verify=False)
    assert calls == 1
    assert data["error_type"] == "incomplete"
    assert data["incomplete"] is True
    assert (data["finish_reason"], data["native_finish_reason"]) == ("content_filter", "content_filter")
    assert data["usage"] == {"prompt_tokens": 7, "completion_tokens": 0}
    assert data["content"].startswith("[搜索失败]") and "finish_reason=content_filter" in data["content"]
    assert "原样重试无效" in data["content"]


@pytest.mark.asyncio
async def test_empty_answer_with_stop_is_still_retried(monkeypatch):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, text=complete({"role": "assistant"}, usage={"prompt_tokens": 5}))
        return httpx.Response(200, text=complete({"content": "second try"}))

    mock_client(monkeypatch, handler)
    result = await GrokSearchProvider(URL, "k", "m").search_with_sources("q")
    assert calls == 2 and result.content == "second try"
