"""web_fetch's default page order (v1.13.1 field report: "web_fetch may return stale content").

Tavily serves pages from its own cache without any freshness control: on 2026-10-17 its copy of
python.org/downloads still called 3.14.8 the latest release and 3.15 a "planned" pre-release, eight days
after 3.15.0 was out, while Firecrawl's live scrape named 3.15.0. Ordinary pages are therefore read by
Firecrawl first (GROK_FETCH_PAGE_FIRST=firecrawl), Tavily only when Firecrawl fails or its copy is unusable,
and a Tavily copy always says that it may be stale. Also here: no extractor after the origin answered 404
(field report: a GitHub 404 was followed by a Tavily and a Firecrawl call, both 404), and image markup
removed from page copies (a PyPI page was 41 % badge and icon markup).
"""
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from grok_search import direct, fetching, verify
from grok_search.config import Config

PUBLIC_IP = "93.184.216.34"
REAL_CLIENT = httpx.AsyncClient

# Recorded 2026-10-17, shortened: Tavily's copy of https://www.python.org/downloads/ (stale) and Firecrawl's
# live scrape of it (the page renders its release table with scripts that did not run in Firecrawl's browser).
PY_DOWNLOADS_TAVILY = (
    "# Download the latest version of Python\n\n[Download Python 3.14.8](/downloads/release/python-3148/)\n\n"
    "## Active Python releases\n\n1. 3.15 pre-release [Download](/downloads/latest/python3.15/) 2026-10-01 (planned)\n"
    "2. 3.14 bugfix [Download](/downloads/latest/python3.14/) 2025-10-07\n\n" + "Release table row. " * 300
)
PY_DOWNLOADS_LIVE = (
    "**Notice:** This page displays a fallback because interactive scripts did not run.\n\n## Active Python releases\n\n"
    "## Sources\n\nDownload the latest [Python 3](https://www.python.org/ftp/python/3.15.0/Python-3.15.0.tar.xz) source.\n\n"
    + "Sponsors and licenses. " * 120
)
CLOUDFLARE_CHALLENGE = "# Just a moment...\n\nVerifying you are human. This may take a few seconds.\n\nPerformance & security by Cloudflare"
ARTICLE = "# Release notes\n\n" + "Version 2.3 fixes the parser. " * 120


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    for name in ("GROK_FETCH_PAGE_FIRST", "GROK_FETCH_STRIP_IMAGES", "GROK_FETCH_MIN_CHARS", "GROK_FETCH_DIRECT",
                 "GROK_FETCH_MAX_AGE_S", "TAVILY_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-k")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-k")
    monkeypatch.setenv("TAVILY_API_URL", "https://tavily.example.test")
    monkeypatch.setenv("FIRECRAWL_API_URL", "https://firecrawl.example.test/v2")
    verify._DOH_CACHE.clear()


def backends(monkeypatch, tavily, firecrawl, origin=None):
    """tavily / firecrawl: (status, json) answered by Tavily Extract and Firecrawl Scrape; origin: a handler for
    every other host (the direct download). Returns the hosts asked, in order."""
    calls = []

    async def handler(request):
        host = request.url.host
        calls.append(host)
        if host == "tavily.example.test":
            return httpx.Response(tavily[0], json=tavily[1])
        if host == "firecrawl.example.test":
            return httpx.Response(firecrawl[0], json=firecrawl[1])
        response = await origin(request)
        response.extensions.setdefault("network_stream", PeerStream())
        return response

    monkeypatch.setattr(fetching.httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs))
    return calls


class PeerStream:
    def get_extra_info(self, name):
        return (PUBLIC_IP, 443) if name == "server_addr" else None


def tavily_ok(text):
    return 200, {"results": [{"url": "u", "raw_content": text}]}


def firecrawl_ok(markdown, status=200, content_type="text/html; charset=utf-8", **metadata):
    return 200, {"success": True, "data": {"markdown": markdown, "metadata": {
        "statusCode": status, "contentType": content_type, **metadata}}}


