import asyncio
import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import time

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


# ------------------------------------------------------------- client-side scope (allow_external / select_domains)
def map_results(monkeypatch, results, base_url="https://docs.example.test"):
    sent = {}

    async def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"base_url": base_url, "results": list(results), "response_time": 0.2})

    mock_tavily(monkeypatch, handler)
    return sent


SITE = ["https://docs.example.test/a", "https://app.example.test/b", "https://other.test/c"]


@pytest.mark.asyncio
async def test_allow_external_false_keeps_only_the_start_host(monkeypatch):
    sent = map_results(monkeypatch, SITE)
    data = json.loads(await map_text({"url": "https://docs.example.test", "allow_external": False}))
    assert data["results"] == ["https://docs.example.test/a"]
    assert data["filtered_out"] == 2 and data["scope"] == "exact_host"
    assert data["base_url"] == "https://docs.example.test" and data["response_time"] == 0.2
    assert sent["allow_external"] is False  # still asked of Tavily too


@pytest.mark.asyncio
async def test_exact_host_treats_www_case_ports_and_bare_domains_alike(monkeypatch):
    results = ["https://example.test/", "https://WWW.Example.test/app", "http://example.test:8080/x", "example.test/bare",
               "https://example.test./dot", "https://status.example.test/", "https://docs.example.test/", "not a url at all"]
    map_results(monkeypatch, results, base_url="https://example.test")
    data = json.loads(await map_text({"url": "example.test", "allow_external": False}))
    assert data["results"] == results[:5] and data["filtered_out"] == 3


@pytest.mark.asyncio
async def test_the_base_url_host_counts_as_the_start_host(monkeypatch):
    map_results(monkeypatch, ["https://www.example.org/a", "https://redirected.example.org/b", "https://cdn.example.org/c"],
                base_url="https://redirected.example.org")
    data = json.loads(await map_text({"url": "https://example.org", "allow_external": False}))
    assert data["results"] == ["https://www.example.org/a", "https://redirected.example.org/b"] and data["filtered_out"] == 1


@pytest.mark.asyncio
async def test_select_domains_replaces_the_exact_host_rule(monkeypatch):
    sent = map_results(monkeypatch, SITE + ["https://status.example.test/d"])
    data = json.loads(await map_text({"url": "https://docs.example.test", "allow_external": False,
                                      "select_domains": ["^(docs|app)\\.example\\.test$"]}))
    assert data["results"] == SITE[:2] and data["scope"] == "select_domains" and data["filtered_out"] == 2
    assert sent["select_domains"] == ["^(docs|app)\\.example\\.test$"]


@pytest.mark.asyncio
async def test_allow_external_true_returns_tavilys_list_unchanged(monkeypatch):
    map_results(monkeypatch, SITE)
    data = json.loads(await map_text({"url": "https://docs.example.test", "select_domains": ["^docs\\.example\\.test$"]}))
    assert data["results"] == SITE and data["scope"] == "unfiltered" and data["filtered_out"] == 0


