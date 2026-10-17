"""web_fetch for text resources (verbatim direct download), freshness, extractor fidelity, bot checks
and the arXiv DataCite fallback (v1.12.1 field report: claims 1, NEW-stale, 4, 8, NEW-403)."""
import gzip
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from grok_search import direct, fetching, verify
from grok_search.config import config

PUBLIC_IP = "93.184.216.34"
RAW = "https://raw.githubusercontent.com/python/cpython/main/Include/Python.h"
# A blob page is read through github.com's own /raw/ URL, which redirects to RAW (or, for a Git LFS
# file, to media.githubusercontent.com).
BLOB_RAW = "https://github.com/python/cpython/raw/main/Include/Python.h"

PYTHON_H = """// Entry point of the Python C API.
// C extensions should only #include <Python.h>, and not include directly
// the other Python header files included by <Python.h>.

#ifndef Py_PYTHON_H
#define Py_PYTHON_H

// Include standard header files
#include <assert.h>               // assert()
#include <inttypes.h>             // uintptr_t
#include <limits.h>               // INT_MAX
#ifdef HAVE_SYS_TYPES_H
#  include <sys/types.h>          // ssize_t
#endif

static inline int
_Py_check(PyObject *op)
{
    if (op == NULL) {
        return -1;
    }
    return 0;
}

#endif /* !Py_PYTHON_H */
"""
# What Tavily made of Python.h (recorded live): tags removed, "_" escaped, blank lines and indentation gone.
PYTHON_H_TAVILY = """// Entry point of the Python C API.
// C extensions should only #include , and not include directly
// the other Python header files included by .
#ifndef Py\\_PYTHON\\_H
#define Py\\_PYTHON\\_H
// Include standard header files
#include  // assert()
#include  // uintptr\\_t
#include  // INT\\_MAX
#ifdef HAVE\\_SYS\\_TYPES\\_H
# include  // ssize\\_t
#endif
"""
LOGGER_PY = "\n".join(
    ["import logging", "", "class DailyFileHandler(logging.FileHandler):"]
    + [f"    def method_{i}(self, record_{i}):\n        return record_{i}" for i in range(20)]
)
LOGGER_PY_TAVILY = "\n".join(line.strip().replace("_", "\\_") for line in LOGGER_PY.split("\n") if line.strip())
PYPROJECT = '[project]\nname = "grok-search"\nversion = "1.12.0"\ndependencies = [\n    "fastmcp>=2.11.0",\n]\n'


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    verify._DOH_CACHE.clear()
    fetching._ARXIV_ABS_CACHE.clear()
    fetching._arxiv_cooldown.update(until=0.0, category="")
    verify._ARXIV_CACHE.clear()
    verify._arxiv_gate.update(loop=None, lock=None, last_start=0.0)
    monkeypatch.setattr(verify, "ARXIV_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(verify, "ARXIV_RETRY_DELAY_S", 0.0)
    for name in ("GROK_FETCH_DIRECT", "GROK_FETCH_MAX_AGE_S", "GROK_VERIFY_FAKE_IP", "TAVILY_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    # The Tavily-first page order (GROK_FETCH_PAGE_FIRST=tavily); test_fetch_live.py covers the default.
    monkeypatch.setenv("GROK_FETCH_PAGE_FIRST", "tavily")
    monkeypatch.setenv("GROK_FETCH_STRIP_IMAGES", "false")  # page texts are compared as the backends sent them


@pytest.fixture
def dns(monkeypatch):
    table: dict = {}

    async def resolve(host, port):
        value = table.get(host, [PUBLIC_IP])
        if isinstance(value, Exception):
            raise value
        return list(value)

    monkeypatch.setattr(verify, "resolve_host", resolve)
    return table


class PeerStream:
    """The network_stream extension of a real connection: direct.py checks the address it went to."""

    def __init__(self, address):
        self.address = address

    def get_extra_info(self, name):
        return (self.address, 443) if name == "server_addr" and self.address else None


def serve(monkeypatch, handler, peer=PUBLIC_IP):
    """Route every httpx client (direct download, DoH, Tavily, Firecrawl, arXiv, DataCite) to handler.

    peer: the address each connection "went to" (a callable gets the request), as httpcore reports it."""
    real = httpx.AsyncClient

    async def connected(request):
        response = await handler(request)
        address = peer(request) if callable(peer) else peer
        response.extensions.setdefault("network_stream", PeerStream(address))
        return response

    monkeypatch.setattr(fetching.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(connected), **kwargs))


def extractors(monkeypatch, tavily=None, firecrawl=None):
    tavily_mock = AsyncMock(return_value=tavily)
    firecrawl_mock = AsyncMock(return_value=firecrawl)
    monkeypatch.setattr(fetching, "_call_tavily_extract", tavily_mock)
    monkeypatch.setattr(fetching, "_call_firecrawl_scrape", firecrawl_mock)
    return tavily_mock, firecrawl_mock


def split(out: str):
    header, body = out.split("\n\n", 1)
    return header.split("\n"), body


def text_file(body: str, content_type="text/plain; charset=utf-8", hits=None):
    async def handler(request):
        if hits is not None:
            hits.append(request)
        return httpx.Response(200, content=body.encode("utf-8"), headers={"content-type": content_type})
    return handler


async def chunks(data: bytes, size: int = 1024):
    """A streamed body, read through direct._read_body's own (bounded) decompression."""
    for start in range(0, len(data), size):
        yield data[start:start + size]


@pytest.mark.asyncio
async def test_corrupt_or_unknown_encodings_fail_cleanly(monkeypatch, dns):
    bodies = iter([
        (chunks(b"\x1f\x8b\x08\x00garbage-not-gzip" * 10), "gzip"),
        (chunks(gzip.compress(PYTHON_H.encode())[:-30]), "gzip"),
        (chunks(b"abc"), "br"),
    ])

    async def handler(request):
        body, coding = next(bodies)
        return httpx.Response(200, content=body, headers={"content-type": "text/plain", "content-encoding": coding})

    serve(monkeypatch, handler)
    extractors(monkeypatch)
    for detail in ("corrupt gzip body", "truncated gzip body", "unsupported content-encoding 'br'"):
        out = await fetching.fetch_page("https://example.com/x.py")
        assert "- direct: parse_error — " + detail in out


# ------------------------------------------------------------ which URLs are downloaded directly
@pytest.mark.parametrize("url, target", [
    (RAW, RAW),
    ("https://github.com/python/cpython/blob/main/Include/Python.h", BLOB_RAW),
    ("https://github.com/python/cpython/blob/main/Include/Python.h?plain=1#L10", BLOB_RAW),
    ("https://github.com/o/r/raw/refs/heads/main/src/x.py", "https://github.com/o/r/raw/refs/heads/main/src/x.py"),
    # Commit and pull request patches and release assets are text files on the forges too.
    ("https://github.com/Cody-Xue/GrokSearch/commit/2ba81d01e01802f9c6cc6cbedfd4d4b0fbcb241f.patch",
     "https://github.com/Cody-Xue/GrokSearch/commit/2ba81d01e01802f9c6cc6cbedfd4d4b0fbcb241f.patch"),
    ("https://github.com/python/cpython/pull/100000.diff", "https://github.com/python/cpython/pull/100000.diff"),
    ("https://github.com/cli/cli/releases/download/v2.40.0/gh_2.40.0_checksums.txt",
     "https://github.com/cli/cli/releases/download/v2.40.0/gh_2.40.0_checksums.txt"),
    ("https://gitlab.com/gitlab-org/gitlab/-/commit/abc123.patch", "https://gitlab.com/gitlab-org/gitlab/-/commit/abc123.patch"),
    ("https://bitbucket.org/o/r/commits/abc123.diff", "https://bitbucket.org/o/r/commits/abc123.diff"),
    ("https://gist.githubusercontent.com/u/0123abcd/raw/f00/snippet.py", "https://gist.githubusercontent.com/u/0123abcd/raw/f00/snippet.py"),
    ("https://gitlab.com/group/sub/proj/-/blob/main/README.md", "https://gitlab.com/group/sub/proj/-/raw/main/README.md"),
    ("https://gitlab.gnome.org/GNOME/glib/-/raw/main/meson.build", "https://gitlab.gnome.org/GNOME/glib/-/raw/main/meson.build"),
    ("https://codeberg.org/o/r/src/branch/main/a.toml", "https://codeberg.org/o/r/raw/branch/main/a.toml"),
    ("https://bitbucket.org/o/r/src/main/setup.py", "https://bitbucket.org/o/r/raw/main/setup.py"),
    ("https://example.com/data/config.yaml?x=1", "https://example.com/data/config.yaml?x=1"),
    ("https://pastebin.com/raw/AbCd1234", "https://pastebin.com/raw/AbCd1234"),
    ("https://example.com/Makefile", "https://example.com/Makefile"),
])
def test_text_resources_are_downloaded_directly(url, target):
    assert direct.direct_target(url) == target


@pytest.mark.parametrize("url", [
    "https://github.com/python/cpython",
    "https://github.com/o/r/tree/main/docs",
    "https://github.com/videojs/video.js",  # repository pages named like files
    "https://github.com/mrdoob/three.js/",
    "https://gitlab.com/group/sub/project.js",
    "https://codeberg.org/o/r.py",
    "https://example.com/page",
    "https://example.com/index.php",
    "https://docs.python.org/3/library/re.html",
    "https://example.com/archive/v1.2",
    "https://arxiv.org/pdf/1706.03762",
])
def test_web_pages_are_not_downloaded_directly(url):
    assert direct.direct_target(url) is None


@pytest.mark.parametrize("content_type, accepted", [
    ("text/plain; charset=utf-8", True), ("text/x-python", True), ("text/markdown", True),
    ("application/json", True), ("application/vnd.api+json", True), ("application/yaml", True),
    ("application/toml", True), ("application/javascript", True), ("application/atom+xml", True),
    ("text/html; charset=utf-8", False), ("application/xhtml+xml", False), ("application/octet-stream", False),
    ("image/png", False), ("", False),
])
def test_accepted_content_types(content_type, accepted):
    assert direct.accepted_type(content_type) is accepted


# ------------------------------------------------------------ verbatim result
@pytest.mark.asyncio
async def test_source_code_comes_back_byte_for_byte_without_paid_calls(monkeypatch, dns):
    hits = []
    serve(monkeypatch, text_file(PYTHON_H, hits=hits))
    tavily, firecrawl = extractors(monkeypatch, tavily=PYTHON_H_TAVILY, firecrawl=PYTHON_H)
    out = await fetching.fetch_page(RAW)
    lines, body = split(out)
    assert body == PYTHON_H
    assert lines == [f"[web_fetch] source=direct total_chars={len(PYTHON_H)} offset=0 returned={len(PYTHON_H)} "
                     "truncated=false content_type=text/plain;charset=utf-8 status=200"]
    assert tavily.await_count == 0 and firecrawl.await_count == 0
    request = hits[0]
    assert str(request.url) == RAW and "authorization" not in request.headers
    assert request.headers["user-agent"].startswith("grok-search-fetch/")


@pytest.mark.asyncio
async def test_damaged_blob_fallback_names_the_raw_url(monkeypatch, dns):
    dns["github.com"] = ["10.1.2.3"]
    tavily, firecrawl = extractors(monkeypatch, tavily=PYTHON_H_TAVILY, firecrawl=None)
    out = await fetching.fetch_page("https://github.com/python/cpython/blob/main/Include/Python.h")
    first = out.split("\n", 1)[0]
    assert first.endswith(f"degraded=escaped_underscores,include_target_stripped final_url={BLOB_RAW}")
    assert firecrawl.await_args.args[0] == BLOB_RAW and tavily.await_args.args[0] == BLOB_RAW  # never the HTML viewer page


def github(files: dict, hits: list, lfs: dict | None = None):
    """github.com's /raw/ redirects to raw.githubusercontent.com, or for an LFS file to media.githubusercontent.com."""
    async def handler(request):
        hits.append(str(request.url))
        host, path = request.url.host, request.url.path
        if host == "github.com" and "/raw/" in path:
            owner, repo, rest = path.lstrip("/").split("/", 2)
            rest = rest.removeprefix("raw/")
            media = (lfs or {}).get(f"{owner}/{repo}/{rest}")
            target = f"https://media.githubusercontent.com/media/{owner}/{repo}/{rest}" if media else \
                f"https://raw.githubusercontent.com/{owner}/{repo}/{rest}"
            return httpx.Response(302, headers={"location": target})
        if host == "media.githubusercontent.com":
            return httpx.Response(200, text=(lfs or {})[path.removeprefix("/media/")], headers={"content-type": "text/plain; charset=utf-8"})
        body = files.get(path.lstrip("/"))
        if body is None:
            return httpx.Response(404, text="404: Not Found", headers={"content-type": "text/plain"})
        return httpx.Response(200, text=body, headers={"content-type": "text/plain; charset=utf-8"})
    return handler


@pytest.mark.asyncio
async def test_github_blob_page_is_read_from_the_raw_host(monkeypatch, dns):
    hits = []
    serve(monkeypatch, github({"python/cpython/main/Include/Python.h": PYTHON_H}, hits))
    extractors(monkeypatch)
    out = await fetching.fetch_page("https://github.com/python/cpython/blob/main/Include/Python.h?plain=1")
    lines, body = split(out)
    assert body == PYTHON_H and hits == [BLOB_RAW, RAW]
    assert lines[0].endswith(f"status=200 final_url={RAW}")


LFS_CSV = "smiles,value\n" + "".join(f"C{i}O,{i}.5\n" for i in range(50))
LFS_POINTER = ("version https://git-lfs.github.com/spec/v1\n"
               "oid sha256:2a91afe2d3c4b5a6978877665544332211009988776655443322110099887766\nsize 19747\n")


@pytest.mark.asyncio
async def test_git_lfs_files_come_back_with_their_content_not_the_pointer(monkeypatch, dns):
    """raw.githubusercontent.com serves only the LFS pointer; github.com's /raw/ redirects to the content."""
    hits = []
    files = {"o/r/master/data/Xc.csv": LFS_POINTER}
    serve(monkeypatch, github(files, hits, lfs={"o/r/master/data/Xc.csv": LFS_CSV}))
    tavily, firecrawl = extractors(monkeypatch)
    for url in ("https://github.com/o/r/blob/master/data/Xc.csv", "https://github.com/o/r/raw/master/data/Xc.csv",
                "https://raw.githubusercontent.com/o/r/master/data/Xc.csv"):
        hits.clear()
        lines, body = split(await fetching.fetch_page(url))
        assert body == LFS_CSV, url
        assert lines[0].startswith("[web_fetch] source=direct") and "final_url=https://media.githubusercontent.com/media/o/r/" in lines[0]
    # The pointer was refetched once through github.com.
    assert hits == ["https://raw.githubusercontent.com/o/r/master/data/Xc.csv", "https://github.com/o/r/raw/master/data/Xc.csv",
                    "https://media.githubusercontent.com/media/o/r/master/data/Xc.csv"]
    assert tavily.await_count == firecrawl.await_count == 0


@pytest.mark.asyncio
async def test_a_small_file_is_fetched_once(monkeypatch, dns):
    serve(monkeypatch, text_file(PYPROJECT, "text/plain; charset=utf-8"))
    tavily, firecrawl = extractors(monkeypatch, tavily="stale " * 100, firecrawl="stale " * 200)
    out = await fetching.fetch_page("https://raw.githubusercontent.com/Cody-Xue/GrokSearch/main/pyproject.toml")
    lines, body = split(out)
    assert body == PYPROJECT and "attempts=" not in lines[0] and "short" not in out.split("\n\n", 1)[0]
    assert tavily.await_count == 0 and firecrawl.await_count == 0


@pytest.mark.asyncio
async def test_an_empty_file_is_a_result(monkeypatch, dns):
    serve(monkeypatch, text_file(""))
    extractors(monkeypatch)
    out = await fetching.fetch_page("https://raw.githubusercontent.com/o/r/main/pkg/__init__.py")
    assert out.startswith("[web_fetch] source=direct total_chars=0 ") and out.endswith("\n\n")


@pytest.mark.asyncio
async def test_declared_charset_is_used(monkeypatch, dns):
    async def handler(request):
        return httpx.Response(200, content="café = 1\n".encode("latin-1"), headers={"content-type": "text/plain; charset=ISO-8859-1"})

    serve(monkeypatch, handler)
    extractors(monkeypatch)
    out = await fetching.fetch_page("https://example.com/legacy.txt")
    assert split(out)[1] == "café = 1\n"


@pytest.mark.asyncio
async def test_gzip_body_is_inflated(monkeypatch, dns):
    async def handler(request):
        assert request.headers["accept-encoding"] == "gzip, deflate"
        return httpx.Response(200, content=chunks(gzip.compress(PYTHON_H.encode())), headers={
            "content-type": "text/plain; charset=utf-8", "content-encoding": "gzip"})

    serve(monkeypatch, handler)
    extractors(monkeypatch)
    assert split(await fetching.fetch_page(RAW))[1] == PYTHON_H


@pytest.mark.asyncio
async def test_download_is_capped_after_decompression(monkeypatch, dns):
    monkeypatch.setattr(direct, "DIRECT_MAX_BYTES", 4096)
    line = "x = 1  # " + "a" * 60 + "\n"
    bomb = gzip.compress((line * 200_000).encode())  # ~14 MB inflated, ~40 KB on the wire

    async def handler(request):
        return httpx.Response(200, content=chunks(bomb), headers={"content-type": "text/plain", "content-encoding": "gzip"})

    serve(monkeypatch, handler)
    extractors(monkeypatch)
    out = await fetching.fetch_page("https://example.com/huge.py")
    lines, body = split(out)
    assert lines[0].startswith("[web_fetch] source=direct") and "warning=size_limit" in lines[0]
    assert lines[1].startswith("注意: 文件超过")
    assert len(body.encode()) <= 4096 and body.endswith("\n") and body == line * (len(body) // len(line))


# ------------------------------------------------------------ the destination guard
@pytest.mark.asyncio
async def test_private_destination_is_never_contacted(monkeypatch, dns):
    dns["raw.githubusercontent.com"] = ["10.0.0.5"]
    hits = []
    serve(monkeypatch, text_file(PYTHON_H, hits=hits))
    tavily, firecrawl = extractors(monkeypatch, tavily=PYTHON_H_TAVILY, firecrawl=PYTHON_H)
    out = await fetching.fetch_page(RAW)
    lines, body = split(out)
    assert hits == [] and body == PYTHON_H
    assert lines[0].startswith("[web_fetch] source=firecrawl") and lines[0].endswith("attempts=direct:blocked,firecrawl:ok")
    assert lines[1].startswith("- direct: blocked — raw.githubusercontent.com resolves to 10.0.0.5")
    firecrawl.assert_awaited_once()
    assert firecrawl.await_args.args[0] == RAW and tavily.await_count == 0  # Firecrawl first for code


@pytest.mark.asyncio
async def test_every_redirect_hop_is_guarded(monkeypatch, dns):
    dns["internal.example.test"] = ["192.168.1.10"]
    hits = []

    async def handler(request):
        hits.append(request.url.host)
        return httpx.Response(302, headers={"location": "http://internal.example.test/secret.txt"})

    serve(monkeypatch, handler)
    extractors(monkeypatch, firecrawl=None, tavily=None)
    out = await fetching.fetch_page("https://example.com/notes.txt")
    assert hits == ["example.com"]  # the redirect target was refused before any request
    assert "- direct: blocked — internal.example.test resolves to 192.168.1.10" in out


@pytest.mark.asyncio
async def test_fake_ip_answers_follow_grok_verify_fake_ip(monkeypatch, dns):
    dns["raw.githubusercontent.com"] = ["198.18.0.15"]
    hits = []

    async def handler(request):
        hits.append(request.url.host)
        if request.url.host == "cloudflare-dns.com":
            kind = request.url.params["type"]
            answer = [{"type": 1, "TTL": 60, "data": "185.199.108.133"}] if kind == "A" else []
            return httpx.Response(200, json={"Status": 0, "Answer": answer})
        return httpx.Response(200, text=PYTHON_H, headers={"content-type": "text/plain; charset=utf-8"})

    serve(monkeypatch, handler)
    extractors(monkeypatch, firecrawl=PYTHON_H)
    out = await fetching.fetch_page(RAW)  # default doh: the real address is checked, then fetched
    assert out.startswith("[web_fetch] source=direct") and split(out)[1] == PYTHON_H
    assert hits.count("cloudflare-dns.com") == 2 and hits[-1] == "raw.githubusercontent.com"
    monkeypatch.setenv("GROK_VERIFY_FAKE_IP", "skip")
    hits.clear()
    out = await fetching.fetch_page(RAW)
    assert hits == [] and "attempts=direct:blocked,firecrawl:ok" in out and "fake-ip" in out


# ------------------------------------------------------------ fallbacks
@pytest.mark.asyncio
async def test_html_answer_falls_back_to_the_page_extractors(monkeypatch, dns):
    serve(monkeypatch, text_file("<html><title>Docs</title></html>", "text/html; charset=utf-8"))
    tavily, firecrawl = extractors(monkeypatch, tavily="rendered docs " * 300)
    out = await fetching.fetch_page("https://docs.example.test/guide/intro.md")
    lines, _ = split(out)
    # A page that answered as a page is not worth a note once the extractors read it (Tavily's cache note stays).
    assert lines == [f"[web_fetch] source=tavily total_chars={len('rendered docs ' * 300)} offset=0 returned={len('rendered docs ' * 300)} "
                     "truncated=false cache=possible", fetching.TAVILY_CACHE_NOTE]
    tavily.assert_awaited_once_with("https://docs.example.test/guide/intro.md")
    assert firecrawl.await_count == 0
    # When the extractors fail too, the direct answer is part of the failure message.
    extractors(monkeypatch)
    out = await fetching.fetch_page("https://docs.example.test/guide/intro.md")
    assert out.startswith("[web_fetch] error=fetch_failed attempts=direct:not_text,tavily:empty_content,firecrawl:empty_content")
    assert "- direct: not_text — text/html is not a plain-text resource" in out


@pytest.mark.asyncio
async def test_binary_body_with_a_text_type_is_not_returned(monkeypatch, dns):
    async def handler(request):
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n\x00\x00\x00", headers={"content-type": "text/plain"})

    serve(monkeypatch, handler)
    tavily, _ = extractors(monkeypatch, tavily="x" * 3000)
    out = await fetching.fetch_page("https://example.com/logo.txt")
    assert out.startswith("[web_fetch] source=tavily") and "direct" not in out.split("\n\n", 1)[0]
    tavily.assert_awaited_once_with("https://example.com/logo.txt")


@pytest.mark.asyncio
async def test_missing_file_is_reported_with_its_status(monkeypatch, dns):
    async def handler(request):
        return httpx.Response(404, text="404: Not Found", headers={"content-type": "text/plain"})

    serve(monkeypatch, handler)
    tavily, firecrawl = extractors(monkeypatch)
    out = await fetching.fetch_page("https://raw.githubusercontent.com/o/r/main/gone.py")
    # The origin said the file does not exist: the extractors, which would read the same URL, are not billed.
    assert out.startswith("[web_fetch] error=fetch_failed attempts=direct:extract_failed page_status=404\n\n源站答复 HTTP 404")
    assert "- direct: extract_failed (HTTP 404) — target page answered HTTP 404" in out
    assert tavily.await_count == firecrawl.await_count == 0


@pytest.mark.asyncio
async def test_damaged_tavily_copy_of_code_is_flagged(monkeypatch, dns):
    async def handler(request):
        raise httpx.ConnectError("unreachable", request=request)

    serve(monkeypatch, handler)
    tavily, firecrawl = extractors(monkeypatch, tavily=PYTHON_H_TAVILY, firecrawl=None)
    out = await fetching.fetch_page(RAW)
    lines, body = split(out)
    assert body == PYTHON_H_TAVILY
    assert lines[0].startswith("[web_fetch] source=tavily") and "cache=possible" in lines[0]
    assert "attempts=direct:network,firecrawl:empty_content,tavily:degraded" in lines[0]
    assert lines[0].endswith("warning=degraded_code degraded=escaped_underscores,include_target_stripped")
    assert lines[1].startswith("注意: 这是提取服务把源代码/文本文件当网页转换后的结果") and "下划线被转义" in lines[1]
    assert lines[2] == fetching.TAVILY_CACHE_NOTE
    assert lines[3].startswith("- direct: network — ") and lines[4] == "- firecrawl: empty_content — no content"
    assert firecrawl.await_args.args[0] == RAW and tavily.await_args.args[0] == RAW


@pytest.mark.asyncio
async def test_intact_firecrawl_copy_beats_a_damaged_tavily_copy(monkeypatch, dns):
    dns["raw.githubusercontent.com"] = ["127.0.0.1"]

    async def handler(request):
        raise AssertionError(f"the guard should have refused {request.url}")

    serve(monkeypatch, handler)
    tavily, firecrawl = extractors(monkeypatch, tavily=LOGGER_PY_TAVILY, firecrawl=LOGGER_PY)
    out = await fetching.fetch_page("https://raw.githubusercontent.com/o/r/main/logger.py")
    lines, body = split(out)
    assert lines[0].startswith("[web_fetch] source=firecrawl") and body == LOGGER_PY and tavily.await_count == 0


def test_code_degradation_rules():
    assert direct.code_degradation(PYTHON_H, RAW) == []
    assert direct.code_degradation(PYTHON_H_TAVILY, RAW) == ["escaped_underscores", "include_target_stripped"]
    assert direct.code_degradation(LOGGER_PY, "https://x.test/logger.py") == []
    assert direct.code_degradation(LOGGER_PY_TAVILY, "https://x.test/logger.py") == ["no_indentation", "escaped_underscores"]
    one_line = LOGGER_PY.replace("\n", " ") * 3
    assert "collapsed_newlines" in direct.code_degradation(one_line, "https://x.test/verify.py")
    assert direct.code_degradation('{"a": "' + "b" * 5000 + '"}', "https://x.test/data.json") == []  # minified JSON is normal


@pytest.mark.asyncio
async def test_direct_download_can_be_disabled(monkeypatch, dns):
    monkeypatch.setenv("GROK_FETCH_DIRECT", "false")
    hits = []
    serve(monkeypatch, text_file(PYTHON_H, hits=hits))
    tavily, _ = extractors(monkeypatch, tavily="x" * 5000)
    out = await fetching.fetch_page(RAW)
    assert out.startswith("[web_fetch] source=tavily") and hits == [] and "direct" not in out.split("\n\n", 1)[0]


@pytest.mark.asyncio
async def test_disabled_direct_download_still_reads_code_files_firecrawl_first(monkeypatch, dns):
    """GROK_FETCH_DIRECT=false only stops the local download: a code file still takes the code path
    (Firecrawl first, damage flagged), never Tavily's HTML-converted copy accepted by length alone."""
    monkeypatch.setenv("GROK_FETCH_DIRECT", "false")
    hits = []
    serve(monkeypatch, text_file(PYTHON_H, hits=hits))
    tavily, firecrawl = extractors(monkeypatch, tavily=PYTHON_H_TAVILY, firecrawl=PYTHON_H)
    out = await fetching.fetch_page("https://github.com/python/cpython/blob/main/Include/Python.h")
    lines, body = split(out)
    assert hits == [] and tavily.await_count == 0 and body == PYTHON_H
    assert lines[0].startswith("[web_fetch] source=firecrawl") and f"final_url={BLOB_RAW}" in lines[0]
    assert firecrawl.await_args.args[0] == BLOB_RAW

    tavily, firecrawl = extractors(monkeypatch, tavily=PYTHON_H_TAVILY, firecrawl=None)
    out = await fetching.fetch_page(RAW)
    lines, body = split(out)
    assert hits == [] and lines[0].startswith("[web_fetch] source=tavily") and body == PYTHON_H_TAVILY
    assert "warning=degraded_code degraded=escaped_underscores,include_target_stripped" in lines[0]


# ------------------------------------------------------------ freshness (Firecrawl maxAge, cache state)
def firecrawl_backend(monkeypatch, metadata, sent, markdown="page " * 600):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-k")
    monkeypatch.setenv("FIRECRAWL_API_URL", "https://firecrawl.example.test/v2")
    monkeypatch.setenv("TAVILY_ENABLED", "false")

    async def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True, "data": {"markdown": markdown, "metadata": metadata}})

    serve(monkeypatch, handler)


