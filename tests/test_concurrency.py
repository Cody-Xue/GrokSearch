import asyncio
import json

import httpx
import pytest
from tenacity import wait_none

from grok_search.providers import grok
from grok_search.providers.grok import GrokSearchProvider


def sse(*deltas):
    return "".join("data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n" for delta in deltas) + "data:[DONE]\n\n"


@pytest.mark.asyncio
async def test_semaphore_caps_inflight_requests(monkeypatch):
    monkeypatch.setenv("GROK_MAX_CONCURRENCY", "2")
    inflight = 0
    peak = 0

    async def handler(request):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.05)
        inflight -= 1
        return httpx.Response(200, text=sse({"content": "ok"}))

    real = httpx.AsyncClient
    monkeypatch.setattr(grok.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(grok, "_WaitWithRetryAfter", lambda *args: wait_none())

    provider = GrokSearchProvider("https://api.example.test/v1", "test-key", "tier")
    results = await asyncio.gather(*(provider.search_with_sources(f"q{i}") for i in range(5)))
    assert all(r.content == "ok" for r in results)
    assert peak == 2
