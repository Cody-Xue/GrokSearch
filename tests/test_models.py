import asyncio
import json
import time

import httpx
import pytest
from fastmcp import Client

from grok_search import models, server
from grok_search.models import ModelListCache, check_model, parse_model_name, routing_base
from grok_search.providers.grok import GrokResponse, GrokSearchProvider
from grok_search.sources import SourcesCache

URL, KEY = "https://api.example.test/v1", "test-key"
DEFAULT = "grok-4.20-multi-agent-0309(high)"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def fake_fetch(monkeypatch, *responses):
    """Each response is a list of model IDs or an exception; the last one repeats."""
    calls = []
    queue = list(responses)

    async def fetch(api_url, api_key, timeout_s=10.0):
        calls.append(api_url)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return list(item)

    monkeypatch.setattr(models, "fetch_available_models", fetch)
    return calls


@pytest.mark.parametrize("name, base, suffix, kind", [
    ("grok-4.20-multi-agent-0309(high)", "grok-4.20-multi-agent-0309", "high", "level"),
    ("grok-4.7(8192)", "grok-4.7", "8192", "budget"),
    ("grok-4.7(-1)", "grok-4.7", "-1", "budget"),
    ("grok-4.7(XHigh)", "grok-4.7", "XHigh", "level"),
    ("grok-4.7()", "grok-4.7", "", "empty"),
    ("grok-4.7(turbo)", "grok-4.7", "turbo", "unknown"),
    ("openrouter://gemini-3-pro-preview(high)", "openrouter://gemini-3-pro-preview", "high", "level"),
    ("grok-4.7", "grok-4.7", None, "none"),
    ("model (preview)", "model (preview)", None, "none"),
])
def test_parse_model_name(name, base, suffix, kind):
    parsed = parse_model_name(name)
    assert (parsed.base, parsed.suffix, parsed.suffix_kind) == (base, suffix, kind)


@pytest.mark.asyncio
async def test_default_model_and_its_other_levels_need_no_listing(monkeypatch):
    calls = fake_fetch(monkeypatch, ["grok-4.20-multi-agent-0309"])
    cache = ModelListCache()
    same = await check_model(DEFAULT, default_model=DEFAULT, api_url=URL, api_key=KEY, cache=cache)
    assert same.ok and same.status == "default" and same.model == DEFAULT
    other_level = await check_model("grok-4.20-multi-agent-0309(low)", default_model=DEFAULT, api_url=URL, api_key=KEY, cache=cache)
    assert other_level.ok and other_level.status == "default_base" and other_level.model == "grok-4.20-multi-agent-0309(low)"
    assert calls == []


@pytest.mark.asyncio
async def test_suffix_is_validated_on_the_base_name_and_kept(monkeypatch):
    fake_fetch(monkeypatch, ["grok-4.7", "grok-4.20-multi-agent-0309", "literal(preview)"])
    cache = ModelListCache()
    check = await check_model("grok-4.7(high)", default_model="grok-4-fast", api_url=URL, api_key=KEY, cache=cache)
    assert check.ok and check.status == "listed" and check.model == "grok-4.7(high)" and check.base == "grok-4.7"
    budget = await check_model("grok-4.7(16384)", default_model="grok-4-fast", api_url=URL, api_key=KEY, cache=cache)
    assert budget.ok and budget.model == "grok-4.7(16384)"
    bad_suffix = await check_model("grok-4.7(turbo)", default_model="grok-4-fast", api_url=URL, api_key=KEY, cache=cache)
    assert not bad_suffix.ok and "turbo" in bad_suffix.detail
    literal = await check_model("literal(preview)", default_model="grok-4-fast", api_url=URL, api_key=KEY, cache=cache)
    assert literal.ok and literal.status == "listed"