@pytest.mark.asyncio
async def test_firecrawl_is_asked_for_a_fresh_copy_by_default(monkeypatch):
    sent = []
    firecrawl_backend(monkeypatch, {"statusCode": 200}, sent)
    out = await fetching.fetch_page("https://example.test/page")
    assert sent[0]["maxAge"] == 0
    assert out.split("\n", 1)[0].endswith("truncated=false cache=bypassed")


@pytest.mark.asyncio
async def test_max_age_setting_and_cache_hit_are_reported(monkeypatch):
    monkeypatch.setenv("GROK_FETCH_MAX_AGE_S", "3600")
    sent = []
    firecrawl_backend(monkeypatch, {"statusCode": 200, "cacheState": "hit", "cachedAt": "2026-10-16T18:35:36.783Z",
                                    "contentType": "text/plain; charset=utf-8"}, sent)
    out = await fetching.fetch_page("https://example.test/page")
    assert sent[0]["maxAge"] == 3_600_000
    assert out.split("\n", 1)[0].endswith(
        "truncated=false content_type=text/plain;charset=utf-8 cache=hit cached_at=2026-10-16T18:35:36.783Z")
    assert config.get_config_info()["GROK_FETCH_MAX_AGE_S"] == 3600.0


@pytest.mark.asyncio
async def test_tavily_results_say_they_may_be_cached(monkeypatch):
    extractors(monkeypatch, tavily="x" * 5000)
    out = await fetching.fetch_page("https://example.test/page")
    assert out.split("\n", 1)[0].endswith("truncated=false cache=possible")


