"""Verbatim download of text resources (source code, configuration, plain text, JSON) for web_fetch.

The cloud extractors run every page through an HTML-to-Markdown converter. For a plain-text file
Tavily turns "#include <assert.h>" into an HTML tag and drops it, collapses newlines, strips all
indentation and escapes "_" as "\\_"; and both extractors may answer from a copy cached days ago.
URLs that name a text resource are therefore downloaded directly from the origin first:

* raw.githubusercontent.com and gist.githubusercontent.com;
* github.com/<owner>/<repo>/(blob|raw)/<ref>/<path>, read as github.com/<owner>/<repo>/raw/<ref>/<path>
  (GitHub redirects that to raw.githubusercontent.com, or to media.githubusercontent.com for a Git LFS
  file, whose raw.githubusercontent.com copy is only the LFS pointer);
* GitLab (/-/raw/, /-/blob/), Codeberg/Gitea (/raw/, /src/) and Bitbucket (/raw/, /src/) file URLs,
  rewritten to their raw form, and any other URL with a /raw/ path segment;
* any URL whose path ends in a text, code or config extension (TEXT_EXTENSIONS), on forges too
  (commit and pull request .patch / .diff, release assets such as checksums.txt).

The first three are files for certain ("strong" targets). An extension alone is weaker evidence
(www.npmjs.com/package/chart.js is a web page): see DirectTarget.

The download is accepted when the Content-Type is text/* (not HTML) or a JSON / XML / YAML / TOML /
JavaScript type, or when a generic type (application/octet-stream, an unknown application/* type, none)
carries a body that looks like text (no NUL bytes, decodable); the bytes are decoded with a BOM, the
declared charset, an XML declaration or, without any of these, UTF-8 and then GB18030 (only when the
result reads as Chinese), and returned unchanged; bytes that are neither are read as Windows-1252 and
flagged warning=charset_guess. Anything else (HTML, binary, an error status, a refused destination) is a ProviderError, and
web_fetch falls back to the extractors.

Safety: this request leaves from the user's machine.
* Every request and every redirect hop passes the verification layer's destination guard (public
  addresses only; for the well-known code hosts fake-ip answers are checked per GROK_VERIFY_FAKE_IP).
* A name is resolved again when the connection is made, so for any other host the address the
  connection actually went to is checked too (DNS rebinding).
* Through a proxy (HTTP(S)_PROXY / ALL_PROXY, system proxy settings) or a fake-ip TUN, the proxy
  resolves the name itself and decides the real destination, which cannot be checked here; only the
  well-known code hosts (TRUSTED_HOSTS: GitHub, GitLab.com, Codeberg, Bitbucket, Gitea.com and the
  jsDelivr / unpkg / npm / PyPI / Rust file hosts) are then downloaded directly, everything else goes
  to the cloud extractors (Firecrawl first).
* The download is capped at DIRECT_MAX_BYTES after decompression, only gzip/deflate are accepted,
  only plain text encodings are decoded (a declared "punycode" or "idna" is ignored), and no API key
  or other credential is ever sent.
  The connected address is checked right after the TCP connect, before TLS and before the request is
  sent, so a name that resolves to a private address only at connection time gets no request at all.
"""
import asyncio
import codecs
import re
import zlib
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import config
from .errors import PageStatusError, ProviderError, classify_exception
from . import verify as _verify

DIRECT_MAX_BYTES = 5 * 1024 * 1024
DIRECT_MAX_REDIRECTS = 5
DIRECT_TIMEOUT_S = 30.0
DIRECT_USER_AGENT = "grok-search-fetch/1.12"