@pytest.mark.asyncio
async def test_unknown_model_refreshes_an_old_list_once_then_rejects(monkeypatch):
    clock = Clock()
    calls = fake_fetch(monkeypatch, ["grok-4.7"], ["grok-4.7", "grok-4.8"])
    cache = ModelListCache(clock=clock)
    assert (await check_model("grok-4.7", default_model="x", api_url=URL, api_key=KEY, cache=cache)).status == "listed"
    clock.t += models.MISS_REFRESH_MIN_AGE_S + 1
    new_model = await check_model("grok-4.8(high)", default_model="x", api_url=URL, api_key=KEY, cache=cache)
    assert new_model.ok and new_model.status == "listed_after_refresh" and len(calls) == 2
    missing = await check_model("grok-4.9", default_model="x", api_url=URL, api_key=KEY, cache=cache)
    assert not missing.ok and missing.status == "invalid" and "grok-4.8" in missing.suggestions
    assert len(calls) == 2  # the list was fresh, so the miss did not refetch


@pytest.mark.asyncio
async def test_failures_are_not_cached(monkeypatch):
    clock = Clock()
    calls = fake_fetch(monkeypatch, httpx.ConnectError("down"), ["grok-4.7"])
    cache = ModelListCache(clock=clock)
    unverified = await check_model("anything(high)", default_model="x", api_url=URL, api_key=KEY, cache=cache)
    assert unverified.ok and unverified.status == "unverified" and "network" in unverified.detail
    again = await check_model("anything", default_model="x", api_url=URL, api_key=KEY, cache=cache)
    assert again.status == "unverified" and len(calls) == 1  # within the short failure backoff
    clock.t += models.FAILURE_BACKOFF_S + 1
    listed = await check_model("grok-4.7", default_model="x", api_url=URL, api_key=KEY, cache=cache)
    assert listed.status == "listed" and len(calls) == 2


@pytest.mark.asyncio
async def test_successful_list_expires_after_ttl(monkeypatch):
    monkeypatch.setenv("GROK_MODELS_CACHE_TTL_S", "60")
    clock = Clock()
    calls = fake_fetch(monkeypatch, ["a"], ["a", "b"])
    cache = ModelListCache(clock=clock)
    assert (await cache.get(URL, KEY)).models == ["a"]
    clock.t += 30
    assert (await cache.get(URL, KEY)).models == ["a"] and len(calls) == 1
    clock.t += 31
    assert (await cache.get(URL, KEY)).models == ["a", "b"] and len(calls) == 2


@pytest.mark.asyncio
async def test_stale_list_never_rejects_a_new_model(monkeypatch):
    monkeypatch.setenv("GROK_MODELS_CACHE_TTL_S", "60")
    clock = Clock()
    fake_fetch(monkeypatch, ["a"], httpx.ReadTimeout("slow"))
    cache = ModelListCache(clock=clock)
    await cache.get(URL, KEY)
    clock.t += 61
    check = await check_model("brand-new", default_model="x", api_url=URL, api_key=KEY, cache=cache)
    assert check.ok and check.status == "unverified"


@pytest.mark.asyncio
async def test_miss_refresh_respects_the_failure_backoff(monkeypatch):
    clock = Clock()
    calls = fake_fetch(monkeypatch, ["grok-4.7"], httpx.ConnectTimeout("down"))
    cache = ModelListCache(clock=clock)
    await cache.get(URL, KEY)
    clock.t += models.MISS_REFRESH_MIN_AGE_S + 1
    first = await check_model("unknown-1", default_model="x", api_url=URL, api_key=KEY, cache=cache)
    assert first.status == "unverified" and len(calls) == 2
    for name in ("unknown-2", "unknown-3(high)"):
        assert (await check_model(name, default_model="x", api_url=URL, api_key=KEY, cache=cache)).status == "unverified"
    assert len(calls) == 2  # within the backoff the failed refresh is not repeated
    clock.t += models.FAILURE_BACKOFF_S + 1
    await check_model("unknown-4", default_model="x", api_url=URL, api_key=KEY, cache=cache)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_concurrent_misses_share_one_refresh_while_models_hangs(monkeypatch):
    clock = Clock()
    calls = []

    async def fetch(api_url, api_key, timeout_s=10.0):
        calls.append(api_url)
        if len(calls) == 1:
            return ["grok-4.7"]
        await asyncio.sleep(0.2)  # stands in for the 10 s timeout of a hanging /models
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(models, "fetch_available_models", fetch)
    cache = ModelListCache(clock=clock)
    await cache.get(URL, KEY)
    clock.t += models.MISS_REFRESH_MIN_AGE_S + 1
    started = time.monotonic()
    results = await asyncio.gather(*(
        check_model(f"unknown-{i}(high)", default_model="x", api_url=URL, api_key=KEY, cache=cache) for i in range(6)))
    elapsed = time.monotonic() - started
    assert [r.status for r in results] == ["unverified"] * 6
    assert len(calls) == 2  # one refresh for six misses, not six refreshes in a row
    assert elapsed < 0.6, elapsed