def test_fetch_settings_defaults(monkeypatch):
    info = config.get_config_info()
    assert info["GROK_FETCH_MAX_AGE_S"] == 0.0 and info["GROK_FETCH_DIRECT"] is True
    monkeypatch.setenv("GROK_FETCH_MAX_AGE_S", "-5")
    assert config.fetch_max_age_s == 0.0


# ------------------------------------------------------------ bot checks delivered by Tavily
REDDIT_TAVILY = (
    "[![Image 1](blob:http://localhost/77f24a5f6b40768701decac547643e78)](https://www.reddit.com/)\n\n"
    "# Prove your humanity\n\nWe’re committed to safety and security. But not for bots. Complete the challenge "
    "below and let us know you’re a real person.\n\n[Reddit, Inc. © \"2026\". All rights reserved.](https://www.redditinc.com/)"
)
FIRECRAWL_UNSUPPORTED = {"success": False, "error": "We apologize for the inconvenience but we do not support this site. "
                         "If you are part of an enterprise and want to have a further conversation about this, please "
                         "contact our sales team here: https://www.firecrawl.dev/contact-sales"}


def both_backends(monkeypatch, tavily, firecrawl):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-k")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-k")
    monkeypatch.setenv("TAVILY_API_URL", "https://tavily.example.test")
    monkeypatch.setenv("FIRECRAWL_API_URL", "https://firecrawl.example.test/v2")

    async def handler(request):
        status, body = tavily if request.url.host == "tavily.example.test" else firecrawl
        return httpx.Response(status, json=body)

    serve(monkeypatch, handler)