TEXT_EXTENSIONS = frozenset("""
    py pyi pyx pxd h hh hpp hxx c cc cpp cxx m mm rs go java kt kts scala groovy gradle cs fs swift dart
    js mjs cjs jsx ts tsx vue svelte css scss less rb pl pm lua r jl ex exs erl hrl hs ml mli clj cljs el
    nim zig sol v vhd sv proto graphql gql thrift sh bash zsh fish ps1 psm1 bat cmd cmake mk
    toml ini cfg conf properties env yaml yml json jsonc json5 ndjson ipynb xml xsd plist
    md markdown rst adoc tex bib txt text csv tsv sql lock patch diff log po pot srt vtt
    tf tfvars hcl nix dockerfile makefile editorconfig gitignore gitattributes
""".split())
# The JSON / XML / YAML / TOML / JavaScript media types accepted besides text/* (text/html excluded).
_TEXT_TYPES = frozenset({
    "application/json", "application/ld+json", "application/x-ndjson", "application/xml",
    "application/javascript", "application/x-javascript", "application/ecmascript",
    "application/yaml", "application/x-yaml", "application/toml", "application/x-toml",
    "application/x-sh", "application/x-shellscript", "application/sql", "application/x-ipynb+json",
    "application/x-tex", "application/x-python", "application/x-httpd-php",
})
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
# Types whose body is never sniffed for text (application/octet-stream and unknown application/* are).
_BINARY_PREFIXES = ("image/", "audio/", "video/", "font/", "model/", "multipart/")
_BINARY_TYPES = frozenset({
    "application/pdf", "application/zip", "application/gzip", "application/x-gzip", "application/x-tar",
    "application/x-bzip2", "application/x-xz", "application/x-7z-compressed", "application/x-rar-compressed",
    "application/vnd.rar", "application/java-archive", "application/wasm", "application/x-msdownload",
    "application/x-executable", "application/x-sharedlib", "application/x-mach-binary", "application/msword",
    "application/vnd.android.package-archive", "application/x-iso9660-image", "application/x-apple-diskimage",
    "application/vnd.debian.binary-package", "application/x-rpm", "application/epub+zip", "application/ogg",
    "application/x-shockwave-flash", "application/x-sqlite3", "application/vnd.sqlite3",
})
SNIFF_BYTES = 8192

# Hosts whose name cannot be pointed at the user's machine by a third party (code forges and the package
# registries' file hosts): the only ones downloaded directly when a proxy or a fake-ip TUN, not this
# process, decides where a name leads. What they serve may come from anyone; where the name leads does not.
TRUSTED_HOSTS = frozenset({
    "github.com", "www.github.com", "gist.github.com", "gitlab.com", "codeberg.org", "bitbucket.org", "gitea.com",
    "cdn.jsdelivr.net", "unpkg.com", "registry.npmjs.org", "files.pythonhosted.org", "static.rust-lang.org",
    "static.crates.io",
})
_TRUSTED_SUFFIXES = (".githubusercontent.com",)  # raw., gist., media., objects., patch-diff., release-assets.

_GITHUB_FILE_RE = re.compile(r"^/([^/]+)/([^/]+)/(?:blob|raw)/(.+)$")
_GITLAB_FILE_RE = re.compile(r"^(/.+?)/-/(?:blob|raw)/(.+)$")
_GITEA_FILE_RE = re.compile(r"^/([^/]+)/([^/]+)/(?:src|raw)/(branch|tag|commit)/(.+)$")
_BITBUCKET_FILE_RE = re.compile(r"^/([^/]+)/([^/]+)/(?:src|raw)/(.+)$")


@dataclass
class DirectPage:
    text: str
    url: str  # the URL the content came from (after rewrite and redirects)
    status: int
    content_type: str
    truncated: bool = False
    charset: str = ""  # the codec used, when it is not the one Content-Type declared (BOM, XML, guessed)
    decode_errors: bool = False  # bytes that are invalid in that codec were replaced with U+FFFD
    charset_guess: bool = False  # no charset declared and neither UTF-8 nor Chinese: read as Windows-1252


@dataclass(frozen=True)
class DirectTarget:
    """What web_fetch downloads verbatim for a URL.

    strong: a raw host, a forge file page or a /raw/ path, so certainly a file. A URL that only ends in
    a code-like extension may still be a web page (npm's /package/chart.js, Wikipedia's /wiki/Node.js).
    """
    url: str
    strong: bool


def _extension(path: str) -> str:
    name = path.rstrip("/").rsplit("/", 1)[-1].lower()
    if name in ("dockerfile", "makefile"):
        return name
    return name.rsplit(".", 1)[-1] if "." in name.strip(".") else ""


def trusted_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return host in TRUSTED_HOSTS or host.endswith(_TRUSTED_SUFFIXES)