# Saved Tavily Map answers with allow_external=false (Oct 2026, abridged): sibling subdomains are listed.
_REAL_MAPS = [
    ("https://tavily.com",
     ["https://tavily.com/", "https://docs.tavily.com/", "https://app.tavily.com/home", "https://tavily.com/blog",
      "https://community.tavily.com/", "https://trust.tavily.com/", "https://docs.tavily.com/documentation/about"],
     ["https://tavily.com/", "https://tavily.com/blog"]),
    ("https://firecrawl.dev",
     ["https://firecrawl.dev/", "https://docs.firecrawl.dev/", "https://status.firecrawl.dev/",
      "https://www.firecrawl.dev/", "https://www.firecrawl.dev/app", "https://firecrawl.dev/pricing"],
     ["https://firecrawl.dev/", "https://www.firecrawl.dev/", "https://www.firecrawl.dev/app", "https://firecrawl.dev/pricing"]),
    ("https://docs.firecrawl.dev",
     ["https://docs.firecrawl.dev/", "https://docs.firecrawl.dev/introduction", "https://docs.firecrawl.dev/sdks/overview"],
     ["https://docs.firecrawl.dev/", "https://docs.firecrawl.dev/introduction", "https://docs.firecrawl.dev/sdks/overview"]),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("url, results, kept", _REAL_MAPS)
async def test_real_tavily_answers_are_scoped(monkeypatch, url, results, kept):
    map_results(monkeypatch, results, base_url=url)
    data = json.loads(await map_text({"url": url, "allow_external": False}))
    assert data["results"] == kept and data["filtered_out"] == len(results) - len(kept)


# ------------------------------------------------------------- select_domains regexes run in a child process (SEC-3)
async def map_while_ticking(arguments):
    """web_map, while a ticker on the same event loop records the longest stall between its 10 ms sleeps.
    Returns (parsed JSON answer, seconds the call took, longest stall)."""
    stalls = []
    done = asyncio.Event()

    async def ticker():
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            stalls.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    started = time.monotonic()
    try:
        text = await map_text(arguments)
    finally:
        done.set()
        await task
    return json.loads(text), time.monotonic() - started, max(stalls, default=0.0)


# Each of these froze the server (and every other tool) when it ran on the event loop.
HOSTILE_PATTERNS = [
    "(?:\\b){50000000}x",  # RR2-SEC3: 4.5 s and 2.7 GB; {400000000} ended in a MemoryError reported as a Tavily error
    "^(?:\\b){400000000}x",
    "^(\\w+\\.?)*example\\.org$",  # 20 s against a 26-letter host
    "^(a?){28}a{28}$",  # 12 s against a 28-letter host
]


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", HOSTILE_PATTERNS)
async def test_a_hostile_select_domains_pattern_never_blocks_the_server(monkeypatch, pattern):
    monkeypatch.setenv("GROK_MAP_REGEX_TIMEOUT_S", "1")
    results = ["https://example.org/", "https://" + "a" * 28 + ".com/x"]
    map_results(monkeypatch, results, base_url="https://example.org")
    data, elapsed, stall = await map_while_ticking({"url": "https://example.org", "allow_external": False,
                                                    "select_domains": [pattern]})
    assert data["results"] == results and data["scope"] == "unfiltered" and data["filtered_out"] == 0
    # Killed at the timeout, or out of memory first where the child's memory is limited (POSIX).
    assert re.fullmatch(r"local regex filter (timed out|failed); results not filtered \(.+\)", data["scope_note"])
    assert elapsed < 3.0 and stall < 0.3


@pytest.mark.asyncio
async def test_a_slow_pattern_is_cut_off_at_the_timeout(monkeypatch):
    monkeypatch.setenv("GROK_MAP_REGEX_TIMEOUT_S", "0.5")
    results = ["https://example.org/", "https://" + "a" * 40 + ".com/x"]
    map_results(monkeypatch, results, base_url="https://example.org")
    data, elapsed, stall = await map_while_ticking({"url": "https://example.org", "allow_external": False,
                                                    "select_domains": ["^(\\w+\\.?)*example\\.org$"]})
    assert data == {"base_url": "https://example.org", "results": results, "response_time": 0.2, "scope": "unfiltered",
                    "filtered_out": 0, "scope_note": "local regex filter timed out; results not filtered "
                                                     "(select_domains matching stopped after 0.5 s, GROK_MAP_REGEX_TIMEOUT_S)"}
    assert elapsed < 2.0 and stall < 0.3


MAP_HOSTS = ["https://example.com/", "https://docs.example.com/a", "https://api.example.com/b", "https://a.b.example.com/c",
             "https://x.y.z.example.com/d", "https://blog.other.org/e", "https://cdn.example.net/f",
             "https://" + "a" * 249 + ".com/g", "https://" + "a" * 300 + ".example.com/h"]


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern, kept", [
    # RR2: refused by the static analyzer of round 2 ("backtracking"), although each takes microseconds.
    (".*(docs|api).*", [1, 2]),
    ("[a-z]+\\.[a-z]+\\.(com|org)", [1, 2, 3, 4, 5]),
    ("^(?:[\\w-]+\\.)?(?:[\\w-]+\\.)?example\\.com$", [0, 1, 2, 3]),
    ("^([\\w-]+\\.)*example\\.com$", [0, 1, 2, 3, 4]),
    ("^(docs|api)\\.example\\.com$", [1, 2]),
])
async def test_realistic_select_domains_patterns_filter_locally(monkeypatch, pattern, kept):
    sent = map_results(monkeypatch, MAP_HOSTS, base_url="https://example.com")
    data, elapsed, stall = await map_while_ticking({"url": "https://example.com", "allow_external": False,
                                                    "select_domains": [pattern]})
    assert data["results"] == [MAP_HOSTS[i] for i in kept] and data["scope"] == "select_domains"
    assert data["filtered_out"] == len(MAP_HOSTS) - len(kept) and "scope_note" not in data
    assert sent["select_domains"] == [pattern] and elapsed < 2.0 and stall < 0.3


