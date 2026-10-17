import json
import re
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp import Client

from grok_search import fetching, server, verify


@pytest.fixture(autouse=True)
def _cached_page_order(monkeypatch):
    # These tests describe the Tavily-first page order (GROK_FETCH_PAGE_FIRST=tavily, and every setup without a
    # Firecrawl key); test_fetch_live.py covers the default, Firecrawl's live scrape first.
    monkeypatch.setenv("GROK_FETCH_PAGE_FIRST", "tavily")
    monkeypatch.setenv("GROK_FETCH_STRIP_IMAGES", "false")  # page texts are compared as the backends sent them


@pytest.fixture(autouse=True)
def _fast_arxiv(monkeypatch):
    monkeypatch.setattr(verify, "ARXIV_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(verify, "ARXIV_RETRY_DELAY_S", 0.0)
    verify._ARXIV_CACHE.clear()
    verify._arxiv_gate.update(loop=None, lock=None, last_start=0.0)
    fetching._ARXIV_ABS_CACHE.clear()
    fetching._arxiv_cooldown.update(until=0.0, category="")

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2607.20852v1</id>
    <title>Code Monitor Red Teaming for
  Public-Test-Passing Code</title>
    <summary>Visible tests are a common gate for LLM-generated code,
  but passing them does not certify specification correctness.</summary>
    <published>2026-07-23T02:23:09Z</published>
    <updated>2026-07-23T02:23:09Z</updated>
    <author><name>Junchi Liao</name></author>
    <author><name>Jiawen Deng</name></author>
    <arxiv:primary_category term="cs.AI"/>
    <category term="cs.AI"/>
  </entry>
</feed>"""


@pytest.mark.asyncio
async def test_short_tavily_result_falls_back_to_longer_firecrawl(monkeypatch):
    monkeypatch.setenv("GROK_FETCH_MIN_CHARS", "2000")
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value="skeleton " * 50))
    firecrawl = AsyncMock(return_value="full " * 1000)
    monkeypatch.setattr(fetching, "_call_firecrawl_scrape", firecrawl)
    out = await fetching.fetch_page("https://example.test/page")
    assert out.startswith("[web_fetch] source=firecrawl total_chars=5000")
    assert firecrawl.await_count == 1


@pytest.mark.asyncio
async def test_short_tavily_result_is_kept_when_firecrawl_fails(monkeypatch):
    monkeypatch.setenv("GROK_FETCH_MIN_CHARS", "2000")
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value="skeleton"))
    monkeypatch.setattr(fetching, "_call_firecrawl_scrape", AsyncMock(return_value=None))
    out = await fetching.fetch_page("https://example.test/page")
    assert out.startswith("[web_fetch] source=tavily total_chars=8")


@pytest.mark.asyncio
async def test_long_tavily_result_skips_firecrawl(monkeypatch):
    monkeypatch.setenv("GROK_FETCH_MIN_CHARS", "2000")
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value="x" * 5000))
    firecrawl = AsyncMock(return_value="y" * 9000)
    monkeypatch.setattr(fetching, "_call_firecrawl_scrape", firecrawl)
    out = await fetching.fetch_page("https://example.test/page")
    assert out.startswith("[web_fetch] source=tavily total_chars=5000")
    assert firecrawl.await_count == 0


@pytest.mark.asyncio
async def test_default_max_chars_comes_from_config(monkeypatch):
    monkeypatch.setenv("GROK_FETCH_MAX_CHARS", "1000")
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value="z" * 3000))
    out = await fetching.fetch_page("https://example.test/page")
    assert "returned=1000 truncated=true next_offset=1000" in out.split("\n\n", 1)[0]


@pytest.mark.asyncio
async def test_arxiv_abs_uses_api_and_skips_extractors(monkeypatch):
    async def handler(request):
        assert request.url.host == "export.arxiv.org"
        assert request.url.params["id_list"] == "2607.20852v1"  # the requested version is honoured
        return httpx.Response(200, text=ATOM)

    real = httpx.AsyncClient
    monkeypatch.setattr(fetching.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    tavily = AsyncMock(return_value="skeleton")
    monkeypatch.setattr(fetching, "_call_tavily_extract", tavily)
    out = await fetching.fetch_page("https://arxiv.org/abs/2607.20852v1")
    assert out.startswith("[web_fetch] source=arxiv-api")
    assert "\nsource: https://arxiv.org/abs/2607.20852v1\n" in out and "\nversion: v1\n" in out
    assert "[pdf](https://arxiv.org/pdf/2607.20852v1)" in out
    assert "# Code Monitor Red Teaming for Public-Test-Passing Code" in out
    assert "**Authors:** Junchi Liao, Jiawen Deng" in out
    assert "Visible tests are a common gate for LLM-generated code, but passing them" in out
    assert tavily.await_count == 0


@pytest.mark.asyncio
async def test_arxiv_abs_recovers_from_one_406(monkeypatch):
    from grok_search import verify
    monkeypatch.setattr(verify, "ARXIV_RETRY_DELAY_S", 0)
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(406, text="Not Acceptable") if calls["n"] == 1 else httpx.Response(200, text=ATOM)

    real = httpx.AsyncClient
    monkeypatch.setattr(fetching.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    tavily = AsyncMock(return_value="skeleton")
    monkeypatch.setattr(fetching, "_call_tavily_extract", tavily)
    out = await fetching.fetch_page("https://arxiv.org/abs/2607.20852")
    assert out.startswith("[web_fetch] source=arxiv-api") and calls["n"] == 2 and tavily.await_count == 0


@pytest.mark.asyncio
async def test_arxiv_api_failure_falls_back_to_extractors(monkeypatch):
    from grok_search import verify
    monkeypatch.setattr(verify, "ARXIV_RETRY_DELAY_S", 0)

    async def handler(request):
        return httpx.Response(503, text="down")

    real = httpx.AsyncClient
    monkeypatch.setattr(fetching.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value="p" * 3000))
    out = await fetching.fetch_page("https://arxiv.org/abs/2607.20852")
    assert out.startswith("[web_fetch] source=tavily")


def header_fields(output: str) -> dict:
    header = output.split("\n", 1)[0]  # later header lines explain failed backends or warnings
    return dict(part.split("=", 1) for part in header.split()[1:])


@pytest.mark.asyncio
async def test_paging_reads_one_snapshot_without_refetching(monkeypatch):
    text = "".join(str(i % 10) for i in range(10000))
    tavily = AsyncMock(return_value=text)
    monkeypatch.setattr(fetching, "_call_tavily_extract", tavily)
    monkeypatch.setattr(fetching, "_call_firecrawl_scrape", AsyncMock(return_value=None))
    first = await fetching.fetch_page("https://example.test/page", max_chars=4000)
    assert first.startswith("[web_fetch] source=tavily total_chars=10000 offset=0 returned=4000 truncated=true next_offset=4000 snapshot_id=")
    sid = header_fields(first)["snapshot_id"]
    tavily.return_value = "the page changed " * 1000  # a refetch would now return different text
    second = await fetching.fetch_page("https://example.test/page", max_chars=4000, offset=4000, snapshot_id=sid)
    header, body = second.split("\n\n", 1)
    assert header.startswith("[web_fetch] source=tavily total_chars=10000 offset=4000 returned=4000 truncated=true next_offset=8000")
    assert "cached=true" in header and body == text[4000:8000]
    tail = await fetching.fetch_page("https://example.test/page", max_chars=4000, offset=8000, snapshot_id=sid)
    assert "returned=2000 truncated=false" in tail.split("\n\n", 1)[0] and tail.endswith(text[8000:])
    assert tavily.await_count == 1


@pytest.mark.asyncio
async def test_page_that_fits_needs_no_snapshot(monkeypatch):
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value="x" * 3000))
    out = await fetching.fetch_page("https://example.test/page", max_chars=5000)
    assert "snapshot_id" not in out.split("\n\n", 1)[0]


@pytest.mark.asyncio
async def test_offset_without_snapshot_is_refused(monkeypatch):
    tavily = AsyncMock(return_value="x" * 5000)
    monkeypatch.setattr(fetching, "_call_tavily_extract", tavily)
    out = await fetching.fetch_page("https://example.test/page", max_chars=1000, offset=1000)
    assert out.startswith("[web_fetch] error=snapshot_required") and tavily.await_count == 0


@pytest.mark.asyncio
async def test_expired_snapshot_requires_a_restart(monkeypatch):
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value="x" * 5000))
    clock = {"t": 0.0}
    monkeypatch.setattr(fetching.SNAPSHOTS, "_clock", lambda: clock["t"])
    sid = header_fields(await fetching.fetch_page("https://example.test/page", max_chars=1000))["snapshot_id"]
    clock["t"] += 600  # reading a page keeps the snapshot alive
    assert (await fetching.fetch_page("https://example.test/page", max_chars=1000, offset=1000, snapshot_id=sid)).startswith("[web_fetch] source=tavily")
    clock["t"] += fetching.config.fetch_snapshot_ttl_s + 1
    out = await fetching.fetch_page("https://example.test/page", max_chars=1000, offset=2000, snapshot_id=sid)
    assert out.startswith(f"[web_fetch] error=snapshot_expired snapshot_id={sid}")


@pytest.mark.asyncio
async def test_snapshot_is_bound_to_its_url(monkeypatch):
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value="x" * 5000))
    sid = header_fields(await fetching.fetch_page("https://example.test/a", max_chars=1000))["snapshot_id"]
    out = await fetching.fetch_page("https://example.test/b", max_chars=1000, offset=1000, snapshot_id=sid)
    assert out.startswith("[web_fetch] error=snapshot_mismatch")


def mock_backends(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(fetching.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setenv("TAVILY_API_URL", "https://tavily.example.test")
    monkeypatch.setenv("FIRECRAWL_API_URL", "https://firecrawl.example.test/v2")
    monkeypatch.delenv("TAVILY_ENABLED", raising=False)


@pytest.mark.asyncio
async def test_failures_are_reported_per_backend_and_redacted(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret-123456")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-secret-654321")

    async def handler(request):
        if request.url.host == "tavily.example.test":
            return httpx.Response(401, json={"detail": {"error": "Unauthorized: invalid API key tvly-secret-123456"}})
        raise httpx.ReadTimeout("timed out", request=request)

    mock_backends(monkeypatch, handler)
    out = await fetching.fetch_page("https://example.test/page")
    assert out.startswith("[web_fetch] error=fetch_failed attempts=tavily:auth,firecrawl:timeout")
    assert "- tavily: auth (HTTP 401)" in out and "- firecrawl: timeout" in out
    assert "secret" not in out


@pytest.mark.asyncio
async def test_tavily_quota_then_firecrawl_success_is_visible(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-k")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-k")

    async def handler(request):
        if request.url.host == "tavily.example.test":
            return httpx.Response(432, json={"detail": {"error": "This request exceeds your plan's set usage limit."}})
        return httpx.Response(200, json={"success": True, "data": {"markdown": "page " * 600}})

    mock_backends(monkeypatch, handler)
    out = await fetching.fetch_page("https://example.test/page")
    assert out.startswith("[web_fetch] source=firecrawl total_chars=3000")
    assert "attempts=tavily:quota,firecrawl:ok" in out.split("\n\n", 1)[0]


@pytest.mark.asyncio
async def test_tavily_enabled_false_is_honoured(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-k")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-k")
    hosts = []

    async def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(200, json={"success": True, "data": {"markdown": "page " * 600}})

    mock_backends(monkeypatch, handler)
    monkeypatch.setenv("TAVILY_ENABLED", "false")
    out = await fetching.fetch_page("https://example.test/page")
    assert out.startswith("[web_fetch] source=firecrawl") and hosts == ["firecrawl.example.test"]
    assert "attempts=" not in out.split("\n\n", 1)[0]


@pytest.mark.asyncio
async def test_nothing_configured(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    out = await fetching.fetch_page("https://example.test/page")
    assert out.startswith("[web_fetch] error=not_configured")
    assert "- tavily: not_configured" in out and "- firecrawl: not_configured" in out


@pytest.mark.asyncio
async def test_mcp_web_fetch_passes_paging_arguments(monkeypatch):
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value="k" * 500))
    monkeypatch.setattr(fetching, "_call_firecrawl_scrape", AsyncMock(return_value=None))
    async with Client(server.mcp) as client:
        first = await client.call_tool("web_fetch", {"url": "https://example.test/p", "max_chars": 50})
        sid = header_fields(next(block.text for block in first.content if block.type == "text"))["snapshot_id"]
        result = await client.call_tool("web_fetch", {"url": "https://example.test/p", "max_chars": 100, "offset": 50, "snapshot_id": sid})
    text = next(block.text for block in result.content if block.type == "text")
    assert text.startswith("[web_fetch] source=tavily total_chars=500 offset=50 returned=100 truncated=true next_offset=150")


@pytest.mark.asyncio
@pytest.mark.parametrize("url, reason", [
    ("not a url", "missing_scheme"),
    ("example.com/page", "missing_scheme"),
    ("ftp://example.com/file", "unsupported_scheme"),
    ("javascript:alert(1)", "unsupported_scheme"),
    ("file:///etc/passwd", "unsupported_scheme"),
    ("http:///path", "missing_host"),
    ("http://[::1", "malformed"),
    ("http://example.com:99999/", "malformed"),
    ("http://localhost:8080/", "local_address"),
    ("http://app.localhost./", "local_address"),
    ("http://127.0.0.1:5173/admin", "local_address"),
    ("http://2130706433/", "local_address"),
    ("http://[::1]:8000/", "local_address"),
    ("http://[::ffff:10.0.0.1]/", "local_address"),
    ("http://169.254.169.254/latest/meta-data/", "local_address"),
    ("http://192.168.1.1/", "local_address"),
    ("http://100.64.0.1/", "local_address"),
])
async def test_unfetchable_urls_are_refused_without_backend_calls(monkeypatch, url, reason):
    tavily = AsyncMock(return_value="x" * 5000)
    firecrawl = AsyncMock(return_value="y" * 5000)
    monkeypatch.setattr(fetching, "_call_tavily_extract", tavily)
    monkeypatch.setattr(fetching, "_call_firecrawl_scrape", firecrawl)
    out = await fetching.fetch_page(url)
    assert out.startswith(f"[web_fetch] error=invalid_url reason={reason}\n\n无效的 URL: "), out
    assert tavily.await_count == 0 and firecrawl.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://example.com/page",
    "HTTP://Example.COM",
    "http://8.8.8.8/",
    "http://[2606:4700:4700::1111]/",
    "https://localhost.example.com/",
    "http://10.example.com/",  # host names are never resolved for this check
])
async def test_public_urls_and_host_names_reach_the_backends(monkeypatch, url):
    tavily = AsyncMock(return_value="x" * 5000)
    monkeypatch.setattr(fetching, "_call_tavily_extract", tavily)
    out = await fetching.fetch_page(url)
    assert out.startswith("[web_fetch] source=tavily") and tavily.await_count == 1


@pytest.mark.asyncio
async def test_mcp_web_fetch_reports_invalid_url_as_a_normal_result(monkeypatch):
    tavily = AsyncMock(return_value="x" * 5000)
    monkeypatch.setattr(fetching, "_call_tavily_extract", tavily)
    async with Client(server.mcp) as client:
        result = await client.call_tool("web_fetch", {"url": "file:///etc/passwd"}, raise_on_error=False)
    text = next(block.text for block in result.content if block.type == "text")
    assert not result.is_error and text.startswith("[web_fetch] error=invalid_url reason=unsupported_scheme")
    assert tavily.await_count == 0


# ------------------------------------------------ Firecrawl page status, redirects and interstitials
ZHIHU_WALL = (
    "![](https://www.zhihu.com/account/unhuman?type=U4E3Z1&need_login=true&session=0&next=%2Fsignin)\n\n"
    "[知乎](https://www.zhihu.com/)\n\n请您登录后查看更多专业优质内容。"
)
ZHIHU_META = {  # recorded live from Firecrawl for https://www.zhihu.com/question/451397804
    "statusCode": 200,
    "url": "https://www.zhihu.com/account/unhuman?type=U4E3Z1&need_login=true&session=0&next=%2Fsignin",
    "sourceURL": "https://www.zhihu.com/question/451397804",
    "title": "安全验证 - 知乎",
}


def scripted_backends(monkeypatch, tavily, scrape):
    """tavily / scrape: (status, json) answered by the mocked Tavily extract and Firecrawl scrape."""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-k")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-k")
    calls = []

    async def handler(request):
        calls.append(request.url.host)
        status, body = tavily if request.url.host == "tavily.example.test" else scrape
        return httpx.Response(status, json=body)

    mock_backends(monkeypatch, handler)
    return calls


TAVILY_FAILED = (200, {"results": [], "failed_results": [{"url": "u", "error": "Failed to fetch url"}]})


@pytest.mark.asyncio
async def test_firecrawl_error_page_is_a_failure_with_its_status(monkeypatch):
    page = "# Page not found\n\n" + "navigation footer " * 60
    calls = scripted_backends(monkeypatch, TAVILY_FAILED, (200, {"success": True, "data": {
        "markdown": page, "metadata": {"statusCode": 404, "error": "Not Found"}}}))
    out = await fetching.fetch_page("https://docs.example.test/3/library/missing.html")
    assert out.startswith("[web_fetch] error=fetch_failed attempts=tavily:extract_failed,firecrawl:extract_failed page_status=404\n")
    assert "- firecrawl: extract_failed (HTTP 404) — target page answered HTTP 404 (Not Found)" in out
    assert "Page not found" not in out
    assert calls.count("firecrawl.example.test") == 1  # an error page is not retried like an empty one


@pytest.mark.asyncio
async def test_firecrawl_error_page_never_replaces_a_short_tavily_extract(monkeypatch):
    valid = "# Release notes\n\nVersion 2.3 fixes the parser. " + "Detail line. " * 80
    blocked = "# 403 Forbidden\n\nAccess denied. " + "Reference footer links. " * 120
    scripted_backends(monkeypatch, (200, {"results": [{"url": "u", "raw_content": valid}]}), (200, {"success": True, "data": {
        "markdown": blocked, "metadata": {"statusCode": 403, "error": "Forbidden"}}}))
    out = await fetching.fetch_page("https://example.test/release-notes")
    header, body = out.split("\n\n", 1)
    first, note, detail = header.split("\n")
    assert first.startswith(f"[web_fetch] source=tavily total_chars={len(valid)}")
    assert first.endswith(f"attempts=tavily:short({len(valid)}),firecrawl:extract_failed")
    assert note == fetching.TAVILY_CACHE_NOTE
    assert detail == "- firecrawl: extract_failed (HTTP 403) — target page answered HTTP 403 (Forbidden)"
    assert body == valid


@pytest.mark.parametrize("status, category", [(404, "extract_failed"), (410, "extract_failed"), (401, "extract_failed"),
                                              (429, "rate_limit"), (500, "upstream_error"), (503, "upstream_error")])
def test_page_status_categories(status, category):
    error = fetching.PageStatusError("firecrawl", status, "x")
    assert error.category == category and error.status == status and error.page_status == status


@pytest.mark.asyncio
async def test_zhihu_login_wall_is_flagged_not_presented_as_the_page(monkeypatch):
    scripted_backends(monkeypatch, TAVILY_FAILED, (200, {"success": True, "data": {"markdown": ZHIHU_WALL, "metadata": ZHIHU_META}}))
    out = await fetching.fetch_page("https://www.zhihu.com/question/451397804")
    first, second = out.split("\n", 2)[:2]
    assert first.startswith(f"[web_fetch] source=firecrawl total_chars={len(ZHIHU_WALL)}")
    assert (" attempts=tavily:extract_failed,firecrawl:interstitial warning=interstitial"
            f" final_url={ZHIHU_META['url']} title=\"安全验证 - 知乎\"") in first
    assert first.endswith('title="安全验证 - 知乎"')
    assert second.startswith("注意: ") and "登录或人机验证拦截页" in second and "unhuman" in second
    assert out.split("\n\n", 1)[1] == ZHIHU_WALL


CLOUDFLARE_CHALLENGE = (
    "## example.test\n\n## Verifying you are human. This may take a few seconds.\n\n"
    "example.test needs to review the security of your connection before proceeding.\n\n"
    "Ray ID: `8f0c2a1b3c4d5e6f`\n\nPerformance & security by [Cloudflare](https://www.cloudflare.com)"
)


@pytest.mark.asyncio
async def test_interstitial_never_replaces_a_short_tavily_extract(monkeypatch):
    # The challenge page used to be 3000 x's: a title-only match vetoed any non-redirected page, which
    # also threw away real articles titled "CAPTCHA - Wikipedia" (see the tests below).
    short = "Real but short answer text."
    scripted_backends(monkeypatch, (200, {"results": [{"url": "u", "raw_content": short}]}),
                      (200, {"success": True, "data": {"markdown": CLOUDFLARE_CHALLENGE, "metadata": {
                          "statusCode": 200, "url": "https://example.test/page", "title": "Just a moment..."}}}))
    out = await fetching.fetch_page("https://example.test/page")
    header, body = out.split("\n\n", 1)
    assert header.startswith("[web_fetch] source=tavily") and body == short
    assert header.split("\n") == [header.split("\n")[0], fetching.TAVILY_CACHE_NOTE]
    assert header.split("\n")[0].endswith(f"attempts=tavily:short({len(short)}),firecrawl:interstitial")
    assert "warning=" not in header


@pytest.mark.asyncio
async def test_redirect_surfaces_final_url_and_title(monkeypatch):
    scripted_backends(monkeypatch, TAVILY_FAILED, (200, {"success": True, "data": {"markdown": "landing " * 400, "metadata": {
        "statusCode": 200, "url": "https://example.test/landing", "title": "Landing page", "sourceURL": "https://sho.rt/abc"}}}))
    out = await fetching.fetch_page("https://sho.rt/abc")
    header = out.split("\n\n", 1)[0]
    first, detail = header.split("\n")
    assert first.endswith('attempts=tavily:extract_failed,firecrawl:ok final_url=https://example.test/landing title="Landing page"')
    assert "warning=" not in header and detail == "- tavily: extract_failed — Failed to fetch url"


@pytest.mark.asyncio
async def test_same_page_after_scheme_or_slash_change_is_not_a_redirect(monkeypatch):
    scripted_backends(monkeypatch, TAVILY_FAILED, (200, {"success": True, "data": {"markdown": "doc " * 800, "metadata": {
        "statusCode": 200, "url": "https://www.example.test/docs/", "title": "Docs"}}}))
    out = await fetching.fetch_page("http://example.test/docs")
    header = out.split("\n\n", 1)[0]
    assert "final_url=" not in header and "title=" not in header


@pytest.mark.asyncio
async def test_snapshot_pages_keep_the_interstitial_warning(monkeypatch):
    wall = ZHIHU_WALL + "\n" + "页脚" * 2000
    scripted_backends(monkeypatch, TAVILY_FAILED, (200, {"success": True, "data": {"markdown": wall, "metadata": ZHIHU_META}}))
    first = await fetching.fetch_page("https://www.zhihu.com/question/451397804", max_chars=1000)
    sid = re.search(r"snapshot_id=(\w+)", first).group(1)
    second = await fetching.fetch_page("https://www.zhihu.com/question/451397804", max_chars=1000, offset=1000, snapshot_id=sid)
    assert "cached=true cache=bypassed warning=interstitial final_url=" in second.split("\n", 1)[0]


@pytest.mark.parametrize("final_url, title, redirected, expected", [
    ("", "安全验证 - 知乎", False, True),
    ("", "百度安全验证", False, True),
    ("", "Just a moment...", False, True),
    ("", "Attention Required! | Cloudflare", False, True),
    ("", "如何实现短信验证码登录 - 知乎", False, False),
    ("", "Captcha solving with ML: a survey", False, False),
    ("https://github.com/login?return_to=%2Fx", "Sign in to GitHub · GitHub", True, True),
    ("https://passport.weibo.com/visitor/visitor?entry=miniblog", "Sina Visitor System", True, True),
    ("https://example.test/u/login.php?next=%2F", "Example", True, True),
    ("https://example.test/blog/how-to-login-faster", "How to login faster", True, False),
    ("https://example.test/blog/new-slug", "New slug", True, False),
    ("https://example.test/account/settings", "用户登录", True, True),
])
def test_interstitial_detection(final_url, title, redirected, expected):
    assert fetching._is_interstitial(final_url, title, redirected) is expected


ARTICLE = "# CAPTCHA\n\n" + "A CAPTCHA is a type of challenge-response test used in computing. " * 60


@pytest.mark.parametrize("url, title", [
    ("https://en.wikipedia.org/wiki/CAPTCHA", "CAPTCHA - Wikipedia"),
    ("https://zh.wikipedia.org/wiki/%E9%AA%8C%E8%AF%81%E7%A0%81", "验证码 - 维基百科，自由的百科全书"),
    ("https://pypi.org/project/captcha/", "captcha · PyPI"),
    ("https://example.test/songs/just-a-moment", "Just a Moment - Example Lyrics"),
])
def test_an_article_titled_like_a_challenge_is_not_an_interstitial(url, title):
    assert fetching._is_interstitial(url, title, False, ARTICLE) is False
    assert fetching._page_info(url, {"final_url": url, "title": title}, ARTICLE) == {}
    # The same title is still a wall when the page is a stub or the request was redirected to it.
    assert fetching._is_interstitial(url, title, False, "请完成下方验证后继续访问") is True
    assert fetching._is_interstitial("https://example.test/elsewhere", title, True, ARTICLE) is True


def test_a_long_challenge_page_is_recognised_by_its_wording():
    page = CLOUDFLARE_CHALLENGE + "\n\n" + "footer link " * 300
    assert len(page) > fetching._WALL_MAX_CHARS
    assert fetching._is_interstitial("https://example.test/page", "Just a moment...", False, page) is True


@pytest.mark.asyncio
async def test_full_article_titled_captcha_replaces_a_short_tavily_extract(monkeypatch):
    short = "CAPTCHA (short extract)"
    scripted_backends(monkeypatch, (200, {"results": [{"url": "u", "raw_content": short}]}),
                      (200, {"success": True, "data": {"markdown": ARTICLE, "metadata": {
                          "statusCode": 200, "url": "https://en.wikipedia.org/wiki/CAPTCHA", "title": "CAPTCHA - Wikipedia"}}}))
    out = await fetching.fetch_page("https://en.wikipedia.org/wiki/CAPTCHA")
    header, body = out.split("\n\n", 1)
    assert header.startswith(f"[web_fetch] source=firecrawl total_chars={len(ARTICLE)}")
    assert header.endswith(f"attempts=tavily:short({len(short)}),firecrawl:ok")
    assert "warning=" not in header and "注意" not in header
    assert body == ARTICLE


# ------------------------------------------------ arXiv: versions, cache, budget and cooldown
def mock_arxiv(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(fetching.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))


def atom_for(version: str, abstract: str) -> str:
    return ATOM.replace("2607.20852v1", f"2607.20852{version}").replace("but passing them does not certify", abstract)


@pytest.mark.asyncio
async def test_arxiv_versions_are_fetched_separately_and_labelled(monkeypatch):
    sent = []

    async def handler(request):
        idl = request.url.params["id_list"]
        sent.append(idl)
        return httpx.Response(200, text=atom_for("v1", "VERSION ONE") if idl.endswith("v1") else atom_for("v3", "LATEST"))

    mock_arxiv(monkeypatch, handler)
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value="skeleton"))
    v1 = await fetching.fetch_page("https://arxiv.org/abs/2607.20852V1/")
    latest = await fetching.fetch_page("https://arxiv.org/abs/2607.20852")
    assert sent == ["2607.20852v1", "2607.20852"]
    assert "VERSION ONE" in v1 and "version: v1\n" in v1 and "source: https://arxiv.org/abs/2607.20852v1\n" in v1
    assert "LATEST" in latest and "version: v3 (latest)\n" in latest and "source: https://arxiv.org/abs/2607.20852\n" in latest


@pytest.mark.asyncio
async def test_arxiv_answer_for_another_version_falls_back_to_extractors(monkeypatch):
    async def handler(request):
        return httpx.Response(200, text=atom_for("v7", "LATEST"))

    mock_arxiv(monkeypatch, handler)
    tavily = AsyncMock(return_value="p" * 3000)
    monkeypatch.setattr(fetching, "_call_tavily_extract", tavily)
    out = await fetching.fetch_page("https://arxiv.org/abs/2607.20852v2")
    assert out.startswith("[web_fetch] source=tavily") and "attempts=arxiv-api:parse_error" in out.split("\n\n", 1)[0]
    tavily.assert_awaited_once_with("https://arxiv.org/abs/2607.20852v2")


@pytest.mark.asyncio
async def test_arxiv_abs_is_cached_and_fills_the_verification_cache(monkeypatch):
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(200, text=ATOM)

    mock_arxiv(monkeypatch, handler)
    first = await fetching.fetch_page("https://arxiv.org/abs/2607.20852")
    again = await fetching.fetch_page("https://arxiv.org/abs/2607.20852")
    assert first == again and calls["n"] == 1
    assert verify._ARXIV_CACHE["2607.20852"][1] == {
        "id": "2607.20852", "title": "Code Monitor Red Teaming for Public-Test-Passing Code",
        "published": "2026-07-23", "authors": ["Junchi Liao", "Jiawen Deng"],
    }
    async with httpx.AsyncClient() as client:  # patched above: requests would reach handler
        await verify.lookup_arxiv(["2607.20852"], client)
    assert calls["n"] == 1  # verification of the fetched paper is served from the cache
    await fetching.fetch_page("https://arxiv.org/abs/2607.20852v1")
    assert calls["n"] == 2 and "2607.20852v1" not in verify._ARXIV_CACHE  # a version is its own page
    clock = {"t": fetching.time.monotonic() + verify.ARXIV_CACHE_TTL_S + 1}
    monkeypatch.setattr(fetching.time, "monotonic", lambda: clock["t"])
    await fetching.fetch_page("https://arxiv.org/abs/2607.20852")
    assert calls["n"] == 3  # expired after ARXIV_CACHE_TTL_S


@pytest.mark.asyncio
async def test_slow_arxiv_api_is_cut_off_by_the_budget_then_cooled_down(monkeypatch):
    import asyncio
    import time

    monkeypatch.setenv("GROK_FETCH_ARXIV_BUDGET_S", "1")
    calls = {"n": 0}

    async def handler(request):  # a throttled arXiv answers its 429s only after 10-45 s
        if request.url.host == "api.datacite.org":
            return httpx.Response(503, text="down")
        calls["n"] += 1
        await asyncio.sleep(30)
        return httpx.Response(429, text="Rate exceeded.")

    mock_arxiv(monkeypatch, handler)
    tavily = AsyncMock(return_value="p" * 3000)
    monkeypatch.setattr(fetching, "_call_tavily_extract", tavily)
    started = time.monotonic()
    out = await fetching.fetch_page("https://arxiv.org/abs/2607.20852")
    assert time.monotonic() - started < 3
    assert out.startswith("[web_fetch] source=tavily")
    assert out.split("\n", 1)[0].endswith("attempts=arxiv-api:timeout,datacite:upstream_error,tavily:ok")
    started = time.monotonic()
    out = await fetching.fetch_page("https://arxiv.org/abs/2607.20853")
    assert time.monotonic() - started < 0.5 and calls["n"] == 1  # skipped while cooling down
    assert out.split("\n", 1)[0].endswith("attempts=arxiv-api:cooldown,datacite:upstream_error,tavily:ok")


@pytest.mark.asyncio
async def test_cooldown_failure_message_and_cached_papers(monkeypatch):
    # verify.arxiv_get does not retry a 429: it starts the process-wide arXiv cooldown at once.
    responses = [httpx.Response(200, text=ATOM), httpx.Response(429, text="Rate exceeded.")]

    async def handler(request):
        return responses.pop(0)

    mock_arxiv(monkeypatch, handler)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    assert (await fetching.fetch_page("https://arxiv.org/abs/2607.20852")).startswith("[web_fetch] source=arxiv-api")
    failed = await fetching.fetch_page("https://arxiv.org/abs/2607.20853")  # 429 -> cooldown
    assert failed.startswith("[web_fetch] error=fetch_failed attempts=arxiv-api:rate_limit")
    skipped = await fetching.fetch_page("https://arxiv.org/abs/2607.20854")
    assert skipped.startswith("[web_fetch] error=fetch_failed attempts=arxiv-api:rate_limit")
    assert "- arxiv-api: rate_limit — skipped: the arXiv API failed recently (rate_limit); next try in" in skipped
    assert (await fetching.fetch_page("https://arxiv.org/abs/2607.20852")).startswith("[web_fetch] source=arxiv-api")
    assert responses == []


@pytest.mark.asyncio
async def test_non_transient_arxiv_failure_does_not_cool_down(monkeypatch):
    async def handler(request):
        return httpx.Response(400, text="bad id")

    mock_arxiv(monkeypatch, handler)
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value="p" * 3000))
    out = await fetching.fetch_page("https://arxiv.org/abs/2607.20852")
    assert "attempts=arxiv-api:bad_request" in out.split("\n\n", 1)[0]
    assert fetching._arxiv_cooldown["until"] == 0.0


# ------------------------------------------------ CJK-aware default page size
@pytest.mark.asyncio
async def test_cjk_page_defaults_to_the_smaller_page(monkeypatch):
    text = ("这是一个中文段落，用来测试分页。" * 3000)[:45000]
    tavily = AsyncMock(return_value=text)
    monkeypatch.setattr(fetching, "_call_tavily_extract", tavily)
    first = await fetching.fetch_page("https://example.test/zh")
    fields = header_fields(first)
    assert (fields["returned"], fields["truncated"], fields["next_offset"]) == ("20000", "true", "20000")
    second = await fetching.fetch_page("https://example.test/zh", offset=20000, snapshot_id=fields["snapshot_id"])
    assert header_fields(second)["returned"] == "20000" and second.split("\n\n", 1)[1] == text[20000:40000]
    explicit = await fetching.fetch_page("https://example.test/zh", max_chars=35000)
    assert header_fields(explicit)["returned"] == "35000"  # an explicit max_chars is honoured


@pytest.mark.asyncio
async def test_cjk_share_threshold_and_mixed_pages(monkeypatch):
    latin = "plain english text with markdown [link](https://example.test) "
    mostly_latin = (latin * 3 + "中文内容" * 10) * 400  # ~18% CJK
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value=mostly_latin))
    out = await fetching.fetch_page("https://example.test/mixed")
    assert header_fields(out)["returned"] == "40000"
    later_cjk = "x" * 40000 + "汉字" * 20000  # English first page, CJK afterwards
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value=later_cjk))
    first = await fetching.fetch_page("https://example.test/later")
    assert header_fields(first)["returned"] == "40000"
    second = await fetching.fetch_page("https://example.test/later", offset=40000, snapshot_id=header_fields(first)["snapshot_id"])
    assert header_fields(second)["returned"] == "20000"


@pytest.mark.asyncio
async def test_cjk_default_never_exceeds_the_configured_default(monkeypatch):
    monkeypatch.setenv("GROK_FETCH_MAX_CHARS", "8000")
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(return_value="日本語のテキスト" * 3000))
    assert header_fields(await fetching.fetch_page("https://example.test/ja"))["returned"] == "8000"
    monkeypatch.setenv("GROK_FETCH_MAX_CHARS", "40000")
    monkeypatch.setenv("GROK_FETCH_MAX_CHARS_CJK", "12000")
    assert header_fields(await fetching.fetch_page("https://example.test/ja"))["returned"] == "12000"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["404", 404.0])
async def test_page_status_may_arrive_as_text_or_float(monkeypatch, status):
    scripted_backends(monkeypatch, TAVILY_FAILED, (200, {"success": True, "data": {"markdown": "gone " * 500, "metadata": {"statusCode": status}}}))
    assert "page_status=404" in (await fetching.fetch_page("https://example.test/gone")).split("\n", 1)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [{}, {"statusCode": None}, {"statusCode": "n/a"}, {"statusCode": True}, {"statusCode": 200, "title": ["", "Doc"]}])
async def test_missing_or_odd_metadata_is_ignored(monkeypatch, metadata):
    scripted_backends(monkeypatch, TAVILY_FAILED, (200, {"success": True, "data": {"markdown": "doc " * 800, "metadata": metadata}}))
    out = await fetching.fetch_page("https://example.test/doc")
    assert out.split("\n", 1)[0].endswith("attempts=tavily:extract_failed,firecrawl:ok")