def classify_target(url: str) -> DirectTarget | None:
    """The URL to download verbatim for url (file pages rewritten to their raw form), or None.

    None means url does not name a text resource and the extractors handle it as a web page.
    """
    try:
        parts = urlsplit((url or "").strip())
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    path = parts.path
    origin = f"{parts.scheme.lower()}://{parts.netloc}"
    keep = urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
    segments = [segment for segment in path.split("/") if segment]
    if host in ("raw.githubusercontent.com", "gist.githubusercontent.com"):
        return DirectTarget(keep, True)
    # A forge URL that is not a file page falls through to the /raw/ and extension rules below, but only
    # inside a repository (github.com/videojs/video.js is the repository page, not a file).
    in_repo = len(segments) >= 3
    if host in ("github.com", "www.github.com"):
        m = _GITHUB_FILE_RE.match(path)
        if m:
            # ?plain=1 and #L10 belong to the HTML viewer. GitHub answers .../raw/... from raw.githubusercontent.com,
            # or for a Git LFS file from media.githubusercontent.com (the raw host only has the LFS pointer).
            return DirectTarget(f"https://github.com/{m.group(1)}/{m.group(2)}/raw/{m.group(3)}", True)
    elif host == "gitlab.com" or host.startswith("gitlab."):
        m = _GITLAB_FILE_RE.match(path)
        if m:
            return DirectTarget(f"{origin}{m.group(1)}/-/raw/{m.group(2)}", True)
        in_repo = "/-/" in path  # GitLab groups nest, so only /-/ marks a URL inside a project
    elif host in ("codeberg.org", "gitea.com") or host.startswith("gitea."):
        m = _GITEA_FILE_RE.match(path)
        if m:
            return DirectTarget(f"{origin}/{m.group(1)}/{m.group(2)}/raw/{m.group(3)}/{m.group(4)}", True)
    elif host == "bitbucket.org":
        m = _BITBUCKET_FILE_RE.match(path)
        if m:
            return DirectTarget(f"{origin}/{m.group(1)}/{m.group(2)}/raw/{m.group(3)}", True)
    else:
        in_repo = True
    if not in_repo:
        return None
    if "/raw/" in path:
        return DirectTarget(keep, True)
    if _extension(path) in TEXT_EXTENSIONS:
        return DirectTarget(keep, False)
    return None


def direct_target(url: str) -> str | None:
    """The URL classify_target downloads for url, or None."""
    target = classify_target(url)
    return target.url if target else None


def accepted_type(content_type: str) -> bool:
    """Whether a Content-Type names a plain-text resource that is returned verbatim."""
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if not mime or mime in _HTML_TYPES:
        return False
    if mime.startswith("text/") or mime in _TEXT_TYPES:
        return True
    return mime.startswith("application/") and mime.endswith(("+json", "+xml", "+yaml", "+toml"))


def _sniffable(mime: str) -> bool:
    """A generic or unknown type whose body may still be a text file (nginx/S3 serve .toml or .rs as
    application/octet-stream, Fossil serves raw files as application/x-fossil-artifact)."""
    if not mime:
        return True
    if mime.startswith(_BINARY_PREFIXES) or mime in _BINARY_TYPES:
        return False
    return mime in ("application/octet-stream", "binary/octet-stream") or (
        mime.startswith("application/") and not mime.startswith(("application/vnd.ms-", "application/vnd.openxmlformats"))
    )


_CONTROL_BYTES = frozenset(range(32)) - {9, 10, 12, 13, 27}


