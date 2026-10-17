import json

import httpx
import pytest
from fastmcp import Client

from grok_search import server


async def map_text(arguments):
    async with Client(server.mcp) as client:
        result = await client.call_tool("web_map", arguments)
    return next(block.text for block in result.content if block.type == "text")


@pytest.mark.asyncio
async def test_scope_parameters_reach_tavily(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.delenv("TAVILY_ENABLED", raising=False)
    sent = {}

    async def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"base_url": "docs.example.test", "results": ["https://docs.example.test/a"], "response_time": 0.1})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    text = await map_text({"url": "https://docs.example.test", "allow_external": False, "select_paths": ["/docs/.*"], "exclude_domains": ["^blog\\."]})
    assert json.loads(text)["results"] == ["https://docs.example.test/a"]
    assert sent["allow_external"] is False and sent["select_paths"] == ["/docs/.*"] and sent["exclude_domains"] == ["^blog\\."]
    assert "select_domains" not in sent and "exclude_paths" not in sent


@pytest.mark.asyncio
async def test_external_links_stay_on_by_default(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.delenv("TAVILY_ENABLED", raising=False)
    sent = {}

    async def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"base_url": "x", "results": []})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    await map_text({"url": "https://docs.example.test"})
    assert sent["allow_external"] is True


@pytest.mark.asyncio
async def test_tavily_enabled_false_is_honoured(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_ENABLED", "false")
    assert "TAVILY_ENABLED=false" in await map_text({"url": "https://docs.example.test"})


def mock_tavily(monkeypatch, handler):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-secret-key")
    monkeypatch.delenv("TAVILY_ENABLED", raising=False)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.asyncio
async def test_web_map_has_no_output_schema_and_no_duplicated_payload(monkeypatch):
    async def handler(request):
        return httpx.Response(200, json={"base_url": "x", "results": ["https://x/a"], "response_time": 0.1})

    mock_tavily(monkeypatch, handler)
    async with Client(server.mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        result = await client.call_tool("web_map", {"url": "https://docs.example.test"})
    assert tools["web_map"].outputSchema is None
    assert result.structured_content is None and len(result.content) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status, body, head, detail", [
    (400, {"detail": {"error": "Invalid regex pattern in select_paths: '['"}}, "[web_map] error=bad_request provider=tavily status=400",
     "- tavily: bad_request (HTTP 400) — Invalid regex pattern in select_paths: '['"),
    (401, {"detail": {"error": "Unauthorized: invalid key tvly-test-secret-key"}}, "[web_map] error=auth provider=tavily status=401",
     "- tavily: auth (HTTP 401) — Unauthorized: invalid key ***"),
    (432, {"detail": {"error": "This request exceeds your plan's set usage limit."}}, "[web_map] error=quota provider=tavily status=432",
     "- tavily: quota (HTTP 432)"),
    (502, {"error": "bad gateway"}, "[web_map] error=upstream_error provider=tavily status=502", "- tavily: upstream_error (HTTP 502)"),
])
async def test_web_map_http_failures_have_a_stable_header(monkeypatch, status, body, head, detail):
    async def handler(request):
        return httpx.Response(status, json=body)

    mock_tavily(monkeypatch, handler)
    text = await map_text({"url": "https://docs.example.test", "select_paths": ["["]})
    assert text.startswith(head + "\n\n映射失败: Tavily Map 返回 HTTP ")
    assert detail in text and "secret" not in text


@pytest.mark.asyncio
async def test_web_map_transport_failures_are_categorised(monkeypatch):
    failure = {"exc": httpx.ReadTimeout}

    async def handler(request):
        raise failure["exc"]("failed", request=request)

    mock_tavily(monkeypatch, handler)
    text = await map_text({"url": "https://docs.example.test", "timeout": 30})
    assert text.startswith("[web_map] error=timeout provider=tavily\n\n映射超时: 请求超过30秒\n- tavily: timeout")
    failure["exc"] = httpx.ConnectError
    text = await map_text({"url": "https://docs.example.test"})
    assert text.startswith("[web_map] error=network provider=tavily\n\n映射失败: Tavily Map 未能完成\n- tavily: network")


@pytest.mark.asyncio
async def test_web_map_configuration_failures(monkeypatch):
    monkeypatch.setenv("TAVILY_ENABLED", "false")
    assert (await map_text({"url": "https://docs.example.test"})).startswith("[web_map] error=disabled provider=tavily\n\n配置错误: Tavily 已禁用")
    monkeypatch.delenv("TAVILY_ENABLED")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert (await map_text({"url": "https://docs.example.test"})).startswith("[web_map] error=not_configured provider=tavily")


@pytest.mark.asyncio
async def test_web_map_refuses_local_urls_but_accepts_bare_domains(monkeypatch):
    sent = []

    async def handler(request):
        sent.append(json.loads(request.content)["url"])
        return httpx.Response(200, json={"base_url": "x", "results": []})

    mock_tavily(monkeypatch, handler)
    for url, reason in (("file:///etc/passwd", "unsupported_scheme"), ("http://127.0.0.1:3000", "local_address"),
                        ("localhost:8080", "local_address"), ("http://[fd00::1]/", "local_address")):
        assert (await map_text({"url": url})).startswith(f"[web_map] error=invalid_url reason={reason}\n\n无效的 URL: "), url
    assert sent == []
    assert json.loads(await map_text({"url": "docs.example.test"}))["results"] == []
    assert sent == ["docs.example.test"]