TAVILY_FAILED = (200, {"results": [], "failed_results": [{"url": "u", "error": "Failed to fetch url"}]})
FIRECRAWL_QUOTA = (402, {"success": False, "error": "Insufficient credits to perform this request."})


def split(out: str):
    header, body = out.split("\n\n", 1)
    return header.split("\n"), body


# ------------------------------------------------------------------ setting
def test_page_first_setting(monkeypatch):
    config = Config()
    assert config.fetch_page_first == "firecrawl"
    monkeypatch.setenv("GROK_FETCH_PAGE_FIRST", " Tavily ")
    assert config.fetch_page_first == "tavily"
    monkeypatch.setenv("GROK_FETCH_PAGE_FIRST", "cheapest")
    assert config.fetch_page_first == "firecrawl"
    assert any("GROK_FETCH_PAGE_FIRST" in warning for warning in config.config_warnings())
    monkeypatch.setenv("GROK_FETCH_PAGE_FIRST", "firecrawl")
    assert config.fetch_page_first == "firecrawl"
    assert not any("GROK_FETCH_PAGE_FIRST" in warning for warning in config.config_warnings())
    info = config.get_config_info()
    assert info["GROK_FETCH_PAGE_FIRST"] == "firecrawl" and info["GROK_FETCH_STRIP_IMAGES"] is True


# ------------------------------------------------------------------ live copy first
@pytest.mark.asyncio
async def test_the_live_copy_is_read_first_and_the_stale_cache_never_asked(monkeypatch):
    calls = backends(monkeypatch, tavily_ok(PY_DOWNLOADS_TAVILY), firecrawl_ok(PY_DOWNLOADS_LIVE))
    lines, body = split(await fetching.fetch_page("https://www.python.org/downloads/"))
    assert calls == ["firecrawl.example.test"]
    assert lines == [f"[web_fetch] source=firecrawl total_chars={len(PY_DOWNLOADS_LIVE)} offset=0 returned={len(PY_DOWNLOADS_LIVE)} "
                     "truncated=false content_type=text/html;charset=utf-8 cache=bypassed"]
    assert "3.15.0" in body and "3.14.8" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("firecrawl, attempt", [
    (FIRECRAWL_QUOTA, "firecrawl:quota"),
    ((200, {"success": True, "data": {"markdown": ""}}), "firecrawl:empty_content"),
])
async def test_without_a_live_copy_tavily_answers_and_says_it_may_be_stale(monkeypatch, firecrawl, attempt):
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "1")
    calls = backends(monkeypatch, tavily_ok(ARTICLE), firecrawl)
    lines, body = split(await fetching.fetch_page("https://example.test/notes"))
    assert calls == ["firecrawl.example.test", "tavily.example.test"] and body == ARTICLE
    assert lines[0].endswith(f"cache=possible attempts={attempt},tavily:ok")
    assert lines[1] == fetching.TAVILY_CACHE_NOTE and lines[2].startswith("- firecrawl: ")


@pytest.mark.asyncio
async def test_a_cached_copy_of_a_page_that_is_gone_now_says_so(monkeypatch):
    backends(monkeypatch, tavily_ok(ARTICLE), firecrawl_ok("# Not Found\n\nnothing here", status=404))
    lines, body = split(await fetching.fetch_page("https://example.test/removed"))
    assert body == ARTICLE and lines[0].startswith("[web_fetch] source=tavily")
    assert "attempts=firecrawl:extract_failed,tavily:ok" in lines[0]
    assert lines[1] == "注意: Firecrawl 实时抓取时源站答复 HTTP 404，下面是 Tavily 缓存的旧副本，该页面现在很可能已经不存在或已移动。"
    assert lines[2] == fetching.TAVILY_CACHE_NOTE
    assert lines[3] == "- firecrawl: extract_failed (HTTP 404) — target page answered HTTP 404"