def looks_textual(sample: bytes, charset: str | None = None) -> bool:
    """Whether the start of a body is text: no NUL bytes, few control bytes, decodable as declared or UTF-8/GB18030."""
    if not sample or sample.startswith((codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return True
    if b"\x00" in sample or sum(byte in _CONTROL_BYTES for byte in sample) > len(sample) // 100:
        return False
    for codec in dict.fromkeys(c for c in (_verify.text_charset(charset), "utf-8", "gb18030") if c):
        try:
            codecs.getincrementaldecoder(codec)("strict").decode(sample, final=False)  # the sample may end mid-character
            return True
        except UnicodeDecodeError:
            continue
    return False


async def _read_body(response: httpx.Response, max_bytes: int, sniff: str | None = None) -> tuple[bytes, bool]:
    """(decoded body, complete). At most max_bytes after gzip/deflate decompression; other codings fail.

    sniff: the response's type when the body must look like text (looks_textual on its first
    SNIFF_BYTES); the download stops with not_text as soon as it does not."""
    checked = sniff is None

    def check(data: bytes) -> None:
        if not looks_textual(data[:SNIFF_BYTES], response.charset_encoding):
            raise ProviderError("direct", "not_text", f"{sniff or 'no content type'} with a binary body")

    if response.is_stream_consumed:  # body already loaded (in-memory transports)
        content = response.content
        if not checked:
            check(content)
        return content[:max_bytes], len(content) <= max_bytes
    coding = (response.headers.get("content-encoding") or "identity").strip().lower()
    if coding in ("", "identity"):
        decoder = None
    elif coding in ("gzip", "x-gzip", "deflate"):
        decoder = zlib.decompressobj(zlib.MAX_WBITS | 32)  # zlib or gzip header, detected
    else:
        raise ProviderError("direct", "parse_error", f"unsupported content-encoding {coding!r}")
    out = bytearray()
    raw_total = 0
    async for chunk in response.aiter_raw():
        raw_total += len(chunk)
        data = chunk
        if decoder is None:
            out += data
        else:
            try:
                while data and len(out) <= max_bytes and not decoder.eof:
                    out += decoder.decompress(data, max_bytes + 1 - len(out))
                    data = decoder.unconsumed_tail
            except zlib.error as e:
                raise ProviderError("direct", "parse_error", f"corrupt {coding} body: {e}") from e
        if not checked and len(out) >= SNIFF_BYTES:
            check(bytes(out[:SNIFF_BYTES]))
            checked = True
        if len(out) > max_bytes:
            return bytes(out[:max_bytes]), False
        if decoder is not None and decoder.eof:
            break
        if raw_total > max_bytes * 2:  # compressed data that inflates to (almost) nothing
            raise ProviderError("direct", "parse_error", f"more than {2 * max_bytes} bytes on the wire")
    if decoder is not None and not decoder.eof and out:
        raise ProviderError("direct", "parse_error", f"truncated {coding} body")
    if not checked:
        check(bytes(out[:SNIFF_BYTES]))
    return bytes(out), True


_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"),
)
_XML_ENCODING_RE = re.compile(rb"<\?xml[^<>]{0,200}?encoding\s*=\s*[\"']([A-Za-z0-9._-]{1,40})[\"']")


_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")
_GB_SAMPLE_CHARS = 4000


def _reads_as_chinese(text: str) -> bool:
    """Whether text decoded as GB18030 is Chinese rather than misread Western text.

    GB18030 accepts most byte sequences: Latin-1 "Renée Müller" decodes without an error as "Ren閑 M黮ler".
    Chinese text is mostly in the GB2312 area (both bytes 0xA1-0xFE: common hanzi and full-width
    punctuation); a misread Western text has an ASCII letter as the second byte, and misread Big5 text
    hits GB18030's user-defined (private-use) areas, which real text almost never uses."""
    wide = _NON_ASCII_RE.findall(text, 0, 400_000)[:_GB_SAMPLE_CHARS]
    if not wide:
        return True
    common = private = 0
    for char in wide:
        if "\ue000" <= char <= "\uf8ff":
            private += 1
            continue
        raw = char.encode("gb18030")
        common += len(raw) == 2 and raw[0] >= 0xA1 and raw[1] >= 0xA1
    return common * 2 >= len(wide) and private * 100 <= len(wide)


def _decode(body: bytes, charset: str | None, complete: bool = True) -> tuple[str, str, bool, bool]:
    """(text, codec, lossy, guessed): a BOM first, then the declared charset; without one an XML
    declaration, UTF-8 and GB18030 (when the result reads as Chinese) are tried strictly, then UTF-8 with
    replacements when most of its multi-byte characters are valid, then Windows-1252 (Latin-1 for its
    five unassigned bytes). lossy: undecodable bytes were replaced (U+FFFD). guessed: the Windows-1252
    fallback, which is right for Western European text and wrong for other languages.

    Only plain text encodings are used (verify.text_charset): a declared "punycode" would take minutes
    on the event loop. An incomplete body (size limit) may end in the middle of a character."""
    def strict(codec: str) -> str:
        return codecs.getincrementaldecoder(codec)("strict").decode(body, final=complete)

    def replace(codec: str) -> str:
        return codecs.getincrementaldecoder(codec)("replace").decode(body, final=complete)

    candidates = [codec for bom, codec in _BOMS if body.startswith(bom)][:1]
    declared = _verify.text_charset(charset)
    if candidates or declared:
        codec = (candidates or [declared])[0]
        try:
            return strict(codec), codec, False, False
        except UnicodeDecodeError:
            return replace(codec), codec, True, False
    m = _XML_ENCODING_RE.match(body[:300])
    xml = _verify.text_charset(m.group(1).decode("ascii")) if m else None
    for codec in dict.fromkeys(c for c in (xml, "utf-8", "gb18030") if c):
        try:
            text = strict(codec)
        except UnicodeDecodeError:
            continue
        if codec == "gb18030" and xml != codec and not _reads_as_chinese(text):
            continue
        return text, codec, False, False
    if xml:
        return replace(xml), xml, True, False
    text = replace("utf-8")
    broken = text.count("\ufffd")
    if len(_NON_ASCII_RE.findall(text)) - broken > broken:
        return text, "utf-8", True, False  # UTF-8 with a few broken bytes
    try:
        return strict("cp1252"), "cp1252", False, True
    except UnicodeDecodeError:
        return strict("iso8859-1"), "iso8859-1", False, True


def _stream_problem(stream, host: str) -> str:
    """Why a connection to host may not be used (empty when the address it went to is public)."""
    try:
        address = stream.get_extra_info("server_addr") if stream is not None else None
    except Exception:
        address = None
    ip = address[0] if isinstance(address, (tuple, list)) and address else None
    if not ip:
        return f"{host}: the address of the connection could not be checked"
    try:
        kind = _verify.address_problem(ip)
    except ValueError:
        kind = "unparsable"
    if kind:
        return f"{host}: the connection went to {ip}, which is not a public address (the name resolved differently when it was checked)"
    return ""


def _connect_check(host: str):
    """httpcore trace hook: after the TCP connect, before TLS and before any byte of the request, the
    address the connection actually went to must be public (the guard's lookup and the connection's
    own lookup can differ: DNS rebinding)."""
    async def trace(event: str, info: dict) -> None:
        if event != "connection.connect_tcp.complete":
            return
        stream = info.get("return_value")
        problem = _stream_problem(stream, host)
        if problem:
            try:
                await stream.aclose()
            except Exception:
                pass
            raise ProviderError("direct", "blocked", problem)
    return trace


def _peer_problem(response: httpx.Response) -> str:
    """The same check on a response, before its body is read (a reused or otherwise unchecked connection)."""
    host = response.request.url.host
    if trusted_host(host):
        return ""
    return _stream_problem(response.extensions.get("network_stream"), host)


def _html_error(status: int, mime: str) -> ProviderError:
    """The origin answered with a web page (error pages included): not a text file."""
    if status >= 400:
        error = PageStatusError("direct", status, f"{mime} page")
    else:
        error = ProviderError("direct", "not_text", f"{mime} is not a plain-text resource")
    error.html = True
    return error


async def _send(client: httpx.AsyncClient, request: httpx.Request) -> httpx.Response:
    response = await client.send(request, stream=True, follow_redirects=False)
    problem = _peer_problem(response)
    if problem:
        await response.aclose()
        raise ProviderError("direct", "blocked", problem)
    return response


async def _download(client: httpx.AsyncClient, url: str) -> DirectPage:
    response = await _send(client, client.build_request("GET", url))
    try:
        hops = 0
        while response.next_request is not None:
            if hops >= DIRECT_MAX_REDIRECTS:
                raise ProviderError("direct", "http_error", f"more than {DIRECT_MAX_REDIRECTS} redirects")
            next_request = response.next_request
            await response.aclose()
            hops += 1
            response = await _send(client, next_request)
        status = response.status_code
        content_type = response.headers.get("content-type", "")
        mime = content_type.split(";", 1)[0].strip().lower()
        if mime in _HTML_TYPES:
            raise _html_error(status, mime)  # a web page or an HTML error page (Cloudflare's 403)
        if status >= 400:
            raise PageStatusError("direct", status)
        if not 200 <= status < 300:
            raise ProviderError("direct", "http_error", f"HTTP {status}", status=status)
        if accepted_type(content_type):
            sniff = None
        elif _sniffable(mime):
            sniff = mime or "no content type"
        else:
            raise ProviderError("direct", "not_text", f"{mime} is not a plain-text resource")
        body, complete = await _read_body(response, DIRECT_MAX_BYTES, sniff)
        final_url = str(response.url)
        charset = response.charset_encoding
    finally:
        await response.aclose()
    if b"\x00" in body[:8192] and not body.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) and not (charset or "").lower().startswith("utf-16"):
        raise ProviderError("direct", "not_text", "binary content (NUL bytes) despite a text content type")
    if not complete:
        cut = body.rfind(b"\n")
        body = body[:cut + 1] if cut > len(body) // 2 else body
    text, codec, lossy, guessed = _decode(body, charset, complete=complete or body.endswith(b"\n"))
    shown = "" if codec.removesuffix("-sig") == (_verify.text_charset(charset) or "utf-8") else codec
    return DirectPage(text, final_url, status, "".join(content_type.split()), truncated=not complete,
                      charset=shown, decode_errors=lossy, charset_guess=guessed)