@pytest.mark.asyncio
async def test_reddit_bot_check_from_tavily_is_flagged_and_firecrawl_403_is_not_auth(monkeypatch):
    both_backends(monkeypatch, (200, {"results": [{"url": "u", "raw_content": REDDIT_TAVILY}]}), (403, FIRECRAWL_UNSUPPORTED))
    out = await fetching.fetch_page("https://www.reddit.com/r/Python/")
    lines, body = split(out)
    assert body == REDDIT_TAVILY
    assert lines[0].endswith("cache=possible attempts=tavily:interstitial,firecrawl:unsupported_site warning=interstitial")
    assert lines[1].startswith("注意: 这很可能不是请求页面的正文") and lines[2] == fetching.TAVILY_CACHE_NOTE
    assert lines[3].startswith("- firecrawl: unsupported_site (HTTP 403) — We apologize for the inconvenience but we do not support this site")
    assert "auth" not in out


@pytest.mark.asyncio
async def test_long_tavily_bot_check_still_tries_firecrawl(monkeypatch):
    challenge = "# Just a moment...\n\nVerifying you are human. This may take a few seconds.\n\n" + "footer link " * 300
    article = "# Thread\n\n" + "real discussion text " * 400
    both_backends(monkeypatch, (200, {"results": [{"url": "u", "raw_content": challenge}]}),
                  (200, {"success": True, "data": {"markdown": article, "metadata": {"statusCode": 200, "title": "Thread"}}}))
    out = await fetching.fetch_page("https://forum.example.test/t/1")
    lines, body = split(out)
    assert lines[0].startswith("[web_fetch] source=firecrawl") and "attempts=tavily:interstitial,firecrawl:ok" in lines[0]
    assert body == article and "warning=" not in lines[0]


# ------------------------------------------------------------ arXiv: DataCite when the arXiv API is down
DATACITE_1706 = {"data": {"attributes": {
    "titles": [{"title": "Attention Is All You Need"}],
    "creators": [{"name": "Vaswani, Ashish", "givenName": "Ashish", "familyName": "Vaswani"},
                 {"name": "Shazeer, Noam", "givenName": "Noam", "familyName": "Shazeer"},
                 {"name": "Google Brain"}],
    "descriptions": [{"description": "The dominant sequence transduction models\nare based on complex recurrent networks.",
                      "descriptionType": "Abstract"},
                     {"description": "15 pages, 5 figures", "descriptionType": "Other"}],
    "subjects": [{"subject": "Computation and Language (cs.CL)", "subjectScheme": "arXiv"},
                 {"subject": "FOS: Computer and information sciences", "subjectScheme": "Fields of Science and Technology (FOS)"}],
    "dates": [{"date": "2017-06-12T17:57:34Z", "dateType": "Submitted", "dateInformation": "v1"},
              {"date": "2023-08-02T00:41:18Z", "dateType": "Submitted", "dateInformation": "v7"},
              {"date": "2017", "dateType": "Issued"}],
    "version": "7", "publicationYear": 2017,
}}}


def arxiv_down(monkeypatch, datacite=(200, DATACITE_1706), seen=None):
    async def handler(request):
        if seen is not None:
            seen.append(request)
        if request.url.host == "api.datacite.org":
            status, body = datacite
            return httpx.Response(status, json=body)
        return httpx.Response(503, text="down")

    serve(monkeypatch, handler)


@pytest.mark.asyncio
async def test_arxiv_abs_page_comes_from_datacite_when_the_api_is_down(monkeypatch):
    seen = []
    arxiv_down(monkeypatch, seen=seen)
    tavily, firecrawl = extractors(monkeypatch, tavily="skeleton " * 300, firecrawl="chrome " * 2000)
    out = await fetching.fetch_page("https://arxiv.org/abs/1706.03762")
    lines, body = split(out)
    assert lines[0].startswith("[web_fetch] source=datacite") and "attempts=arxiv-api:upstream_error" in lines[0]
    assert "fetched_via: datacite" in body and "version: v7 (latest)" in body
    assert "**Authors:** Ashish Vaswani, Noam Shazeer, Google Brain" in body
    assert "**Published:** 2017-06-12 | **Updated:** 2023-08-02" in body
    assert "**Categories:** Computation and Language (cs.CL)\n**Comments:** 15 pages, 5 figures" in body
    assert "## Abstract\n\nThe dominant sequence transduction models are based on complex recurrent networks." in body
    assert tavily.await_count == 0 and firecrawl.await_count == 0
    datacite = [r for r in seen if r.url.host == "api.datacite.org"][0]
    assert datacite.url.path == "/dois/10.48550/arXiv.1706.03762" and datacite.headers["accept"] == "application/vnd.api+json"


@pytest.mark.asyncio
async def test_old_style_arxiv_id_uses_its_doi(monkeypatch):
    seen = []
    arxiv_down(monkeypatch, seen=seen)
    extractors(monkeypatch)
    out = await fetching.fetch_page("https://arxiv.org/abs/hep-th/9711200")
    assert out.startswith("[web_fetch] source=datacite")
    assert [r.url.path for r in seen if r.url.host == "api.datacite.org"] == ["/dois/10.48550/arXiv.hep-th/9711200"]


