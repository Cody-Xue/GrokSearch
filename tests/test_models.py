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


# ------------------------------------------------------------- non-chat models and suggestions
from grok_search.models import non_chat_kind, suggest_models  # noqa: E402

# The gateway's real /models list (CPA), TTS/voice and image models included.
GATEWAY_MODELS = [
    "grok-4.7", "grok-4.7-build-fast", "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-luna", "codex-auto-review",
    "gpt-image-2.5-sunburst", "grok-tts", "grok-voice-tts-1.0", "gpt-6-sol", "gpt-5.6-luna", "gpt-image-1.5", "gpt-image-2",
    "grok-4.20-multi-agent-0309", "gpt-5.5", "gpt-6.1-sol", "gpt-image-2.5-flare", "gpt-image-2.5",
]


@pytest.mark.parametrize("name, kind", [
    ("grok-tts", "audio/speech"), ("grok-voice-tts-1.0", "audio/speech"), ("Grok-TTS", "audio/speech"), ("tts-1-hd", "audio/speech"),
    ("gpt-4o-mini-tts", "audio/speech"), ("whisper-1", "audio/speech"), ("gpt-4o-transcribe", "audio/speech"),
    ("gpt-image-2", "image generation"), ("dall-e-3", "image generation"), ("grok-imagine-image", "image generation"),
    ("imagen-4", "image generation"), ("openai/gpt-image-1", "image generation"), ("sora-2", "video generation"),
    ("text-embedding-3-small", "embedding"), ("mistral-embed", "embedding"), ("omni-moderation-latest", "moderation"),
    ("gpt-4o-realtime-preview", "realtime"), ("jina-reranker-v2", "rerank"),
    # chat models: vision models chat, and tokens only count as whole name parts
    ("grok-4.7", ""), ("grok-2-vision-1212", ""), ("grok-code-fast-1", ""), ("deepseek-chat", ""), ("openai/gpt-5:online", ""),
    ("grok-4.20-multi-agent-0309", ""), ("gpt-4o-search-preview", ""), ("grok-4-fast-reasoning", ""), ("grok-4(high)", ""),
])
def test_non_chat_kind(name, kind):
    assert non_chat_kind(name) == kind


@pytest.mark.parametrize("typed", ["grok-4.8", "grok-3", "grok-5", "grok", "grok-beta", "grok-3-mini", "grok-imagine", "grok-tts"])
def test_suggestions_put_the_chat_model_of_the_same_family_first(typed):
    suggestions = suggest_models(typed, GATEWAY_MODELS)
    assert suggestions[0] == "grok-4.7", suggestions
    assert not any(non_chat_kind(m) for m in suggestions) and len(suggestions) <= 5


@pytest.mark.parametrize("typed, first", [
    ("grok-4.20", "grok-4.20-multi-agent-0309"),  # a name that extends what was typed
    ("grok-code-fast-1", "grok-4.7-build-fast"),
    ("gpt-5", "gpt-5.5"),
])
def test_suggestion_ranking(typed, first):
    suggestions = suggest_models(typed, GATEWAY_MODELS)
    assert suggestions[0] == first and "grok-4.7" in suggest_models("grok-4-1-fast-reasoning", GATEWAY_MODELS)
    assert not any(non_chat_kind(m) for m in suggestions)
    if typed == "gpt-5":
        assert all(m.startswith("gpt-") for m in suggestions)


def test_suggestions_need_some_similarity_outside_the_family():
    assert suggest_models("claude-x", GATEWAY_MODELS) == [] and suggest_models("", GATEWAY_MODELS) == []
    assert suggest_models("x-ai/grok-4.8", ["x-ai/grok-4.7", "x-ai/grok-tts"]) == ["x-ai/grok-4.7"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["grok-tts", "grok-voice-tts-1.0", "gpt-image-2", "grok-tts(high)", "grok-tts(turbo)"])
async def test_listed_non_chat_models_are_rejected(monkeypatch, name):
    fake_fetch(monkeypatch, GATEWAY_MODELS)
    check = await check_model(name, default_model=DEFAULT, api_url=URL, api_key=KEY, cache=ModelListCache())
    assert not check.ok and check.status == "invalid" and check.reason == "not_chat_model"
    assert "is not a chat/search model" in check.detail and check.to_dict()["reason"] == "not_chat_model"
    assert check.suggestions and not any(non_chat_kind(m) for m in check.suggestions)


@pytest.mark.asyncio
async def test_non_chat_models_are_rejected_even_when_models_is_down(monkeypatch):
    fake_fetch(monkeypatch, httpx.ConnectError("down"))
    check = await check_model("grok-tts", default_model=DEFAULT, api_url=URL, api_key=KEY, cache=ModelListCache())
    assert not check.ok and check.reason == "not_chat_model" and check.suggestions == []


@pytest.mark.asyncio
async def test_unlisted_names_are_rejected_with_chat_suggestions_only(monkeypatch):
    fake_fetch(monkeypatch, GATEWAY_MODELS)
    check = await check_model("grok-3", default_model=DEFAULT, api_url=URL, api_key=KEY, cache=ModelListCache())
    assert not check.ok and check.reason == "not_listed"
    assert check.suggestions[0] == "grok-4.7" and "grok-tts" not in check.suggestions