_LFS_POINTER_RE = re.compile(r"version https://git-lfs\.github\.com/spec/v1\noid sha256:[0-9a-f]{64}\nsize \d+\n?")


def _lfs_url(page: DirectPage) -> str | None:
    """github.com's /raw/ URL for a Git LFS pointer served by raw.githubusercontent.com, else None."""
    parts = urlsplit(page.url)
    if (parts.hostname or "").lower() != "raw.githubusercontent.com" or not _LFS_POINTER_RE.fullmatch(page.text):
        return None
    owner_repo_rest = parts.path.lstrip("/").split("/", 2)
    if len(owner_repo_rest) < 3:
        return None
    owner, repo, rest = owner_repo_rest
    return f"https://github.com/{owner}/{repo}/raw/{rest}"


def _through_proxy(client: httpx.AsyncClient, url: httpx.URL) -> bool:
    """Whether client sends a request for url through a proxy (HTTP(S)_PROXY / ALL_PROXY / system settings)."""
    try:
        return client._transport_for_url(url) is not client._transport
    except Exception:  # httpx internals changed: assume a proxy (then only trusted hosts are downloaded)
        return True


class DirectSkipped(ProviderError):
    """The direct download was not attempted: a proxy or fake-ip TUN decides where the name leads."""

    def __init__(self, detail: str):
        super().__init__("direct", "disabled", detail)


