import json
import tempfile

import httpx
import pytest
from fastmcp import Client

from grok_search import config as config_module
from grok_search import models, server
from grok_search.models import ModelListCache, check_model, model_name_problem
from grok_search.providers.grok import GrokResponse, GrokSearchProvider
from grok_search.sources import SourcesCache

URL, KEY = "https://api.example.test/v1", "test-key-0123456789abcdef0123"
OPENROUTER = "https://openrouter.ai/api/v1"


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _mock_transport(monkeypatch, handler):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs))


@pytest.fixture
def listed(monkeypatch):
    """/models answers with these IDs (or raises when an exception is put in the list)."""
    state = {"models": ["grok-4.7", "grok-4.20-multi-agent-0309"], "calls": 0}

    async def fetch(api_url, api_key, timeout_s=10.0):
        state["calls"] += 1
        if isinstance(state["models"], Exception):
            raise state["models"]
        return list(state["models"])

    monkeypatch.setattr(models, "fetch_available_models", fetch)
    return state


@pytest.fixture
def isolated(monkeypatch, tmp_path, listed):
    monkeypatch.setenv("GROK_API_URL", URL)
    monkeypatch.setenv("GROK_API_KEY", KEY)
    monkeypatch.setenv("GROK_MODEL", "grok-4.20-multi-agent-0309(high)")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    for key in ("TAVILY_API_KEY", "FIRECRAWL_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    cfg = server.config
    for name, value in (("_cached_model", None), ("_model_source", None), ("_config_file", None),
                        ("_private_dir", None), ("_notes", {}), ("_model_persisted", False)):
        monkeypatch.setattr(cfg, name, value)
    monkeypatch.setattr(config_module, "_ENV_WARNINGS", {})
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(server, "_SOURCES_CACHE", SourcesCache())
    monkeypatch.setattr(server, "_MODEL_CACHE", ModelListCache())

    async def models_ok(request):  # get_config_info's connection test never leaves the process
        return httpx.Response(200, json={"data": [{"id": m} for m in ("grok-4.7", "grok-4.20-multi-agent-0309")]})

    _mock_transport(monkeypatch, models_ok)
    return tmp_path


async def call(client, name, arguments=None):
    result = await client.call_tool(name, arguments or {})
    assert not result.is_error
    return json.loads(next(block.text for block in result.content if block.type == "text"))


def config_json(tmp_path):
    path = tmp_path / "home" / ".config" / "grok-search" / "config.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


@pytest.mark.parametrize("name, problem", [
    ("", "empty"), ("x" * 201, "201 characters"), ("grok\n4", "control"), ("grok-4.7(high)", ""), ("model (preview)", ""),
])
def test_model_name_problem(name, problem):
    assert (problem in model_name_problem(name)) if problem else model_name_problem(name) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "   ", "\t", "a" * 300, "grok-4\x00"])
async def test_switch_model_rejects_degenerate_names(isolated, name):
    async with Client(server.mcp) as client:
        data = await call(client, "switch_model", {"model": name})
        info = await call(client, "get_config_info")
    assert data["status"] == "❌ 失败" and "未做任何修改" in data["message"]
    assert config_json(isolated) is None
    assert server.config.grok_model == "grok-4.20-multi-agent-0309(high)" and info["GROK_MODEL_SOURCE"] == "env"


@pytest.mark.asyncio
async def test_switch_model_rejects_unlisted_names_with_suggestions(isolated, listed):
    async with Client(server.mcp) as client:
        data = await call(client, "switch_model", {"model": "grok-4.77"})
    assert data["status"] == "❌ 失败" and data["model_check"]["status"] == "invalid"
    assert "grok-4.7" in data["message"] and data["current_model"] == "grok-4.20-multi-agent-0309(high)"
    assert config_json(isolated) is None and listed["calls"] == 1