@pytest.mark.asyncio
async def test_web_search_rejects_a_non_chat_model_without_any_upstream_call(configured, monkeypatch):
    calls = fake_fetch(monkeypatch, GATEWAY_MODELS)
    used = []

    async def search(self, query, platform="", **kwargs):
        used.append(self.model)
        return GrokResponse("ok")

    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", search)
    async with Client(configured) as client:
        tts = await call(client, "web_search", {"query": "q", "model": "grok-tts"})
        typo = await call(client, "web_search", {"query": "q", "model": "grok-4.8"})
    assert used == [] and len(calls) == 1
    assert tts["error_type"] == "invalid_model" and tts["content"].startswith("[搜索失败] 无效模型: grok-tts（")
    assert "not a chat/search model" in tts["content"] and "相近的可用模型: grok-4.7" in tts["content"]
    assert tts["model_check"]["reason"] == "not_chat_model"
    assert typo["model_check"]["suggestions"][0] == "grok-4.7"
    assert not any(non_chat_kind(m) for m in typo["model_check"]["suggestions"])


@pytest.mark.asyncio
async def test_get_config_info_separates_chat_models(configured, monkeypatch):
    async def handler(request):
        return httpx.Response(200, json={"data": [{"id": m} for m in GATEWAY_MODELS]})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    async with Client(configured) as client:
        info = await call(client, "get_config_info", {})
    test = info["connection_test"]
    assert test["available_models"] == GATEWAY_MODELS
    assert "grok-4.7" in test["chat_models"] and "grok-tts" not in test["chat_models"]
    assert set(test["non_chat_models"]) == {m for m in GATEWAY_MODELS if "tts" in m or "image" in m}
    assert len(test["chat_models"]) + len(test["non_chat_models"]) == len(GATEWAY_MODELS)
    assert not any("不是对话/搜索模型" in w for w in info.get("config_warnings", []))


@pytest.mark.asyncio
async def test_get_config_info_warns_about_a_non_chat_default(configured, monkeypatch):
    monkeypatch.setenv("GROK_MODEL", "grok-tts")

    async def handler(request):
        return httpx.Response(200, json={"data": [{"id": "grok-4.7"}, {"id": "grok-tts"}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    async with Client(configured) as client:
        info = await call(client, "get_config_info", {})
    assert any("GROK_MODEL=grok-tts" in w and "不是对话/搜索模型" in w for w in info["config_warnings"])


# ------------------------------------------------------------- capabilities a gateway declares (OpenRouter)
OPENROUTER = "https://openrouter.ai/api/v1"
# Entries as OpenRouter's public /api/v1/models lists them (abridged).
OPENROUTER_MODELS = [
    {"id": "openai/gpt-5-image", "architecture": {"modality": "text+image+file->text+image", "output_modalities": ["image", "text"]}},
    {"id": "google/gemini-2.5-flash-image", "architecture": {"modality": "text+image->text+image", "output_modalities": ["image", "text"]}},
    {"id": "openai/gpt-audio", "architecture": {"modality": "text+audio->text+audio"}},
    {"id": "x-ai/grok-4-fast", "architecture": {"modality": "text+image->text", "output_modalities": ["text"]}},
    {"id": "acme/painter-pro", "architecture": {"modality": "text->image", "output_modalities": ["image"]}},
    {"id": "acme/plain-tts"},  # no capability data: the name decides
]


def openrouter_listing(monkeypatch):
    calls = []

    async def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"data": OPENROUTER_MODELS})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("name, ok, detail", [
    ("openai/gpt-5-image", True, ""),  # chat completions with text output on OpenRouter; accepted by v1.12.0 too
    ("google/gemini-2.5-flash-image", True, ""),
    ("openai/gpt-audio:online", True, ""),
    ("x-ai/grok-4-fast", True, ""),
    ("acme/painter-pro", False, "declares no text output"),  # a chat-like name, but image output only
    ("acme/plain-tts", False, "name contains 'tts'"),
])
async def test_declared_output_modalities_win_over_the_name(monkeypatch, name, ok, detail):
    openrouter_listing(monkeypatch)
    check = await check_model(name, default_model="x-ai/grok-4.7:online", api_url=OPENROUTER, api_key=KEY, cache=ModelListCache())
    assert check.ok is ok, check.to_dict()
    if ok:
        assert check.status == "listed" and check.model.endswith(":online")
    else:
        assert check.reason == "not_chat_model" and detail in check.detail


def test_declared_text_output_shapes():
    from grok_search.models import declared_text_output
    assert declared_text_output({"architecture": {"output_modalities": ["text"]}}) is True
    assert declared_text_output({"architecture": {"output_modalities": ["image"]}}) is False
    assert declared_text_output({"architecture": {"modality": "text->text"}}) is True
    assert declared_text_output({"architecture": {"modality": "text+image->image"}}) is False
    assert declared_text_output({"architecture": {}}) is None and declared_text_output({"id": "x"}) is None


@pytest.mark.asyncio
async def test_get_config_info_uses_declared_capabilities(monkeypatch):
    monkeypatch.setenv("GROK_API_URL", OPENROUTER)
    monkeypatch.setenv("GROK_API_KEY", KEY)
    monkeypatch.setenv("GROK_MODEL", "openai/gpt-5-image")
    openrouter_listing(monkeypatch)
    async with Client(server.mcp) as client:
        info = await call(client, "get_config_info", {})
    test = info["connection_test"]
    assert "openai/gpt-5-image" in test["chat_models"] and "openai/gpt-audio" in test["chat_models"]
    assert set(test["non_chat_models"]) == {"acme/painter-pro", "acme/plain-tts"}
    assert not any("不是对话/搜索模型" in w for w in info.get("config_warnings", []))