def _hop_guard(client: httpx.AsyncClient, fake_ip: str, doh):
    async def guard(request: httpx.Request) -> None:
        """httpx request hook: runs before the first request and before every redirect hop."""
        url = request.url
        host = url.raw_host.decode("ascii", "replace")
        port = url.port or (443 if url.scheme == "https" else 80)
        if trusted_host(host):
            request.extensions.pop("trace", None)
            await _verify.check_destination(host, port, fake_ip, doh)
            return
        if _through_proxy(client, url):
            raise ProviderError("direct", "blocked", f"{host}: requests go through a proxy, which resolves the name itself; "
                                "only well-known code hosts are downloaded directly through a proxy")
        # A fake-ip answer means a TUN-mode proxy picks the real destination: refused whatever GROK_VERIFY_FAKE_IP says,
        # since DNS-over-HTTPS cannot tell what the proxy's own resolver answers.
        await _verify.check_destination(host, port, "skip")
        request.extensions["trace"] = _connect_check(host)
    return guard


async def fetch_direct(url: str) -> DirectPage:
    """Download url verbatim (see the module docstring). Raises ProviderError on every failure, and
    DirectSkipped (category disabled) when the download is not attempted at all."""
    fake_ip = config.verify_fake_ip
    timeout = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
    headers = {
        "User-Agent": DIRECT_USER_AGENT,
        "Accept": "text/plain, text/*;q=0.9, application/json;q=0.9, */*;q=0.5",
        "Accept-Encoding": "gzip, deflate",
    }
    first_host = ""
    try:
        first = httpx.URL(url)
        first_host = first.raw_host.decode("ascii", "replace")  # as the guard and BlockedAddress name it
        async with httpx.AsyncClient(timeout=httpx.Timeout(_verify.DOH_TIMEOUT_S)) as doh_client:
            doh = _verify.DohResolver(config.verify_doh_url, doh_client) if fake_ip == "doh" else None
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=headers) as client:
                if not trusted_host(first_host) and _through_proxy(client, first):
                    raise DirectSkipped(f"{first_host} is reached through a proxy, which resolves the name itself; "
                                        "only well-known code hosts are downloaded directly through a proxy")
                client.event_hooks = {"request": [_hop_guard(client, fake_ip, doh)]}

                async def run() -> DirectPage:
                    page = await _download(client, url)
                    lfs = _lfs_url(page)
                    return await _download(client, lfs) if lfs else page

                return await asyncio.wait_for(run(), timeout=DIRECT_TIMEOUT_S)
    except ProviderError:
        raise
    except _verify.BlockedAddress as e:
        if e.kind == "fake_ip" and e.host.lower() == first_host.lower() and not trusted_host(first_host):
            raise DirectSkipped(f"{e} (only well-known code hosts are downloaded directly then)") from e
        raise ProviderError("direct", "blocked", str(e)) from e
    except _verify.ResolveFailed as e:
        raise ProviderError("direct", "network", f"dns lookup failed: {e}") from e
    except asyncio.TimeoutError as e:
        raise ProviderError("direct", "timeout", f"no complete answer within {DIRECT_TIMEOUT_S:g} s") from e
    except Exception as e:
        raise classify_exception("direct", e) from e