@pytest.mark.asyncio
async def test_several_patterns_any_of_which_may_match(monkeypatch):
    map_results(monkeypatch, MAP_HOSTS, base_url="https://example.com")
    data = json.loads(await map_text({"url": "https://example.com", "allow_external": False,
                                      "select_domains": ["^docs\\.", "\\.org$", "^example\\.com$"]}))
    assert data["results"] == [MAP_HOSTS[0], MAP_HOSTS[1], MAP_HOSTS[5]] and data["filtered_out"] == len(MAP_HOSTS) - 3


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern, error", [
    ("(?<", "unexpected end of pattern"),
    ("(?<=a+)b", "look-behind requires fixed-width pattern"),  # a compile-time error the parser alone misses
    ("a{3,2}", "min repeat greater than max repeat"),
    ("[z-a]", "bad character range"),
    ("(", "missing ), unterminated subpattern"),
])
async def test_an_invalid_select_domains_pattern_is_refused_before_tavily(monkeypatch, pattern, error):
    sent = map_results(monkeypatch, SITE)
    text = await map_text({"url": "https://docs.example.test", "allow_external": False,
                           "select_domains": ["^docs\\.example\\.test$", pattern]})
    assert text.startswith("[web_map] error=bad_request reason=invalid_regex\n\nselect_domains 中的正则「")
    assert pattern in text and error in text and sent == {}


@pytest.mark.asyncio
async def test_select_domains_count_and_length_are_capped(monkeypatch):
    sent = map_results(monkeypatch, SITE)
    many = [f"^host{i}\\.example\\.test$" for i in range(60)]
    text = await map_text({"url": "https://docs.example.test", "allow_external": False, "select_domains": many})
    assert text.startswith("[web_map] error=bad_request reason=too_many_patterns\n\n") and "收到 60 个" in text
    long = "^" + "a" * 300 + "$"
    text = await map_text({"url": "https://docs.example.test", "select_domains": [long]})
    assert text.startswith("[web_map] error=bad_request reason=pattern_too_long\n\n") and "有 302 个" in text
    assert sent == {}
    # 60 copies of one pattern are one pattern.
    data = json.loads(await map_text({"url": "https://docs.example.test", "allow_external": False,
                                      "select_domains": ["^(docs|app)\\.example\\.test$"] * 60 + [" ^(docs|app)\\.example\\.test$ "]}))
    assert data["results"] == SITE[:2] and data["scope"] == "select_domains"
    assert sent["select_domains"] == ["^(docs|app)\\.example\\.test$"]


