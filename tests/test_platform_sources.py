"""web_search(platform=...) steers the extra-source engines (E11)."""
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp import Client

from grok_search import server
from grok_search.platforms import host_matches, resolve_platform
from grok_search.providers.grok import GrokResponse, GrokSearchProvider
from grok_search.sources import SourcesCache

GITHUB = ["github.com"]
GITHUB_ACCEPTED = ["github.com", "githubusercontent.com"]
_REAL_ASYNC_CLIENT = httpx.AsyncClient


# --- platform names -> domains ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform, domains, hint", [
    ("GitHub", GITHUB, ""),
    ("github", GITHUB, ""),
    ("GitHub, Reddit", GITHUB + ["reddit.com"], ""),
    ("GitHub | Reddit", GITHUB + ["reddit.com"], ""),
    ("GitHub or Reddit", GITHUB + ["reddit.com"], ""),
    ("X/Twitter", ["x.com", "twitter.com"], ""),
    ("Twitter", ["x.com", "twitter.com"], ""),
    ("Stack Overflow", ["stackoverflow.com"], ""),
    ("stack-overflow", ["stackoverflow.com"], ""),
    ("Hacker News", ["news.ycombinator.com"], ""),
    ("YouTube", ["youtube.com"], ""),
    ("arXiv", ["arxiv.org"], ""),
    ("Wikipedia", ["wikipedia.org"], ""),
    ("知乎", ["zhihu.com"], ""),
    ("知乎和B站", ["zhihu.com", "bilibili.com"], ""),
    ("知乎、微博", ["zhihu.com", "weibo.com"], ""),
    ("Bilibili", ["bilibili.com"], ""),
    ("CSDN", ["csdn.net"], ""),
    ("掘金", ["juejin.cn"], ""),
    ("V2EX", ["v2ex.com"], ""),
    ("Hugging Face", ["huggingface.co"], ""),
    ("PyPI", ["pypi.org"], ""),
    ("npm", ["npmjs.com"], ""),
    ("github.com", ["github.com"], ""),
    ("GitHub.com", ["github.com"], ""),
    ("Fly.io", ["fly.io"], ""),
    ("tokio.rs", ["tokio.rs"], ""),
    ("https://ASP.NET/", ["asp.net"], ""),
    ("www.V2EX.com", ["v2ex.com"], ""),
    ("https://docs.python.org/3/library/", ["docs.python.org"], ""),
    ("GitHub issues", GITHUB, "issues"),
    ("Stack Overflow questions", ["stackoverflow.com"], "questions"),
])
def test_known_platforms_and_hostnames_become_domain_filters(platform, domains, hint):
    scope = resolve_platform(platform)
    assert scope.applied == "domain_filter"
    assert scope.domains == domains and scope.hint == hint and scope.unresolved == []


@pytest.mark.parametrize("platform, unresolved", [
    ("Mastodon", ["Mastodon"]),
    ("GitHub, Mastodon", ["Mastodon"]),        # a filter would drop the Mastodon results
    ("X Window System", ["X Window System"]),  # short aliases count only as a whole name
    ("Node.js", ["Node.js"]),                  # a project name, not a hostname
    ("discord.py", ["discord.py"]),            # a library; discord.py does not even resolve (NXDOMAIN)
    ("ASP.NET", ["ASP.NET"]),                  # product names written in capitals
    ("Socket.IO", ["Socket.IO"]),
    ("Google Scholar", ["Google Scholar"]),
])
def test_unknown_platforms_become_a_query_hint_without_a_filter(platform, unresolved):
    scope = resolve_platform(platform)
    assert scope.applied == "query_hint" and scope.domains == []
    assert scope.hint == platform and scope.unresolved == unresolved
    assert scope.engine_query("q") == f"q {platform}"
    assert scope.allows("https://anything.example.test/")


@pytest.mark.parametrize("platform", ["", "   ", "，，", " | "])
def test_empty_platform_applies_nothing(platform):
    scope = resolve_platform(platform)
    assert scope.applied == "none" and scope.engine_query("q") == "q"


@pytest.mark.parametrize("url, expected", [
    ("https://github.com/encode/httpx", True),
    ("https://www.github.com/linq-team/linq-python", True),
    ("https://gist.github.com/x/y", True),
    ("https://raw.githubusercontent.com/a/b/main/c.py", True),
    ("https://GitHub.com./a", True),
    ("https://githubhelp.com/Andarius/niquests", False),  # look-alike seen in a live run
    ("https://evilgithub.com/x", False),
    ("https://github.com.evil.example/x", False),
    ("https://example.test/?u=https://github.com/", False),
    ("not a url", False),
    ("http://[::1", False),
])
def test_host_filter_is_suffix_based_never_substring(url, expected):
    assert host_matches(url, GITHUB_ACCEPTED) is expected
    assert resolve_platform("GitHub").allows(url) is expected