@pytest.mark.asyncio
async def test_a_short_live_copy_gives_way_only_to_a_much_longer_cached_copy(monkeypatch):
    stub = "Loading… please enable JavaScript. " * 10  # 350 characters
    calls = backends(monkeypatch, tavily_ok(ARTICLE), firecrawl_ok(stub))
    lines, body = split(await fetching.fetch_page("https://example.test/app"))
    assert calls == ["firecrawl.example.test", "tavily.example.test"] and body == ARTICLE
    assert lines[0].endswith(f"attempts=firecrawl:short({len(stub)}),tavily:ok") and lines[1] == fetching.TAVILY_CACHE_NOTE
    # A short page stays live when the cached copy is not at least twice as long (boilerplate, an older version).
    short_page = "A short but complete page. " * 60  # 1620 characters
    backends(monkeypatch, tavily_ok(short_page + "Footer and navigation. " * 50), firecrawl_ok(short_page))
    lines, body = split(await fetching.fetch_page("https://example.test/short"))
    assert body == short_page and lines[0].startswith("[web_fetch] source=firecrawl")
    assert lines[0].endswith(f"attempts=firecrawl:short({len(short_page)}),tavily:unused") and len(lines) == 1


@pytest.mark.asyncio
async def test_a_live_bot_check_is_replaced_by_the_cached_copy(monkeypatch):
    backends(monkeypatch, tavily_ok(ARTICLE), firecrawl_ok(CLOUDFLARE_CHALLENGE, title="Just a moment..."))
    lines, body = split(await fetching.fetch_page("https://example.test/notes"))
    assert body == ARTICLE and lines[0].startswith("[web_fetch] source=tavily")
    assert lines[0].endswith("attempts=firecrawl:interstitial,tavily:ok") and "warning=" not in lines[0]


@pytest.mark.asyncio
async def test_two_bot_checks_return_the_live_one_flagged(monkeypatch):
    backends(monkeypatch, tavily_ok(CLOUDFLARE_CHALLENGE), firecrawl_ok(CLOUDFLARE_CHALLENGE, title="Just a moment..."))
    lines, _ = split(await fetching.fetch_page("https://example.test/notes"))
    assert lines[0].startswith("[web_fetch] source=firecrawl") and "warning=interstitial" in lines[0]
    assert "attempts=firecrawl:interstitial,tavily:interstitial" in lines[0]
    assert lines[1].startswith("注意: 这很可能不是请求页面的正文")


