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
"""
import html
import re
import time
import uuid
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass

import httpx

from .config import config
from .errors import ProviderError, classify_exception
from .logger import log_info
from .verify import arxiv_get

ARXIV_API = "https://export.arxiv.org/api/query"
_ARXIV_ABS_RE = re.compile(r"^https?://(?:www\.)?arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/[0-9]{7})(?:v\d+)?/?(?:[?#].*)?$", re.I)
_ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


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


async def _call_firecrawl_scrape(url: str, ctx=None) -> str | None:
    """Firecrawl Scrape; empty pages are retried with a longer waitFor. Raises ProviderError on failure."""
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
        markdown = (payload.get("markdown") or "") if isinstance(payload, dict) else ""
        if markdown.strip():
            return markdown
        await log_info(ctx, f"Firecrawl: markdown为空, 重试 {attempt + 1}/{max_retries}", config.debug_enabled)
    raise ProviderError("firecrawl", "empty_content", f"empty markdown after {max_retries} attempt(s)")


async def _fetch_arxiv_abs(arxiv_id: str) -> str | None:
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        response = await arxiv_get(client, {"id_list": arxiv_id, "max_results": 1})
        root = ET.fromstring(response.text)
    entry = root.find("a:entry", _ATOM)
    if entry is None:
        return None
    text = lambda tag, ns="a": re.sub(r"\s+", " ", (entry.findtext(f"{ns}:{tag}", default="", namespaces=_ATOM) or "").strip())
    title = text("title")
    if not title or "/abs/" not in (entry.findtext("a:id", default="", namespaces=_ATOM) or ""):
        return None
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
    return "\n".join(lines)


# ------------------------------------------------------------------ snapshots
@dataclass
class Snapshot:
    url: str
    text: str
    provider: str
    created: float
    last_used: float


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

    def put(self, url: str, text: str, provider: str) -> str:
        now = self._clock()
        sid = uuid.uuid4().hex[:12]
        self._items[sid] = Snapshot(url, text, provider, now, now)
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


def _paginate(text: str, provider: str, max_chars: int, offset: int, *, snapshot_id: str = "", cached: bool = False, notes=None) -> str:
    total = len(text)
    offset = max(0, offset)
    limit = max_chars if max_chars and max_chars > 0 else config.fetch_max_chars
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
    return header + "\n\n" + piece


def _failure_message(errors: list[ProviderError]) -> str:
    if errors and all(e.skipped for e in errors):
        head = "[web_fetch] error=not_configured"
        title = "配置错误: 没有可用的抓取服务（Tavily 与 Firecrawl 均未启用或未配置）"
    else:
        head = "[web_fetch] error=fetch_failed attempts=" + ",".join(f"{e.provider}:{e.category}" for e in errors)
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

    m = _ARXIV_ABS_RE.match(url)
    if m:
        text = await _try("arxiv-api", _fetch_arxiv_abs(m.group(1)), errors, notes)
        provider = "arxiv-api"
        if not text:
            await log_info(ctx, f"arXiv API failed, falling back to extractors: {errors[-1].summary()}", config.debug_enabled)

    if not text:
        tavily = await _try("tavily", _call_tavily_extract(url), errors, notes)
        text, provider = tavily, "tavily"
        if tavily is None or len(tavily) < config.fetch_min_chars:
            if tavily is not None:
                notes.append(f"tavily:short({len(tavily)})")
            await log_info(ctx, "Tavily result missing or too short, trying Firecrawl...", config.debug_enabled)
            firecrawl = await _try("firecrawl", _call_firecrawl_scrape(url, ctx), errors, notes)
            if firecrawl and (tavily is None or len(firecrawl) > len(tavily)):
                text, provider = firecrawl, "firecrawl"
                notes.append("firecrawl:ok")
    if notes and all(note.endswith(":ok") for note in notes):
        notes = []  # nothing notable happened
    return text, provider, notes, errors


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
        return _paginate(snap.text, snap.provider, max_chars, offset, snapshot_id=snapshot_id, cached=True)

    if offset > 0:
        return (
            "[web_fetch] error=snapshot_required\n\n"
            "offset>0 时必须同时传入首屏返回的 snapshot_id，才能保证各页来自同一份页面内容。"
            "请从 offset=0 开始抓取，再按 next_offset 与 snapshot_id 翻页。"
        )

    text, provider, notes, errors = await _fetch_fresh(url, ctx)
    if not text:
        await log_info(ctx, "Fetch Failed!", config.debug_enabled)
        return _failure_message(errors)

    limit = max_chars if max_chars and max_chars > 0 else config.fetch_max_chars
    sid = SNAPSHOTS.put(url, text, provider) if len(text) > limit else ""
    await log_info(ctx, f"Fetch Finished ({provider})!", config.debug_enabled)
    return _paginate(text, provider, max_chars, 0, snapshot_id=sid, notes=notes)
