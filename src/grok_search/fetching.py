"""web_fetch backends: direct download, Tavily extract, Firecrawl scrape, the arXiv API, DataCite, snapshots.

Order of backends:
* arXiv abstract pages use the arXiv export API (complete and stable). When it does not answer,
  the page is built from DataCite's record of the paper's DOI (10.48550/arXiv.<id>, free), then
  Firecrawl and, as the last resort, Tavily, whose copy of an abs page has no abstract.
* Text resources (raw code, config and text files; see direct.py) are downloaded directly from the
  origin and returned verbatim. When that is not possible (a proxy or fake-ip TUN decides where an
  ordinary host leads, the download failed, GROK_FETCH_DIRECT=false), Firecrawl (which returns
  text/plain unchanged) is tried before Tavily, whose HTML-to-Markdown conversion damages code; a
  damaged copy is flagged, unless the backend says the URL is an HTML page. When the origin answers
  with a web page (HTML, including an HTML error page), or a URL that only ends in a code-like
  extension answers with an HTTP error or a binary type, it is read as a web page instead
  (www.npmjs.com/package/chart.js is a page, not a JavaScript file); Firecrawl's content type then
  decides whether such a URL is a file. When the origin answers 404 or 410, no extractor is asked:
  they would read the same address.
* Other pages: Firecrawl's live scrape first (GROK_FETCH_PAGE_FIRST=firecrawl, the default), Tavily
  when Firecrawl fails, answers with a bot check, or its copy is shorter than GROK_FETCH_MIN_CHARS;
  GROK_FETCH_PAGE_FIRST=tavily (or no Firecrawl key) keeps the older order, Tavily first, Firecrawl when
  Tavily's copy is short or a bot check. Image markup is removed from page copies
  (GROK_FETCH_STRIP_IMAGES), never from files.

Freshness: Firecrawl is asked for a copy no older than GROK_FETCH_MAX_AGE_S (default 0: always
scraped fresh) and the header reports its cache state. Tavily caches pages per URL and offers no
freshness control (its python.org/downloads copy was over a week old), which the header says as
cache=possible plus a 注意 line.

Paging never refetches: a page that does not fit into one response is kept as
a snapshot, and later pages are cut from exactly that text via snapshot_id.
Once a snapshot has expired the caller has to start again at offset 0.

Backend failures are not swallowed: each one is reported as a ProviderError
(category, HTTP status, redacted detail) in the web_fetch output.

Tavily and Firecrawl run in the cloud, so URLs no cloud service can reach (non-http(s) schemes, no
host, localhost or an IP literal that is not public) are refused locally before any paid call.
Host names are not resolved for this check; the direct download resolves them and passes the
verification layer's destination guard before every request.
"""
import asyncio
import html
import json
import re
import time
import uuid
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from .config import config
from .direct import DIRECT_MAX_BYTES, classify_target, code_degradation, fetch_direct, links_like_a_page
from .errors import PageStatusError, ProviderError, classify_exception
from .interstitial import WALL_MAX_CHARS, is_interstitial, markdown_title, url_key
from .logger import log_info
from . import verify as _verify
from .verify import address_problem, arxiv_get, literal_address

ARXIV_API = "https://export.arxiv.org/api/query"
DIRECT_MAX_MB = DIRECT_MAX_BYTES // (1024 * 1024)
_ARXIV_ABS_RE = re.compile(r"^https?://(?:www\.)?arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/[0-9]{7})(v\d+)?/?(?:[?#].*)?$", re.I)
_ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

# The arXiv fast path has a fallback (the extractors), so it runs within GROK_FETCH_ARXIV_BUDGET_S,
# and after a rate limit, timeout, network or server error the API is skipped for a while: a
# throttled arXiv answers its 429s after 10-45 s, which used to stall every abs fetch for ~43 s.
ARXIV_FETCH_COOLDOWN_S = 120.0
_arxiv_cooldown: dict = {"until": 0.0, "category": ""}
# Rendered abstract pages are kept as long as verification keeps its lookups (ARXIV_CACHE_TTL_S),
# keyed by the requested id (with its version, if any).
ARXIV_ABS_CACHE_MAX = 256
_ARXIV_ABS_CACHE: "OrderedDict[str, tuple[float, str]]" = OrderedDict()


def url_problem(url: str) -> tuple[str, str] | None:
    """(reason, message) when url cannot be fetched by a cloud extractor, else None. Never resolves DNS."""
    shown = (url or "")[:200]
    try:
        parts = urlsplit(url or "")
        host = (parts.hostname or "").rstrip(".")
        parts.port  # noqa: B018 - raises ValueError for an invalid port
    except ValueError as e:
        return "malformed", f"无法解析 {shown!r}（{e}）。请传入完整的 http(s) 地址，例如 https://example.com/page。"
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        if not scheme:
            return "missing_scheme", f"{shown!r} 缺少 http:// 或 https://。请传入完整地址，例如 https://example.com/page。"
        return "unsupported_scheme", f"不支持的协议 {scheme}:（{shown!r}），只能抓取 http(s) 网页。"
    if not host:
        return "missing_host", f"{shown!r} 缺少主机名。请传入完整地址，例如 https://example.com/page。"
    address = literal_address(host)
    if host.lower() == "localhost" or host.lower().endswith(".localhost") or (address is not None and address_problem(address)):
        return "local_address", (
            f"{host} 是本机、局域网或保留地址（loopback / 私有 / 链路本地 / CGNAT）。web_fetch 通过云端服务"
            "（Tavily / Firecrawl）抓取，它们无法访问你的机器或局域网；请改用本地工具读取。"
        )
    return None


async def _call_tavily_extract(url: str) -> str | None:
    """Tavily Extract. Returns the page text; raises ProviderError for every failure."""
    if not config.tavily_enabled:
        raise ProviderError("tavily", "disabled", "TAVILY_ENABLED=false")
    api_key = config.tavily_api_key
    if not api_key:
        raise ProviderError("tavily", "not_configured", "TAVILY_API_KEY is not set")
    endpoint = f"{config.tavily_api_url.rstrip('/')}/extract"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    # format=text was measured worse for code (it also drops "#" from "#include" and turns "*" into "\\"),
    # and extract_depth=advanced returned the same text, so code files go to the direct download and
    # Firecrawl first instead. Tavily has no freshness parameter (docs.tavily.com, Extract endpoint).
    body = {"urls": [url], "format": "markdown"}
    try:
        async with httpx.AsyncClient(timeout=config.tavily_extract_timeout_s) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        raise classify_exception("tavily", e) from e
    if not isinstance(data, dict):
        raise ProviderError("tavily", "parse_error", f"unexpected body type {type(data).__name__}")
    results = data.get("results") or []
    if results and isinstance(results[0], dict):
        content = results[0].get("raw_content") or ""
        if content.strip():
            return content
        raise ProviderError("tavily", "empty_content", "extract returned an empty raw_content")
    failed = data.get("failed_results") or []
    if failed:
        reason = failed[0].get("error") if isinstance(failed[0], dict) else failed[0]
        raise ProviderError("tavily", "extract_failed", str(reason or "listed in failed_results"))
    raise ProviderError("tavily", "empty_content", "extract returned no results")