@pytest.mark.asyncio
async def test_datacite_is_not_used_for_another_version_and_firecrawl_goes_first(monkeypatch):
    arxiv_down(monkeypatch)
    tavily, firecrawl = extractors(monkeypatch, tavily="skeleton " * 300, firecrawl="abstract page " * 50)
    out = await fetching.fetch_page("https://arxiv.org/abs/1706.03762v1")
    lines, body = split(out)
    assert lines[0].startswith("[web_fetch] source=firecrawl total_chars=700")  # no length threshold here
    assert "attempts=arxiv-api:upstream_error,datacite:extract_failed,firecrawl:ok" in lines[0]
    assert "- datacite: extract_failed — DataCite describes only the latest version (v7), not v1" in lines
    assert tavily.await_count == 0


@pytest.mark.asyncio
async def test_tavily_is_the_last_resort_for_arxiv(monkeypatch):
    arxiv_down(monkeypatch, datacite=(404, {"errors": [{"status": "404"}]}))
    tavily, firecrawl = extractors(monkeypatch, tavily="skeleton " * 100, firecrawl=None)
    out = await fetching.fetch_page("https://arxiv.org/abs/1706.03762")
    assert out.startswith("[web_fetch] source=tavily")
    assert "attempts=arxiv-api:upstream_error,datacite:extract_failed,firecrawl:empty_content,tavily:ok" in out.split("\n", 1)[0]


# ------------------------------------------------------------ web pages that look like files (C1/R3, R6)
# Firecrawl's Markdown of www.npmjs.com/package/chart.js (recorded live): no indented line in 30+ lines.
NPM_PAGE = "\n".join(["# chart.js", "", "Simple HTML5 charts using the canvas element."]
                     + [f"- [Link {i}](https://www.npmjs.com/package/x{i})" for i in range(40)])


def cloudflare_403(hits=None):
    async def handler(request):
        if hits is not None:
            hits.append(str(request.url))
        return httpx.Response(403, text="<!DOCTYPE html><title>Just a moment...</title>", headers={"content-type": "text/html; charset=UTF-8"})
    return handler


def firecrawl_page(monkeypatch, markdown, content_type="text/html; charset=utf-8", final_url=""):
    async def scrape(url, ctx=None, page=None):
        if page is not None:
            page.update(final_url=final_url or url, title="chart.js - npm", content_type=content_type, max_age_ms=0)
        return markdown
    mock = AsyncMock(side_effect=scrape)
    monkeypatch.setattr(fetching, "_call_firecrawl_scrape", mock)
    return mock


@pytest.mark.asyncio
async def test_an_html_error_page_for_a_js_url_is_read_as_a_page_without_a_code_warning(monkeypatch, dns):
    """npm answers the direct request with Cloudflare's 403 HTML: a web page, read as in v1.12.0."""
    serve(monkeypatch, cloudflare_403())
    tavily, _ = extractors(monkeypatch, tavily=None)
    monkeypatch.setattr(fetching, "_call_tavily_extract", AsyncMock(side_effect=fetching.ProviderError("tavily", "extract_failed", "x")))
    firecrawl = firecrawl_page(monkeypatch, NPM_PAGE)
    out = await fetching.fetch_page("https://www.npmjs.com/package/chart.js")
    lines, body = split(out)
    assert body == NPM_PAGE and lines[0].startswith("[web_fetch] source=firecrawl")
    assert "degraded" not in out and "注意: 这是提取服务" not in out and "direct" not in lines[0]
    assert "attempts=tavily:extract_failed,firecrawl:ok" in lines[0]
    assert firecrawl.await_args.args[0] == "https://www.npmjs.com/package/chart.js"


def no_direct_answer(monkeypatch, setup):
    """The direct download of a weak (extension-only) target gives no answer: it times out, is turned
    off, or is not attempted because a proxy resolves the name."""
    hits = []

    async def handler(request):
        hits.append(str(request.url))
        raise httpx.ReadTimeout("slow", request=request)

    serve(monkeypatch, handler)
    if setup == "disabled":
        monkeypatch.setenv("GROK_FETCH_DIRECT", "false")
    if setup == "proxy":
        monkeypatch.setattr(direct, "_through_proxy", lambda client, url: True)
    return hits


WIKI_NODE = "https://en.wikipedia.org/wiki/Node.js"
# Tavily's Markdown of the article (recorded live: 80,637 characters, links throughout).
WIKI_PAGE = "\n".join(["# Node.js", "", "Node.js is a cross-platform JavaScript runtime environment."]
                      + [f"* [{i} Section](#Section_{i})\n  + [{i}.1 Detail](#Detail_{i})" for i in range(30)]
                      + ["Ryan Dahl wrote Node.js in 2009. " * 20])


@pytest.mark.asyncio
@pytest.mark.parametrize("setup", ["timeout", "disabled", "proxy"])
async def test_a_page_with_a_code_like_url_is_read_by_tavily_alone(monkeypatch, dns, setup):
    """RR1-R6: behind a proxy en.wikipedia.org/wiki/Node.js took 4.8 s through Firecrawl, 1.5 s through
    Tavily in v1.12.0. A weak target that was not downloaded is read as a page first."""
    hits = no_direct_answer(monkeypatch, setup)
    tavily, _ = extractors(monkeypatch, tavily=WIKI_PAGE)
    firecrawl = firecrawl_page(monkeypatch, NPM_PAGE)
    out = await fetching.fetch_page(WIKI_NODE)
    lines, body = split(out)
    assert body == WIKI_PAGE and lines[0].startswith("[web_fetch] source=tavily")
    assert "degraded" not in out and "注意: 这是提取服务" not in out
    assert tavily.await_args.args[0] == WIKI_NODE and firecrawl.await_count == 0
    assert hits == ([] if setup != "timeout" else [WIKI_NODE])


@pytest.mark.asyncio
@pytest.mark.parametrize("setup", ["timeout", "disabled", "proxy"])
async def test_firecrawl_html_for_a_code_like_url_is_never_flagged_as_damaged_code(monkeypatch, dns, setup):
    """Tavily's copy has no links and no indentation but is shorter than GROK_FETCH_MIN_CHARS, so Firecrawl
    is asked; Firecrawl reports text/html: the URL is a page and the copies are compared as pages."""
    no_direct_answer(monkeypatch, setup)
    flat = "\n".join(f"Paragraph {i} of the article about the runtime." for i in range(40))
    assert direct.code_degradation(flat, WIKI_NODE) == ["no_indentation"]
    tavily, _ = extractors(monkeypatch, tavily=flat)
    firecrawl = firecrawl_page(monkeypatch, NPM_PAGE)
    out = await fetching.fetch_page(WIKI_NODE)
    lines, body = split(out)
    assert body == NPM_PAGE and lines[0].startswith("[web_fetch] source=firecrawl")
    assert "degraded" not in out and "注意: 这是提取服务" not in out and f"tavily:short({len(flat)}),firecrawl:ok" in lines[0]
    firecrawl.assert_awaited_once()
    # A longer flat copy is kept as a page would be, without a code warning.
    long_flat = flat * 3
    extractors(monkeypatch, tavily=long_flat)
    firecrawl_page(monkeypatch, NPM_PAGE)
    lines, body = split(await fetching.fetch_page(WIKI_NODE))
    assert body == long_flat and lines[0].startswith("[web_fetch] source=tavily") and "degraded" not in lines[0]
    assert "tavily:degraded" not in lines[0] and "attempts=" + ("direct:timeout," if setup == "timeout" else "") + "firecrawl:shorter" in lines[0]


@pytest.mark.asyncio
async def test_a_small_file_behind_a_proxy_is_read_by_firecrawl_as_text(monkeypatch, dns):
    """E4: behind a proxy a short Tavily copy of a URL that only ends in a text extension cannot be told
    from a page stub, so Firecrawl is asked (one extra call); it reports text/plain, and its copy (the
    file unchanged) is returned."""
    no_direct_answer(monkeypatch, "proxy")
    note = "Release notes\n\n1.2.0: fixed the parser.\n"
    tavily, _ = extractors(monkeypatch, tavily=note.rstrip("\n"))
    firecrawl = firecrawl_page(monkeypatch, note, content_type="text/plain; charset=utf-8")
    lines, body = split(await fetching.fetch_page("https://downloads.example.org/notes.txt"))
    assert body == note and lines[0].startswith("[web_fetch] source=firecrawl")
    assert f"attempts=tavily:short({len(note) - 1}),firecrawl:ok" in lines[0] and "degraded" not in lines[0]
    tavily.assert_awaited_once()
    firecrawl.assert_awaited_once()


# Tavily's copy of bundlephobia.com/package/chart.js behind a proxy (recorded live, 521 characters): a
# page stub with headings, *emphasis* and one link, and no Markdown link list.
BUNDLEPHOBIA = "https://bundlephobia.com/package/chart.js"
BUNDLEPHOBIA_TAVILY = (
    "# chart.js\n\n#### Recent searches\n\n### What does Bundlephobia do?\n\nJavaScript bloat is more real today than it "
    "ever was. Sites continuously get bigger as more (often redundant) libraries are thrown to solve new problems. Until "
    "of-course, the  *big rewrite* happens.\n\nBundlephobia lets you understand the performance cost of`npm\xa0install` "
    "ing a new npm package before it becomes a part of your bundle. Analyze size, compositions and exports\n\n"
    "`npm\xa0install`\n\nCredits to  [@thekitze](https://twitter.com/thekitze) for the name.")