@pytest.mark.asyncio
async def test_the_matcher_runs_only_for_caller_regexes(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("the matcher process was started")

    monkeypatch.setattr(server, "_match_hosts", forbidden)
    map_results(monkeypatch, SITE)
    data = json.loads(await map_text({"url": "https://docs.example.test", "allow_external": False}))
    assert data["scope"] == "exact_host" and data["results"] == SITE[:1]  # plain string comparison
    data = json.loads(await map_text({"url": "https://docs.example.test", "select_domains": ["^docs\\."]}))
    assert data["scope"] == "unfiltered" and "scope_note" not in data  # allow_external=true: only steers the crawl


@pytest.mark.asyncio
@pytest.mark.parametrize("program, why", [
    ("raise MemoryError", "MemoryError"),
    ("import sys; sys.exit(3)", "exit status 3"),
    ("print('not json')", "unreadable matcher output"),
    ("print('[true]')", "unreadable matcher output"),  # one answer for several hosts
])
async def test_a_failing_matcher_leaves_the_list_unfiltered(monkeypatch, program, why):
    monkeypatch.setattr(server, "_MAP_MATCHER", program)
    map_results(monkeypatch, SITE)
    data = json.loads(await map_text({"url": "https://docs.example.test", "allow_external": False,
                                      "select_domains": ["^docs\\."]}))
    assert data["results"] == SITE and data["scope"] == "unfiltered"
    assert data["scope_note"] == f"local regex filter failed; results not filtered ({why})"


@pytest.mark.asyncio
async def test_a_matcher_that_cannot_start_leaves_the_list_unfiltered(monkeypatch):
    monkeypatch.setattr(server.sys, "executable", "/nonexistent/grok-search-python")
    map_results(monkeypatch, SITE)
    text = await map_text({"url": "https://docs.example.test", "allow_external": False, "select_domains": ["^docs\\."]})
    data = json.loads(text)
    assert data["results"] == SITE and data["scope"] == "unfiltered" and "error=" not in text
    assert data["scope_note"] == "local regex filter failed; results not filtered (could not start the matcher: FileNotFoundError)"


def test_the_matcher_runs_the_base_interpreter_on_windows(monkeypatch, tmp_path):
    """A Windows venv's python.exe only launches the base interpreter; killing the launcher must not leave
    the regex running in its child."""
    base = tmp_path / "python.exe"
    base.write_text("")
    monkeypatch.setattr(server.sys, "_base_executable", str(base), raising=False)
    assert server._matcher_python() == server.sys.executable  # POSIX: the venv's python is the interpreter
    monkeypatch.setattr(server.os, "name", "nt")
    assert server._matcher_python() == str(base)
    monkeypatch.setattr(server.sys, "_base_executable", str(tmp_path / "missing.exe"))
    assert server._matcher_python() == server.sys.executable


@pytest.mark.asyncio
async def test_a_cancelled_call_kills_the_matcher(monkeypatch):
    started = []
    spawn = asyncio.create_subprocess_exec

    async def recording(*args, **kwargs):
        started.append(await spawn(*args, **kwargs))
        return started[-1]

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", recording)
    task = asyncio.create_task(server._match_hosts(["^(\\w+\\.?)*example\\.org$"], ["a" * 40 + ".com"]))
    while not started:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.wait_for(started[0].wait(), 5) != 0


@pytest.mark.asyncio
async def test_subdomain_patterns_still_filter_and_overlong_hosts_are_dropped(monkeypatch):
    results = ["https://example.org/", "https://docs.example.org/a", "https://" + "a" * 300 + ".example.org/", "https://other.test/"]
    map_results(monkeypatch, results, base_url="https://example.org")
    data = json.loads(await map_text({"url": "https://example.org", "allow_external": False,
                                      "select_domains": ["^([\\w-]+\\.)*example\\.org$"]}))
    assert data["results"] == results[:2] and data["scope"] == "select_domains" and data["filtered_out"] == 2


def test_the_matcher_timeout_setting(monkeypatch):
    from grok_search.config import config
    monkeypatch.delenv("GROK_MAP_REGEX_TIMEOUT_S", raising=False)
    assert config.map_regex_timeout_s == 2.0 and config.get_config_info()["GROK_MAP_REGEX_TIMEOUT_S"] == 2.0
    for value, expected in (("5", 5.0), ("0.1", 0.5), ("1000", 60.0), ("soon", 2.0)):
        monkeypatch.setenv("GROK_MAP_REGEX_TIMEOUT_S", value)
        assert config.map_regex_timeout_s == expected



# ------------------------------------------------------------- what the matcher may take (repair round 2)
SECRET_NAMES = ("GROK_API_KEY", "TAVILY_API_KEY", "FIRECRAWL_API_KEY", "GITHUB_TOKEN", "HTTPS_PROXY", "http_proxy",
                "ALL_PROXY")
SHOW_SECRET_NAMES = ("import os, sys; sys.stdin.read(); sys.stderr.write(' '.join(sorted(k for k in os.environ if any("
                     "w in k.upper() for w in ('KEY', 'TOKEN', 'PROXY')))) or 'none'); sys.exit(1)")


@pytest.mark.asyncio
async def test_the_matcher_inherits_no_secrets(monkeypatch):
    """V1-1: the child ran with the server's whole environment, API keys and proxies included."""
    for name in SECRET_NAMES:
        monkeypatch.setenv(name, "secret-value")
    captured = {}
    spawn = asyncio.create_subprocess_exec

    async def recording(*args, **kwargs):
        captured.update(kwargs)
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", recording)
    assert await server._match_hosts(["^docs\\."], ["docs.example.test", "app.example.test"]) == ([True, False], "")
    assert captured["env"] == ({} if os.name != "nt" else {"SYSTEMROOT": os.environ["SYSTEMROOT"]})
    # What the child itself sees.
    monkeypatch.setattr(server, "_MAP_MATCHER", SHOW_SECRET_NAMES)
    assert await server._match_hosts(["^docs\\."], ["docs.example.test"]) == (None, "failed; results not filtered (none)")


def test_the_matcher_environment_on_windows(monkeypatch):
    monkeypatch.setattr(server.os, "name", "nt")
    monkeypatch.setenv("SYSTEMROOT", "D:\\Win")
    monkeypatch.setenv("TAVILY_API_KEY", "secret-value")
    assert server._matcher_env() == {"SYSTEMROOT": "D:\\Win"}  # Python on Windows needs it to start
    monkeypatch.delenv("SYSTEMROOT")
    assert server._matcher_env() == {"SYSTEMROOT": "C:\\Windows"}


def recording_spawns(monkeypatch):
    spawned = []
    spawn = asyncio.create_subprocess_exec

    async def recording(*args, **kwargs):
        spawned.append(await spawn(*args, **kwargs))
        return spawned[-1]

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", recording)
    return spawned


async def timed(coroutine):
    started = time.monotonic()
    return await coroutine, time.monotonic() - started


# A matcher that sleeps for the number of seconds its first pattern names.
SLEEPING_MATCHER = ("import json, sys, time; job = json.loads(sys.stdin.read()); time.sleep(float(job['patterns'][0])); "
                    "print(json.dumps([True] * len(job['hosts'])))")


@pytest.mark.asyncio
async def test_at_most_two_matchers_run_at_once(monkeypatch):
    """V1-2: ten hostile calls ran ten 450 MB children at once (2.9 GB). Waiting for a free matcher counts
    toward GROK_MAP_REGEX_TIMEOUT_S."""
    monkeypatch.setenv("GROK_MAP_REGEX_TIMEOUT_S", "1")
    real = server._MAP_MATCHER
    monkeypatch.setattr(server, "_MAP_MATCHER", SLEEPING_MATCHER)
    spawned = recording_spawns(monkeypatch)
    answers = await asyncio.gather(*(timed(server._match_hosts(["30"], ["a.example"])) for _ in range(3)))
    assert server.MAP_MATCHERS_AT_ONCE == 2 and len(spawned) == 2
    killed = "timed out; results not filtered (select_domains matching stopped after 1 s, GROK_MAP_REGEX_TIMEOUT_S)"
    assert [answer for answer, _ in answers[:2]] == [(None, killed)] * 2
    assert answers[2][0] == (None, "timed out; results not filtered (no free matcher within 1 s, at most 2 run at a "
                                   "time; GROK_MAP_REGEX_TIMEOUT_S)")
    assert max(elapsed for _, elapsed in answers) < 2.0  # the third waited, but not beyond its own limit
    assert all(proc.returncode is not None for proc in spawned)
    # The slots are free again.
    monkeypatch.setattr(server, "_MAP_MATCHER", real)
    assert await server._match_hosts(["^a\\."], ["a.example", "b.example"]) == ([True, False], "")


@pytest.mark.asyncio
async def test_a_waiting_call_runs_in_the_time_it_has_left(monkeypatch):
    monkeypatch.setenv("GROK_MAP_REGEX_TIMEOUT_S", "1.5")
    monkeypatch.setattr(server, "_MAP_MATCHER", SLEEPING_MATCHER)
    answers = await asyncio.gather(*(server._match_hosts([seconds], ["a.example"]) for seconds in ("0.3", "0.3", "0.1")))
    assert answers == [([True], "")] * 3  # the third started once one of the first two was done
    # Here the third gets its slot after about 0.6 s and would need 0.5 s, more than the 1 s it was given.
    monkeypatch.setenv("GROK_MAP_REGEX_TIMEOUT_S", "1")
    spawned = recording_spawns(monkeypatch)
    answers = await asyncio.gather(*(timed(server._match_hosts([seconds], ["a.example"])) for seconds in ("0.6", "0.6", "0.5")))
    assert answers[2][0] == (None, "timed out; results not filtered (select_domains matching stopped after 1 s, "
                                   "GROK_MAP_REGEX_TIMEOUT_S)")
    assert len(spawned) == 3 and answers[2][1] < 1.6


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="POSIX alarm; on Windows the Job Object ends the matcher")
@pytest.mark.parametrize("payload", [None, {"patterns": ["^(\\w+\\.?)*example\\.org$"], "hosts": ["a" * 40 + ".com"]}])
def test_the_matcher_ends_itself_without_the_server(payload):
    """V1-3: a server killed by SIGTERM/SIGKILL cannot kill its matcher, which ran on until RLIMIT_CPU
    (61 s of CPU with GROK_MAP_REGEX_TIMEOUT_S=60). Its own alarm ends it, also while it waits for input."""
    started = time.monotonic()
    proc = subprocess.Popen([sys.executable, "-I", "-S", "-c", server._MAP_MATCHER, str(server.MAP_MATCHER_MEMORY_BYTES), "1"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={})
    try:
        if payload is None:  # stdin left open: nothing but the alarm can end it
            status = proc.wait(10)
        else:
            proc.communicate(json.dumps(payload).encode(), timeout=10)
            status = proc.returncode
    finally:
        proc.kill()
        proc.communicate()
    assert status in ((-signal.SIGALRM,) if payload is None else (-signal.SIGALRM, -signal.SIGXCPU))
    assert time.monotonic() - started < 5


def process_gone(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] in ("Z", "X")  # ended, not yet reaped by its new parent
    except OSError:
        return True


# A stand-in for a server: starts a matcher on a slow pattern with a 30 s limit, prints its pid, waits.
FAKE_SERVER = """
import json, os, subprocess, sys, time
proc = subprocess.Popen([sys.executable, "-I", "-S", "-c", sys.argv[1], "536870912", "30", str(os.getpid())],
                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={})
proc.stdin.write(json.dumps({"patterns": [sys.argv[2]], "hosts": ["a" * 40 + ".com"]}).encode())
proc.stdin.close()
print(proc.pid, flush=True)
time.sleep(60)
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux-only")
def test_a_matcher_dies_with_a_killed_server():
    """V1-3: with GROK_MAP_REGEX_TIMEOUT_S=60 the matcher of a SIGKILLed server ran on at PID 1 (6.75 s of
    CPU 6 s later); now the kernel kills it with its parent."""
    server_proc = subprocess.Popen([sys.executable, "-I", "-c", FAKE_SERVER, server._MAP_MATCHER, "^(\\w+\\.?)*example\\.org$"],
                                   stdout=subprocess.PIPE)
    try:
        matcher = int(server_proc.stdout.readline())
        time.sleep(0.5)
        assert not process_gone(matcher)  # busy on the pattern
        server_proc.kill()
        server_proc.wait(5)
        deadline = time.monotonic() + 3
        while not process_gone(matcher) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert process_gone(matcher)
    finally:
        server_proc.kill()
        server_proc.stdout.close()
        server_proc.wait(5)


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="POSIX alarm")
@pytest.mark.asyncio
async def test_a_matcher_that_ended_itself_at_its_deadline_timed_out(monkeypatch):
    monkeypatch.setattr(server, "_MAP_MATCHER", "import os, signal; os.kill(os.getpid(), signal.SIGALRM)")
    monkeypatch.setenv("GROK_MAP_REGEX_TIMEOUT_S", "1")
    assert await server._match_hosts(["^a"], ["a.example"]) == (
        None, "timed out; results not filtered (select_domains matching stopped after 1 s, GROK_MAP_REGEX_TIMEOUT_S)")


@pytest.mark.asyncio
async def test_shutting_down_kills_running_matchers(monkeypatch):
    """main() kills the matchers before os._exit (SIGTERM, SIGINT, end of the stdio session)."""
    monkeypatch.setenv("GROK_MAP_REGEX_TIMEOUT_S", "30")
    spawned = recording_spawns(monkeypatch)
    task = asyncio.create_task(server._match_hosts(["^(\\w+\\.?)*example\\.org$"], ["a" * 40 + ".com"]))
    while not spawned:
        await asyncio.sleep(0.01)
    assert spawned[0] in server._live_matchers
    server._kill_matchers()
    matches, note = await asyncio.wait_for(task, 5)
    assert matches is None and note.startswith("failed; results not filtered")
    assert spawned[0].returncode is not None and spawned[0] not in server._live_matchers


@pytest.mark.asyncio
async def test_each_matcher_is_put_in_the_job_before_it_gets_its_input(monkeypatch):
    contained = []
    spawned = recording_spawns(monkeypatch)
    monkeypatch.setattr(server, "_contain_matcher", lambda pid: contained.append((pid, spawned[-1].stdin.transport.is_closing())))
    assert await server._match_hosts(["^a\\."], ["a.example"]) == ([True], "")
    assert contained == [(spawned[0].pid, False)]


class FakeKernel32:
    """The kernel32 calls the matcher job makes, recorded; each returns what the test asks for."""

    def __init__(self, job=0x40, set_ok=1, process=0x80):
        self.job, self.set_ok, self.process = job, set_ok, process
        self.calls = []

    def CreateJobObjectW(self, attributes, name):
        self.calls.append(("CreateJobObjectW", attributes, name))
        return self.job

    def SetInformationJobObject(self, job, info_class, info, size):
        self.calls.append(("SetInformationJobObject", job, info_class, info._obj, size))
        return self.set_ok

    def OpenProcess(self, access, inherit, pid):
        self.calls.append(("OpenProcess", access, inherit, pid))
        return self.process

    def AssignProcessToJobObject(self, job, process):
        self.calls.append(("AssignProcessToJobObject", job, process))
        return 1

    def CloseHandle(self, handle):
        self.calls.append(("CloseHandle", handle))
        return 1


def as_windows(monkeypatch, kernel32):
    monkeypatch.setattr(server, "_matcher_job", None)
    monkeypatch.setattr(server.os, "name", "nt")
    if kernel32 is not None:
        monkeypatch.setattr(server, "_kernel32", lambda: kernel32)


def test_windows_matchers_live_in_a_job_object(monkeypatch):
    """V1-2/V1-3 on Windows: no resource limits and no SIGALRM there; the job caps each matcher's memory
    and kills every matcher when the server's handle closes, that is when the server ends."""
    kernel32 = FakeKernel32()
    as_windows(monkeypatch, kernel32)
    server._contain_matcher(4242)
    server._contain_matcher(4343)
    names = [call[0] for call in kernel32.calls]
    assert names == ["CreateJobObjectW", "SetInformationJobObject"] + ["OpenProcess", "AssignProcessToJobObject", "CloseHandle"] * 2
    _, job, info_class, info, size = kernel32.calls[1]
    assert job == 0x40 and info_class == 9 and size == ctypes.sizeof(info) == (144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112)
    assert info.BasicLimitInformation.LimitFlags == 0x2000 | 0x100 | 0x400  # KILL_ON_JOB_CLOSE, PROCESS_MEMORY, DIE_ON_UNHANDLED_EXCEPTION
    assert info.ProcessMemoryLimit == server.MAP_MATCHER_MEMORY_BYTES + 64 * 1024 * 1024
    assert kernel32.calls[2] == ("OpenProcess", 0x0100 | 0x0001, False, 4242)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
    assert kernel32.calls[3:5] == [("AssignProcessToJobObject", 0x40, 0x80), ("CloseHandle", 0x80)]
    assert kernel32.calls[5][3] == 4343
    ctypes.c_void_p.from_param(ctypes.byref(info))  # what SetInformationJobObject's argtypes accept


@pytest.mark.parametrize("answers, calls", [
    ({"job": None}, ["CreateJobObjectW"]),  # no job: the timeout kill alone, and no retry per call
    ({"set_ok": 0}, ["CreateJobObjectW", "SetInformationJobObject", "CloseHandle"]),
    (None, None),  # no ctypes.WinDLL (this platform): nothing raised
])
def test_without_a_job_object_the_timeout_still_applies(monkeypatch, answers, calls):
    kernel32 = FakeKernel32(**answers) if answers is not None else None
    as_windows(monkeypatch, kernel32)
    server._contain_matcher(4242)
    server._contain_matcher(4343)
    assert server._matcher_job == 0
    if kernel32 is not None:
        assert [call[0] for call in kernel32.calls] == calls


def test_the_job_object_is_windows_only(monkeypatch):
    kernel32 = FakeKernel32()
    monkeypatch.setattr(server, "_kernel32", lambda: kernel32)
    monkeypatch.setattr(server, "_matcher_job", None)
    monkeypatch.setattr(server.os, "name", "posix")
    server._contain_matcher(4242)
    assert kernel32.calls == [] and server._matcher_job is None
