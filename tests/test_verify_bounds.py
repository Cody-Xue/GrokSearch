"""URL checks of the verification layer read a bounded amount: no gzip bomb, no regex blow-up,
no unread redirect bodies, and the verification timeout always fires."""
import asyncio
import time
import zlib

import httpx
import pytest

from grok_search import verify

PUBLIC_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    table: dict = {}

    async def resolve(host, port):
        value = table.get(host, [PUBLIC_IP])
        if isinstance(value, Exception):
            raise value
        return list(value)

    monkeypatch.setattr(verify, "resolve_host", resolve)
    return table


def mock_verify_client(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(verify.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))


class CountingStream(httpx.AsyncByteStream):
    """A response body that is produced lazily, so tests can see how much of it was read."""

    def __init__(self, chunks):
        self.chunks = chunks
        self.sent = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.sent += 1
            yield chunk


def gzip_bomb(block: bytes, blocks: int):
    """gzip chunks inflating to len(block) * blocks bytes, compressed on demand."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, 31)
    for _ in range(blocks):
        out = compressor.compress(block)
        if out:
            yield out
    yield compressor.flush()


# ------------------------------------------------------------ bounded reads
@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", [b"A", b"<title>"])
async def test_gzip_bomb_is_inflated_only_up_to_the_cap(pattern):
    stream = CountingStream(gzip_bomb(pattern * (1 << 20), 512))  # 512 MB of A or 3.5 GB of <title>

    async def handler(request):
        return httpx.Response(200, headers={"Content-Encoding": "gzip", "Content-Type": "text/html"}, stream=stream)

    started = time.monotonic()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        info = await verify.check_url("https://bomb.example.test/", client)
    assert time.monotonic() - started < 2
    assert info["status"] == 200 and info["title"] == "" and stream.sent <= 3


@pytest.mark.asyncio
async def test_read_capped_counts_decoded_bytes():
    stream = CountingStream(gzip_bomb(b"x" * (1 << 20), 64))

    async def handler(request):
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with client.stream("GET", "https://bomb.example.test/") as response:
            body = await verify._read_capped(response, 65536)
    assert len(body) == 65536


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding, body, title", [
    ("gzip", b"".join(gzip_bomb(b"<html><title>Gz &amp; page</title>", 1)), "Gz & page"),
    ("deflate", zlib.compress(b"<title>Deflated</title>"), "Deflated"),
    ("br", b"\x8b\x02\x80binary", ""),  # not decoded: no title, but the status still counts
])
async def test_content_codings(encoding, body, title):

    async def handler(request):
        assert request.headers["accept-encoding"] == "gzip, deflate"
        return httpx.Response(200, headers={"Content-Encoding": encoding}, stream=CountingStream([body]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        info = await verify.check_url("https://site.example.test/", client)
    assert info == {"url": "https://site.example.test/", "status": 200, "title": title}


@pytest.mark.parametrize("text", [
    "<title" + "a" * 2_000_000,
    "<title>" * 300_000,
    "<TITLE x='" + "y" * 1_000_000,
    "<title>" + "z" * 2_000_000,
])
def test_title_extraction_is_linear_and_bounded(text):
    started = time.monotonic()
    title = verify._extract_title(text)
    assert time.monotonic() - started < 0.2 and len(title) <= 200


def test_title_extraction_reads_normal_pages():
    assert verify._extract_title("<html><head><title lang='en'>\n A &amp; B </title></head>") == "A & B"
    assert verify._extract_title("<html><body>no title</body>") == ""


@pytest.mark.asyncio
async def test_redirect_bodies_are_not_read(monkeypatch):
    redirect_body = CountingStream(gzip_bomb(b"<title>" * (1 << 17), 256))
    hits = []

    async def handler(request):
        hits.append(request.url.path)
        if request.url.path == "/r":
            return httpx.Response(302, headers={"Location": "/ok", "Content-Encoding": "gzip"}, stream=redirect_body)
        return httpx.Response(200, text="<title>Final</title>")

    mock_verify_client(monkeypatch, handler)
    result = await verify.verify_answer("x", [{"url": "https://site.example.test/r"}], check_urls=True, timeout_s=5)
    assert hits == ["/r", "/ok"] and redirect_body.sent == 0
    assert result["urls"] == [{"url": "https://site.example.test/r", "status": 200, "title": "Final"}]


@pytest.mark.asyncio
async def test_redirect_loops_are_lookup_failed(monkeypatch):
    async def handler(request):
        return httpx.Response(302, headers={"Location": "/again"})

    mock_verify_client(monkeypatch, handler)
    result = await verify.verify_answer("x", [{"url": "https://loop.example.test/"}], check_urls=True, timeout_s=5)
    assert result["lookup_failed"] == [{"kind": "url", "value": "https://loop.example.test/", "reason": f"more than {verify.URL_MAX_REDIRECTS} redirects"}]


@pytest.mark.asyncio
async def test_verification_timeout_fires_during_a_slow_body(monkeypatch):
    async def trickle():
        yield b"<html>"
        await asyncio.sleep(30)
        yield b"<title>late</title>"

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            async for chunk in trickle():
                yield chunk

    async def handler(request):
        return httpx.Response(200, stream=SlowStream())

    mock_verify_client(monkeypatch, handler)
    started = time.monotonic()
    result = await verify.verify_answer("x", [{"url": "https://slow.example.test/"}], check_urls=True, timeout_s=0.3)
    assert time.monotonic() - started < 2 and result["timed_out"] is True
    assert result["lookup_failed"] == [{"kind": "url", "value": "https://slow.example.test/", "reason": "timed out"}]