# ------------------------------------------------------------ extractor fidelity
# When the direct download is not possible, an extractor's copy of a code file is checked for the
# damage Tavily's HTML-to-Markdown conversion does to plain text (measured on real files: Python.h,
# verify.py, logger.py, grok.py). Firecrawl returns text/plain resources unchanged.
_LINE_ORIENTED = frozenset("""
    py pyi h hh hpp c cc cpp cxx rs go java kt kts scala cs swift dart js mjs cjs jsx ts tsx rb pl lua r jl
    ex exs hs ml nim zig sh bash zsh ps1 cmake mk toml ini cfg conf yaml yml md markdown rst sql gradle
    dockerfile makefile proto
""".split())
_INDENTED = frozenset("""
    py pyi rs go java kt kts scala cs swift dart js mjs cjs jsx ts tsx c cc cpp cxx rb lua jl ex exs hs ml
    nim zig yaml yml php
""".split())
# Files that may legitimately be one long line (minified data, generated maps).
_ONE_LINE_OK = frozenset("json jsonc json5 ndjson map svg xml csv tsv lock".split())
COLLAPSED_LINE_CHARS = 2000  # a line this long in a line-oriented source file
COLLAPSED_ANY_CHARS = 20000  # a line this long in any other known text file (sqlite.h.in served as one line)
_INCLUDE_STRIPPED_RE = re.compile(r"^#\s*include\s*(?://.*)?$", re.M)


def code_degradation(text: str, url: str, *, known_text: bool = False) -> list[str]:
    """Reasons why text does not look like the verbatim file at url (empty when it looks intact).

    known_text: url is certainly a text file (raw host, forge file, /raw/ path), so a line of
    COLLAPSED_ANY_CHARS counts as collapsed whatever the extension (minified and data files excepted)."""
    try:
        path = urlsplit(url).path
    except ValueError:
        path = ""
    ext = _extension(path)
    minified = ".min." in path.rsplit("/", 1)[-1].lower()
    lines = [line for line in (text or "").split("\n") if line.strip()]
    longest = max((len(line) for line in lines), default=0)
    reasons = []
    if not minified and (
        (ext in _LINE_ORIENTED and longest > COLLAPSED_LINE_CHARS)
        or (known_text and ext not in _ONE_LINE_OK and longest > COLLAPSED_ANY_CHARS)
    ):
        reasons.append("collapsed_newlines")
    if ext in _INDENTED and len(lines) >= 30 and not any(line[:1] in (" ", "\t") for line in lines):
        reasons.append("no_indentation")
    escaped = text.count("\\_")
    if escaped >= max(3, text.count("_") // 2):
        reasons.append("escaped_underscores")
    if _INCLUDE_STRIPPED_RE.search(text):
        reasons.append("include_target_stripped")
    return reasons


# ------------------------------------------------------------ extension-only URLs read by an extractor
# A URL that only ends in a code-like extension (a weak target) may be a web page. An extractor's copy
# long enough for a page is taken for the file unless it links to other pages as a rendered page does; a
# shorter copy cannot be told from a page stub (bundlephobia.com/package/chart.js: 521 characters), so
# Firecrawl is asked as for any short page and its content type decides (fetching._extract_page).
_MD_LINK_RE = re.compile(r"\]\((?:https?://|/|#)")
_PAGE_MIN_LINKS = 3


def links_like_a_page(text: str) -> bool:
    """Whether an extractor's Markdown links to other pages as a rendered web page does (a plain-text
    file read by Tavily has no Markdown links)."""
    return len(_MD_LINK_RE.findall(text[:20000])) >= _PAGE_MIN_LINKS
