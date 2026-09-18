"""web_fetch backends: Tavily extract, Firecrawl scrape, and site adapters.

A result is only accepted from the fast path (Tavily) when it is long enough
to be a real page; otherwise the slower Firecrawl path runs and the longer
of the two is returned. arXiv abstract pages bypass both and use the arXiv
export API, whose output is complete and stable.
"""
import html
import re
import xml.etree.ElementTree as ET

import httpx

from .config import config
from .logger import log_info
from .verify import arxiv_get

ARXIV_API = "https://export.arxiv.org/api/query"
_ARXIV_ABS_RE = re.compile(r"^https?://(?:www\.)?arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/[0-9]{7})(?:v\d+)?/?(?:[?#].*)?$", re.I)
_ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


async def _call_tavily_extract(url: str) -> str | None:
    api_url = config.tavily_api_url
    api_key = config.tavily_api_key
    if not api_key:
        return None
    endpoint = f"{api_url.rstrip('/')}/extract"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"urls": [url], "format": "markdown"}
    try:
        async with httpx.AsyncClient(timeout=config.tavily_extract_timeout_s) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            if data.get("results") and len(data["results"]) > 0:
                content = data["results"][0].get("raw_content", "")
                return content if content and content.strip() else None
            return None
    except Exception:
        return None


async def _call_firecrawl_scrape(url: str, ctx=None) -> str | None:
    api_url = config.firecrawl_api_url
    api_key = config.firecrawl_api_key
    if not api_key:
        return None
    endpoint = f"{api_url.rstrip('/')}/scrape"
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
                markdown = data.get("data", {}).get("markdown", "")
                if markdown and markdown.strip():
                    return markdown
                await log_info(ctx, f"Firecrawl: markdown为空, 重试 {attempt + 1}/{max_retries}", config.debug_enabled)
        except Exception as e:
            await log_info(ctx, f"Firecrawl error: {e}", config.debug_enabled)
            return None
    return None


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


def _paginate(text: str, provider: str, max_chars: int, offset: int) -> str:
    total = len(text)
    offset = max(0, offset)
    limit = max_chars if max_chars and max_chars > 0 else config.fetch_max_chars
    piece = text[offset:offset + limit]
    end = offset + len(piece)
    truncated = end < total
    header = f"[web_fetch] source={provider} total_chars={total} offset={offset} returned={len(piece)} truncated={'true' if truncated else 'false'}"
    if truncated:
        header += f" next_offset={end}"
    return header + "\n\n" + piece


async def fetch_page(url: str, max_chars: int = 0, offset: int = 0, ctx=None) -> str:
    url = (url or "").strip()
    text: str | None = None
    provider = ""

    m = _ARXIV_ABS_RE.match(url)
    if m:
        try:
            text = await _fetch_arxiv_abs(m.group(1))
            provider = "arxiv-api"
        except Exception as e:
            await log_info(ctx, f"arXiv API failed, falling back to extractors: {e}", config.debug_enabled)
            text = None

    if not text:
        tavily = await _call_tavily_extract(url)
        text, provider = tavily, "tavily"
        if tavily is None or len(tavily) < config.fetch_min_chars:
            await log_info(ctx, "Tavily result missing or too short, trying Firecrawl...", config.debug_enabled)
            firecrawl = await _call_firecrawl_scrape(url, ctx)
            if firecrawl and (tavily is None or len(firecrawl) > len(tavily)):
                text, provider = firecrawl, "firecrawl"

    if not text:
        await log_info(ctx, "Fetch Failed!", config.debug_enabled)
        if not config.tavily_api_key and not config.firecrawl_api_key:
            return "配置错误: TAVILY_API_KEY 和 FIRECRAWL_API_KEY 均未配置"
        return "提取失败: 所有提取服务均未能获取内容"

    await log_info(ctx, f"Fetch Finished ({provider})!", config.debug_enabled)
    return _paginate(text, provider, max_chars, offset)