@pytest.mark.asyncio
async def test_without_a_firecrawl_key_tavily_reads_pages_first(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY")
    calls = backends(monkeypatch, tavily_ok(ARTICLE), firecrawl_ok(PY_DOWNLOADS_LIVE))
    lines, body = split(await fetching.fetch_page("https://example.test/notes"))
    assert calls == ["tavily.example.test"] and body == ARTICLE and lines[1] == fetching.TAVILY_CACHE_NOTE


@pytest.mark.asyncio
async def test_tavily_first_is_still_available(monkeypatch):
    monkeypatch.setenv("GROK_FETCH_PAGE_FIRST", "tavily")
    calls = backends(monkeypatch, tavily_ok(PY_DOWNLOADS_TAVILY), firecrawl_ok(PY_DOWNLOADS_LIVE))
    lines, body = split(await fetching.fetch_page("https://www.python.org/downloads/"))
    assert calls == ["tavily.example.test"] and body == PY_DOWNLOADS_TAVILY
    assert lines[0].endswith("cache=possible") and lines[1] == fetching.TAVILY_CACHE_NOTE


# ------------------------------------------------------------------ URLs that may be files
def proxied_origin(monkeypatch):
    """The direct download is not attempted: a proxy resolves the name (only code hosts go direct then)."""
    monkeypatch.setattr(direct, "_through_proxy", lambda client, url: True)


@pytest.mark.asyncio
async def test_firecrawl_content_type_tells_a_file_from_a_page(monkeypatch):
    proxied_origin(monkeypatch)
    source = "import { helpers } from './helpers.js';\n\n![not an image](in-a-string)\nexport const add = (a, b) => a + b;\n"
    calls = backends(monkeypatch, tavily_ok("unused"), firecrawl_ok(source, content_type="application/javascript"))
    lines, body = split(await fetching.fetch_page("https://static.example.org/lib/add.js"))
    # A file is complete at any length and returned verbatim: no Tavily call, no image stripping.
    assert body == source and calls == ["firecrawl.example.test"]
    assert lines[0].startswith("[web_fetch] source=firecrawl") and "images_stripped" not in lines[0]
    page = "# chart.js\n\n![npm version](https://img.shields.io/npm/v/chart.js.svg)\n\n" + "Simple yet flexible charting. " * 100
    backends(monkeypatch, tavily_ok("unused"), firecrawl_ok(page))
    lines, body = split(await fetching.fetch_page("https://www.npmjs.com/package/chart.js"))
    assert "img.shields.io" not in body and "[image: npm version]" in body
    assert "images_stripped=1" in lines[0] and "degraded" not in lines[0]


@pytest.mark.asyncio
async def test_a_damaged_live_file_copy_is_compared_with_tavily(monkeypatch):
    proxied_origin(monkeypatch)
    lines_of_code = "\n".join(f"    value_{i} = compute_{i}(value_{i - 1})" for i in range(40))
    intact = "def run():\n" + lines_of_code + "\n"
    damaged = intact.replace("_", "\\_")
    backends(monkeypatch, tavily_ok(intact), firecrawl_ok(damaged, content_type="text/plain"))
    lines, body = split(await fetching.fetch_page("https://static.example.org/pkg/run.py"))
    assert body == intact and lines[0].startswith("[web_fetch] source=tavily")
    assert "attempts=firecrawl:degraded,tavily:ok" in lines[0]


@pytest.mark.asyncio
async def test_when_firecrawl_fails_a_tavily_copy_of_a_file_is_still_checked_for_damage(monkeypatch):
    proxied_origin(monkeypatch)
    collapsed = "import { helpers } from './helpers.js'; export function add(a, b) { return helpers.sum(a, b); }" * 30
    backends(monkeypatch, tavily_ok(collapsed), FIRECRAWL_QUOTA)
    lines, body = split(await fetching.fetch_page("https://static.example.org/lib/add.js"))
    assert body == collapsed and "warning=degraded_code degraded=collapsed_newlines" in lines[0]
    assert "attempts=firecrawl:quota,tavily:degraded" in lines[0]


# ------------------------------------------------------------------ the origin said 404
@pytest.mark.asyncio
@pytest.mark.parametrize("status, content_type", [(404, "text/html; charset=utf-8"), (404, "text/plain"), (410, "text/plain")])
async def test_no_extractor_after_the_origin_said_not_found(monkeypatch, status, content_type):
    async def origin(request):
        return httpx.Response(status, text="Not Found", headers={"content-type": content_type})

    monkeypatch.setattr(verify, "resolve_host", AsyncMock(return_value=[PUBLIC_IP]))
    calls = backends(monkeypatch, tavily_ok(ARTICLE), firecrawl_ok(ARTICLE), origin=origin)
    out = await fetching.fetch_page("https://github.com/o/r/blob/main/src/gone.py")
    assert out.startswith(f"[web_fetch] error=fetch_failed attempts=direct:extract_failed page_status={status}\n\n源站答复 HTTP {status}")
    assert "tavily.example.test" not in calls and "firecrawl.example.test" not in calls


@pytest.mark.asyncio
async def test_other_origin_errors_still_reach_the_extractors(monkeypatch):
    async def origin(request):
        return httpx.Response(403, text="Forbidden", headers={"content-type": "text/plain"})

    monkeypatch.setattr(verify, "resolve_host", AsyncMock(return_value=[PUBLIC_IP]))
    calls = backends(monkeypatch, tavily_ok("print('hello')\n" * 200), firecrawl_ok("print('hello')\n", content_type="text/plain"), origin=origin)
    lines, body = split(await fetching.fetch_page("https://github.com/o/r/blob/main/src/hello.py"))
    assert "firecrawl.example.test" in calls and lines[0].startswith("[web_fetch] source=firecrawl")


# ------------------------------------------------------------------ image markup
PYPI_LIKE = (
    "![PyPI](/static/images/logo-small.0e0855d0.svg)\n\n# fastapi 0.143.3\n\n"
    "[![Test](https://pypi-camo.freetls.fastly.net/e377/6874747073)](https://github.com/fastapi/fastapi/actions?query=workflow%3ATest)\n"
    "[![](https://pypi-camo.freetls.fastly.net/a15e/6874)](https://opencollective.com/fastapi)\n"
    "![Diagram (v2)](https://example.test/img/arch_(v2).png \"Architecture\")\n"
    "![tiny](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk)\n\n"
    + "".join(f"![](https://pypi.org/static/images/white-cube.2351a86c.svg)\n[0.{i}.0](/project/fastapi/0.{i}.0/)\nOct {i}, 2025\n"
              for i in range(1, 6))
)


def test_strip_images():
    text, removed = fetching.strip_images(PYPI_LIKE)
    assert removed == 10 and "![" not in text and "white-cube" not in text and "base64" not in text
    assert "[image: PyPI]" in text and "[image: Diagram (v2)]" in text and "[image: tiny]" in text
    assert "[Test](https://github.com/fastapi/fastapi/actions?query=workflow%3ATest)" in text
    assert "opencollective" not in text  # a linked image without alt text says nothing
    assert "[0.3.0](/project/fastapi/0.3.0/)\nOct 3, 2025" in text and "\n\n\n" not in text
    assert fetching.strip_images("No images, [a link](https://example.test/) and ![broken (markup") == (
        "No images, [a link](https://example.test/) and ![broken (markup", 0)


def test_strip_images_is_linear_on_hostile_markup():
    for hostile in ("![a](" * 50_000, "[![a](" * 50_000, "![" + "a" * 300 + "](x" * 2_000, "![a](" + "x" * 500_000):
        started = time.monotonic()
        fetching.strip_images(hostile)
        assert time.monotonic() - started < 2


@pytest.mark.asyncio
async def test_page_copies_lose_their_image_markup_unless_turned_off(monkeypatch):
    page = PYPI_LIKE + "Release history. " * 200
    backends(monkeypatch, tavily_ok("unused"), firecrawl_ok(page))
    lines, body = split(await fetching.fetch_page("https://pypi.org/project/fastapi/"))
    assert "cache=bypassed images_stripped=10" in lines[0] and "white-cube" not in body
    monkeypatch.setenv("GROK_FETCH_STRIP_IMAGES", "false")
    lines, body = split(await fetching.fetch_page("https://pypi.org/project/fastapi/"))
    assert body == page and "images_stripped" not in lines[0]


@pytest.mark.asyncio
async def test_a_directly_downloaded_file_keeps_its_images(monkeypatch):
    readme = "# Project\n\n[![CI](https://github.com/o/r/actions/workflows/ci.yml/badge.svg)](https://github.com/o/r/actions)\n"

    async def origin(request):
        return httpx.Response(200, text=readme, headers={"content-type": "text/plain; charset=utf-8"})

    monkeypatch.setattr(verify, "resolve_host", AsyncMock(return_value=[PUBLIC_IP]))
    backends(monkeypatch, tavily_ok("unused"), firecrawl_ok("unused"), origin=origin)
    lines, body = split(await fetching.fetch_page("https://raw.githubusercontent.com/o/r/main/README.md"))
    assert body == readme and lines[0].startswith("[web_fetch] source=direct")
