"""Classification of gateway errors: CPA pool vocabulary, Retry-After, Cloudflare 52x, redaction order."""
import json
from types import SimpleNamespace

import httpx
import pytest
from tenacity import wait_none

from grok_search.providers import grok
from grok_search.providers.grok import (
    GrokSearchProvider,
    GrokUpstreamError,
    _WaitWithRetryAfter,
    _http_error,
    _make_stream_error,
)
from grok_search.throttle import BreakerOpen, breaker

URL = "https://api.example.test/v1"
MODEL = "grok-4.20-multi-agent-0309(high)"
KEY = (URL, "grok-4.20-multi-agent-0309")
REAL_CLIENT = httpx.AsyncClient

# Byte-identical shapes of CLIProxyAPI (CPA) error bodies (see the audit's cpa_shapes.py).
AUTH_UNAVAILABLE_TEXT = ("auth_unavailable: no auth available (providers=xai, model=grok-4.20-multi-agent-0309(high); "
                         "last upstream error: I'm sorry, I can't help with that request.)")
AUTH_UNAVAILABLE = json.dumps({"error": {"message": AUTH_UNAVAILABLE_TEXT, "type": "server_error", "code": "internal_server_error"}})
TERMINAL_AUTH = json.dumps({"error": {"message": AUTH_UNAVAILABLE_TEXT, "type": "authentication_error",
                                      "code": "upstream_authentication_required", "retryable": False}})
AUTH_NOT_FOUND = json.dumps({"error": {"message": "auth_not_found: no auth available (providers=xai, model=grok-4.20-multi-agent-0309(high))",
                                       "type": "server_error", "code": "internal_server_error"}})
MODEL_COOLDOWN = json.dumps({"error": {"code": "model_cooldown", "message": "All credentials for model grok-4.20-multi-agent-0309 are cooling down via provider xai",
                                       "model": "grok-4.20-multi-agent-0309", "reset_seconds": 86400, "provider": "xai"}})
XAI_FREE_USAGE = '{"code":"subscription:free-usage-exhausted","error":"You\'ve used all the included free usage for now."}'
CF_524_PAGE = """<!DOCTYPE html>
<!--[if lt IE 7]> <html class="no-js ie6 oldie" lang="en-US"> <![endif]-->
<!--[if IE 7]>    <html class="no-js ie7 oldie" lang="en-US"> <![endif]-->
<!--[if IE 8]>    <html class="no-js ie8 oldie" lang="en-US"> <![endif]-->
<!--[if gt IE 8]><!--> <html class="no-js" lang="en-US"> <!--<![endif]-->
<head>
<title>cpa.example.test | 524: A timeout occurred</title>
</head><body>Visit <a href="https://www.cloudflare.com/5xx-error-landing">cloudflare.com</a></body></html>"""