@pytest.mark.asyncio
async def test_queued_misses_reuse_a_refresh_that_just_succeeded(monkeypatch):
    clock = Clock()
    calls = []

    async def fetch(api_url, api_key, timeout_s=10.0):
        calls.append(api_url)
        await asyncio.sleep(0.01)
        return ["grok-4.7"] if len(calls) == 1 else ["grok-4.7", "grok-4.8"]

    monkeypatch.setattr(models, "fetch_available_models", fetch)
    cache = ModelListCache(clock=clock)
    await cache.get(URL, KEY)
    clock.t += models.MISS_REFRESH_MIN_AGE_S + 1
    # Hold the cache while six checks start, so each reads the old list and queues for the refresh.
    lock = cache._lock((URL, KEY))
    await lock.acquire()
    checks = [asyncio.ensure_future(check_model("grok-4.8(high)", default_model="x", api_url=URL, api_key=KEY, cache=cache))
              for _ in range(6)]
    await asyncio.sleep(0.01)
    lock.release()
    results = await asyncio.gather(*checks)
    assert [r.status for r in results] == ["listed_after_refresh"] * 6
    assert all(r.model == "grok-4.8(high)" for r in results)
    assert len(calls) == 2  # one refresh, reused by the five queued checks


@pytest.mark.parametrize("model, base", [
    ("grok-4.7(high)", "grok-4.7"), ("grok-4.7(8192)", "grok-4.7"), ("grok-4.7(-1)", "grok-4.7"), ("grok-4.7()", "grok-4.7"),
    ("grok-4.7", "grok-4.7"), ("grok-4.7(high):online", "grok-4.7"), ("literal(preview)", "literal(preview)"),
])
def test_routing_base(model, base):
    assert routing_base(model) == base


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("GROK_API_URL", URL)
    monkeypatch.setenv("GROK_API_KEY", KEY)
    monkeypatch.setenv("GROK_MODEL", DEFAULT)
    for key in ("TAVILY_API_KEY", "FIRECRAWL_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(server.config, "_cached_model", None)
    monkeypatch.setattr(server, "_SOURCES_CACHE", SourcesCache())
    monkeypatch.setattr(server, "_MODEL_CACHE", ModelListCache())
    return server.mcp


async def call(client, name, arguments):
    result = await client.call_tool(name, arguments)
    assert not result.is_error
    return json.loads(next(block.text for block in result.content if block.type == "text"))


@pytest.mark.asyncio
async def test_web_search_sends_the_full_suffixed_name(configured, monkeypatch):
    fake_fetch(monkeypatch, ["grok-4.20-multi-agent-0309", "grok-4.7"])
    used = []

    async def search(self, query, platform="", **kwargs):
        used.append(self.model)
        return GrokResponse("ok")

    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", search)
    async with Client(configured) as client:
        default = await call(client, "web_search", {"query": "q", "model": DEFAULT})
        other = await call(client, "web_search", {"query": "q", "model": "grok-4.7(xhigh)"})
        bad = await call(client, "web_search", {"query": "q", "model": "nope-model"})
    assert used == [DEFAULT, "grok-4.7(xhigh)"]
    assert "model_check" not in default and other["model_check"]["status"] == "listed"
    assert bad["error_type"] == "invalid_model" and bad["model_check"]["status"] == "invalid"


@pytest.mark.asyncio
async def test_get_config_info_refreshes_the_model_cache(configured, monkeypatch):
    async def handler(request):
        return httpx.Response(200, json={"data": [{"id": "fresh-model"}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    async with Client(configured) as client:
        info = await call(client, "get_config_info", {})
    assert info["connection_test"]["model_cache"] == "refreshed"
    listing = await server._MODEL_CACHE.get(URL, KEY)
    assert listing.from_cache and listing.models == ["fresh-model"]