# Firecrawl's copy (recorded live, 3,424 characters; abridged): the page itself, served as text/html.
BUNDLEPHOBIA_FIRECRAWL = (
    "# chart.js\n\n@4.5.1\n\nCreated with Sketch.\n\ntree-shakeable\n\nsome side-effects\n\nDependenciesCreated with "
    "Sketch.1 dependency\n\n[View chart.js on npm](https://npmjs.com/package/chart.js)[Github Link]"
    "(https://github.com/chartjs/Chart.js.git)\n\n### Bundle Size\n\n196.1\n\nkB\n\nMinified\n\n66.8\n\nkB\n\n"
    "Minified + Gzipped\n\n### Download Time\n\n1.34\n\ns\n\nSlow 3G\n\ni\n\n76\n\nms\n\nEmerging 4G\n\n## Composition\n\n"
    "(self)\n\n96.2%\n\n[@kurkle/color](https://bundlephobia.com/package/@kurkle/color)\n\n3.8%\n\n**Note:** These sizes "
    "represent the contribution made by dependencies (direct or transitive) to `chart.js`'s size. These may be different "
    "from the dependencies' standalone sizes.\n\n## Exports Analysis\n\nGZIP sizes of individual exports\n\n### \\_\n\n"
    "\\_detectPlatform\n\n35.0kB\n\n\\_adapters\n\n33.0kB\n\n### a\n\nArcElement\n\n34.6kB\n\nAnimations\n\n33.0kB\n\n"
    "animator\n\n33.0kB\n\nAnimation\n\n32.9kB\n\n### b\n\nBarElement\n\n33.9kB\n\nBasicPlatform\n\n33.1kB\n\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("setup", ["timeout", "disabled", "proxy"])
async def test_a_short_page_stub_for_a_code_like_url_goes_on_to_firecrawl(monkeypatch, dns, setup):
    """RR2-R6: behind a proxy Tavily's 521-character stub of bundlephobia.com/package/chart.js was taken
    for a complete .js file (fewer than 3 links); v1.12.0 returned Firecrawl's 3,424-character page, and
    so does this version: a copy shorter than GROK_FETCH_MIN_CHARS always goes on to Firecrawl."""
    assert len(BUNDLEPHOBIA_TAVILY) == 521
    no_direct_answer(monkeypatch, setup)
    tavily, _ = extractors(monkeypatch, tavily=BUNDLEPHOBIA_TAVILY)
    firecrawl = firecrawl_page(monkeypatch, BUNDLEPHOBIA_FIRECRAWL, final_url=BUNDLEPHOBIA + "@4.5.1")
    lines, body = split(await fetching.fetch_page(BUNDLEPHOBIA))
    assert body == BUNDLEPHOBIA_FIRECRAWL and lines[0].startswith("[web_fetch] source=firecrawl")
    assert "attempts=" + ("direct:timeout," if setup == "timeout" else "") + "tavily:short(521),firecrawl:ok" in lines[0]
    assert "degraded" not in lines[0] and "注意" not in "\n".join(lines)
    assert firecrawl.await_args.args[0] == BUNDLEPHOBIA


@pytest.mark.asyncio
async def test_a_short_page_stub_is_kept_when_firecrawl_fails(monkeypatch, dns):
    """Without Firecrawl the stub is what there is, returned as in v1.12.0; code_degradation finds no
    damage in it, so there is no code warning."""
    no_direct_answer(monkeypatch, "proxy")
    extractors(monkeypatch, tavily=BUNDLEPHOBIA_TAVILY, firecrawl=None)
    lines, body = split(await fetching.fetch_page(BUNDLEPHOBIA))
    assert body == BUNDLEPHOBIA_TAVILY and lines[0].startswith("[web_fetch] source=tavily")
    assert "attempts=tavily:short(521),firecrawl:empty_content" in lines[0] and "degraded" not in lines[0]


SHORT_JS = "import { helpers } from './helpers.js';\n\nexport function add(a, b) {\n  return helpers.sum(a, b);\n}\n"
SHORT_JSON = '{\n  "name": "demo-widget",\n  "version": "1.4.2",\n  "main": "dist/index.js",\n  "license": "MIT"\n}\n'
SHORT_FILES = [
    ("https://static.example.org/lib/add.js", SHORT_JS, "application/javascript"),
    ("https://static.example.org/widget/package.json", SHORT_JSON, "application/json"),
    ("https://downloads.example.org/notes.txt", "Release notes\n\n1.2.0: fixed the parser.\n", "text/plain; charset=utf-8"),
    ("https://static.example.org/pkg/_version.py", 'VERSION = "2.4.0"\nRELEASE: str = "stable"\n', "text/x-python"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("url, text, content_type", SHORT_FILES)
async def test_a_genuine_short_file_behind_a_proxy_is_firecrawls_copy(monkeypatch, dns, url, text, content_type):
    """A short Tavily copy is never taken for the file on sight: Firecrawl is asked as for any short page
    (as in v1.12.0), reports a text type, and its copy (text/plain unchanged) beats Tavily's."""
    no_direct_answer(monkeypatch, "proxy")
    tavily_copy = text.strip()  # Tavily drops the final newline
    tavily, _ = extractors(monkeypatch, tavily=tavily_copy)
    firecrawl = firecrawl_page(monkeypatch, text, content_type=content_type)
    lines, body = split(await fetching.fetch_page(url))
    assert body == text and lines[0].startswith("[web_fetch] source=firecrawl")
    assert f"attempts=tavily:short({len(tavily_copy)}),firecrawl:ok" in lines[0] and "degraded" not in lines[0]
    tavily.assert_awaited_once()
    assert firecrawl.await_args.args[0] == url


@pytest.mark.asyncio
@pytest.mark.parametrize("url, text, content_type", SHORT_FILES)
async def test_a_genuine_short_file_is_tavilys_copy_when_firecrawl_fails(monkeypatch, dns, url, text, content_type):
    """Without Firecrawl the short copy is what there is, returned as before; it is intact, so no warning."""
    no_direct_answer(monkeypatch, "proxy")
    assert direct.code_degradation(text, url) == []
    extractors(monkeypatch, tavily=text, firecrawl=None)
    lines, body = split(await fetching.fetch_page(url))
    assert body == text and lines[0].startswith("[web_fetch] source=tavily")
    assert f"attempts=tavily:short({len(text)}),firecrawl:empty_content" in lines[0] and "degraded" not in lines[0]


# What Tavily makes of a short code file (V3-1): every newline collapsed, and for Python "_" escaped too.
# code_degradation's collapse check needs a line of COLLAPSED_LINE_CHARS, impossible in a copy shorter
# than GROK_FETCH_MIN_CHARS; repair round 1 took such a copy for the verbatim file without asking Firecrawl.
SHORT_PY_HELPERS = "def add_one(value):\n    return value + 1\n\n\ndef add_two(value):\n    return add_one(add_one(value))\n"
COLLAPSED_SHORT_COPIES = [
    ("https://static.example.org/lib/add.js", SHORT_JS, " ".join(SHORT_JS.split()), []),
    ("https://static.example.org/pkg/helpers.py", SHORT_PY_HELPERS,
     " ".join(SHORT_PY_HELPERS.replace("_", "\\_").split()), ["escaped_underscores"]),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("url, text, collapsed, damage", COLLAPSED_SHORT_COPIES)
async def test_a_collapsed_short_copy_of_a_code_file_goes_on_to_firecrawl(monkeypatch, dns, url, text, collapsed, damage):
    assert "\n" not in collapsed and direct.code_degradation(collapsed, url) == damage
    no_direct_answer(monkeypatch, "proxy")
    extractors(monkeypatch, tavily=collapsed)
    firecrawl = firecrawl_page(monkeypatch, text, content_type="text/plain; charset=utf-8")
    lines, body = split(await fetching.fetch_page(url))
    assert body == text and lines[0].startswith("[web_fetch] source=firecrawl")
    assert f"attempts=tavily:short({len(collapsed)}),firecrawl:ok" in lines[0] and "degraded" not in lines[0]
    firecrawl.assert_awaited_once()
    # Without Firecrawl the collapsed copy is returned, flagged for the damage code_degradation finds.
    extractors(monkeypatch, tavily=collapsed, firecrawl=None)
    lines, body = split(await fetching.fetch_page(url))
    assert body == collapsed and lines[0].startswith("[web_fetch] source=tavily")
    assert f"attempts=tavily:short({len(collapsed)}),firecrawl:empty_content" in lines[0]
    if damage:
        assert "warning=degraded_code degraded=" + ",".join(damage) in lines[0]
        assert lines[1].startswith("注意: 这是提取服务把源代码/文本文件当网页转换后的结果")
    else:
        assert "degraded" not in lines[0] and lines[1:] == [fetching.TAVILY_CACHE_NOTE, "- firecrawl: empty_content — no content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", [1500, 5000, 12000])
async def test_a_deeply_nested_short_json_copy_cannot_crash_web_fetch(monkeypatch, dns, depth):
    """V3-2: a short Tavily copy such as "[" * 5000 of a .json URL was parsed with json.loads, which raised
    RecursionError (not a ValueError; from depth 1,000 on Python 3.10, 10,000 on 3.13), and web_fetch
    failed with a tool error. A short copy is no longer parsed: it goes on to Firecrawl."""
    monkeypatch.setenv("GROK_FETCH_MIN_CHARS", "20000")
    url = "https://static.example.org/data/deep.json"
    nested = "[" * depth
    no_direct_answer(monkeypatch, "proxy")
    extractors(monkeypatch, tavily=nested, firecrawl=None)
    lines, body = split(await fetching.fetch_page(url))
    assert body == nested and lines[0].startswith("[web_fetch] source=tavily")
    assert f"attempts=tavily:short({depth}),firecrawl:empty_content" in lines[0]
    firecrawl_page(monkeypatch, "[[]]\n", content_type="application/json")
    lines, body = split(await fetching.fetch_page(url))
    assert body == "[[]]\n" and lines[0].startswith("[web_fetch] source=firecrawl")


@pytest.mark.asyncio
async def test_a_short_markdown_file_keeps_the_page_length_rule(monkeypatch, dns):
    """A .md file is Markdown, so its copy cannot be told from a page stub: short means Firecrawl, as in v1.12.0."""
    no_direct_answer(monkeypatch, "proxy")
    readme = "# Widget\n\nA small widget.\n\n## Usage\n\n```js\nwidget()\n```\n"
    extractors(monkeypatch, tavily=readme[:30])
    firecrawl = firecrawl_page(monkeypatch, readme, content_type="text/markdown; charset=utf-8")
    lines, body = split(await fetching.fetch_page("https://example.org/docs/README.md"))
    assert body == readme and lines[0].startswith("[web_fetch] source=firecrawl") and "tavily:short(30)" in lines[0]
    firecrawl.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_page_like_short_copy_is_still_checked_when_firecrawl_says_file(monkeypatch, dns):
    """Tavily's copy is short, so Firecrawl is asked; it reports a text type, and the copies are compared
    as a file's: Tavily's damage counts against it as well."""
    no_direct_answer(monkeypatch, "proxy")
    tavily_copy = "# app.js\n\nconst a\\_b = 1;\nconst c\\_d = 2;\nconst e\\_f = 3;\n"
    firecrawl_copy = "const a\\_b = 1;\nconst c\\_d = 2;\nconst e\\_f = 3;\nconst g = 4;\n"
    extractors(monkeypatch, tavily=tavily_copy)
    firecrawl_page(monkeypatch, firecrawl_copy, content_type="application/javascript")
    lines, body = split(await fetching.fetch_page("https://example.org/app.js"))
    assert body == firecrawl_copy and lines[0].startswith("[web_fetch] source=firecrawl")
    assert f"attempts=tavily:short({len(tavily_copy)}),firecrawl:degraded" in lines[0]
    assert "warning=degraded_code degraded=escaped_underscores" in lines[0]


# Page stubs that repair round 1 took for files and returned without asking Firecrawl (V1-4; v1.12.0
# asked Firecrawl for all three, and so does this version: they are short).
PYPI_REQUIRES_STUB = ("https://pypi.example/project/web.py/",
                      "web.py 0.62\n\nweb.py is a web framework for Python that is as simple as it is powerful.\n\n"
                      "Requires: Python >=3.5\n\nLicense: Public Domain")  # ": Python >" read as an annotation
PYPI_NESTED_LIST_STUB = ("https://pypi.example/project/discord.py/",
                         "discord.py 2.4.0\n\npip install discord.py\n\nNavigation\n\n- Project description\n"
                         "  - Release history\n\nA Python wrapper for the Discord API")  # "  - " read as indentation
FENCED_SNIPPET_STUB = ("https://pkgs.example/package/chart.js",
                       "chart.js\n\nSimple HTML5 charts using the canvas element.\n\n```js\n"
                       "import Chart from 'chart.js/auto';\n```\n")  # the usage snippet has JavaScript syntax


@pytest.mark.asyncio
@pytest.mark.parametrize("url, stub", [PYPI_REQUIRES_STUB, PYPI_NESTED_LIST_STUB, FENCED_SNIPPET_STUB])
async def test_package_page_stubs_go_on_to_firecrawl(monkeypatch, dns, url, stub):
    assert len(stub) < fetching.config.fetch_min_chars
    no_direct_answer(monkeypatch, "proxy")
    extractors(monkeypatch, tavily=stub)
    firecrawl = firecrawl_page(monkeypatch, BUNDLEPHOBIA_FIRECRAWL)
    lines, body = split(await fetching.fetch_page(url))
    assert body == BUNDLEPHOBIA_FIRECRAWL and lines[0].startswith("[web_fetch] source=firecrawl")
    assert f"attempts=tavily:short({len(stub)}),firecrawl:ok" in lines[0] and "degraded" not in lines[0]
    firecrawl.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_damaged_copy_behind_a_proxy_is_flagged_when_firecrawl_has_nothing_better(monkeypatch, dns):
    no_direct_answer(monkeypatch, "proxy")
    tavily, firecrawl = extractors(monkeypatch, tavily=PYTHON_H_TAVILY, firecrawl=None)
    lines, body = split(await fetching.fetch_page("http://example.org/config.h"))
    assert body == PYTHON_H_TAVILY and lines[0].startswith("[web_fetch] source=tavily")
    assert f"attempts=tavily:short({len(PYTHON_H_TAVILY)}),firecrawl:empty_content" in lines[0]
    assert "warning=degraded_code degraded=escaped_underscores,include_target_stripped" in lines[0]
    assert lines[1].startswith("注意: 这是提取服务把源代码/文本文件当网页转换后的结果")


@pytest.mark.asyncio
async def test_a_text_plain_firecrawl_copy_is_still_checked(monkeypatch, dns):
    monkeypatch.setenv("GROK_FETCH_DIRECT", "false")
    extractors(monkeypatch, tavily=None)
    firecrawl_page(monkeypatch, LOGGER_PY_TAVILY, content_type="text/plain; charset=utf-8")
    out = await fetching.fetch_page("https://example.com/logger.py")
    assert "warning=degraded_code degraded=no_indentation,escaped_underscores" in out


# ------------------------------------------------------------ text files served with a generic type (C2)
@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", ["binary/octet-stream", "application/octet-stream", "application/x-fossil-artifact", None])
async def test_text_served_with_a_generic_type_is_returned_verbatim(monkeypatch, dns, content_type):
    toml = '[pkg.rust]\nversion = "1.90.0"\n\n[pkg.rust.target.x86_64-unknown-linux-gnu]\navailable = true\n'

    async def handler(request):
        headers = {"content-type": content_type} if content_type else {}
        return httpx.Response(200, content=toml.encode(), headers=headers)

    serve(monkeypatch, handler)
    tavily, firecrawl = extractors(monkeypatch, tavily="collapsed " * 500)
    out = await fetching.fetch_page("https://static.rust-lang.org/dist/channel-rust-stable.toml")
    lines, body = split(out)
    assert body == toml and lines[0].startswith("[web_fetch] source=direct")
    assert tavily.await_count == firecrawl.await_count == 0


@pytest.mark.asyncio
async def test_binary_under_a_generic_type_stops_after_the_first_bytes(monkeypatch, dns):
    served = {"chunks": 0}

    async def body():
        for _ in range(1000):
            served["chunks"] += 1
            yield b"\x89PNG\r\n\x1a\n\x00\x00" + bytes(range(256)) * 16

    async def handler(request):
        return httpx.Response(200, content=body(), headers={"content-type": "application/octet-stream"})

    serve(monkeypatch, handler)
    tavily, _ = extractors(monkeypatch, tavily="page " * 600)
    out = await fetching.fetch_page("https://example.com/download/blob.txt")
    assert out.startswith("[web_fetch] source=tavily") and served["chunks"] < 5


@pytest.mark.asyncio
async def test_collapsed_copy_of_a_raw_file_with_any_extension_is_flagged(monkeypatch, dns):
    """sqlite.h.in from Fossil's /raw/ URL: Tavily returned the 2,297-line file as one 192,846-character line."""
    monkeypatch.setattr(direct, "_through_proxy", lambda client, url: True)
    one_line = "/* SQLite header */ " + "#define SQLITE_VERSION \"3.50.0\" " * 1500
    tavily, firecrawl = extractors(monkeypatch, tavily=one_line, firecrawl=None)
    out = await fetching.fetch_page("https://www.sqlite.org/src/raw/src/sqlite.h.in?name=trunk")
    lines, body = split(out)
    assert lines[0].startswith("[web_fetch] source=tavily") and "warning=degraded_code degraded=collapsed_newlines" in lines[0]
    assert firecrawl.await_args.args[0] == "https://www.sqlite.org/src/raw/src/sqlite.h.in?name=trunk"
    assert direct.code_degradation(one_line, "https://cdn.example.test/raw/app.min.js", known_text=True) == []
    assert direct.code_degradation(one_line, "https://example.test/raw/data.json", known_text=True) == []


# ------------------------------------------------------------ forge patches (C3)
@pytest.mark.asyncio
async def test_a_commit_patch_is_downloaded_directly(monkeypatch, dns):
    patch = "From 2ba81d0 Mon Sep 17 00:00:00 2001\nSubject: [PATCH] fix\n---\n src/x.py | 2 +-\n\n-    old\n+    new\n"
    serve(monkeypatch, text_file(patch))
    tavily, firecrawl = extractors(monkeypatch, tavily="short", firecrawl=patch)
    out = await fetching.fetch_page("https://github.com/Cody-Xue/GrokSearch/commit/2ba81d01e01802f9c6cc6cbedfd4d4b0fbcb241f.patch")
    assert split(out)[1] == patch and out.startswith("[web_fetch] source=direct")
    assert tavily.await_count == firecrawl.await_count == 0


# ------------------------------------------------------------ decoding (C4, SEC-2)
@pytest.mark.asyncio
@pytest.mark.parametrize("content_type, raw, text, charset", [
    ("text/plain", "中文说明：安装步骤\r\n".encode("gbk"), "中文说明：安装步骤\r\n", "gb18030"),
    ("text/plain; charset=ISO-8859-1", "﻿café\n".encode("utf-8"), "café\n", "utf-8-sig"),
    ("application/xml", '<?xml version="1.0" encoding="GBK"?><a>中文</a>'.encode("gbk"), '<?xml version="1.0" encoding="GBK"?><a>中文</a>', "gbk"),
    ("text/plain", "plain utf-8 ✓\n".encode(), "plain utf-8 ✓\n", ""),
    ("text/plain; charset=utf-8", "naïve\n".encode(), "naïve\n", ""),
])
async def test_charset_is_sniffed_when_not_declared(monkeypatch, dns, content_type, raw, text, charset):
    async def handler(request):
        return httpx.Response(200, content=raw, headers={"content-type": content_type})

    serve(monkeypatch, handler)
    extractors(monkeypatch)
    lines, body = split(await fetching.fetch_page("https://example.com/README.txt"))
    assert body == text and "decode_errors" not in lines[0]
    assert (f" charset={charset} " in lines[0]) if charset else "charset=" not in lines[0].replace(content_type.replace(" ", ""), "")


@pytest.mark.asyncio
@pytest.mark.parametrize("raw, text, charset", [
    # RR1-C4: GB18030 decoded this without an error as "Ren閑 M黮ler wohnt in M黱chen; caf閟 se Gre".
    ("Renée Müller wohnt in München; cafés süße Grüße\n".encode("latin-1"), "Renée Müller wohnt in München; cafés süße Grüße\n", "cp1252"),
    ("“Smart quotes” – 10 €\n".encode("cp1252"), "“Smart quotes” – 10 €\n", "cp1252"),
    ("這是繁體中文的測試\n".encode("big5"), "這是繁體中文的測試\n".encode("big5").decode("cp1252"), "cp1252"),
])
async def test_undeclared_text_that_is_not_chinese_is_not_read_as_gb18030(monkeypatch, dns, raw, text, charset):
    async def handler(request):
        return httpx.Response(200, content=raw, headers={"content-type": "text/plain"})

    serve(monkeypatch, handler)
    extractors(monkeypatch)
    lines, body = split(await fetching.fetch_page("https://example.com/README.txt"))
    assert body == text and f" charset={charset} " in lines[0] and "warning=charset_guess" in lines[0]
    assert lines[1].startswith("注意: 源站没有声明字符集") and "cp1252" in lines[1]


def test_gb18030_is_kept_for_chinese_and_utf8_with_a_few_broken_bytes_stays_utf8():
    for sample in ("中文说明：安装步骤\n", "這是繁體中文的測試，說明與設定檔\n", "配置文件 config: 路径=/usr/local # 注释\n" * 5):
        assert direct._decode(sample.encode("gb18030"), None) == (sample, "gb18030", False, False)
    text, codec, lossy, guessed = direct._decode("naïve café résumé ".encode() * 20 + b"\xff end", None)
    assert codec == "utf-8" and lossy and not guessed and text.startswith("naïve café") and text.endswith("\ufffd end")
    assert direct._decode(b"abc \x81 def \xe9", None) == ("abc \x81 def é", "iso8859-1", False, True)


@pytest.mark.asyncio
async def test_undecodable_bytes_are_flagged(monkeypatch, dns):
    async def handler(request):
        return httpx.Response(200, content=b"ok \xff\xfe broken\n", headers={"content-type": "text/plain; charset=utf-8"})

    serve(monkeypatch, handler)
    extractors(monkeypatch)
    lines, body = split(await fetching.fetch_page("https://example.com/notes.txt"))
    assert body == "ok �� broken\n" and "warning=decode_errors" in lines[0]
    assert lines[1].startswith("注意: 文件中有按") and "U+FFFD" in lines[1]


@pytest.mark.asyncio
async def test_a_declared_punycode_charset_is_not_used(monkeypatch, dns):
    """CPython's punycode decoder is quadratic and runs on the event loop: 800 KB took 40 s."""
    import time
    data = b"a" * 400_000 + b"-" + b"a" * 400_000

    async def handler(request):
        return httpx.Response(200, content=data, headers={"content-type": "text/plain; charset=punycode"})

    serve(monkeypatch, handler)
    extractors(monkeypatch)
    started = time.monotonic()
    lines, body = split(await fetching.fetch_page("https://example.com/puny.txt", max_chars=len(data)))
    assert time.monotonic() - started < 2.0 and body == data.decode()
    for charset in ("punycode", "idna", "undefined", "unicode_escape", "rot13", "x-unknown"):
        assert verify.text_charset(charset) is None
        assert verify._decode(b"abc", charset) == "abc"
    assert verify.text_charset("GBK") == "gbk" and verify.text_charset("latin1") == "iso8859-1"


# ------------------------------------------------------------ where the connection goes (SEC-1)
@pytest.mark.asyncio
async def test_a_connection_that_went_to_a_private_address_is_not_read(monkeypatch, dns):
    """DNS rebinding: the guard saw a public address, the connection resolved the name again."""
    hits = []
    serve(monkeypatch, text_file('{"db_password": "LAB-SECRET"}', "application/json", hits=hits), peer="127.0.0.1")
    tavily, firecrawl = extractors(monkeypatch, tavily=None, firecrawl=None)
    out = await fetching.fetch_page("http://rebind.example.test/config.json")
    assert "LAB-SECRET" not in out and len(hits) == 1
    assert "attempts=direct:blocked" in out and "the connection went to 127.0.0.1, which is not a public address" in out
    firecrawl.assert_awaited_once()  # the cloud extractors read the public URL instead


@pytest.mark.asyncio
async def test_through_a_proxy_only_well_known_code_hosts_are_downloaded(monkeypatch, dns):
    """A proxy (or a fake-ip TUN) resolves the name itself, so the guard cannot know the destination."""
    monkeypatch.setattr(direct, "_through_proxy", lambda client, url: True)
    hits = []
    serve(monkeypatch, text_file(PYTHON_H, hits=hits), peer="127.0.0.1")  # the peer is the proxy
    tavily, firecrawl = extractors(monkeypatch, tavily=PYTHON_H_TAVILY, firecrawl=PYTHON_H)
    out = await fetching.fetch_page("http://example.org/config.h")
    lines, body = split(out)
    assert hits == [] and body == PYTHON_H and lines[0].startswith("[web_fetch] source=firecrawl")
    assert "direct" not in lines[0] and len(lines) == 1  # not attempted, so nothing to report
    # Only the extension says file: Tavily first (most such URLs are pages), its short copy brings in Firecrawl.
    assert f"attempts=tavily:short({len(PYTHON_H_TAVILY)}),firecrawl:ok" in lines[0]
    assert tavily.await_count == 1 and firecrawl.await_args.args[0] == "http://example.org/config.h"
    # GitHub cannot be pointed at the user's machine: still downloaded directly through the proxy.
    out = await fetching.fetch_page(RAW)
    assert out.startswith("[web_fetch] source=direct") and [str(r.url) for r in hits] == [RAW]


@pytest.mark.asyncio
async def test_through_a_proxy_a_redirect_off_the_code_hosts_is_refused(monkeypatch, dns):
    monkeypatch.setattr(direct, "_through_proxy", lambda client, url: True)
    hits = []

    async def handler(request):
        hits.append(request.url.host)
        return httpx.Response(302, headers={"location": "http://lab.example.test/secret.txt"})

    serve(monkeypatch, handler)
    extractors(monkeypatch)
    out = await fetching.fetch_page("https://gist.githubusercontent.com/u/1/raw/x.txt")
    assert hits == ["gist.githubusercontent.com"]
    assert "- direct: blocked — lab.example.test: requests go through a proxy" in out


@pytest.mark.asyncio
async def test_fake_ip_names_of_ordinary_hosts_are_not_downloaded_whatever_doh_says(monkeypatch, dns):
    dns["example.org"] = ["198.18.2.54"]
    hits = []

    async def handler(request):
        hits.append(request.url.host)
        if request.url.host == "cloudflare-dns.com":
            return httpx.Response(200, json={"Status": 0, "Answer": [{"type": 1, "TTL": 60, "data": PUBLIC_IP}]})
        return httpx.Response(200, text='{"secret": 1}', headers={"content-type": "application/json"})

    serve(monkeypatch, handler)
    tavily, firecrawl = extractors(monkeypatch, firecrawl='{"public": 1}')
    out = await fetching.fetch_page("http://example.org/config.json")  # GROK_VERIFY_FAKE_IP=doh (default)
    assert hits == [] and out.startswith("[web_fetch] source=firecrawl") and "direct" not in out.split("\n\n", 1)[0]
    # When everything fails the reason is listed.
    extractors(monkeypatch)
    out = await fetching.fetch_page("http://example.org/config.json")
    assert "attempts=direct:disabled," in out and "fake-ip" in out