@pytest.mark.asyncio
async def test_switch_model_strips_and_reports_the_true_source(isolated, monkeypatch):
    seen = []

    async def search(self, query, platform="", **kwargs):
        seen.append(self.model)
        return GrokResponse("ok")

    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", search)
    async with Client(server.mcp) as client:
        data = await call(client, "switch_model", {"model": "  grok-4.7(low)  "})
        await call(client, "web_search", {"query": "q", "verify": False})
        info = await call(client, "get_config_info")
    assert data["status"] == "✅ 成功" and data["current_model"] == "grok-4.7(low)"
    assert data["model_source"] == "switch_model" and data["model_check"]["status"] == "listed"
    assert "GROK_MODEL=grok-4.20-multi-agent-0309(high)" in data["warning"]
    assert seen == ["grok-4.7(low)"]
    assert config_json(isolated) == {"model": "grok-4.7(low)"}
    assert info["GROK_MODEL"] == "grok-4.7(low)" and info["GROK_MODEL_SOURCE"] == "switch_model"
    assert "重启后会恢复为 grok-4.20-multi-agent-0309(high)" in info["GROK_MODEL_NOTE"]


@pytest.mark.asyncio
async def test_switch_model_is_unverified_when_models_is_down(isolated, listed, monkeypatch):
    monkeypatch.delenv("GROK_MODEL")
    listed["models"] = httpx.ConnectError("down")
    async with Client(server.mcp) as client:
        data = await call(client, "switch_model", {"model": "brand-new-model"})
        info = await call(client, "get_config_info")
    assert data["status"] == "✅ 成功" and data["model_check"]["status"] == "unverified"
    assert "未能通过 /models 校验" in data["warning"] and "GROK_MODEL" not in data["warning"]
    assert info["GROK_MODEL_SOURCE"] == "switch_model" and "仍然生效" in info["GROK_MODEL_NOTE"]


@pytest.mark.asyncio
async def test_switch_model_survives_a_broken_config_file(isolated, monkeypatch):
    monkeypatch.delenv("GROK_MODEL")
    path = isolated / "home" / ".config" / "grok-search" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text("[]", encoding="utf-8")
    async with Client(server.mcp) as client:
        before = await call(client, "get_config_info")
        data = await call(client, "switch_model", {"model": "grok-4.7"})
    assert before["GROK_MODEL_SOURCE"] == "default" and any("不是 JSON 对象" in w for w in before["config_warnings"])
    assert data["status"] == "✅ 成功" and config_json(isolated) == {"model": "grok-4.7"}


@pytest.mark.asyncio
async def test_switch_model_with_unwritable_home_uses_a_temp_dir(isolated, monkeypatch):
    monkeypatch.delenv("GROK_MODEL")
    (isolated / "home" / ".config").write_text("not a directory", encoding="utf-8")
    async with Client(server.mcp) as client:
        data = await call(client, "switch_model", {"model": "grok-4.7"})
        info = await call(client, "get_config_info")
    assert data["status"] == "✅ 成功" and data["config_file"].startswith(str(isolated / "grok-search-"))
    assert "临时目录" in data["warning"]
    assert info["GROK_CONFIG_FILE"] == data["config_file"] and info["GROK_MODEL"] == "grok-4.7"


# --------------------------------------------------------------- OpenRouter
@pytest.mark.asyncio
async def test_check_model_adds_online_on_openrouter(listed):
    listed["models"] = ["x-ai/grok-4", "x-ai/grok-4-fast"]
    cache = ModelListCache()
    kwargs = dict(default_model="x-ai/grok-4-fast:online", api_url=OPENROUTER, api_key=KEY, cache=cache)
    default = await check_model("x-ai/grok-4-fast", **kwargs)
    assert default.ok and default.status == "default" and default.model == "x-ai/grok-4-fast:online"
    other = await check_model("x-ai/grok-4", **kwargs)
    assert other.ok and other.status == "listed" and other.model == "x-ai/grok-4:online"
    explicit = await check_model("x-ai/grok-4:online", **kwargs)
    assert explicit.ok and explicit.status == "listed" and explicit.model == "x-ai/grok-4:online"
    missing = await check_model("x-ai/grok-9:online", **kwargs)
    assert not missing.ok and missing.status == "invalid"


@pytest.mark.asyncio
async def test_online_is_not_special_elsewhere(listed):
    check = await check_model("grok-4.7:online", default_model="grok-4-fast", api_url=URL, api_key=KEY, cache=ModelListCache())
    assert not check.ok and check.status == "invalid"
    plain = await check_model("grok-4.7", default_model="grok-4-fast", api_url=URL, api_key=KEY, cache=ModelListCache())
    assert plain.ok and plain.model == "grok-4.7"


