"""web_fetch backends: Tavily extract, Firecrawl scrape, the arXiv API, and page snapshots.

A result is only accepted from the fast path (Tavily) when it is long enough
to be a real page; otherwise the slower Firecrawl path runs and the longer
of the two is returned. arXiv abstract pages bypass both and use the arXiv
export API, whose output is complete and stable.

Paging never refetches: a page that does not fit into one response is kept as
a snapshot, and later pages are cut from exactly that text via snapshot_id.
Once a snapshot has expired the caller has to start again at offset 0.

Backend failures are not swallowed: each one is reported as a ProviderError
(category, HTTP status, redacted detail) in the web_fetch output.

Every backend runs in the cloud, so URLs no cloud service can reach (non-http(s)
schemes, no host, localhost or an IP literal that is not public) are refused
locally before any paid call. Host names are not resolved for this check.
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
from .errors import ProviderError, classify_exception
from .logger import log_info
from . import verify as _verify
from .verify import address_problem, arxiv_get, literal_address

ARXIV_API = "https://export.arxiv.org/api/query"
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


class PageStatusError(ProviderError):
    """The backend reached the page, but the page itself answered with an HTTP error status."""

    def __init__(self, provider: str, status: int, detail: str = ""):
        category = "rate_limit" if status == 429 else "upstream_error" if status >= 500 else "extract_failed"
        text = f"target page answered HTTP {status}" + (f" ({detail})" if detail else "")
        super().__init__(provider, category, text, status=status)
        self.page_status = status


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
    redirects and the page title are stored in page, when given."""
    api_key = config.firecrawl_api_key
    if not api_key:
        raise ProviderError("firecrawl", "not_configured", "FIRECRAWL_API_KEY is not set")
    endpoint = f"{config.firecrawl_api_url.rstrip('/')}/scrape"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    max_retries = max(1, config.retry_max_attempts)
    for attempt in range(max_retries):
        body = {
            "url": url,
            "formats": ["markdown"],
            "timeout": 60000,
            "waitFor": (attempt + 1) * 1500,
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
                page.update(final_url=_first_text(metadata.get("url")), title=_first_text(metadata.get("title")))
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
# anti-bot check (Zhihu's /account/unhuman "安全验证", Cloudflare's "Just a moment...") comes
# back with HTTP 200, so only these two signals tell it apart from the requested page.
_WALL_SEGMENTS = {"login", "log-in", "signin", "sign-in", "sign_in", "logon", "servicelogin", "unhuman", "captcha"}
_WALL_HOST_LABELS = {"login", "signin", "passport", "sso"}
_WALL_QUERY_RE = re.compile(r"(?:^|&)(?:need_login|captcha)=", re.I)
# Matched against the whole title up to the first " - " / " | " / "_" site suffix, so an article
# about captchas ("如何实现短信验证码登录 - 知乎") is not taken for a captcha page.
_WALL_TITLE_RE = re.compile(
    r"(?:百度)?安全验证|\S{0,4}验证码\S{0,2}|人机(?:身份)?验证|环境异常|访问异常|访问验证|unhuman|captcha|"
    r"just a moment\W*|attention required\W*|are you a robot\W*|verify you are human\W*|access denied\W*|security check\W*",
    re.I)
_TITLE_SUFFIX_RE = re.compile(r"\s+[-|–—·]\s+|_")
_LOGIN_TITLE_RE = re.compile(r"(?:log ?in|sign ?in)\b.*|\S{0,6}(?:登录|登入)\S{0,4}", re.I)
# Without a redirect such a title alone is not enough: Wikipedia's "CAPTCHA - Wikipedia" or PyPI's
# "captcha · PyPI" are ordinary pages. A challenge served in place (Cloudflare's "Just a moment...")
# is a short page, or carries the challenge's own wording near the top.
_WALL_MAX_CHARS = 1500
_CHALLENGE_TEXT_RE = re.compile(
    r"cf-challenge|/cdn-cgi/challenge-platform|checking (?:if the site connection is secure|your browser)|"
    r"verifying you are human|needs to review the security of your connection|"
    r"enable javascript and cookies to continue|网络不给力，请稍后重试|请完成(?:下方|以下)?(?:安全)?验证",
    re.I)


def _url_key(url: str) -> str:
    """URL identity for redirect detection: scheme, www., trailing slash and fragment ignored."""
    try:
        parts = urlsplit((url or "").strip())
        host = (parts.hostname or "").lower().removeprefix("www.")
        return f"{host}:{parts.port or ''}{parts.path.rstrip('/')}?{parts.query}"
    except ValueError:
        return (url or "").strip()


def _is_interstitial(final_url: str, title: str, redirected: bool, text: str = "") -> bool:
    head = _TITLE_SUFFIX_RE.split(title.strip(), 1)[0].strip() if title else ""
    if head and _WALL_TITLE_RE.fullmatch(head) and (
        redirected or len(text) <= _WALL_MAX_CHARS or _CHALLENGE_TEXT_RE.search(text[:2000])
    ):
        return True
    if not redirected:
        return False
    if head and _LOGIN_TITLE_RE.fullmatch(head):
        return True
    try:
        parts = urlsplit(final_url)
    except ValueError:
        return False
    segments = {segment.lower().rsplit(".", 1)[0] for segment in parts.path.split("/") if segment}
    return bool(
        segments & _WALL_SEGMENTS
        or (parts.hostname or "").split(".")[0].lower() in _WALL_HOST_LABELS
        or _WALL_QUERY_RE.search(parts.query)
        or "/cdn-cgi/challenge-platform" in parts.path
    )


def _page_info(url: str, page: dict, text: str = "") -> dict:
    """Header fields for a backend result: final_url and title after a redirect, warning for an interstitial."""
    final_url = page.get("final_url") or ""
    title = page.get("title") or ""
    redirected = bool(final_url) and _url_key(final_url) != _url_key(url)
    info = {}
    if _is_interstitial(final_url, title, redirected, text or ""):
        info["warning"] = "interstitial"
    if redirected:
        info["final_url"] = final_url
    if title and info:
        info["title"] = title[:120]
    return info


def _info_header(info: dict | None) -> str:
    if not info:
        return ""
    out = ""
    if info.get("warning"):
        out += f" warning={info['warning']}"
    if info.get("final_url"):
        out += " final_url=" + info["final_url"].replace(" ", "%20")
    if info.get("title"):
        out += " title=" + json.dumps(info["title"], ensure_ascii=False)  # last: it may contain spaces
    if info.get("warning") == "interstitial":
        where = f"（被重定向到 {info['final_url']}）" if info.get("final_url") else ""
        out += (
            f"\n注意: 这很可能不是请求页面的正文，而是登录或人机验证拦截页{where}。"
            "该页面需要登录或拦截了抓取，请改用其他来源（例如 web_search）。"
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


def _paginate(text: str, provider: str, max_chars: int, offset: int, *, snapshot_id: str = "", cached: bool = False, notes=None, info=None) -> str:
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
    if notes:
        header += " attempts=" + ",".join(notes)
    header += _info_header(info)
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
            await log_info(ctx, f"arXiv API failed, falling back to extractors: {errors[-1].summary()}", config.debug_enabled)

    if not text:
        tavily = await _try("tavily", _call_tavily_extract(url), errors, notes)
        text, provider = tavily, "tavily"
        if tavily is None or len(tavily) < config.fetch_min_chars:
            if tavily is not None:
                notes.append(f"tavily:short({len(tavily)})")
            await log_info(ctx, "Tavily result missing or too short, trying Firecrawl...", config.debug_enabled)
            page: dict = {}
            firecrawl = await _try("firecrawl", _call_firecrawl_scrape(url, ctx, page), errors, notes)
            firecrawl_info = _page_info(url, page, firecrawl) if firecrawl else {}
            if firecrawl and firecrawl_info.get("warning") and tavily is not None:
                notes.append("firecrawl:interstitial")  # a login wall never replaces a real extract
            elif firecrawl and (tavily is None or len(firecrawl) > len(tavily)):
                text, provider, info = firecrawl, "firecrawl", firecrawl_info
                notes.append("firecrawl:interstitial" if info.get("warning") else "firecrawl:ok")
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
    if not text:
        await log_info(ctx, "Fetch Failed!", config.debug_enabled)
        return _failure_message(errors)

    limit = max_chars if max_chars and max_chars > 0 else _default_page_chars(text, 0)
    sid = SNAPSHOTS.put(url, text, provider, info) if len(text) > limit else ""
    await log_info(ctx, f"Fetch Finished ({provider})!", config.debug_enabled)
    return _paginate(text, provider, max_chars, 0, snapshot_id=sid, notes=notes, info=info)