def _first_text(value) -> str:
    """A metadata field as one line (Firecrawl sometimes sends lists for repeated meta tags)."""
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, str) and v.strip()), "")
    return " ".join(value.split()) if isinstance(value, str) else ""


def _status_code(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _call_firecrawl_scrape(url: str, ctx=None, page: dict | None = None) -> str | None:
    """Firecrawl Scrape; empty pages are retried with a longer waitFor. Raises ProviderError on failure.

    The page's own HTTP status (metadata.statusCode >= 400) is a failure. The final URL after
    redirects, the page title, the content type and the cache state are stored in page, when given.
    maxAge (GROK_FETCH_MAX_AGE_S, default 0) keeps Firecrawl from answering with its cached copy:
    without it Firecrawl serves copies up to 2 days old (https://docs.firecrawl.dev/features/fast-scraping)."""
    api_key = config.firecrawl_api_key
    if not api_key:
        raise ProviderError("firecrawl", "not_configured", "FIRECRAWL_API_KEY is not set")
    endpoint = f"{config.firecrawl_api_url.rstrip('/')}/scrape"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    max_retries = max(1, config.retry_max_attempts)
    max_age_ms = int(config.fetch_max_age_s * 1000)
    if page is not None:
        page["max_age_ms"] = max_age_ms
    for attempt in range(max_retries):
        body = {
            "url": url,
            "formats": ["markdown"],
            "timeout": 60000,
            "waitFor": (attempt + 1) * 1500,
            "maxAge": max_age_ms,
        }
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                response = await client.post(endpoint, headers=headers, json=body)
                response.raise_for_status()
                data = response.json()
        except Exception as e:
            raise classify_exception("firecrawl", e) from e
        if isinstance(data, dict) and data.get("success") is False:
            raise ProviderError("firecrawl", "extract_failed", str(data.get("error") or "success=false"))
        payload = data.get("data") if isinstance(data, dict) else None
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        if isinstance(metadata, dict):
            status = _status_code(metadata.get("statusCode"))
            if status is not None and status >= 400:
                raise PageStatusError("firecrawl", status, _first_text(metadata.get("error")))
            if page is not None:
                page.update(
                    final_url=_first_text(metadata.get("url")), title=_first_text(metadata.get("title")),
                    content_type=_first_text(metadata.get("contentType")),
                    cache_state=_first_text(metadata.get("cacheState")).lower(), cached_at=_first_text(metadata.get("cachedAt")),
                )
        markdown = (payload.get("markdown") or "") if isinstance(payload, dict) else ""
        if markdown.strip():
            return markdown
        await log_info(ctx, f"Firecrawl: markdown为空, 重试 {attempt + 1}/{max_retries}", config.debug_enabled)
    raise ProviderError("firecrawl", "empty_content", f"empty markdown after {max_retries} attempt(s)")


def _arxiv_cached(arxiv_id: str) -> str | None:
    cached = _ARXIV_ABS_CACHE.get(arxiv_id)
    if cached and time.monotonic() - cached[0] < _verify.ARXIV_CACHE_TTL_S:
        return cached[1]
    return None


async def _fetch_arxiv_abs(arxiv_id: str) -> str | None:
    """Abstract page of arxiv_id from the arXiv API; a version suffix (1706.03762v1) is honoured."""
    cached = _arxiv_cached(arxiv_id)
    if cached:
        return cached
    budget = config.fetch_arxiv_budget_s
    try:
        async with httpx.AsyncClient(timeout=budget, follow_redirects=True) as client:
            response = await asyncio.wait_for(arxiv_get(client, {"id_list": arxiv_id, "max_results": 1}), timeout=budget)
    except asyncio.TimeoutError as e:  # the budget (spacing gate, retry and request), not a single read
        raise ProviderError("arxiv-api", "timeout", f"no answer within the {budget:g} s budget of the arXiv fast path") from e
    root = ET.fromstring(response.text)
    entry = root.find("a:entry", _ATOM)
    if entry is None:
        return None
    text = lambda tag, ns="a": re.sub(r"\s+", " ", (entry.findtext(f"{ns}:{tag}", default="", namespaces=_ATOM) or "").strip())
    title = text("title")
    entry_id = entry.findtext("a:id", default="", namespaces=_ATOM) or ""
    if not title or "/abs/" not in entry_id:
        return None
    returned = entry_id.split("/abs/")[-1].strip()
    version = (re.search(r"v\d+$", returned) or [""])[0]
    requested_version = (re.search(r"v\d+$", arxiv_id) or [""])[0]
    if requested_version and version != requested_version:
        raise ProviderError("arxiv-api", "parse_error", f"asked for {arxiv_id}, the API answered with {returned}")
    summary = (entry.findtext("a:summary", default="", namespaces=_ATOM) or "").strip()
    summary = re.sub(r"[ \t]*\n[ \t]*", " ", summary)
    authors = [a.findtext("a:name", default="", namespaces=_ATOM) for a in entry.findall("a:author", _ATOM)]
    categories = [c.get("term", "") for c in entry.findall("a:category", _ATOM) if c.get("term")]
    primary = entry.find("arxiv:primary_category", _ATOM)
    published = text("published")[:10]
    updated = text("updated")[:10]
    comment = text("comment", "arxiv")
    journal = text("journal_ref", "arxiv")
    doi = text("doi", "arxiv")
    lines = [
        "---",
        f"source: https://arxiv.org/abs/{arxiv_id}",
        f"title: {title}",
        "fetched_via: arxiv-api",
        "---",
        "",
        f"# {title}",
        "",
        f"**Authors:** {', '.join(a for a in authors if a)}",
        f"**Published:** {published}" + (f" | **Updated:** {updated}" if updated and updated != published else ""),
        f"**Primary category:** {primary.get('term') if primary is not None else ''}" + (f" | **Categories:** {', '.join(categories)}" if categories else ""),
    ]
    if version:
        lines.insert(3, f"version: {version}" + ("" if requested_version else " (latest)"))
    if comment:
        lines.append(f"**Comments:** {comment}")
    if journal:
        lines.append(f"**Journal reference:** {journal}")
    if doi:
        lines.append(f"**DOI:** {doi}")
    lines += [
        f"**Links:** [abs](https://arxiv.org/abs/{arxiv_id}) | [pdf](https://arxiv.org/pdf/{arxiv_id}) | [html](https://arxiv.org/html/{arxiv_id})",
        "",
        "## Abstract",
        "",
        html.unescape(summary),
        "",
    ]
    page = "\n".join(lines)
    _ARXIV_ABS_CACHE[arxiv_id] = (time.monotonic(), page)
    _ARXIV_ABS_CACHE.move_to_end(arxiv_id)
    while len(_ARXIV_ABS_CACHE) > ARXIV_ABS_CACHE_MAX:
        _ARXIV_ABS_CACHE.popitem(last=False)
    if not requested_version and _verify.extract_arxiv_ids(arxiv_id) == [arxiv_id]:
        # Fill the verification cache too, so verifying this paper next does not query arXiv again.
        _verify._ARXIV_CACHE[arxiv_id] = (time.monotonic(), {
            "id": arxiv_id, "title": title, "published": published, "authors": authors[:3],
        })
    return page


def _datacite_names(creators) -> list[str]:
    names = []
    for creator in creators or []:
        if not isinstance(creator, dict):
            continue
        given, family = creator.get("givenName"), creator.get("familyName")
        name = f"{given} {family}" if isinstance(given, str) and isinstance(family, str) and given and family else creator.get("name")
        if isinstance(name, str) and name.strip():
            names.append(" ".join(name.split()))
    return names


def _datacite_text(items, key: str, kind_key: str, kind: str) -> list[str]:
    return [
        " ".join(item[key].split()) for item in items or []
        if isinstance(item, dict) and isinstance(item.get(key), str) and item[key].strip() and item.get(kind_key) == kind
    ]


async def _fetch_arxiv_datacite(arxiv_id: str) -> str:
    """Abstract page of arxiv_id from DataCite, which registers arXiv's DOIs (10.48550/arXiv.<id>).

    Used when the arXiv API does not answer. DataCite describes only the latest version, so a
    request for another version (1706.03762v1) is refused rather than answered with the wrong text."""
    m = re.search(r"v(\d+)$", arxiv_id)
    requested_version = m.group(1) if m else ""
    base = arxiv_id[:m.start()] if m else arxiv_id
    doi = f"10.48550/arXiv.{base}"
    budget = config.fetch_arxiv_budget_s
    try:
        async with httpx.AsyncClient(timeout=budget, follow_redirects=False, headers={"User-Agent": "grok-search-fetch/1.12"}) as client:
            response = await asyncio.wait_for(_verify._registry_get(
                client, _verify._registry_url(_verify.DATACITE_API, doi), headers={"Accept": "application/vnd.api+json"},
            ), timeout=budget)
    except asyncio.TimeoutError as e:
        raise ProviderError("datacite", "timeout", f"no answer within {budget:g} s") from e
    except _verify.LookupFailed as e:
        raise ProviderError("datacite", "parse_error", str(e)) from e
    if response.status_code == 404:
        raise ProviderError("datacite", "extract_failed", f"DataCite has no record of {doi}", status=404)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    attributes = data.get("attributes") if isinstance(data, dict) else None
    if not isinstance(attributes, dict):
        raise ProviderError("datacite", "parse_error", "record without attributes")
    titles = [t for t in attributes.get("titles") or [] if isinstance(t, dict) and isinstance(t.get("title"), str) and t["title"].strip()]
    main = [t for t in titles if not t.get("titleType")] or titles
    title = " ".join(main[0]["title"].split()) if main else ""
    abstracts = _datacite_text(attributes.get("descriptions"), "description", "descriptionType", "Abstract")
    if not title or not abstracts:
        raise ProviderError("datacite", "empty_content", f"the record of {doi} has no " + ("title" if not title else "abstract"))
    latest = str(attributes.get("version") or "").strip().lstrip("vV")
    if requested_version and latest != requested_version:
        raise ProviderError("datacite", "extract_failed", (
            f"DataCite describes only the latest version (v{latest}), not v{requested_version}" if latest
            else f"DataCite does not say which version it describes (v{requested_version} requested)"
        ))
    authors = _datacite_names(attributes.get("creators"))
    comments = _datacite_text(attributes.get("descriptions"), "description", "descriptionType", "Other")
    subjects = _datacite_text(attributes.get("subjects"), "subject", "subjectScheme", "arXiv")
    submitted = sorted(
        d["date"] for d in attributes.get("dates") or []
        if isinstance(d, dict) and d.get("dateType") == "Submitted" and isinstance(d.get("date"), str)
    )
    published = submitted[0][:10] if submitted else str(attributes.get("publicationYear") or "")
    updated = submitted[-1][:10] if submitted else ""
    lines = [
        "---",
        f"source: https://arxiv.org/abs/{arxiv_id}",
        f"title: {title}",
        "fetched_via: datacite (the arXiv API did not answer; DataCite registers arXiv's DOIs)",
        "---",
        "",
        f"# {title}",
        "",
        f"**Authors:** {', '.join(authors)}",
        f"**Published:** {published}" + (f" | **Updated:** {updated}" if updated and updated != published else ""),
    ]
    if latest:
        lines.insert(3, f"version: v{latest}" + ("" if requested_version else " (latest)"))
    if subjects:
        lines.append(f"**Categories:** {', '.join(subjects)}")
    if comments:
        lines.append(f"**Comments:** {comments[0]}")
    lines += [
        f"**arXiv DOI:** {doi}",
        f"**Links:** [abs](https://arxiv.org/abs/{arxiv_id}) | [pdf](https://arxiv.org/pdf/{arxiv_id}) | [html](https://arxiv.org/html/{arxiv_id})",
        "",
        "## Abstract",
        "",
        html.unescape(abstracts[0]),
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ snapshots
@dataclass
class Snapshot:
    url: str
    text: str
    provider: str
    created: float
    last_used: float
    info: dict = field(default_factory=dict)


class SnapshotStore:
    """Full page texts for paging, evicted after GROK_FETCH_SNAPSHOT_TTL_S idle seconds or by size (LRU)."""

    def __init__(self, max_entries: int = 32, max_total_chars: int = 8_000_000, clock=time.monotonic):
        self.max_entries = max_entries
        self.max_total_chars = max_total_chars
        self._clock = clock
        self._items: "OrderedDict[str, Snapshot]" = OrderedDict()

    def _evict(self, now: float) -> None:
        ttl = config.fetch_snapshot_ttl_s
        for sid in [sid for sid, snap in self._items.items() if now - snap.last_used > ttl]:
            del self._items[sid]
        total = sum(len(snap.text) for snap in self._items.values())
        # The newest snapshot always stays, even when it alone exceeds the size budget.
        while len(self._items) > 1 and (len(self._items) > self.max_entries or total > self.max_total_chars):
            _, snap = self._items.popitem(last=False)
            total -= len(snap.text)

    def put(self, url: str, text: str, provider: str, info: dict | None = None) -> str:
        now = self._clock()
        sid = uuid.uuid4().hex[:12]
        self._items[sid] = Snapshot(url, text, provider, now, now, dict(info or {}))
        self._evict(now)
        return sid

    def get(self, sid: str):
        now = self._clock()
        self._evict(now)
        snap = self._items.get(sid)
        if snap is not None:
            snap.last_used = now
            self._items.move_to_end(sid)
        return snap

    def clear(self) -> None:
        self._items.clear()


SNAPSHOTS = SnapshotStore()


def _same_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


# --------------------------------------------------- redirects and interstitials
# Firecrawl follows redirects and reports the final URL and the page title. A login wall or
# anti-bot check (Zhihu's /account/unhuman "安全验证", Cloudflare's "Just a moment...", Reddit's
# "Prove your humanity") comes back with HTTP 200; interstitial.is_interstitial, which the URL
# checks of the verification layer use too, tells it apart from the requested page.
_WALL_MAX_CHARS = WALL_MAX_CHARS
_url_key = url_key


def _is_interstitial(final_url: str, title: str, redirected: bool, text: str = "", url: str = "") -> bool:
    return is_interstitial(title, text, redirected=redirected, final_url=final_url, url=url)


_CACHE_STATE_RE = re.compile(r"[a-z_]{1,20}")


def _page_info(url: str, page: dict, text: str = "") -> dict:
    """Header fields for a Firecrawl result: content type and cache state, final_url and title after a
    redirect, warning for an interstitial."""
    final_url = page.get("final_url") or ""
    title = page.get("title") or ""
    redirected = bool(final_url) and url_key(final_url) != url_key(url)
    info = {}
    content_type = "".join((page.get("content_type") or "").split())
    if content_type:
        info["content_type"] = content_type[:80]
    cache_state = page.get("cache_state") or ""
    if _CACHE_STATE_RE.fullmatch(cache_state):
        info["cache"] = cache_state
        cached_at = "".join((page.get("cached_at") or "").split())
        if cached_at:
            info["cached_at"] = cached_at[:40]
    elif page.get("max_age_ms") == 0:
        info["cache"] = "bypassed"  # maxAge 0: Firecrawl scraped the page instead of consulting its cache
    if _is_interstitial(final_url, title, redirected, text or "", url):
        info["warning"] = "interstitial"
    if redirected:
        info["final_url"] = final_url
    if title and (redirected or info.get("warning")):
        info["title"] = title[:120]
    return info


def _tavily_info(text: str) -> dict:
    """Header fields for a Tavily result. Tavily serves pages from its own per-URL cache and has no
    freshness parameter, hence cache=possible; it reports no title, so the first heading stands in."""
    info = {"cache": "possible"}
    if is_interstitial(markdown_title(text), text):
        info["warning"] = "interstitial"
    return info


# Markdown images in an extracted web page: badges, icons and logos cost tokens and say nothing but their alt
# text (pypi.org/project/fastapi: 397 images, 41 % of Tavily's 85,000 characters, 325 of them the empty-alt
# "white-cube" icon of each release). A linked image keeps its link with the alt text as the link text, an
# image with alt text becomes "[image: alt]", one without alt text goes. A target may not contain brackets,
# parentheses (one balanced pair aside) or whitespace, so no two attempts scan the same characters.
_IMG_URL = r"(?:[^()\[\]\s]|\([^()\[\]\s]*\))+"
_IMG_TITLE = r"(?:[ \t]+\"[^\"\n]{0,300}\")?"
_LINKED_IMAGE_RE = re.compile(r"\[!\[([^\[\]\n]{0,300})\]\(" + _IMG_URL + _IMG_TITLE + r"\)\]\((" + _IMG_URL + r")" + _IMG_TITLE + r"\)")
_IMAGE_RE = re.compile(r"!\[([^\[\]\n]{0,300})\]\(" + _IMG_URL + _IMG_TITLE + r"\)")
_BLANK_RUN_RE = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")


def strip_images(text: str) -> tuple[str, int]:
    """(text without Markdown image markup, number of images removed); see _IMAGE_RE above."""
    count = 0

    def linked(m: re.Match) -> str:
        nonlocal count
        count += 1
        alt = " ".join(m.group(1).split())
        return f"[{alt}]({m.group(2)})" if alt else ""

    def image(m: re.Match) -> str:
        nonlocal count
        count += 1
        alt = " ".join(m.group(1).split())
        return f"[image: {alt}]" if alt else ""

    text = _IMAGE_RE.sub(image, _LINKED_IMAGE_RE.sub(linked, text or ""))
    return (_BLANK_RUN_RE.sub("\n\n", text) if count else text), count


def _page_result(text: str, provider: str, info: dict) -> tuple[str, str, dict]:
    """An extractor's copy of a web page as web_fetch returns it: images stripped (GROK_FETCH_STRIP_IMAGES)."""
    if config.fetch_strip_images:
        text, removed = strip_images(text)
        if removed:
            info["images_stripped"] = removed
    return text, provider, info


def _add_warning(info: dict, warning: str) -> None:
    warnings = [w for w in (info.get("warning") or "").split(",") if w]
    if warning not in warnings:
        info["warning"] = ",".join(warnings + [warning])


_DEGRADED_TEXT = {
    "collapsed_newlines": "换行被合并",
    "no_indentation": "缩进全部丢失",
    "escaped_underscores": "下划线被转义为 \\_",
    "include_target_stripped": "#include <...> 的目标被当作 HTML 标签删除",
}
_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


def _info_fields(info: dict | None) -> str:
    """Header fields that describe the content itself (placed before attempts=)."""
    if not info:
        return ""
    out = ""
    for key in ("content_type", "charset", "status", "cache", "cached_at", "images_stripped"):
        if info.get(key) not in (None, ""):
            out += f" {key}={info[key]}"
    return out


# Every page Tavily answers carries this line: the header's cache=possible alone was easy to miss.
TAVILY_CACHE_NOTE = (
    "注意: 这是 Tavily 的缓存副本，Tavily 没有新鲜度控制，副本可能是几天甚至几周前的版本；"
    "版本号、发布日期、价格等时效性信息不要只凭此页确认。"
)


def _info_header(info: dict | None) -> str:
    if not info:
        return ""
    out = ""
    warnings = [w for w in (info.get("warning") or "").split(",") if w]
    if warnings:
        out += f" warning={','.join(warnings)}"
    if info.get("degraded"):
        out += f" degraded={info['degraded']}"
    if info.get("final_url"):
        out += " final_url=" + info["final_url"].replace(" ", "%20")
    if info.get("title"):
        out += " title=" + json.dumps(info["title"], ensure_ascii=False)  # last: it may contain spaces
    if "interstitial" in warnings:
        where = f"（被重定向到 {info['final_url']}）" if info.get("final_url") else ""
        out += (
            f"\n注意: 这很可能不是请求页面的正文，而是登录或人机验证拦截页{where}。"
            "该页面需要登录或拦截了抓取，请改用其他来源（例如 web_search）。"
        )
    if "degraded_code" in warnings:
        damage = "、".join(_DEGRADED_TEXT.get(r, r) for r in (info.get("degraded") or "").split(",") if r)
        out += (
            f"\n注意: 这是提取服务把源代码/文本文件当网页转换后的结果，内容已被改写（{damage}），不是原文。"
            "不要据此引用或修改代码；请稍后重试，或改用 raw / commit 固定的链接。"
        )
    if info.get("live_status"):
        out += (f"\n注意: Firecrawl 实时抓取时源站答复 HTTP {info['live_status']}，下面是 Tavily 缓存的旧副本，"
                "该页面现在很可能已经不存在或已移动。")
    if info.get("cache") == "possible":
        out += "\n" + TAVILY_CACHE_NOTE
    if "size_limit" in warnings:
        out += f"\n注意: 文件超过 {DIRECT_MAX_MB} MB，只返回了前 {DIRECT_MAX_MB} MB（在最后一个完整行处截断）。"
    if "decode_errors" in warnings:
        out += (
            f"\n注意: 文件中有按 {info.get('charset') or info.get('content_type') or 'UTF-8'} 无法解码的字节，"
            "已替换为 U+FFFD（�）；这些位置的字符不是原文。"
        )
    if "charset_guess" in warnings:
        out += (
            f"\n注意: 源站没有声明字符集，内容既不是 UTF-8 也不像 GB18030 中文，已按 {info.get('charset') or 'cp1252'}"
            "（西欧文字）解码；如果原文是其他语言，非 ASCII 字符可能显示错误。"
        )
    return out


# CJK text (Han, kana, Hangul, CJK punctuation, full-width forms) costs about one token per
# character, Latin text about a quarter. A default 40,000-character page that is mostly CJK can
# therefore exceed the ~25k-token result limit of MCP clients such as Claude Code, so when more
# than CJK_SHARE_THRESHOLD of the page would be CJK the default page is GROK_FETCH_MAX_CHARS_CJK.
_CJK_RE = re.compile("[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")
CJK_SHARE_THRESHOLD = 0.3


def _default_page_chars(text: str, offset: int) -> int:
    limit = config.fetch_max_chars
    window = text[offset:offset + limit]
    if window and len(window) - len(_CJK_RE.sub("", window)) > CJK_SHARE_THRESHOLD * len(window):
        return min(limit, config.fetch_max_chars_cjk)
    return limit


def _paginate(text: str, provider: str, max_chars: int, offset: int, *, snapshot_id: str = "", cached: bool = False, notes=None, info=None, details=None) -> str:
    total = len(text)
    offset = max(0, offset)
    limit = max_chars if max_chars and max_chars > 0 else _default_page_chars(text, offset)
    piece = text[offset:offset + limit]
    end = offset + len(piece)
    truncated = end < total
    header = f"[web_fetch] source={provider} total_chars={total} offset={offset} returned={len(piece)} truncated={'true' if truncated else 'false'}"
    if truncated:
        header += f" next_offset={end}"
    if snapshot_id:
        header += f" snapshot_id={snapshot_id} expires_in_s={int(config.fetch_snapshot_ttl_s)}"
    if cached:
        header += " cached=true"
    header += _info_fields(info)
    if notes:
        header += " attempts=" + ",".join(notes)
    header += _info_header(info)
    for error in details or ():
        # Why a backend listed in attempts= failed even though another one answered.
        header += "\n- " + error.summary()[:200]
    return header + "\n\n" + piece


def _failure_message(errors: list[ProviderError]) -> str:
    if errors and all(e.skipped for e in errors):
        head = "[web_fetch] error=not_configured"
        title = "配置错误: 没有可用的抓取服务（Tavily 与 Firecrawl 均未启用或未配置）"
    else:
        head = "[web_fetch] error=fetch_failed attempts=" + ",".join(f"{e.provider}:{e.category}" for e in errors)
        page_statuses = list(dict.fromkeys(str(e.page_status) for e in errors if isinstance(e, PageStatusError)))
        if page_statuses:
            head += " page_status=" + ",".join(page_statuses)
        gone = [e for e in errors if _origin_says_gone(e)]
        if gone and len(errors) == len(gone):
            title = (f"源站答复 HTTP {gone[-1].page_status}：该地址不存在。未再调用 Tavily / Firecrawl"
                     "（它们读取同一地址，只会得到同样的结果）；请检查 URL，或用 web_search 查找正确的地址")
        else:
            title = "提取失败: 所有提取服务均未能获取内容"
    return "\n".join([head, "", title] + [f"- {e.summary()}" for e in errors])


async def _try(provider: str, coro, errors: list, notes: list):
    """Await one backend; record its failure (unless it was merely disabled or unconfigured)."""
    try:
        text = await coro
    except Exception as e:
        error = classify_exception(provider, e)
        errors.append(error)
        if not error.skipped:
            notes.append(f"{provider}:{error.category}")
        return None
    if not text or not text.strip():
        errors.append(ProviderError(provider, "empty_content", "no content"))
        notes.append(f"{provider}:empty_content")
        return None
    return text


async def _try_direct(url: str, errors: list, notes: list):
    """The verbatim download of a text resource, or None (failure recorded as direct:<category>; a
    download that was not attempted, DirectSkipped, is kept for a failure message but not noted)."""
    try:
        return await fetch_direct(url)
    except Exception as e:
        error = classify_exception("direct", e)
        errors.append(error)
        if not error.skipped:
            notes.append(f"direct:{error.category}")
        return None


def _direct_info(url: str, page) -> dict:
    info = {"content_type": page.content_type[:80] or "unknown", "status": page.status}
    if page.charset:
        info["charset"] = page.charset  # BOM, XML declaration or guessed: not what Content-Type said
    if page.truncated:
        info["warning"] = "size_limit"
    if page.decode_errors:
        _add_warning(info, "decode_errors")
    if page.charset_guess:
        _add_warning(info, "charset_guess")
    if url_key(page.url) != url_key(url):
        info["final_url"] = page.url  # a blob page rewritten to its raw file, or a redirect
    return info


def _mark_degraded(info: dict, reasons: list[str]) -> None:
    _add_warning(info, "degraded_code")
    info["degraded"] = ",".join(reasons)


async def _extract_page(url: str, ctx, errors: list, notes: list, *, maybe_file: bool = False):
    """A web page (or, with maybe_file, a URL that may be a file): Firecrawl's live scrape first by default,
    Tavily's cached copy first with GROK_FETCH_PAGE_FIRST=tavily or without a Firecrawl key."""
    if config.fetch_page_first == "firecrawl" and config.firecrawl_active:
        return await _extract_page_live(url, ctx, errors, notes, maybe_file=maybe_file)
    return await _extract_page_cached(url, ctx, errors, notes, maybe_file=maybe_file)


# A clean live copy shorter than GROK_FETCH_MIN_CHARS (a stub, or a page whose scripts did not run) gives way to
# Tavily's cached copy only when that is at least this many times longer.
CACHED_COPY_MIN_RATIO = 2


async def _extract_page_live(url: str, ctx, errors: list, notes: list, *, maybe_file: bool = False):
    """Firecrawl first (scraped now: maxAge GROK_FETCH_MAX_AGE_S); Tavily when Firecrawl fails, answers with a bot
    check or damaged code, or with a copy shorter than GROK_FETCH_MIN_CHARS.

    Tavily caches pages without any freshness control: its python.org/downloads copy still called 3.14.8 the
    latest release eight days after 3.15.0 was out. So between two clean copies of a page the live one wins
    unless it is short and Tavily's is at least CACHED_COPY_MIN_RATIO times longer.
    maybe_file: Firecrawl's content type tells a file (anything but HTML, complete at any length and checked
    for code damage) from a page; when Firecrawl failed, Tavily's copy is judged as _extract_page_cached does."""
    page: dict = {}
    firecrawl = await _try("firecrawl", _call_firecrawl_scrape(url, ctx, page), errors, notes)
    live_info: dict = {}
    live_problems: list[str] = []
    as_file = False
    if firecrawl is not None:
        live_info = _page_info(url, page, firecrawl)
        as_file = maybe_file and not _is_html_info(live_info)
        live_problems = _judge(firecrawl, live_info, url, as_file)
        if not live_problems and (as_file or len(firecrawl) >= config.fetch_min_chars):
            notes.append("firecrawl:ok")
            return (firecrawl, "firecrawl", live_info) if as_file else _page_result(firecrawl, "firecrawl", live_info)
        notes.append(f"firecrawl:{live_problems[0]}" if live_problems else f"firecrawl:short({len(firecrawl)})")
    await log_info(ctx, "Firecrawl result missing, short, damaged or a bot check, trying Tavily...", config.debug_enabled)
    tavily = await _try("tavily", _call_tavily_extract(url), errors, notes)
    if tavily is None:
        if firecrawl is None:
            return None, "", {}
        return (firecrawl, "firecrawl", live_info) if as_file else _page_result(firecrawl, "firecrawl", live_info)
    cached_info = _tavily_info(tavily)
    if firecrawl is None:
        # Nothing tells a file from a page but the copy itself: a long copy with page links is a page.
        as_file = maybe_file and not (len(tavily) >= config.fetch_min_chars and links_like_a_page(tavily))
    cached_problems = _judge(tavily, cached_info, url, as_file)
    if firecrawl is None:
        live_error = next((e for e in reversed(errors) if e.provider == "firecrawl"), None)
        if isinstance(live_error, PageStatusError) and live_error.page_status in GONE_STATUSES:
            cached_info["live_status"] = live_error.page_status  # the page is gone now; this copy predates that
        notes.append(f"tavily:{cached_problems[0]}" if cached_problems else "tavily:ok")
        return (tavily, "tavily", cached_info) if as_file else _page_result(tavily, "tavily", cached_info)
    if live_problems or cached_problems:
        # A real page before a bot check, an intact copy before a damaged one, then the live copy.
        candidates = [(live_problems, 0, firecrawl, "firecrawl", live_info), (cached_problems, 1, tavily, "tavily", cached_info)]
        problems, _, text, provider, info = min(candidates, key=lambda c: ("interstitial" in c[0], bool(c[0]), c[1]))
    elif len(tavily) >= max(config.fetch_min_chars, CACHED_COPY_MIN_RATIO * len(firecrawl)):
        text, provider, info = tavily, "tavily", cached_info
    else:
        text, provider, info = firecrawl, "firecrawl", live_info
    notes.append(f"tavily:{cached_problems[0]}" if cached_problems else "tavily:ok" if provider == "tavily" else "tavily:unused")
    return (text, provider, info) if as_file else _page_result(text, provider, info)


async def _extract_page_cached(url: str, ctx, errors: list, notes: list, *, maybe_file: bool = False):
    """A web page: Tavily first; Firecrawl when Tavily's text is missing, short or a bot check
    (GROK_FETCH_PAGE_FIRST=tavily, the order of v1.13.0 and earlier).

    maybe_file: url only ends in a code-like extension and was not downloaded directly (behind a proxy,
    a failed download, GROK_FETCH_DIRECT=false), so it may be a page (en.wikipedia.org/wiki/Node.js,
    www.npmjs.com/package/chart.js, bundlephobia.com/package/chart.js) or a file. It is read like a page,
    Tavily first (one fast call for a page, as in v1.12.0). A copy long enough for a page
    (GROK_FETCH_MIN_CHARS) with few Markdown links is read as the file, complete as it is unless
    code_degradation finds damage, which brings in Firecrawl. A shorter copy cannot be told from a page
    stub (and a short file whose newlines Tavily collapsed looks intact to code_degradation), so
    Firecrawl is asked as for any short page. If Firecrawl reports text/html the URL was a page and the
    copies are compared as pages (no degraded_code); otherwise as a file's (an intact copy first, then
    Firecrawl's, which keeps text/plain unchanged). If Firecrawl fails, the short copy is returned,
    flagged when code_degradation finds damage in it."""
    tavily = await _try("tavily", _call_tavily_extract(url), errors, notes)
    tavily_info = _tavily_info(tavily) if tavily is not None else {}
    tavily_wall = bool(tavily_info.get("warning"))
    long_enough = tavily is not None and len(tavily) >= config.fetch_min_chars
    as_file = maybe_file and long_enough and not links_like_a_page(tavily)
    damage = code_degradation(tavily, url) if as_file else []
    if long_enough and not tavily_wall and not damage:
        return (tavily, "tavily", tavily_info) if as_file else _page_result(tavily, "tavily", tavily_info)
    tavily_note = len(notes)
    if tavily is not None:
        notes.append("tavily:interstitial" if tavily_wall else "tavily:degraded" if damage else f"tavily:short({len(tavily)})")
    await log_info(ctx, "Tavily result missing, too short, damaged or a bot check, trying Firecrawl...", config.debug_enabled)
    page: dict = {}
    firecrawl = await _try("firecrawl", _call_firecrawl_scrape(url, ctx, page), errors, notes)
    if firecrawl is None:
        if maybe_file and tavily is not None and not long_enough:
            damage = code_degradation(tavily, url)  # the short copy may be the file: its damage is flagged
        if damage:
            _mark_degraded(tavily_info, damage)
        if tavily is None:
            return None, "", {}
        if as_file or (maybe_file and not long_enough):
            return tavily, "tavily", tavily_info
        return _page_result(tavily, "tavily", tavily_info)
    firecrawl_info = _page_info(url, page, firecrawl)
    if maybe_file and not _is_html_info(firecrawl_info):
        # A file after all: an intact copy beats a damaged one, and Firecrawl's (text/plain unchanged) goes first.
        if tavily is not None and not as_file:
            damage = code_degradation(tavily, url)  # a short copy, or one with page links, is checked now
        problems = _judge(firecrawl, firecrawl_info, url, True)
        notes.append(f"firecrawl:{problems[0]}" if problems else "firecrawl:ok")
        candidates = [(problems, 0, firecrawl, "firecrawl", firecrawl_info)]
        if tavily is not None:
            candidates.append(((["interstitial"] if tavily_wall else []) + (["degraded"] if damage else []),
                               1, tavily, "tavily", tavily_info))
        problems, _, text, provider, info = min(candidates, key=lambda c: ("interstitial" in c[0], bool(c[0]), c[1]))
        if provider == "tavily" and damage:
            _mark_degraded(info, damage)
        return text, provider, info
    if damage:
        # Firecrawl saw an HTML page: Tavily's long copy was a page too, judged by length as any page.
        del notes[tavily_note]
    firecrawl_wall = bool(firecrawl_info.get("warning"))
    if firecrawl_wall and tavily is not None and not tavily_wall:
        notes.append("firecrawl:interstitial")  # a login wall never replaces a real extract
        return _page_result(tavily, "tavily", tavily_info)
    if tavily is None or (tavily_wall and not firecrawl_wall) or len(firecrawl) > len(tavily):
        notes.append("firecrawl:interstitial" if firecrawl_wall else "firecrawl:ok")
        return _page_result(firecrawl, "firecrawl", firecrawl_info)
    notes.append("firecrawl:shorter")
    return _page_result(tavily, "tavily", tavily_info)


def _is_html_info(info: dict) -> bool:
    return (info.get("content_type") or "").split(";", 1)[0].strip().lower() in _HTML_CONTENT_TYPES


def _judge(text: str, info: dict, url: str, code: bool, strong: bool = False) -> list[str]:
    """Problems of an extractor result: interstitial, and for a code file the damage found in it.

    A result the backend reports as text/html is a web page (npm's /package/chart.js), not a
    converted code file, so its Markdown is not checked for code damage."""
    problems = ["interstitial"] if "interstitial" in (info.get("warning") or "") else []
    if code and not _is_html_info(info):
        reasons = code_degradation(text, url, known_text=strong)
        if reasons:
            _mark_degraded(info, reasons)
            problems.append("degraded")
    return problems


async def _extract_exact(url: str, ctx, errors: list, notes: list, *, code: bool, strong: bool = False):
    """A code file whose direct download failed, or an arXiv abs page whose APIs failed: Firecrawl
    first (it returns text/plain unchanged and has the abstract), Tavily only as the last resort.
    No length threshold applies: a small file or abstract page is complete as it is."""
    candidates = []
    page: dict = {}
    firecrawl = await _try("firecrawl", _call_firecrawl_scrape(url, ctx, page), errors, notes)
    if firecrawl is not None:
        info = _page_info(url, page, firecrawl)
        problems = _judge(firecrawl, info, url, code, strong)
        if _is_html_info(info):
            code = False  # Firecrawl saw an HTML page: Tavily's copy of it is not a code file either
        notes.append(f"firecrawl:{problems[0]}" if problems else "firecrawl:ok")
        if not problems:
            return firecrawl, "firecrawl", info
        candidates.append((problems, 0, firecrawl, "firecrawl", info))
    await log_info(ctx, "Firecrawl result missing or unusable, trying Tavily...", config.debug_enabled)
    tavily = await _try("tavily", _call_tavily_extract(url), errors, notes)
    if tavily is not None:
        info = _tavily_info(tavily)
        problems = _judge(tavily, info, url, code, strong)
        notes.append(f"tavily:{problems[0]}" if problems else "tavily:ok")
        candidates.append((problems, 1, tavily, "tavily", info))
    if not candidates:
        return None, "", {}
    # Prefer a real page over a bot check, an intact copy over a damaged one, then Firecrawl.
    _, _, text, provider, info = min(candidates, key=lambda c: ("interstitial" in c[0], bool(c[0]), c[1]))
    return text, provider, info


# Statuses with which the origin says the URL does not exist (an error page in HTML included).
GONE_STATUSES = frozenset({404, 410})


def _origin_says_gone(error: ProviderError) -> bool:
    """The direct download reached the origin, which answered 404 or 410: no extractor is asked after that."""
    return isinstance(error, PageStatusError) and error.provider == "direct" and error.page_status in GONE_STATUSES


def _answered_as_page(error: ProviderError) -> bool:
    """The direct download reached the origin and got a web page or a non-text type, not a failure."""
    return bool(getattr(error, "html", False)) or error.category == "not_text"


def _still_a_file(error: ProviderError, strong: bool) -> bool:
    """After a failed direct download: whether the URL may still be a file (or is a page).

    An origin that answered with HTML, or with a binary type, is a page (or a document the page
    extractors handle). A URL that only ends in a code-like extension is also read as a page after an
    HTTP error status, as in v1.12.0. Everything else (not attempted because of a proxy, refused
    destination, network, timeout, a strong target's error status) may still be a file: a strong target
    takes the code path (Firecrawl first), a weak one the page path with the file checks
    (_extract_page maybe_file)."""
    if _answered_as_page(error):
        return False
    return strong or not isinstance(error, PageStatusError)


async def _fetch_fresh(url: str, ctx=None):
    errors: list[ProviderError] = []
    notes: list[str] = []
    text: str | None = None
    provider = ""
    info: dict = {}

    m = _ARXIV_ABS_RE.match(url)
    if m:
        arxiv_id = m.group(1) + (m.group(2) or "").lower()
        provider = "arxiv-api"
        wait = _arxiv_cooldown["until"] - time.monotonic()
        if wait > 0 and _arxiv_cached(arxiv_id) is None:
            category = _arxiv_cooldown["category"]
            errors.append(ProviderError("arxiv-api", category, f"skipped: the arXiv API failed recently ({category}); next try in {wait:.0f} s"))
            notes.append("arxiv-api:cooldown")
        else:
            text = await _try("arxiv-api", _fetch_arxiv_abs(arxiv_id), errors, notes)
            if not text and errors[-1].retryable:
                _arxiv_cooldown.update(until=time.monotonic() + ARXIV_FETCH_COOLDOWN_S, category=errors[-1].category)
        if not text:
            await log_info(ctx, f"arXiv API failed, trying DataCite: {errors[-1].summary()}", config.debug_enabled)
            text = await _try("datacite", _fetch_arxiv_datacite(arxiv_id), errors, notes)
            provider = "datacite"
        if not text:
            text, provider, info = await _extract_exact(url, ctx, errors, notes, code=False)
    else:
        target = classify_target(url)
        extract_url, code, maybe_file, direct_error = url, False, False, None
        if target and config.fetch_direct_enabled:
            page = await _try_direct(target.url, errors, notes)
            if page is not None:
                return page.text, "direct", [], errors, _direct_info(url, page)
            direct_error = errors[-1]
            if _origin_says_gone(direct_error):
                # The extractors would read the same address and bill for the same 404.
                return None, "", notes, errors, {}
        # No local download (GROK_FETCH_DIRECT=false, a proxy, a failure) of what may still be a file.
        if target and (direct_error is None or _still_a_file(direct_error, target.strong)):
            if target.strong:
                # Certainly a file: the extractors read the raw URL, Firecrawl first, and damaged copies
                # are flagged (Tavily rewrites text/plain as HTML).
                extract_url, code = target.url, True
            else:
                # Only the extension says file, and most such URLs behind a proxy are pages
                # (en.wikipedia.org/wiki/Node.js): Tavily first, its copy checked for code damage.
                maybe_file = True
        if code:
            text, provider, info = await _extract_exact(extract_url, ctx, errors, notes, code=True,
                                                        strong=bool(target and target.strong))
            if text is not None and extract_url != url and not info.get("final_url"):
                info["final_url"] = extract_url
        else:
            text, provider, info = await _extract_page(url, ctx, errors, notes, maybe_file=maybe_file)
            if text is not None and direct_error is not None and _answered_as_page(direct_error):
                # The origin served a web page and the extractors read it: nothing worth reporting.
                errors.remove(direct_error)
                if f"direct:{direct_error.category}" in notes:
                    notes.remove(f"direct:{direct_error.category}")
    if notes and all(note.endswith(":ok") for note in notes):
        notes = []  # nothing notable happened
    return text, provider, notes, errors, info


async def fetch_page(url: str, max_chars: int = 0, offset: int = 0, ctx=None, snapshot_id: str = "") -> str:
    url = (url or "").strip()
    offset = max(0, int(offset or 0))
    snapshot_id = (snapshot_id or "").strip()

    if snapshot_id:
        snap = SNAPSHOTS.get(snapshot_id)
        if snap is None:
            return (
                f"[web_fetch] error=snapshot_expired snapshot_id={snapshot_id}\n\n"
                "快照不存在或已过期。请去掉 snapshot_id、从 offset=0 重新抓取并从头分页；"
                "重新抓取的页面可能已变化，旧的 offset 不再适用。"
            )
        if url and _same_url(url) != _same_url(snap.url):
            return (
                f"[web_fetch] error=snapshot_mismatch snapshot_id={snapshot_id}\n\n"
                f"该 snapshot_id 属于 {snap.url}，与请求的 URL 不一致。请去掉 snapshot_id、从 offset=0 重新抓取。"
            )
        return _paginate(snap.text, snap.provider, max_chars, offset, snapshot_id=snapshot_id, cached=True, info=snap.info)

    problem = url_problem(url)
    if problem:
        reason, message = problem
        return f"[web_fetch] error=invalid_url reason={reason}\n\n无效的 URL: {message}"

    if offset > 0:
        return (
            "[web_fetch] error=snapshot_required\n\n"
            "offset>0 时必须同时传入首屏返回的 snapshot_id，才能保证各页来自同一份页面内容。"
            "请从 offset=0 开始抓取，再按 next_offset 与 snapshot_id 翻页。"
        )

    text, provider, notes, errors, info = await _fetch_fresh(url, ctx)
    if text is None or (not text and provider != "direct"):  # an empty file downloaded directly is a result
        await log_info(ctx, "Fetch Failed!", config.debug_enabled)
        return _failure_message(errors)

    limit = max_chars if max_chars and max_chars > 0 else _default_page_chars(text, 0)
    sid = SNAPSHOTS.put(url, text, provider, info) if len(text) > limit else ""
    await log_info(ctx, f"Fetch Finished ({provider})!", config.debug_enabled)
    details = [e for e in errors if not e.skipped]
    return _paginate(text, provider, max_chars, 0, snapshot_id=sid, notes=notes, info=info, details=details)