def sse_ok(text="ok"):
    return ("data: " + json.dumps({"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}) + "\n\n"
            + "data: [DONE]\n\n")


class Upstream:
    """Answers chat requests from a queue of (status, body, headers); the last entry repeats."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    async def __call__(self, request):
        self.calls += 1
        status, body, headers = self.responses[min(self.calls, len(self.responses)) - 1]
        return httpx.Response(status, text=body, headers=headers or {})


def mock_client(monkeypatch, handler):
    monkeypatch.setattr(grok.httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(grok, "_WaitWithRetryAfter", lambda *args: wait_none())


async def search(model=MODEL):
    return await GrokSearchProvider(URL, "test-key", model).search_with_sources("q")


# -- CPA pool exhaustion (429/503) ------------------------------------------------


@pytest.mark.asyncio
async def test_cpa_503_auth_unavailable_is_an_exhausted_pool(monkeypatch):
    upstream = Upstream((503, AUTH_UNAVAILABLE, {"Retry-After": "1372"}))
    mock_client(monkeypatch, upstream)
    with pytest.raises(GrokUpstreamError) as info:
        await search()
    err = info.value
    assert upstream.calls == 1  # no blind retries: the 1372 s cooldown exceeds the retry budget
    assert (err.error_type, err.exhausted, err.retry_after, err.status) == ("rate_limit", True, 1372, 503)
    assert breaker.is_open(KEY)
    with pytest.raises(BreakerOpen):
        await search("grok-4.20-multi-agent-0309(low)")  # same base model, same pool
    assert upstream.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body, headers", [
    (MODEL_COOLDOWN, {"Retry-After": "86400"}),
    (XAI_FREE_USAGE, None),
])
async def test_cpa_and_xai_429_quota_bodies_open_the_breaker_on_the_first_call(monkeypatch, body, headers):
    upstream = Upstream((429, body, headers))
    mock_client(monkeypatch, upstream)
    with pytest.raises(GrokUpstreamError) as info:
        await search()
    assert upstream.calls == 1 and info.value.exhausted and info.value.error_type == "rate_limit"
    assert breaker.is_open(KEY)


@pytest.mark.asyncio
async def test_short_cooldown_is_waited_out_once_then_succeeds(monkeypatch):
    short = json.loads(MODEL_COOLDOWN)
    short["error"]["reset_seconds"] = 12
    upstream = Upstream((429, json.dumps(short), {"Retry-After": "12"}), (200, sse_ok("after cooldown"), None))
    mock_client(monkeypatch, upstream)
    waits = []  # what the real wait strategy asks for; the test itself does not sleep
    monkeypatch.setattr(grok, "_WaitWithRetryAfter", lambda *args: (lambda retry_state: waits.append(_WaitWithRetryAfter(*args)(retry_state)) or 0))
    assert (await search()).content == "after cooldown"
    assert upstream.calls == 2 and waits == [12.0]  # exactly the announced cooldown, not a blind backoff
    assert breaker.snapshot() == {}  # the success closed the breaker the cooldown had opened

    # Still cooling down after the announced time: waited out once, not again and again.
    persistent = Upstream((429, json.dumps(short), {"Retry-After": "12"}))
    mock_client(monkeypatch, persistent)
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "3")
    with pytest.raises(GrokUpstreamError) as info:
        await search("grok-4.7")
    assert persistent.calls == 2 and info.value.exhausted and info.value.retry_after == 12


@pytest.mark.asyncio
async def test_exhausted_pool_without_recovery_time_is_not_retried(monkeypatch):
    upstream = Upstream((429, XAI_FREE_USAGE, None), (200, sse_ok(), None))
    mock_client(monkeypatch, upstream)
    with pytest.raises(GrokUpstreamError) as info:
        await search()
    assert upstream.calls == 1 and info.value.exhausted and not info.value.retryable


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [TERMINAL_AUTH, AUTH_NOT_FOUND])
async def test_deterministic_cpa_503s_are_not_retried(monkeypatch, body):
    upstream = Upstream((503, body, None))
    mock_client(monkeypatch, upstream)
    with pytest.raises(GrokUpstreamError) as info:
        await search()
    assert upstream.calls == 1
    assert info.value.error_type == "auth" and not info.value.retryable and not info.value.exhausted
    assert not breaker.is_open(KEY)


def test_explicit_retryable_false_on_a_5xx_is_honoured():
    body = json.dumps({"error": {"message": "nope", "type": "server_error", "retryable": False}})
    err = _http_error(502, body, httpx.Headers())
    assert err.error_type == "http_502" and not err.retryable


@pytest.mark.parametrize("payload, retry_after", [
    ({"error": json.loads(MODEL_COOLDOWN)["error"]}, 86400),
    (json.loads(XAI_FREE_USAGE), None),
    ({"error": json.loads(AUTH_UNAVAILABLE)["error"]}, None),
])
def test_in_stream_quota_frames_are_exhausted_rate_limits(payload, retry_after):
    err = _make_stream_error(payload["error"], None, payload)
    assert err.error_type == "rate_limit" and err.exhausted
    assert err.retry_after == retry_after
    assert err.retryable == (retry_after is not None)  # only a known recovery time is waited for
    assert err.status == 429  # the in-stream failure does not report the stream's HTTP 200


def test_in_stream_terminal_auth_frame_is_not_retried():
    payload = json.loads(TERMINAL_AUTH)
    err = _make_stream_error(payload["error"], None, payload)
    assert err.error_type == "auth" and not err.retryable and not err.exhausted


@pytest.mark.asyncio
async def test_in_stream_cooldown_frame_feeds_the_breaker(monkeypatch):
    upstream = Upstream((200, ": keep-alive\n\ndata: " + MODEL_COOLDOWN + "\n\n", None))
    mock_client(monkeypatch, upstream)
    with pytest.raises(GrokUpstreamError) as info:
        await search()
    assert upstream.calls == 1 and info.value.retry_after == 86400 and breaker.is_open(KEY)


# -- Retry-After on every status ----------------------------------------------------


def test_retry_after_is_parsed_for_503_and_used_as_the_wait():
    err = _http_error(503, "busy", httpx.Headers({"Retry-After": "5"}))
    assert err.retry_after == 5 and err.retryable and err.error_type == "http_503"
    state = SimpleNamespace(outcome=SimpleNamespace(failed=True, exception=lambda: err), attempt_number=1)
    assert _WaitWithRetryAfter(1, 10)(state) == 5.0


@pytest.mark.asyncio
async def test_503_retry_after_beyond_budget_stops_and_is_reported(monkeypatch):
    upstream = Upstream((503, "overloaded", {"Retry-After": "120"}))
    mock_client(monkeypatch, upstream)
    monkeypatch.setenv("GROK_RETRY_BUDGET_S", "45")
    with pytest.raises(GrokUpstreamError) as info:
        await search()
    assert upstream.calls == 1 and info.value.retry_after == 120 and info.value.error_type == "http_503"


# -- Cloudflare edge errors ---------------------------------------------------------


@pytest.mark.asyncio
async def test_cloudflare_524_is_a_short_named_timeout_and_not_rerun(monkeypatch):
    upstream = Upstream((524, CF_524_PAGE, {"Content-Type": "text/html", "Server": "cloudflare"}))
    mock_client(monkeypatch, upstream)
    with pytest.raises(GrokUpstreamError) as info:
        await search()
    message = str(info.value)
    assert upstream.calls == 1 and info.value.error_type == "timeout" and info.value.status == 524
    assert message.startswith("Cloudflare 524: origin timed out") and "lighter model" in message
    assert "<!" not in message and len(message) < 300


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [520, 521, 523, 530])
async def test_cloudflare_edge_errors_are_retried_once(monkeypatch, status):
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "3")
    upstream = Upstream((status, f"error code: {status}", {"Server": "cloudflare"}))
    mock_client(monkeypatch, upstream)
    with pytest.raises(GrokUpstreamError) as info:
        await search()
    assert upstream.calls == 2 and info.value.error_type == f"http_{status}"
    assert str(info.value).startswith(f"Cloudflare {status}: ")

    blip = Upstream((status, f"error code: {status}", {"Server": "cloudflare"}), (200, sse_ok("after blip"), None))
    mock_client(monkeypatch, blip)
    assert (await search()).content == "after blip" and blip.calls == 2


def test_cloudflare_522_is_a_timeout_without_retry():
    err = _http_error(522, "error code: 522", httpx.Headers({"Server": "cloudflare"}))
    assert err.error_type == "timeout" and not err.retryable and err.status == 522


def test_html_error_pages_are_reduced_to_their_title():
    page = "<html><head><title>502 Bad Gateway</title></head><body>" + "x" * 2000 + "</body></html>"
    err = _http_error(502, page, httpx.Headers({"Content-Type": "text/html", "Server": "nginx"}))
    assert str(err) == "HTTP 502: 502 Bad Gateway" and err.retryable


# -- redaction happens before truncation --------------------------------------------


@pytest.mark.parametrize("secret", ["g2a_FAKEc0ffee1234567890abcdef123456", "xai-FAKEc0ffee1234567890abcdef12345", "AbCdEfGh0123456789XyZabcdefghijkl"])
@pytest.mark.parametrize("pad", [250, 270, 290])
def test_echoed_key_is_redacted_before_the_body_is_cut(monkeypatch, secret, pad):
    monkeypatch.setenv("GROK_API_KEY", secret)
    body = "x" * pad + " invalid credential " + secret + " trailing text " * 10
    for err in (_http_error(400, body, httpx.Headers()), _http_error(429, body, httpx.Headers())):
        text = str(err)
        assert all(secret[i:i + 8] not in text for i in range(len(secret) - 7)), text[-60:]


@pytest.mark.asyncio
async def test_unparseable_error_frame_is_redacted_before_truncation(monkeypatch):
    secret = "g2a_FAKEc0ffee1234567890abcdef123456"
    monkeypatch.setenv("GROK_API_KEY", secret)
    body = "event: error\ndata: " + "y" * 470 + " key " + secret + "\n\n"
    provider = GrokSearchProvider(URL, secret, "m")
    with pytest.raises(GrokUpstreamError) as info:
        await provider._parse_streaming_result(httpx.Response(200, text=body))
    assert all(secret[i:i + 8] not in str(info.value) for i in range(len(secret) - 7))