def test_short_link_hosts_are_accepted_but_not_sent_to_the_engines():
    scope = resolve_platform("GitHub, Reddit, YouTube")
    assert scope.domains == ["github.com", "reddit.com", "youtube.com"]
    assert scope.also_accepted == ["githubusercontent.com", "redd.it", "youtu.be"]
    assert scope.allows("https://redd.it/abc") and scope.allows("https://youtu.be/xyz")
    assert scope.report(["tavily"], 0)["also_accepted"] == ["githubusercontent.com", "redd.it", "youtu.be"]


# --- extra_sources with a platform ----------------------------------------------------------------------

@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("GROK_API_URL", "https://api.example.test/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setenv("GROK_MODEL", "test-model")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setenv("GROK_VERIFY_URLS", "false")
    monkeypatch.setenv("GROK_VERIFY_IDS", "false")
    monkeypatch.delenv("TAVILY_ENABLED", raising=False)
    monkeypatch.setattr(server.config, "_cached_model", None)
    monkeypatch.setattr(server, "_SOURCES_CACHE", SourcesCache())
    grok = AsyncMock(return_value=GrokResponse("answer [[1]](https://github.com/cited/repo)",
                                                [{"type": "url_citation", "url": "https://github.com/cited/repo", "title": "1"}]))
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", grok)
    return grok


def engine(make):
    async def run(query, limit, include_domains=None):
        run.calls.append({"query": query, "limit": limit, "include_domains": include_domains})
        return make(limit)
    run.calls = []
    return run


def hits(host, n, prefix="r"):
    return [{"url": f"https://{host}/{prefix}{i}", "title": f"{host} {i}"} for i in range(1, n + 1)]


async def search(**arguments):
    async with Client(server.mcp) as client:
        result = await client.call_tool("web_search", {"query": "httpx", **arguments})
    assert not result.is_error
    return json.loads(next(block.text for block in result.content if block.type == "text"))


@pytest.mark.asyncio
async def test_known_platform_is_sent_to_both_engines_and_reported(configured, monkeypatch):
    tavily = engine(lambda n: hits("github.com", n, "t"))
    firecrawl = engine(lambda n: hits("gist.github.com", n, "f"))
    monkeypatch.setattr(server, "_call_tavily_search", tavily)
    monkeypatch.setattr(server, "_call_firecrawl_search", firecrawl)
    out = await search(platform="GitHub", extra_sources=4)
    assert tavily.calls == [{"query": "httpx", "limit": 4, "include_domains": GITHUB}]
    assert firecrawl.calls == [{"query": "httpx", "limit": 4, "include_domains": GITHUB}]
    # Grok still gets the platform as its own argument.
    assert configured.await_args.args[:2] == ("httpx", "GitHub")
    report = out["extra_sources_report"]
    assert report["returned"] == 4
    assert report["platform"] == {
        "requested": "GitHub", "applied": "domain_filter", "domains": GITHUB, "also_accepted": ["githubusercontent.com"],
        "engines": {"tavily": "include_domains", "firecrawl": "site:"}, "off_platform_skipped": 0,
    }


@pytest.mark.asyncio
async def test_off_platform_hits_are_dropped_and_backfilled(configured, monkeypatch):
    def tavily_results(n):
        # Interleaved: every other hit is off GitHub (including a look-alike host).
        mixed = []
        for i in range(1, n + 1):
            mixed.append({"url": f"https://github.com/t{i}" if i % 2 else f"https://githubhelp.com/t{i}", "title": str(i)})
        return mixed

    tavily = engine(tavily_results)
    firecrawl = engine(lambda n: [{"url": "https://blog.example.test/post", "title": "off"}])
    monkeypatch.setattr(server, "_call_tavily_search", tavily)
    monkeypatch.setattr(server, "_call_firecrawl_search", firecrawl)
    out = await search(platform="GitHub", extra_sources=4)
    report = out["extra_sources_report"]
    # First round: Tavily 2+2=4 hits (2 on GitHub), Firecrawl 2+2=4 asked, 1 off-platform hit (exhausted).
    # Backfill: Tavily 4 + shortfall 2 + margin 2 + 3 off-platform skipped = 11 -> 6 on GitHub.
    assert [call["limit"] for call in tavily.calls] == [4, 11]
    assert report["returned"] == 4
    assert report["engines"]["tavily"]["off_platform_skipped"] == 5
    assert report["engines"]["firecrawl"]["off_platform_skipped"] == 1
    assert report["platform"]["off_platform_skipped"] == 6
    assert "githubhelp.com" not in out["content"] and "blog.example.test" not in out["content"]
    assert out["content"].count("[tavily]") == 4


@pytest.mark.asyncio
async def test_unknown_platform_goes_into_the_engine_query(configured, monkeypatch):
    tavily = engine(lambda n: hits("social.example.test", n, "t"))
    firecrawl = engine(lambda n: hits("blog.example.test", n, "f"))
    monkeypatch.setattr(server, "_call_tavily_search", tavily)
    monkeypatch.setattr(server, "_call_firecrawl_search", firecrawl)
    out = await search(platform="Mastodon", extra_sources=2)
    assert tavily.calls == [{"query": "httpx Mastodon", "limit": 3, "include_domains": None}]
    assert firecrawl.calls == [{"query": "httpx Mastodon", "limit": 3, "include_domains": None}]
    report = out["extra_sources_report"]
    assert report["returned"] == 2  # nothing is filtered
    assert report["platform"] == {
        "requested": "Mastodon", "applied": "query_hint", "engines": {"tavily": "query", "firecrawl": "query"},
        "query_hint": "Mastodon", "unresolved": ["Mastodon"],
    }


@pytest.mark.asyncio
async def test_several_platforms_use_the_union_of_their_domains(configured, monkeypatch):
    tavily = engine(lambda n: hits("www.reddit.com", n, "t"))
    firecrawl = engine(lambda n: hits("github.com", n, "f") + hits("stackoverflow.com", 1, "x"))
    monkeypatch.setattr(server, "_call_tavily_search", tavily)
    monkeypatch.setattr(server, "_call_firecrawl_search", firecrawl)
    out = await search(platform="GitHub, Reddit", extra_sources=4)
    assert tavily.calls[0]["include_domains"] == GITHUB + ["reddit.com"]
    report = out["extra_sources_report"]
    assert report["returned"] == 4 and report["platform"]["off_platform_skipped"] == 1
    assert "stackoverflow.com" not in out["content"]


@pytest.mark.asyncio
async def test_qualifier_words_are_added_to_the_query_with_the_filter(configured, monkeypatch):
    tavily = engine(lambda n: hits("github.com", n))
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    monkeypatch.delenv("FIRECRAWL_API_KEY")
    monkeypatch.setattr(server, "_call_tavily_search", tavily)
    out = await search(platform="GitHub issues", extra_sources=1)
    assert tavily.calls == [{"query": "httpx issues", "limit": 3, "include_domains": GITHUB}]
    assert out["extra_sources_report"]["platform"]["query_hint"] == "issues"


@pytest.mark.asyncio
async def test_without_platform_the_engines_are_called_as_before(configured, monkeypatch):
    calls = []

    async def two_args(query, limit):  # stand-ins without include_domains keep working
        calls.append((query, limit))
        return hits("example.test", limit)

    monkeypatch.setattr(server, "_call_tavily_search", two_args)
    monkeypatch.setattr(server, "_call_firecrawl_search", two_args)
    out = await search(extra_sources=2)
    assert sorted(calls) == [("httpx", 3), ("httpx", 3)]
    assert "platform" not in out["extra_sources_report"]


# --- what the engines receive -----------------------------------------------------------------------------

@pytest.fixture
def engine_http(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    monkeypatch.setenv("TAVILY_API_URL", "https://tavily.example.test")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setenv("FIRECRAWL_API_URL", "https://firecrawl.example.test/v2")
    monkeypatch.delenv("TAVILY_ENABLED", raising=False)
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if request.url.host.startswith("tavily"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"success": True, "data": {"web": []}})

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _REAL_ASYNC_CLIENT(*a, **{**kw, "transport": httpx.MockTransport(handler)}))
    return bodies


@pytest.mark.asyncio
async def test_tavily_request_carries_include_domains(engine_http):
    await server._call_tavily_search("httpx", 3, include_domains=GITHUB)
    await server._call_tavily_search("httpx", 3)
    assert engine_http[0]["include_domains"] == GITHUB and engine_http[0]["query"] == "httpx"
    assert "include_domains" not in engine_http[1]


@pytest.mark.asyncio
async def test_firecrawl_request_carries_site_operators(engine_http):
    await server._call_firecrawl_search("httpx", 3, include_domains=["github.com"])
    await server._call_firecrawl_search("httpx", 3, include_domains=["github.com", "reddit.com"])
    await server._call_firecrawl_search("httpx", 3)
    assert [body["query"] for body in engine_http] == [
        "httpx site:github.com",
        "httpx site:github.com OR site:reddit.com",
        "httpx",
    ]
    assert all("includeDomains" not in body for body in engine_http)