@pytest.mark.asyncio
async def test_web_search_sends_online_for_per_call_models_on_openrouter(isolated, listed, monkeypatch):
    listed["models"] = ["x-ai/grok-4", "x-ai/grok-4-fast"]
    monkeypatch.setenv("GROK_API_URL", OPENROUTER)
    monkeypatch.setenv("GROK_MODEL", "x-ai/grok-4-fast")
    seen = []

    async def search(self, query, platform="", **kwargs):
        seen.append(self.model)
        return GrokResponse("ok")

    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", search)
    async with Client(server.mcp) as client:
        for model in ("", "x-ai/grok-4-fast", "x-ai/grok-4", "x-ai/grok-4:online"):
            result = await call(client, "web_search", {"query": "q", "model": model, "verify": False})
            assert "error" not in result, result
    assert seen == ["x-ai/grok-4-fast:online", "x-ai/grok-4-fast:online", "x-ai/grok-4:online", "x-ai/grok-4:online"]


# ------------------------------------------------------- get_config_info probe

@pytest.mark.asyncio
async def test_connection_test_retries_one_transport_error(isolated, monkeypatch):
    attempts = []

    async def handler(request):
        attempts.append(request.url)
        if len(attempts) == 1:
            raise httpx.ConnectError("")
        return httpx.Response(200, json={"data": [{"id": "grok-4.7"}]})

    _mock_transport(monkeypatch, handler)
    monkeypatch.setattr(server.asyncio, "sleep", _no_sleep)
    async with Client(server.mcp) as client:
        info = await call(client, "get_config_info")
    test = info["connection_test"]
    assert len(attempts) == 2 and test["status"] == "✅ 连接成功"
    assert "proxy tunnel closed or upstream unreachable (ConnectError)" in test["retried_after"]


async def _no_sleep(*args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_connection_test_explains_an_empty_transport_error(isolated, monkeypatch):
    attempts = []

    async def handler(request):
        attempts.append(1)
        raise httpx.ConnectError("")

    _mock_transport(monkeypatch, handler)
    monkeypatch.setattr(server.asyncio, "sleep", _no_sleep)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxyuser:proxypass@127.0.0.1:7890")
    async with Client(server.mcp) as client:
        info = await call(client, "get_config_info")
    test = info["connection_test"]
    assert len(attempts) == 2 and test["status"] == "❌ 连接失败" and test["attempts"] == 2
    assert "proxy tunnel closed or upstream unreachable (ConnectError)" in test["message"]
    assert "via proxy http://***@127.0.0.1:7890" in test["message"] and "proxypass" not in json.dumps(info)


@pytest.mark.asyncio
async def test_timeouts_are_not_retried(isolated, monkeypatch):
    attempts = []

    async def handler(request):
        attempts.append(1)
        raise httpx.ReadTimeout("slow")

    _mock_transport(monkeypatch, handler)
    async with Client(server.mcp) as client:
        info = await call(client, "get_config_info")
    assert len(attempts) == 1 and info["connection_test"]["status"] == "❌ 连接超时"


@pytest.mark.asyncio
async def test_get_config_info_masks_credentials_everywhere(isolated, monkeypatch):
    from grok_search.throttle import breaker
    url = "http://user:url-secret-pw@127.0.0.1:9/v1"
    monkeypatch.setenv("GROK_API_URL", url)
    monkeypatch.setenv("GROK_API_KEY", "sk-1234567")
    breaker.record_failure((url, "grok-4.20-multi-agent-0309"), exhausted=True, retry_after=30)

    async def handler(request):
        return httpx.Response(200, json={"data": [{"id": "grok-4.7"}]})

    _mock_transport(monkeypatch, handler)
    async with Client(server.mcp) as client:
        info = await call(client, "get_config_info")
    text = json.dumps(info, ensure_ascii=False)
    assert "url-secret-pw" not in text and "1234567" not in text
    assert info["GROK_API_URL"] == "http://***@127.0.0.1:9/v1"
    assert any("http://***@127.0.0.1:9/v1" in key for key in info["breaker"])
