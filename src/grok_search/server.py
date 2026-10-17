import sys
from pathlib import Path

# 支持直接运行：添加 src 目录到 Python 路径
src_dir = Path(__file__).parent.parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from fastmcp import FastMCP, Context
from typing import Annotated, Optional
from pydantic import Field

# 尝试使用绝对导入（支持 mcp run）
try:
    from grok_search.providers.grok import GrokResponse, GrokSearchProvider, GrokUpstreamError
    from grok_search.logger import log_info, log_error, logger
    from grok_search.config import config, mask_url
    from grok_search.sources import SourcesCache, consulted_identities, document_identity, merge_sources, new_session_id, sources_from_annotations, sources_from_text, split_answer_and_sources, url_identity as _url_identity
    from grok_search.planning import engine as planning_engine, _split_csv
    from grok_search.throttle import breaker, BreakerOpen
    from grok_search.verify import compact_verification, verify_answer
    from grok_search.fetching import fetch_page, url_problem
    from grok_search.citations import normalize_citations
    from grok_search.errors import ProviderError, classify_exception, redact, sanitize_no_proxy_env, summarize_error_body
    from grok_search.models import ModelListCache, base_is_listed, check_model, model_kind, model_name_problem, non_chat_kind, remember_capabilities, routing_base, suggest_models
    from grok_search.platforms import resolve_platform
except ImportError:
    from .providers.grok import GrokResponse, GrokSearchProvider, GrokUpstreamError
    from .logger import log_info, log_error, logger
    from .config import config, mask_url
    from .sources import SourcesCache, consulted_identities, document_identity, merge_sources, new_session_id, sources_from_annotations, sources_from_text, split_answer_and_sources, url_identity as _url_identity
    from .planning import engine as planning_engine, _split_csv
    from .throttle import breaker, BreakerOpen
    from .verify import compact_verification, verify_answer
    from .fetching import fetch_page, url_problem
    from .citations import normalize_citations
    from .errors import ProviderError, classify_exception, redact, sanitize_no_proxy_env, summarize_error_body
    from .models import ModelListCache, base_is_listed, check_model, model_kind, model_name_problem, non_chat_kind, remember_capabilities, routing_base, suggest_models
    from .platforms import resolve_platform

import asyncio
import json
import math
import os
import re
import signal
import subprocess
import tempfile
from urllib.parse import urlparse

import httpx


def _package_version() -> str:
    try:
        from importlib.metadata import version as _version
        return _version("grok-search")
    except Exception:
        return "1.13.1"


mcp = FastMCP("grok-search", version=_package_version())

# Claude Code runs MCP tools concurrently only when they declare readOnlyHint;
# every tool that does not mutate anything is annotated so a parallel fan-out
# of searches is executed in parallel instead of one after another.
_READ_ONLY_WEB = {"readOnlyHint": True, "openWorldHint": True}
_READ_ONLY_LOCAL = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}

# GROK_TOOL_PREFIX (read once, at start) goes before every tool name, for clients that do not namespace MCP
# tools and merge tools of the same name (AstrBot replaces its own web_search with this server's, or the other
# way round: https://github.com/AstrBotDevs/AstrBot/issues/5689). The descriptions then say so.
_TOOL_PREFIX = config.tool_prefix
_TOOL_NAMES = ("web_search", "get_sources", "web_fetch", "web_map", "get_config_info", "switch_model", "toggle_builtin_tools")


def _tool_name(name: str) -> str:
    return _TOOL_PREFIX + name


def _tool_description(text: str) -> str:
    if not _TOOL_PREFIX:
        return text
    names = ", ".join(_tool_name(name) for name in _TOOL_NAMES)
    return text.rstrip() + (f"\n\n    Tool names carry the prefix '{_TOOL_PREFIX}' (GROK_TOOL_PREFIX): the tools named in this "
                            f"text without it are {names}.\n")


_SOURCES_CACHE = SourcesCache(max_size=256)
_MODEL_CACHE = ModelListCache()
_SANITIZED_ENV: list[str] = []

# extra_sources: cap per call, candidates asked beyond each engine's share to absorb
# duplicates, and the per-request ceiling of the search APIs.
_EXTRA_SOURCES_MAX = 20
_EXTRA_MARGIN = 2
_EXTRA_MAX_PER_CALL = 20
# Snippet caps: Tavily returns up to ~13 KB of page text per hit, which would push get_sources past the
# client's MCP output limit; the Extra sources section in content uses a shorter plain-text preview.
_EXTRA_SNIPPET_CHARS = 500
_EXTRA_TITLE_CHARS = 300
_EXTRA_SECTION_SNIPPET_CHARS = 300


def _clip(text: str, limit: int) -> str:
    """Collapse whitespace and cut at a word boundary, marking the cut with "…"."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:-—") + "…"


def _engine_items(engine: str, results: list[dict] | None) -> list[dict]:
    items: list[dict] = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        url = (r.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        item: dict = {"url": url, "provider": engine}
        title = _clip(r.get("title") or "", _EXTRA_TITLE_CHARS)
        if title:
            item["title"] = title
        desc = _clip(r.get("description") or r.get("content") or "", _EXTRA_SNIPPET_CHARS)
        if desc:
            item["description"] = desc
        items.append(item)
    return items


_EXTRA_PROVIDERS = ("tavily", "firecrawl")


def _extra_engines() -> list[str]:
    """Engines usable for extra_sources, in allocation order (Tavily takes the odd share)."""
    engines = []
    if config.tavily_active:
        engines.append("tavily")
    if config.firecrawl_active:
        engines.append("firecrawl")
    return engines


def _split_shares(total: int, engines: list[str]) -> dict[str, int]:
    if total <= 0 or not engines:
        return {}
    if len(engines) == 1:
        return {engines[0]: total}
    first = (total + 1) // 2
    return {engines[0]: first, engines[1]: total - first}


async def _run_engine(engine: str, query: str, limit: int, include_domains: list[str] | None = None) -> list[dict]:
    # include_domains is passed only when a platform is applied, so engine stand-ins keep (query, limit).
    kwargs = {"include_domains": include_domains} if include_domains else {}
    if engine == "tavily":
        results = await _call_tavily_search(query, limit, **kwargs)
    else:
        results = await _call_firecrawl_search(query, limit, **kwargs)
    return _engine_items(engine, results)


class _ExtraSources:
    """extra_sources strategy.

    1. Share: with both engines Tavily gets ceil(n/2) and Firecrawl floor(n/2); one engine gets n.
    2. Each engine is asked for its share + 2 (at most 20), in parallel with the Grok search, so
       duplicates can be absorbed without another round trip.
    3. Dedup: URLs Grok already cited and repeats across engines are dropped (scheme, "www.",
       fragment, trailing slash and tracking parameters are ignored; a repository's page, its default
       tree and its root README are one document, as are a directory and its index.html).
    4. Selection: round-robin across engines, keeping each engine's own ranking.
    5. Backfill: if an engine failed or the selection is still short, every engine that answered
       and may have more results is asked once more for (previous request + shortfall + 2 + the
       duplicates and off-platform hits skipped so far, at most 20), so new duplicates do not leave the
       request short again.
    6. Platform (web_search platform=...): a known platform or hostname becomes a domain list, sent to
       Tavily as include_domains and to Firecrawl as "site:" operators; hits whose host is not one of
       those domains (or a subdomain) are dropped before selection. An unknown platform is appended to
       the engine query as text, without a filter; so is a library or product name written like a host
       ("discord.py", "Node.js", "ASP.NET", "Socket.IO"). extra_sources_report["platform"] says which was applied.
    """

    def __init__(self, query: str, wanted: int, platform: str = ""):
        self.query = query
        self.wanted = wanted
        self.scope = resolve_platform(platform) if platform else None
        self.engine_query = self.scope.engine_query(query) if self.scope else query
        self.domains = list(self.scope.domains) if self.scope else []
        self.engines = _extra_engines()
        self.shares = _split_shares(wanted, self.engines)
        self.items: dict[str, list[dict]] = {engine: [] for engine in self.engines}
        self.off_platform: dict[str, set[str]] = {engine: set() for engine in self.engines}
        self.state: dict[str, dict] = {engine: {"share": self.shares.get(engine, 0)} for engine in self.engines}

    async def _ask(self, engine: str, limit: int, phase: str) -> None:
        state = self.state[engine]
        state[f"{phase}_asked"] = limit
        try:
            if self.domains:
                got = await _run_engine(engine, self.engine_query, limit, include_domains=self.domains)
            else:
                got = await _run_engine(engine, self.engine_query, limit)
        except Exception as e:
            state["status"] = "error"
            state["error"] = classify_exception(engine, e).to_dict()
            return
        state["status"] = "ok"
        state[f"{phase}_received"] = len(got)
        state["exhausted"] = len(got) < limit
        known = {document_identity(item["url"]) for item in self.items[engine]}
        for item in got:
            key = document_identity(item["url"])
            if self.scope is not None and not self.scope.allows(item["url"]):
                # The engines filter by domain themselves; this catches what slips through
                # (look-alike hosts such as githubhelp.com are not GitHub).
                self.off_platform[engine].add(key)
                continue
            if key not in known:
                known.add(key)
                self.items[engine].append(item)
        if self.off_platform[engine]:
            state["off_platform_skipped"] = len(self.off_platform[engine])

    def _off_platform_count(self) -> int:
        return sum(len(keys) for keys in self.off_platform.values())

    async def first_round(self) -> None:
        await asyncio.gather(*(
            self._ask(engine, min(share + _EXTRA_MARGIN, _EXTRA_MAX_PER_CALL), "first")
            for engine, share in self.shares.items() if share > 0
        ))

    def _select(self, exclude: set[str]) -> tuple[list[dict], int]:
        seen = set(exclude)
        picked: list[dict] = []
        duplicates = 0
        queues = {engine: list(self.items[engine]) for engine in self.engines}
        while len(picked) < self.wanted and any(queues.values()):
            for engine in self.engines:
                queue = queues[engine]
                while queue:
                    item = queue.pop(0)
                    key = document_identity(item["url"])
                    if key in seen:
                        duplicates += 1
                        continue
                    seen.add(key)
                    picked.append(item)
                    break
                if len(picked) >= self.wanted:
                    break
        return picked, duplicates

    async def finish(self, exclude_urls) -> tuple[list[dict], dict]:
        exclude = {document_identity(url) for url in exclude_urls}
        picked, duplicates = self._select(exclude)
        for engine in self.engines:
            shortfall = self.wanted - len(picked)
            if shortfall <= 0:
                break
            state = self.state[engine]
            if state.get("status") == "error" or state.get("exhausted"):
                continue
            previous = state.get("first_asked", 0)
            limit = min(previous + shortfall + _EXTRA_MARGIN + duplicates + self._off_platform_count(), _EXTRA_MAX_PER_CALL)
            if limit <= previous:
                continue
            await self._ask(engine, limit, "backfill")
            picked, duplicates = self._select(exclude)
        for engine in self.engines:
            self.state[engine].setdefault("status", "not_needed")
            self.state[engine]["selected"] = sum(1 for item in picked if item["provider"] == engine)
        report = {"requested": self.wanted, "returned": len(picked), "duplicates_skipped": duplicates, "engines": self.state}
        if self.scope is not None:
            report["platform"] = self.scope.report(self.engines, self._off_platform_count())
        if not self.engines:
            report["engines"] = config.provider_status()
            report["note"] = "no extra-source engine is active"
        return picked, report


def _distinct_domains(sources: list[dict] | None) -> int:
    hosts: set[str] = set()
    for item in sources or []:
        url = (item or {}).get("url")
        if not isinstance(url, str):
            continue
        try:
            host = (urlparse(url.strip()).hostname or "").lower()
        except ValueError:
            continue
        if host.startswith("www."):
            host = host[4:]
        if host:
            hosts.add(host)
    return len(hosts)


_MD_ESCAPE_RE = re.compile(r"([\\`*_\[\]<>~])")
_MD_LINK_TEXT_RE = re.compile(r"!?\[([^\[\]\n]{0,500})\]\((?:[^()\n]|\([^()\n]*\))*\)")
_BARE_URL_RE = re.compile(r"(?:https?|ftp)://\S+|www\.\S+\.\S+")
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^<>\n]{0,200}>")
_HEADING_MARK_RE = re.compile(r"(?:^|(?<=\s))#{1,6}(?=\s)")
_FENCE_MARK_RE = re.compile(r"`{3,}[\w+-]*|~{3,}")


def _md_text(text: str) -> str:
    """One line of literal text inside Markdown: whitespace collapsed, markup characters escaped."""
    return _MD_ESCAPE_RE.sub(r"\\\1", " ".join((text or "").split()))


def _plain_snippet(text: str, limit: int) -> str:
    """Page text (Markdown/HTML) reduced to a plain one-line preview without links, URLs or headings."""
    text = _MD_LINK_TEXT_RE.sub(r"\1", text or "")
    text = _HTML_TAG_RE.sub(" ", text)
    text = _BARE_URL_RE.sub("", text)
    text = _FENCE_MARK_RE.sub(" ", text)
    text = _HEADING_MARK_RE.sub("", text)
    return _md_text(_clip(text, limit))


def _md_url(url: str) -> str:
    """Link destination: only characters that would end it early are percent-encoded."""
    if url.count("(") != url.count(")") or any(ch in url for ch in " <>\n\t"):
        for ch, code in (("(", "%28"), (")", "%29"), (" ", "%20"), ("<", "%3C"), (">", "%3E"), ("\n", ""), ("\t", "")):
            url = url.replace(ch, code)
    return url


def _format_extra_section(extra: list[dict]) -> str:
    """One list item per line: title (escaped) linked to the URL, the engine, and a plain-text preview."""
    lines = ["## Extra sources (independent search engines)"]
    for i, item in enumerate(extra, 1):
        url = item.get("url", "")
        title = _md_text(item.get("title") or "").lstrip("#").strip() or _md_text(url)
        line = f"{i}. [{title}]({_md_url(url)}) [{item.get('provider', '')}]"
        desc = _plain_snippet(item.get("description") or "", _EXTRA_SECTION_SNIPPET_CHARS)
        if desc:
            line += f" — {desc}"
        lines.append(line)
    return "\n".join(lines)


_CONSULTED_LISTED = 100  # get_sources lists at most this many consulted-only pages (all are counted)


def _cited_and_consulted(answer: str, native: list[dict], split_off: list[dict],
                         annotations: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """(cited, consulted, cited in the order the answer mentions them) for one answer.

    Cited: the native citations, then the documents the answer itself names (inline markers, Markdown
    links, bare URLs) and a split-off sources list, each once. A native source whose annotations all have
    an empty span (consulted_identities) and that the answer never names is a page the upstream's search
    opened, not a citation: it is listed in consulted, never counted in sources_count and never verified.
    The third list puts what the answer mentions first, so the per-answer limit of URL checks is spent
    on the links a reader sees.
    """
    written = sources_from_text(answer) if answer else []
    for item in split_off:
        item.setdefault("provider", "grok")
    mentioned = {_url_identity(item["url"]) for item in written + split_off}
    unpositioned = consulted_identities(annotations) - mentioned
    native_cited = [item for item in native if _url_identity(item["url"]) not in unpositioned]
    consulted = [{key: item[key] for key in ("url", "provider", "title") if key in item}
                 for item in merge_sources([item for item in native if _url_identity(item["url"]) in unpositioned])]
    # A native source keeps its own citations; the answer's link text can still give it a title.
    link_titles = {_url_identity(item["url"]): item["title"] for item in written if item.get("title")}
    native_keys = set()
    for i, item in enumerate(native_cited):
        key = _url_identity(item["url"])
        native_keys.add(key)
        if not item.get("title") and key in link_titles:
            native_cited[i] = {**item, "title": link_titles[key]}
    cited = merge_sources(native_cited, [item for item in written if _url_identity(item["url"]) not in native_keys], split_off)
    first_mention: dict[str, int] = {}
    for i, item in enumerate(written):
        first_mention.setdefault(_url_identity(item["url"]), i)
    in_answer_order = sorted(cited, key=lambda item: first_mention.get(_url_identity(item["url"]), len(written)))
    return cited, consulted, in_answer_order


def _counts(cited: list[dict], extra: list[dict], total: list[dict]) -> dict:
    return {
        "sources_count": len(cited),
        "distinct_domains": _distinct_domains(cited),
        "extra_sources_count": len(extra),
        "total_sources_count": len(total),
    }


def _is_blank(text: str | None) -> bool:
    """True when nothing visible is left: whitespace (any Unicode space), zero-width/format and control
    characters only. str.strip() keeps U+200B, U+FEFF and C0 controls, which would still be billed upstream."""
    return not any(ch.isprintable() and not ch.isspace() for ch in (text or ""))


def _early_failure(session_id: str, detail: str, error: str, error_type: str) -> dict:
    """Failure before any search ran (configuration, model name): same shape as other failures."""
    return {
        "session_id": session_id,
        "content": f"[搜索失败] {detail}",
        **_counts([], [], []),
        "error": error,
        "error_type": error_type,
    }


@mcp.tool(
    name=_tool_name("web_search"),
    output_schema=None,
    annotations=_READ_ONLY_WEB,
    description=_tool_description("""
    Performs a deep web search through Grok's live web search and returns the answer directly.
    Read-only: several searches may be issued in parallel.

    Returns:
    - session_id: pass to get_sources to list the sources (cited ones with citation offsets, then extra ones)
    - content: the answer in Markdown with inline citations, followed by an "Extra sources" section when
      extra_sources > 0. Every failure starts with "[搜索失败]" (extra-source hits found before the failure
      are still listed after it). Statements marked "Inference:" are the searcher's own reasoning, not
      statements of a source. "Not found in this search" only means this search did not find it; to
      confirm that something does not exist, check the platform directly (for example web_fetch its issue
      search page).
    - sources_count / distinct_domains: how many sources Grok cited and on how many distinct sites: its
      native citations plus the markers, links and URLs written in the answer. extra_sources hits are not
      included: extra_sources_count counts them and total_sources_count is cited + extra (the length of
      the get_sources list)
    - consulted_sources_count (only when non-zero): pages the upstream reported as opened by its search
      without citing them (Responses API annotations with an empty span) and that the answer never
      mentions; often unrelated to the answer. Not sources, not counted above, not verified; get_sources
      lists them separately as consulted
    - citation_positions: native citation offsets that were valid, relocated (to the marker, or when the
      answer has none, the Markdown link or URL of the same page) or removed after repeated markers were
      collapsed; consulted_only counts the consulted pages. Offsets returned by get_sources always refer
      to this content and count Unicode code points (not UTF-16 units)
    - incomplete / finish_reason / native_finish_reason (only when the upstream cut the answer short, e.g.
      output cap or content filter): the answer is partial; a one-line note is appended after it
    - usage (when the upstream reports it): prompt_tokens, completion_tokens, reasoning_tokens, and when
      reported cached_tokens, cost_usd (what the upstream billed for this search) and web_search_calls /
      x_search_calls (searches Grok ran itself). Multi-agent / high-reasoning models can bill 0.5-1.6M prompt
      tokens for one search; for routine lookups pass a lighter model= (only when the user asked for it) and
      avoid repeating a call that already answered
    - search_queries (GROK_API_MODE=responses): queries Grok searched with that the stream showed, at most
      10. search_queries_partial=true: Grok ran more searches than are listed (multi-agent models run most
      of them in sub-agents whose queries are never reported; usage.web_search_calls / x_search_calls give
      the totals), so the list cannot tell whether a "not found" covered the right terms
    - verification (when enabled): existence and reachability only, for the arXiv IDs / DOIs in the
      answer and the URLs Grok cited, the ones written in the answer first (extra sources and consulted
      pages are not checked). Identifiers are resolved against
      arXiv, Crossref and the doi.org registry (DataCite), URLs are fetched, and paper titles written next
      to an identifier or in the same record (the lines of a "- Title: / - arXiv: / - DOI:" list under its
      heading) are compared with the registry title (title_checks: match / partial /
      possible_mismatch / not_stated). It does NOT check whether a source supports the sentence it is
      attached to (claim_support = "not_checked"). Treat unresolved (the registry or site said it does not
      exist: 404/410, unknown domain) and possible_mismatch entries as suspect; treat lookup_failed (rate
      limit, 5xx, timeout, network, any other 4xx such as 401/403/451 refusals or the 400/405/406/418 of bot
      defences) as unknown, not as evidence against the citation; a bot-check or interstitial page
      (captcha, "prove your humanity", Cloudflare / AWS WAF challenge) is lookup_failed too, never reachable; skipped
      lists what was not checked and why (including DOI patterns written in prose, such as
      "10.48550/arXiv.*"). By default (GROK_VERIFY_DETAIL=compact, detail = "compact") only what needs
      attention is listed: every unresolved and lookup_failed entry (URLs of one host that failed for the
      same reason are one entry {kind: "url", host, reason, count, sample}), title_checks other than match,
      urls_unusual (reachable URLs whose final status is not 200, e.g. a 202/203 bot check; only when
      present) and the first 10 specific skipped entries (skipped_omitted counts the rest; URLs over the
      per-answer limit or with checks disabled are only counted, in skipped_by_reason); checked and summary
      keep every count (urls_reachable, identifiers_found, title_match, ...). GROK_VERIFY_DETAIL=full
      also lists every resolved identifier record (arxiv, doi), every reachable URL with status and
      title (urls), matching title checks and the full scope text.
    - extra_sources_report (when extra_sources > 0): share, results, errors and backfill per engine; with a
      platform also "platform": {requested, applied (domain_filter / query_hint / none), domains (sent to the
      engines), also_accepted (short-link hosts kept by the filter), engines (tavily include_domains,
      firecrawl site:), off_platform_skipped, query_hint, unresolved}
    - model_check (when model is given and differs from the default, also on failure): how the name was validated
    - error / error_type / retry_after_s / status / exhausted (failures only). error_type is one of:
      bad_request (the query is empty or only whitespace / invisible / control characters: refused locally,
      nothing is sent to Grok or the extra-source engines), config (GROK_API_URL / GROK_API_KEY missing or
      invalid), proxy_config, invalid_model, rate_limit
      (exhausted=true: this model's account pool is blocked), circuit_open, timeout (no complete answer in
      time, or gateway 522/524), network (connection/proxy failure), truncated_stream, empty_response,
      incomplete (the upstream stopped before any content, e.g. finish_reason length or content_filter;
      finish_reason / native_finish_reason / usage are included; rephrase or narrow the query instead of
      repeating it), auth (the gateway has no usable credential for the model), http_<status>, unknown, or an error code
      passed through from the upstream stream (for example invalid_request_error, model_not_found,
      server_error, upstream_error). Wait retry_after_s seconds or switch model before calling again
    """),
    meta={"version": "2.5.0", "author": "guda.studio"},
)
async def web_search(
    query: Annotated[str, "Clear, self-contained natural-language search query."],
    platform: Annotated[str, "Target platform to focus on (e.g., 'Twitter', 'GitHub', 'Reddit'; several may be comma-separated, e.g. 'GitHub, Reddit'). Grok is asked to focus on it. extra_sources are restricted to the platform's domains when the name is known (GitHub, Reddit, X/Twitter, Stack Overflow, Hacker News, YouTube, arXiv, Wikipedia, 知乎, Bilibili, 微博, CSDN, 掘金, V2EX, Hugging Face, PyPI, npm, GitLab, Medium, 小红书, 豆瓣, 微信公众号 and others) or is a hostname such as 'docs.python.org' (written in lower case; names such as 'discord.py' or 'ASP.NET' count as text); otherwise the platform text is added to the extra-source query. Leave empty for general web search."] = "",
    model: Annotated[str, "Optional model ID for this request only. This value is used ONLY when user explicitly provided. A thinking suffix such as 'grok-4(high)' or 'grok-4(8192)' is accepted: the base name is checked against /models and the full name is sent. Non-chat models (TTS/voice/audio, image/video generation, embedding, moderation, realtime, rerank) are rejected locally as invalid_model."] = "",
    extra_sources: Annotated[int, Field(description="Number of additional results from the independent engines (Tavily/Firecrawl), 0-20, appended as an 'Extra sources' section and counted in extra_sources_count, not in sources_count. With both engines active the share is split (Tavily ceil(n/2), Firecrawl floor(n/2)); URLs Grok already cited are skipped and a failed engine is backfilled by the other. With a known platform the engines search only its domains (Tavily include_domains, Firecrawl site:) and off-platform hits are dropped, so fewer than n may be returned; see extra_sources_report.platform. Default 0.", ge=0, le=_EXTRA_SOURCES_MAX)] = 0,
    instructions: Annotated[str, "Optional per-call instructions for the searcher, e.g. 'list at least 15 distinct sources' or 'return arXiv IDs with titles, no analogies'."] = "",
    verify: Annotated[bool, "Attach the verification layer: identifier existence, link reachability and title comparison (NOT whether sources support the claims). Default true; identifier lookups follow GROK_VERIFY_IDS and URL checks GROK_VERIFY_URLS (both off: no verification); GROK_VERIFY_DETAIL=full lists every checked item instead of only problems and counts (compact, the default)."] = True,
) -> dict:
    session_id = new_session_id()
    # A blank query is refused before anything that can reach the network (check_model may GET /models,
    # Grok bills the system prompt and search tools even for an empty question, the engines bill per call).
    if _is_blank(query):
        await _SOURCES_CACHE.set(session_id, [])
        return _early_failure(
            session_id,
            "查询为空（只包含空白或控制字符），未发送到上游；请提供具体的搜索问题",
            "empty query",
            "bad_request",
        )
    platform = "" if _is_blank(platform) else (platform or "").strip()
    try:
        api_url = config.grok_api_url
        api_key = config.grok_api_key
    except ValueError as e:
        await _SOURCES_CACHE.set(session_id, [])
        return _early_failure(session_id, f"配置错误: {str(e)}", str(e), "config")

    effective_model = config.grok_model
    model_check = None
    if model and model.strip():
        model_check = await check_model(model, default_model=effective_model, api_url=api_url, api_key=api_key, cache=_MODEL_CACHE)
        if not model_check.ok:
            await _SOURCES_CACHE.set(session_id, [])
            detail = model_check.detail
            if model_check.suggestions:
                detail += "；相近的可用模型: " + ", ".join(model_check.suggestions)
            result = _early_failure(session_id, f"无效模型: {model}（{detail}）", f"无效模型: {model}", "invalid_model")
            result["model_check"] = model_check.to_dict()
            return result
        effective_model = model_check.model

    grok_provider = GrokSearchProvider(api_url, api_key, effective_model)
    wanted_extra = max(0, min(int(extra_sources or 0), _EXTRA_SOURCES_MAX))
    extra_job = _ExtraSources(query, wanted_extra, platform) if wanted_extra > 0 else None
    grok_error: dict = {}

    async def _safe_grok() -> GrokResponse:
        try:
            kwargs = {"instructions": instructions} if instructions else {}
            return await grok_provider.search_with_sources(query, platform, **kwargs)
        except BreakerOpen as e:
            grok_error.update(error=redact(str(e), 600), error_type="circuit_open", retry_after_s=round(e.retry_after_s, 1))
        except GrokUpstreamError as e:
            grok_error.update(error=redact(str(e), 600), error_type=e.error_type, exhausted=e.exhausted)
            if e.retry_after is not None:
                grok_error["retry_after_s"] = round(float(e.retry_after), 1)
            if e.status is not None:
                grok_error["status"] = e.status
            # incomplete / finish_reason / usage of a stream that ended without an answer.
            grok_error.update(e.result_fields)
        except Exception as e:
            # The provider classifies its own transport failures; this names whatever is left
            # (for example an invalid proxy environment) instead of a bare class name.
            classified = classify_exception("grok", e)
            grok_error.update(error=redact(classified.detail or type(e).__name__, 600), error_type=classified.category)
        await log_error(None, f"web_search upstream failed (model={effective_model}): {grok_error}")
        return GrokResponse()

    # The first round of extra-source searches runs in parallel with Grok.
    if extra_job is not None:
        grok_result, _ = await asyncio.gather(_safe_grok(), extra_job.first_round())
    else:
        grok_result = await _safe_grok()

    answer, grok_sources = split_answer_and_sources(grok_result.content)
    native_sources = sources_from_annotations(grok_result.annotations)
    citation_report = None
    if native_sources:
        # The full answer is kept (removing a sources section would shift offsets); repeated
        # markers are collapsed and every offset is checked against the text actually returned.
        answer, native_sources, citation_report = normalize_citations(grok_result.content, native_sources)
    elif answer:
        # No native citations: the answer is kept as written (only a pure link list under a sources
        # heading was split off).
        answer, _, _ = normalize_citations(answer, [])
    cited_sources, consulted, verify_order = _cited_and_consulted(answer, native_sources, grok_sources, grok_result.annotations)
    if citation_report is not None and consulted:
        citation_report["consulted_only"] = len(consulted)

    extra: list[dict] = []
    extra_report = None
    if extra_job is not None:
        extra, extra_report = await extra_job.finish(item["url"] for item in cited_sources)
    all_sources = merge_sources(cited_sources, extra)

    await _SOURCES_CACHE.set(session_id, all_sources, consulted)
    # Counts describe what Grok cited; extra-source hits are reported separately.
    counts = _counts(cited_sources, extra, all_sources)
    if consulted:
        counts["consulted_sources_count"] = len(consulted)

    if not answer:
        if grok_error:
            detail = f"上游搜索失败: {grok_error['error']}"
        elif cited_sources:
            detail = "上游只返回了来源列表，无正文内容（可用 get_sources 查看来源）"
        else:
            detail = "上游返回空内容（未检测到明确错误）"
        hint = ""
        if grok_error.get("retry_after_s") is not None:
            hint += f"，建议 {grok_error['retry_after_s']:.0f} 秒后重试"
        if grok_error.get("exhausted"):
            hint += "，该模型的账号池已耗尽（同一模型的不同思考档位共用状态），可用 model 参数临时换用其他模型"
        if grok_error.get("error_type") == "incomplete":
            hint += "，上游在输出任何内容前就结束了生成，原样重试无效（content_filter 时请改写查询，length 时请缩小问题范围）"
        content = f"[搜索失败] {detail}。模型: {effective_model}{hint}。"
        if extra:
            # The independent-engine results were already paid for: show them instead of only the error.
            content += (
                f"\n\n独立搜索引擎已找到 {len(extra)} 条结果（未经 Grok 引用与核对，get_sources 中也有）：\n\n"
                + _format_extra_section(extra)
            )
        result = {
            "session_id": session_id,
            "content": content,
            **counts,
            "error": detail,
            "error_type": grok_error.get("error_type", "empty_response"),
        }
        for field in ("retry_after_s", "status", "exhausted", "incomplete", "finish_reason", "native_finish_reason",
                      "usage", "search_queries", "search_queries_partial", "dropped_events"):
            if field in grok_error:
                result[field] = grok_error[field]
        if extra_report is not None:
            result["extra_sources_report"] = extra_report
        if model_check is not None and model_check.status != "default":
            result["model_check"] = model_check.to_dict()
        return result

    grok_answer = answer
    # incomplete / finish_reason / native_finish_reason (answer cut short upstream) and token usage.
    completion = grok_result.result_fields()
    if completion.get("incomplete"):
        # The note follows the answer itself, so citation offsets stay valid.
        answer = answer.rstrip() + "\n\n" + grok_result.incomplete_note()
    if extra:
        # Appending after the answer keeps native citation offsets valid.
        answer = answer.rstrip() + "\n\n" + _format_extra_section(extra)

    result = {"session_id": session_id, "content": answer, **counts}
    result.update(completion)
    if citation_report is not None:
        result["citation_positions"] = citation_report
    if extra_report is not None:
        result["extra_sources_report"] = extra_report
    if model_check is not None and model_check.status != "default":
        result["model_check"] = model_check.to_dict()

    if verify and (config.verify_ids or config.verify_urls):
        try:
            # Only what Grok cited is verified, links written in the answer first; extra_sources hits are
            # search results and consulted pages were not cited.
            verification = await verify_answer(
                grok_answer,
                verify_order,
                check_ids=config.verify_ids,
                check_urls=config.verify_urls,
                timeout_s=config.verify_timeout_s,
                mailto=config.verify_mailto,
                fake_ip=config.verify_fake_ip,
                doh_url=config.verify_doh_url,
            )
            # compact (default): only what needs attention plus all counts; full: v1.12.0's complete listing.
            result["verification"] = verification if config.verify_detail == "full" else compact_verification(verification)
        except Exception as e:
            result["verification"] = {"error": f"{type(e).__name__}: {e}", "claim_support": "not_checked"}

    return result


@mcp.tool(
    name=_tool_name("get_sources"),
    annotations=_READ_ONLY_LOCAL,
    description=_tool_description("""
    When you feel confused or curious about the search response content, use the session_id returned by web_search to invoke the this tool to obtain the corresponding list of information sources.
    Retrieve all cached sources for a previous web_search call.
    Provide the session_id returned by web_search to get the full source list.

    - sources: the sources Grok cited (provider "grok") first, then the extra_sources hits (provider
      "tavily" / "firecrawl"), which Grok did not cite. sources_count here is the whole list
      (web_search's total_sources_count); cited_sources_count / extra_sources_count split it.
    - citations: start_index / end_index are Unicode code-point offsets (not UTF-16 units) into the
      content returned by web_search; label is the citation number when the upstream title was one.
      An offset that did not match a marker was moved to the marker for the same URL
      (position "relocated") or dropped (position "removed"); the upstream values are then kept as
      upstream_start_index / upstream_end_index.
    - description (extra sources): a whitespace-collapsed snippet of at most 500 characters ending with
      "…" when cut; web_fetch the URL for the full page.
    - consulted / consulted_sources_count (only when there are some): pages the upstream reported as
      opened by its search without citing them (Responses API annotations with an empty span) and that
      the answer does not mention. They are not sources of any statement, often unrelated to the answer,
      and are neither counted in sources_count nor verified; at most 100 are listed.
    """),
    meta={"version": "1.2.0", "author": "guda.studio"},
)
async def get_sources(
    session_id: Annotated[str, "Session ID from previous web_search call."]
) -> dict:
    entry = await _SOURCES_CACHE.get_entry(session_id)
    if entry is None:
        return {
            "session_id": session_id,
            "sources": [],
            "sources_count": 0,
            "error": "session_id_not_found_or_expired",
        }
    sources, consulted = entry
    extra_count = sum(1 for item in sources if isinstance(item, dict) and item.get("provider") in _EXTRA_PROVIDERS)
    result = {
        "session_id": session_id,
        "sources": sources,
        "sources_count": len(sources),
        "cited_sources_count": len(sources) - extra_count,
        "extra_sources_count": extra_count,
    }
    if consulted:
        result["consulted"] = consulted[:_CONSULTED_LISTED]
        result["consulted_sources_count"] = len(consulted)
    return result


async def _call_tavily_search(query: str, max_results: int = 6, include_domains: list[str] | None = None) -> list[dict]:
    """Tavily Search for extra_sources. Raises ProviderError on failure, also when disabled or unconfigured.

    include_domains restricts the results to those domains and their subdomains (Tavily's default
    include_domains_mode "restrict"; https://docs.tavily.com/documentation/api-reference/endpoint/search).
    """
    import httpx
    if not config.tavily_enabled:
        raise ProviderError("tavily", "disabled", "TAVILY_ENABLED=false")
    api_key = config.tavily_api_key
    if not api_key:
        raise ProviderError("tavily", "not_configured", "TAVILY_API_KEY is not set")
    endpoint = f"{config.tavily_api_url.rstrip('/')}/search"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "query": query,
        "max_results": max_results,
        "search_depth": "advanced",
        "include_raw_content": False,
        "include_answer": False,
    }
    if include_domains:
        body["include_domains"] = list(include_domains)
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        raise classify_exception("tavily", e) from e
    results = data.get("results") if isinstance(data, dict) else None
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", ""), "score": r.get("score", 0)}
        for r in results or [] if isinstance(r, dict)
    ]


async def _call_firecrawl_search(query: str, limit: int = 14, include_domains: list[str] | None = None) -> list[dict]:
    """Firecrawl Search for extra_sources. Raises ProviderError on failure.

    include_domains is written into the query as "site:a OR site:b" (the documented site: operator,
    https://docs.firecrawl.dev/api-reference/endpoint/search). Firecrawl's v2 includeDomains parameter
    builds the same suffix server-side; in the query text it also works with a v1 endpoint.
    """
    import httpx
    api_key = config.firecrawl_api_key
    if not api_key:
        raise ProviderError("firecrawl", "not_configured", "FIRECRAWL_API_KEY is not set")
    endpoint = f"{config.firecrawl_api_url.rstrip('/')}/search"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if include_domains:
        query = f"{query} " + " OR ".join(f"site:{domain}" for domain in include_domains)
    body = {"query": query, "limit": limit}
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        raise classify_exception("firecrawl", e) from e
    if isinstance(data, dict) and data.get("success") is False:
        raise ProviderError("firecrawl", "upstream_error", str(data.get("error") or "success=false"))
    payload = data.get("data") if isinstance(data, dict) else None
    results = payload.get("web") if isinstance(payload, dict) else payload  # v2: data.web, v1: data
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "description": r.get("description", "")}
        for r in results or [] if isinstance(r, dict)
    ]


@mcp.tool(
    name=_tool_name("web_fetch"),
    output_schema=None,
    annotations=_READ_ONLY_WEB,
    description=_tool_description("""
    Fetches a URL: web pages as Markdown, text files verbatim. Read-only: several fetches may run in parallel.

    Backends:
    - Text files (raw.githubusercontent.com, gist raw, GitHub/GitLab/Codeberg/Bitbucket file pages such as
      github.com/<o>/<r>/blob/<ref>/<path>, which are read from their raw URL (Git LFS files included),
      /raw/ URLs, and URLs ending in a code/config/text extension such as .py .h .toml .json .md .txt .patch,
      commit/PR .patch/.diff and release assets on the forges too) are downloaded directly and returned
      byte for byte (`source=direct`), also when served as application/octet-stream if the body is text;
      the charset comes from a BOM, Content-Type, an XML declaration or, failing those, UTF-8, GB18030 when
      the text reads as Chinese, else Windows-1252 with `warning=charset_guess` (`charset=` names a guessed
      one; `warning=decode_errors` means undecodable bytes became U+FFFD).
      Safety: public addresses only (the guard of citation verification), checked again on the connection
      itself; at most 5 MB; no API key sent. Through a proxy (HTTP(S)_PROXY, system proxy) or a fake-ip TUN
      the proxy decides where a name leads, so only GitHub, GitLab.com, Codeberg, Bitbucket, Gitea.com and
      the jsDelivr/unpkg/npm/PyPI/Rust file hosts are downloaded directly then; other text URLs go to the
      extractors. GROK_FETCH_DIRECT=false skips the
      direct download. Without it, raw/gist/forge file and /raw/ URLs are read by Firecrawl and then Tavily;
      a URL that only ends in such an extension (often a page: www.npmjs.com/package/chart.js,
      en.wikipedia.org/wiki/Node.js) like a page, Firecrawl's content type telling a file from a page.
      When the origin itself answers 404 or 410, no extractor is called (they would read the same address):
      `error=fetch_failed attempts=direct:extract_failed page_status=404`. Visibly damaged code
      (collapsed newlines, lost indentation, escaped "_", stripped #include targets) is marked
      `warning=degraded_code degraded=<reasons>`: do not quote it. A URL whose origin
      answers with HTML is fetched as a web page, and a backend copy reported as text/html is never flagged
      as damaged code. A branch URL on GitHub's raw host can lag a push by ~5 min.
    - arXiv abstract pages: the arXiv API (a requested version such as .../abs/1706.03762v1 is honoured;
      cached for an hour; given up after GROK_FETCH_ARXIV_BUDGET_S, default 8 s, then skipped for 2 minutes:
      `arxiv-api:cooldown`), then DataCite's record (`source=datacite`, latest version only), Firecrawl, Tavily.
    - Other pages: Firecrawl's live scrape first; Tavily Extract when Firecrawl fails, answers with a bot
      check, or its copy is shorter than GROK_FETCH_MIN_CHARS (2,000) and Tavily's is at least twice as long.
      GROK_FETCH_PAGE_FIRST=tavily (or no Firecrawl key) reads Tavily first, Firecrawl when that is short or
      a bot check. Image markup is removed from page copies (badges, icons; a linked image keeps its link,
      an image its alt text as [image: alt]; `images_stripped=N`; GROK_FETCH_STRIP_IMAGES=false keeps it),
      never from files.

    The first line is a metadata header:
    `[web_fetch] source=<backend> total_chars=N offset=O returned=R truncated=true|false [next_offset=X snapshot_id=S expires_in_s=T] [cached=true] [content_type=T charset=C status=S] [cache=hit|miss|bypassed|possible cached_at=T] [images_stripped=N] [attempts=...] [warning=interstitial|degraded_code|size_limit|decode_errors|charset_guess] [degraded=...] [final_url=<url> title="<title>"]`
    cache= is the backend's cache: Firecrawl is asked for a copy at most GROK_FETCH_MAX_AGE_S old (default 0:
    `cache=bypassed`, scraped now); `cache=possible` is Tavily, which caches by URL without freshness
    control (a python.org copy was over a week old): such a result also has a `注意:` line saying it may be
    stale, so do not confirm versions, dates or prices with it alone. cached=true: served from a paging snapshot.
    A backend in attempts= that failed while another answered gets a `- <backend>: <category> ...` line.
    The default page is GROK_FETCH_MAX_CHARS (40,000) characters, or GROK_FETCH_MAX_CHARS_CJK (20,000) when
    more than 30% of it is CJK text (about one token per character), to stay under the ~25k-token MCP result
    limit; an explicit max_chars is used as given.
    A page longer than one response is kept as a server-side snapshot. To read on, call again with the
    same url, `offset=next_offset` and `snapshot_id=S`: every page then comes from exactly the same text.
    offset > 0 without snapshot_id is refused; when a snapshot has expired, start again at offset 0.
    final_url is the URL the text came from after a redirect or raw rewrite. `warning=interstitial` plus a
    `注意:` line mean a login wall or anti-bot check ("安全验证", "Just a moment...", "Prove your humanity"),
    not the requested page: use another source.

    On failure the first line is `[web_fetch] error=...`, followed by one line per backend with an error
    category (auth, quota, rate_limit, timeout, proxy_config, network, upstream_error, bad_request,
    unsupported_site, blocked, not_text, extract_failed, empty_content, http_error, disabled, not_configured),
    the HTTP status and a redacted detail; unsupported_site is a provider refusing the site (Firecrawl's 403
    "we do not support this site"), not a key problem.
    A page that itself answered HTTP >= 400 counts as failed ("target page answered HTTP 404"), and the
    first line adds `page_status=404`. Not an absolute http(s) URL with a host, localhost, or a
    loopback/private/link-local/CGNAT IP literal: refused locally without any backend call, as
    `[web_fetch] error=invalid_url reason=<malformed|missing_scheme|unsupported_scheme|missing_host|local_address>`
    (host names are not resolved for this check). The cloud backends cannot reach your machine or LAN.

    **Edge Cases & Best Practices:**
        - Ensure URL is complete and accessible (not behind authentication or paywalls).
        - May not capture dynamically loaded content requiring JavaScript execution.
    """),
    meta={"version": "1.8.0", "author": "guda.studio"},
)
async def web_fetch(
    url: Annotated[str, "Complete public http(s) URL of the target page (e.g. https://example.com/page). Local/LAN addresses are refused."],
    max_chars: Annotated[int, Field(description="Maximum characters to return in this call. 0 uses GROK_FETCH_MAX_CHARS (default 40000), or GROK_FETCH_MAX_CHARS_CJK (default 20000) when more than 30% of the page is CJK.", ge=0)] = 0,
    offset: Annotated[int, Field(description="Character offset to start from; use next_offset from the previous page together with its snapshot_id.", ge=0)] = 0,
    snapshot_id: Annotated[str, "snapshot_id from the header of an earlier page of the same url; required when offset > 0."] = "",
    ctx: Context = None,
) -> str:
    await log_info(ctx, f"Begin Fetch: {url}", config.debug_enabled)
    return await fetch_page(url, max_chars=max_chars, offset=offset, ctx=ctx, snapshot_id=snapshot_id)


def _map_failure(error: ProviderError, title: str) -> str:
    """web_map failure text: a stable first line like web_fetch's, then the redacted backend summary."""
    head = f"[web_map] error={error.category} provider={error.provider}"
    if error.status is not None:
        head += f" status={error.status}"
    return "\n".join([head, "", title, f"- {error.summary()}"])


def _url_host(url: str) -> str:
    """Lower-case host of a URL or bare domain, without port or trailing dot; "" when there is none."""
    text = (url or "").strip()
    try:
        host = urlparse(text if "://" in text else f"https://{text}").hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".")


def _site_host(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


try:  # the regex parser, to check select_domains patterns without compiling them
    from re import _constants as _sre_constants, _parser as _sre_parse
except ImportError:  # Python 3.10
    import sre_constants as _sre_constants
    import sre_parse as _sre_parse

MAP_HOST_MAX_CHARS = 253  # the longest valid DNS name
MAP_MAX_PATTERNS = 20  # select_domains patterns per call, after removing duplicates
MAP_PATTERN_MAX_CHARS = 300
MAP_MATCHER_MEMORY_BYTES = 512 * 1024 * 1024  # memory a matcher may add to its own (RLIMIT_AS / Job Object)
MAP_MATCHERS_AT_ONCE = 2  # matcher processes alive at the same time, across all web_map calls
_REGEX_SCOPE_OPS = {
    _sre_constants.SUBPATTERN, _sre_constants.MAX_REPEAT, _sre_constants.MIN_REPEAT, _sre_constants.BRANCH,
    _sre_constants.ASSERT, _sre_constants.ASSERT_NOT, _sre_constants.GROUPREF_EXISTS,
    getattr(_sre_constants, "POSSESSIVE_REPEAT", None), getattr(_sre_constants, "ATOMIC_GROUP", None),
} - {None}

# select_domains is matched against the result hosts by this program in a child process. A caller's
# regex can backtrack for minutes or allocate gigabytes ((?:\b){50000000}x), which no reading of the
# pattern rules out reliably; run here it would block the event loop and with it every tool, while a
# child can be killed. A server killed outright cannot kill it, and a thread in the child would not run
# while the regex engine holds the GIL, so the child brings its own ends: on POSIX it ends itself after
# argv[2] seconds (SIGALRM, whose default action ends the process even inside the regex engine), on
# Linux also as soon as the server (argv[3], its parent) is gone (PR_SET_PDEATHSIG), and it limits its
# own memory (argv[1] bytes more than at start) and CPU time; on Windows the server's Job Object
# (_matcher_job) limits its memory and ends it with the server. stdin: {"patterns": [...], "hosts":
# [...]}; stdout: one boolean per host.
_MAP_MATCHER = r"""
import json, os, re, sys
seconds = int(sys.argv[2])
try:
    import signal
    signal.alarm(seconds)
except (ImportError, AttributeError):
    pass
if sys.platform.startswith("linux") and len(sys.argv) > 3:
    try:
        import ctypes
        ctypes.CDLL(None).prctl(1, 9, 0, 0, 0)  # PR_SET_PDEATHSIG: SIGKILL when the parent ends
    except Exception:
        pass
    if os.getppid() != int(sys.argv[3]):  # the server ended before that
        sys.exit(1)
try:
    import resource
except ImportError:
    resource = None
if resource is not None:
    try:
        with open("/proc/self/statm") as statm:
            used = int(statm.read().split()[0]) * resource.getpagesize()
    except (OSError, ValueError, IndexError):
        used = 0
    for name, value in (("RLIMIT_AS", used + int(sys.argv[1])), ("RLIMIT_CPU", seconds)):
        try:
            limit = getattr(resource, name)
            hard = resource.getrlimit(limit)[1]
            if hard != resource.RLIM_INFINITY:
                value = min(value, hard)
            resource.setrlimit(limit, (value, hard))
        except (AttributeError, ValueError, OSError):
            pass
job = json.loads(sys.stdin.buffer.read())
patterns = [re.compile(p) for p in job["patterns"]]
sys.stdout.write(json.dumps([any(p.search(h) for p in patterns) for h in job["hosts"]]))
"""


def _lookbehind_problem(items) -> str:
    """The error compiling would raise for a look-behind in a parsed pattern, or "" (the parser leaves it
    to the compiler)."""
    for op, av in items:
        if op not in _REGEX_SCOPE_OPS:
            continue
        if op in (_sre_constants.ASSERT, _sre_constants.ASSERT_NOT):
            if av[0] < 0:
                low, high = av[1].getwidth()
                if low != high:
                    return "look-behind requires fixed-width pattern"
                if low > 0xFFFFFFFF:
                    return "looks too much behind"
            parts = [av[1]]
        elif op == _sre_constants.SUBPATTERN:
            parts = [av[-1]]
        elif op == _sre_constants.BRANCH:
            parts = av[1]
        elif op == _sre_constants.GROUPREF_EXISTS:
            parts = [part for part in av[1:] if part is not None]
        elif op == getattr(_sre_constants, "ATOMIC_GROUP", None):
            parts = [av]
        else:  # a repeat
            parts = [av[2]]
        for part in parts:
            problem = _lookbehind_problem(part)
            if problem:
                return problem
    return ""


def _regex_problem(pattern: str) -> str:
    """Why pattern is not a valid Python regular expression, or "".

    The pattern is only parsed (linear in its length): compiling can take a tenth of a second for a
    300-character pattern ((?i) with wide character ranges), and the matcher process compiles it."""
    try:
        return _lookbehind_problem(_sre_parse.parse(pattern))
    except (re.error, OverflowError, RecursionError, ValueError, TypeError) as e:
        return str(e) or type(e).__name__


def _map_domain_patterns(values) -> tuple[list[str], str, str]:
    """select_domains as sent to Tavily and matched locally: stripped, duplicates removed.

    Returns (patterns, reason, message); reason is "" or the bad_request reason (too_many_patterns,
    pattern_too_long, invalid_regex) and message says what to change."""
    patterns = list(dict.fromkeys(v.strip() for v in values or [] if isinstance(v, str) and v.strip()))
    if len(patterns) > MAP_MAX_PATTERNS:
        return patterns, "too_many_patterns", (
            f"select_domains 最多 {MAP_MAX_PATTERNS} 个正则（去重后），收到 {len(patterns)} 个；"
            "请合并为更少的正则，例如 ^(docs|api|blog)\\.example\\.com$")
    for pattern in patterns:
        if len(pattern) > MAP_PATTERN_MAX_CHARS:
            return patterns, "pattern_too_long", (
                f"select_domains 的正则最长 {MAP_PATTERN_MAX_CHARS} 个字符，「{pattern[:80]}…」有 {len(pattern)} 个")
    for pattern in patterns:
        problem = _regex_problem(pattern)
        if problem:
            return patterns, "invalid_regex", (
                f"select_domains 中的正则「{pattern[:80]}」不是有效的 Python 正则表达式: {redact(problem, 200)}")
    return patterns, "", ""


def _matcher_python() -> str:
    """The interpreter that runs _MAP_MATCHER (-I -S: no site-packages, nothing from the environment).

    On Windows a venv's python.exe is a launcher that runs the base interpreter as a second process, so
    the base interpreter is started directly: killing the matcher then stops the process that runs the regex."""
    if os.name == "nt":
        base = getattr(sys, "_base_executable", "")
        if base and os.path.isfile(base):
            return base
    return sys.executable


def _matcher_env() -> dict:
    """The matcher's whole environment. It runs re and json on the caller's input and needs nothing of
    the server's (API keys, tokens, proxy settings); Python on Windows needs SYSTEMROOT to start."""
    if os.name == "nt":
        return {"SYSTEMROOT": os.environ.get("SYSTEMROOT") or "C:\\Windows"}
    return {}


def _kill(proc) -> None:
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass  # already gone


# Windows has neither SIGALRM nor resource limits, so one Job Object holds every matcher: each may commit
# MAP_MATCHER_MEMORY_BYTES more than a starting interpreter, and all are killed when the job's last handle
# closes, that is when the server process ends, however it ends (the handle is not inherited).
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
_JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x400  # no error dialog that keeps a crashed matcher alive
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_MATCHER_STARTUP_BYTES = 64 * 1024 * 1024
_matcher_job = None  # Windows: the job's handle once created, 0 when Windows refused one


def _job_limit_information():
    """ctypes layout of JOBOBJECT_EXTENDED_LIMIT_INFORMATION (144 bytes on 64-bit Windows, 112 on 32-bit)."""
    import ctypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32)]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimitInformation), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    return ExtendedLimitInformation


def _kernel32():
    """kernel32 with the signatures the matcher job uses (its own WinDLL object: ctypes.windll's is shared)."""
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, restype, argtypes in (
        ("CreateJobObjectW", ctypes.c_void_p, (ctypes.c_void_p, ctypes.c_wchar_p)),
        ("SetInformationJobObject", ctypes.c_int, (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)),
        ("OpenProcess", ctypes.c_void_p, (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)),
        ("AssignProcessToJobObject", ctypes.c_int, (ctypes.c_void_p, ctypes.c_void_p)),
        ("CloseHandle", ctypes.c_int, (ctypes.c_void_p,)),
    ):
        function = getattr(kernel32, name)
        function.restype, function.argtypes = restype, argtypes
    return kernel32


def _create_matcher_job(kernel32) -> int:
    """A Job Object with the matchers' limits, or 0 when Windows refuses one."""
    import ctypes
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return 0
    info = _job_limit_information()()
    info.BasicLimitInformation.LimitFlags = (_JOB_OBJECT_LIMIT_PROCESS_MEMORY | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                                             | _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION)
    info.ProcessMemoryLimit = MAP_MATCHER_MEMORY_BYTES + _MATCHER_STARTUP_BYTES
    if not kernel32.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
                                            ctypes.sizeof(info)):
        kernel32.CloseHandle(job)
        return 0
    return job


def _contain_matcher(pid: int) -> None:
    """Windows: put a new matcher in the job before it is sent its input (until then it only waits on
    stdin). When Windows refuses, the timeout kill remains its limit."""
    global _matcher_job
    if os.name != "nt" or _matcher_job == 0:
        return
    try:
        kernel32 = _kernel32()
        if _matcher_job is None:
            _matcher_job = _create_matcher_job(kernel32) or 0
        if not _matcher_job:
            return
        process = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if process:
            try:
                kernel32.AssignProcessToJobObject(_matcher_job, process)
            finally:
                kernel32.CloseHandle(process)
    except Exception:  # no ctypes, or an API that is missing
        _matcher_job = _matcher_job or 0


# Each matcher may use MAP_MATCHER_MEMORY_BYTES, so how many run at once bounds the memory web_map calls
# can take together. One semaphore per event loop (an asyncio primitive belongs to the loop that first
# waits on it; the server has one loop, tests have many).
_matcher_slots = None  # (event loop, asyncio.Semaphore(MAP_MATCHERS_AT_ONCE))
_live_matchers: set = set()  # running matcher processes, killed when the server shuts down
# Exit statuses of a matcher that ended itself at its deadline (POSIX: SIGALRM, or SIGXCPU from RLIMIT_CPU).
_MATCHER_DEADLINE_EXITS = frozenset(-getattr(signal, name) for name in ("SIGALRM", "SIGXCPU") if hasattr(signal, name))


def _matcher_slot() -> asyncio.Semaphore:
    global _matcher_slots
    loop = asyncio.get_running_loop()
    if _matcher_slots is None or _matcher_slots[0] is not loop:
        _matcher_slots = (loop, asyncio.Semaphore(MAP_MATCHERS_AT_ONCE))
    return _matcher_slots[1]


def _kill_matchers() -> None:
    """Kill the matchers still running (the server is exiting without cancelling its calls)."""
    for proc in list(_live_matchers):
        try:
            _kill(proc)
        except Exception:
            pass


async def _match_hosts(patterns: list[str], hosts: list[str]) -> tuple[list[bool] | None, str]:
    """Which hosts one of patterns finds (re.search), decided by _MAP_MATCHER in a child process.

    At most MAP_MATCHERS_AT_ONCE matchers run at a time; waiting for a free one counts toward
    GROK_MAP_REGEX_TIMEOUT_S, after which the child is killed. Returns (matches, "") or (None, what went
    wrong)."""
    timeout = config.map_regex_timeout_s
    if getattr(sys, "frozen", False):  # a bundled app: sys.executable is the app itself, not Python
        return None, "failed; results not filtered (no Python interpreter for the matcher)"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    slot = _matcher_slot()
    try:
        await asyncio.wait_for(slot.acquire(), timeout)
    except asyncio.TimeoutError:
        return None, (f"timed out; results not filtered (no free matcher within {timeout:g} s, at most "
                      f"{MAP_MATCHERS_AT_ONCE} run at a time; GROK_MAP_REGEX_TIMEOUT_S)")
    try:
        return await _run_matcher(patterns, hosts, timeout, deadline - loop.time())
    finally:
        slot.release()


async def _run_matcher(patterns: list[str], hosts: list[str], timeout: float,
                       remaining: float) -> tuple[list[bool] | None, str]:
    timed_out = (None, (f"timed out; results not filtered (select_domains matching stopped after {timeout:g} s, "
                        "GROK_MAP_REGEX_TIMEOUT_S)"))
    if remaining < 0.1:  # less than starting the interpreter takes: the slot came too late
        return timed_out
    payload = json.dumps({"patterns": patterns, "hosts": hosts}).encode("ascii")
    # No console window per call when the server itself has none (Windows GUI clients).
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        proc = await asyncio.create_subprocess_exec(
            _matcher_python(), "-I", "-S", "-c", _MAP_MATCHER, str(MAP_MATCHER_MEMORY_BYTES), str(math.ceil(remaining) + 1),
            str(os.getpid()), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_matcher_env(), **flags)
    except Exception as e:  # no usable interpreter, or an event loop without subprocess support
        return None, f"failed; results not filtered (could not start the matcher: {type(e).__name__})"
    _live_matchers.add(proc)
    try:
        _contain_matcher(proc.pid)
        out, err = await asyncio.wait_for(proc.communicate(payload), remaining)
    except asyncio.TimeoutError:
        _kill(proc)
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            pass
        return timed_out
    except BaseException:  # cancelled: the child must not outlive the call
        _kill(proc)
        raise
    finally:
        _live_matchers.discard(proc)
    if proc.returncode in _MATCHER_DEADLINE_EXITS:
        return timed_out
    if proc.returncode != 0:
        lines = err.decode("utf-8", "replace").strip().splitlines()
        why = redact(lines[-1], 120) if lines else f"exit status {proc.returncode}"
        return None, f"failed; results not filtered ({why})"
    try:
        matches = json.loads(out)
    except ValueError:
        matches = None
    if not isinstance(matches, list) or len(matches) != len(hosts) or not all(isinstance(m, bool) for m in matches):
        return None, "failed; results not filtered (unreadable matcher output)"
    return matches, ""


async def _scope_map_results(url: str, base_url, results, allow_external: bool,
                             select_domains: list[str]) -> tuple[list, dict]:
    """Apply web_map's scope to Tavily's result list on the client side.

    Tavily does not count sibling subdomains (app., status., docs. ...) as external, and select_domains
    only steers its crawl, so neither keeps them out of the list. With allow_external=false only URLs on
    the start host are kept (www.<host> counts as <host>; Tavily's base_url host too; plain string
    comparison), or, when select_domains is given, only URLs whose host matches one of its regexes
    (re.search, as Tavily documents them). Those regexes come from the caller and the hosts from the
    crawled site, so they are matched in a child process with a time limit (_match_hosts); hosts longer
    than a DNS name are dropped. When matching does not finish, Tavily's list is returned unfiltered with
    a scope_note. allow_external=true returns Tavily's list unchanged.
    Returns the kept results and the fields describing the filter (scope, filtered_out[, scope_note]).
    """
    results = results if isinstance(results, list) else []
    if allow_external:
        return results, {"scope": "unfiltered", "filtered_out": 0}
    # An entry that is not a URL string (host None) is left to Tavily.
    hosts = [_url_host(link) if isinstance(link, str) else None
             for link in (item.get("url") if isinstance(item, dict) else item for item in results)]
    if select_domains:
        scope = "select_domains"
        candidates = sorted({host for host in hosts if host is not None and len(host) <= MAP_HOST_MAX_CHARS})
        matches, problem = await _match_hosts(select_domains, candidates) if candidates else ([], "")
        if problem:
            return results, {"scope": "unfiltered", "filtered_out": 0, "scope_note": f"local regex filter {problem}"}
        allowed = {host for host, matched in zip(candidates, matches) if matched}

        def keep(host: str) -> bool:
            return host in allowed
    else:
        scope = "exact_host"
        sites = {_site_host(_url_host(u)) for u in (url, base_url) if isinstance(u, str)} - {""}

        def keep(host: str) -> bool:
            return _site_host(host) in sites

    kept = [item for item, host in zip(results, hosts) if host is None or keep(host)]
    return kept, {"scope": scope, "filtered_out": len(results) - len(kept)}


async def _call_tavily_map(url: str, instructions: str = None, max_depth: int = 1,
                           max_breadth: int = 20, limit: int = 50, timeout: int = 150,
                           allow_external: bool = True, select_paths: list[str] | None = None,
                           select_domains: list[str] | None = None, exclude_paths: list[str] | None = None,
                           exclude_domains: list[str] | None = None) -> str:
    import httpx
    import json
    # Tavily Map accepts a bare domain ("docs.example.com"); everything else follows web_fetch's local check.
    problem = url_problem(url if "://" in (url or "") else f"https://{(url or '').strip()}")
    if problem:
        return f"[web_map] error=invalid_url reason={problem[0]}\n\n无效的 URL: {problem[1]}"
    domain_patterns, reason, message = _map_domain_patterns(select_domains)
    if reason:
        return f"[web_map] error=bad_request reason={reason}\n\n{message}"
    if not config.tavily_enabled:
        return _map_failure(ProviderError("tavily", "disabled", "TAVILY_ENABLED=false"),
                            "配置错误: Tavily 已禁用（TAVILY_ENABLED=false），web_map 不可用")
    api_url = config.tavily_api_url
    api_key = config.tavily_api_key
    if not api_key:
        return _map_failure(ProviderError("tavily", "not_configured", "TAVILY_API_KEY is not set"),
                            "配置错误: TAVILY_API_KEY 未配置，请设置环境变量 TAVILY_API_KEY")
    endpoint = f"{api_url.rstrip('/')}/map"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"url": url, "max_depth": max_depth, "max_breadth": max_breadth, "limit": limit,
            "timeout": timeout, "allow_external": bool(allow_external)}
    if instructions:
        body["instructions"] = instructions
    for key, values in (("select_paths", select_paths), ("select_domains", domain_patterns),
                        ("exclude_paths", exclude_paths), ("exclude_domains", exclude_domains)):
        cleaned = list(dict.fromkeys(v.strip() for v in values or [] if isinstance(v, str) and v.strip()))
        if cleaned:
            body[key] = cleaned
    try:
        async with httpx.AsyncClient(timeout=float(timeout + 10)) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            base_url, results, response_time = data.get("base_url", ""), data.get("results", []), data.get("response_time", 0)
    except Exception as e:
        error = classify_exception("tavily", e)
        if isinstance(e, httpx.TimeoutException):
            title = f"映射超时: 请求超过{timeout}秒"
        elif isinstance(e, httpx.HTTPStatusError):
            title = f"映射失败: Tavily Map 返回 HTTP {e.response.status_code}"
        else:
            title = "映射失败: Tavily Map 未能完成"
        return _map_failure(error, title)
    # Outside the try: the local filter reports its own problems in scope_note, never as a Tavily failure.
    results, scope = await _scope_map_results(url, base_url, results, bool(allow_external), domain_patterns)
    return json.dumps({"base_url": base_url, "results": results, "response_time": response_time, **scope},
                      ensure_ascii=False, indent=2)


@mcp.tool(
    name=_tool_name("web_map"),
    output_schema=None,
    annotations=_READ_ONLY_WEB,
    description=_tool_description("""
    Maps a website's structure by traversing it like a graph, discovering URLs and generating a comprehensive site map.

    **Key Features:**
        - **Graph Traversal:** Explores website structure starting from root URL.
        - **Depth & Breadth Control:** Configure traversal limits to balance coverage and performance.
        - **Scope Control:** allow_external (default true: links to other sites are included).
          allow_external=false keeps only URLs on the start URL's host (www.<host> counts as the same
          host); other subdomains such as app. or status. are removed client-side, because Tavily
          treats subdomains of the same site as internal. With allow_external=false and select_domains,
          the select_domains regexes decide instead (Python re.search against each result's host, also
          client-side), e.g. ['^(docs|api)\\.example\\.com$'] or ['^([\\w-]+\\.)*example\\.com$']. They are
          matched in a separate process (no environment variables, limited memory, at most 2 at a time;
          waiting for one counts toward the limit) that is killed after GROK_MAP_REGEX_TIMEOUT_S (default
          2 s), so a slow pattern never blocks the server: the list then comes back unfiltered with
          scope_note "local regex filter timed out; results not filtered". select_domains takes at most 20 patterns
          (duplicates removed) of at most 300 characters; more, longer or invalid ones are refused locally
          with `[web_map] error=bad_request reason=too_many_patterns|pattern_too_long|invalid_regex`.
          select_paths keeps only matching paths; exclude_paths / exclude_domains drop matching URLs (all
          regular expressions; these three are applied by Tavily only).
        - **Instruction Filtering:** Use natural language to focus crawler on specific content types.

    **Output:** on success a JSON object {base_url, results, response_time, scope, filtered_out}: scope is
    exact_host (allow_external=false), select_domains (allow_external=false with select_domains) or
    unfiltered (allow_external=true, or the local select_domains filter timed out or failed; scope_note
    says why), and filtered_out counts the URLs removed client-side. On failure the first line is
    `[web_map] error=<category> provider=tavily [status=<HTTP status>]` (categories as in web_fetch: auth,
    quota, rate_limit, timeout, network, upstream_error, bad_request, unsupported_site (Tavily refuses the
    site; not a key problem), http_error, disabled, not_configured, ...), then a
    short message and a redacted detail line. Local/LAN addresses and non-http(s) URLs are refused locally
    with `[web_map] error=invalid_url reason=...` (Tavily runs in the cloud and cannot reach them).

    **Edge Cases & Best Practices:**
        - Start with low max_depth (1-2) for initial exploration, increase if needed.
        - Use allow_external=false to stay on the start host; add select_domains (with allow_external=false)
          to allow several hosts of the same site.
        - Use instructions to filter for specific content (e.g., "only documentation pages").
        - Large sites may hit timeout limits; adjust timeout and limit parameters accordingly.
    """),
    meta={"version": "1.6.0", "author": "guda.studio"},
)
async def web_map(
    url: Annotated[str, "Root URL to begin the mapping (e.g., 'https://docs.example.com')."],
    instructions: Annotated[str, "Natural language instructions for the crawler to filter or focus on specific content."] = "",
    max_depth: Annotated[int, Field(description="Maximum depth of mapping from the base URL.", ge=1, le=5)] = 1,
    max_breadth: Annotated[int, Field(description="Maximum number of links to follow per page.", ge=1, le=500)] = 20,
    limit: Annotated[int, Field(description="Total number of links to process before stopping.", ge=1, le=500)] = 50,
    timeout: Annotated[int, Field(description="Maximum time in seconds for the operation.", ge=10, le=150)] = 150,
    allow_external: Annotated[bool, "Include links to other domains and subdomains in the results (default true). false keeps only URLs on the start URL's host (www. counts as the same host; other subdomains are removed client-side), or, with select_domains, on hosts matching those regexes."] = True,
    select_paths: Annotated[list[str] | None, "Regex patterns; only URLs whose path matches are returned, e.g. ['/docs/.*']."] = None,
    select_domains: Annotated[list[str] | None, "Regex patterns (Python syntax, at most 20 of at most 300 characters) restricting the crawl to matching domains or subdomains, e.g. ['^docs\\.example\\.com$']. With allow_external=false the results are also filtered client-side to hosts matching one of them (re.search on the host, in a time-limited child process); with allow_external=true they only steer the crawl."] = None,
    exclude_paths: Annotated[list[str] | None, "Regex patterns; URLs whose path matches are skipped, e.g. ['/blog/.*']."] = None,
    exclude_domains: Annotated[list[str] | None, "Regex patterns; matching domains or subdomains are skipped."] = None,
) -> str:
    result = await _call_tavily_map(url, instructions, max_depth, max_breadth, limit, timeout,
                                    allow_external, select_paths, select_domains, exclude_paths, exclude_domains)
    return result


_CONNECTION_TEST_ATTEMPTS = 2
# Connection reset/refused, a closed proxy tunnel or a dropped connection; timeouts are not retried.
_RETRYABLE_TRANSPORT_ERRORS = (httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProxyError)


def _transport_error_text(exc: BaseException) -> str:
    """One line for a transport failure, useful even when the exception text is empty."""
    name = type(exc).__name__
    text = redact(str(exc), 200)
    if text:
        return f"{name}: {text}"
    message = f"proxy tunnel closed or upstream unreachable ({name})"
    proxy = next((os.environ[k] for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy") if os.environ.get(k)), "")
    if proxy:
        message += f"; via proxy {mask_url(proxy)}"
    return message


def _masked_breaker_snapshot() -> dict:
    """breaker.snapshot() with credentials in the API URL part of each key hidden."""
    snapshot = breaker.snapshot()
    try:
        raw_url = config.grok_api_url
    except ValueError:
        return snapshot
    masked = mask_url(raw_url)
    if masked == raw_url:
        return snapshot
    return {key.replace(raw_url, masked): value for key, value in snapshot.items()}


@mcp.tool(
    name=_tool_name("get_config_info"),
    output_schema=None,
    annotations=_READ_ONLY_WEB,
    description=_tool_description("""
    Returns current Grok Search MCP server configuration, circuit-breaker state, and tests API connectivity.

    **Key Features:**
        - **Configuration Check:** Verifies environment variables and current settings, including which
          optional engines (Tavily, Firecrawl) are actually active.
        - **Breaker State:** Shows which model tiers are currently cooling down after rate limits.
        - **Connection Test:** Sends request to /models endpoint to validate API access; a transport
          failure (connection reset or refused, proxy tunnel closed) is retried once.
        - **Model Discovery:** Lists all available models from the API (available_models) and refreshes
          the cached list that web_search(model=...) is validated against. chat_models are the ones
          web_search / switch_model accept; non_chat_models (TTS/voice/audio, image/video generation,
          embedding, moderation, realtime, rerank, recognised by name unless the gateway's /models entry
          declares text output, as OpenRouter's architecture.output_modalities does) are listed but rejected.
          connection_test.current_model_listed says whether GROK_MODEL (its base name) is on that list; when
          it is not (for example a gateway without the built-in default, grok-4.20-multi-agent-0309(high)),
          config_warnings says so and names close chat models.
        - **Model Source:** GROK_MODEL_SOURCE is env, switch_model (set in this process), config
          (config file) or default; GROK_MODEL_NOTE says how long a switch_model choice lasts.
        - **Warnings:** config_warnings lists environment values that were not understood (their
          default was used) and config-file or log-directory problems.

    **Edge Cases & Best Practices:**
        - Use this tool first when debugging connection or configuration issues.
        - API keys are masked (at most a quarter of a key is shown) and credentials in URLs are hidden.
        - Connection test timeout is 10 seconds per attempt; network issues may cause delays.
    """),
    meta={"version": "1.8.0", "author": "guda.studio"},
)
async def get_config_info() -> str:
    import json
    import time
    import httpx

    config_info = config.get_config_info()
    config_info["server_version"] = _package_version()
    config_info["breaker"] = _masked_breaker_snapshot()
    if _SANITIZED_ENV:
        config_info["NO_PROXY_SANITIZED"] = list(_SANITIZED_ENV)

    test_result = {"status": "未测试", "message": "", "response_time_ms": 0}
    listed_models: list[str] = []
    try:
        api_url = config.grok_api_url
        api_key = config.grok_api_key
        models_url = f"{api_url.rstrip('/')}/models"
        start_time = time.time()
        for attempt in range(_CONNECTION_TEST_ATTEMPTS):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(
                        models_url,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    )
                break
            except _RETRYABLE_TRANSPORT_ERRORS as e:
                if attempt + 1 >= _CONNECTION_TEST_ATTEMPTS:
                    raise
                test_result["retried_after"] = _transport_error_text(e)
                await asyncio.sleep(0.5)
        test_result["response_time_ms"] = round((time.time() - start_time) * 1000, 2)
        if response.status_code == 200:
            test_result["status"] = "✅ 连接成功"
            test_result["message"] = f"成功获取模型列表 (HTTP {response.status_code})"
            try:
                models_data = response.json()
                if "data" in models_data and isinstance(models_data["data"], list):
                    test_result["message"] += f"，共 {len(models_data['data'])} 个模型"
                    model_names = [m["id"] for m in models_data["data"] if isinstance(m, dict) and isinstance(m.get("id"), str)]
                    if model_names:
                        listed_models = model_names
                        remember_capabilities(api_url, models_data["data"])
                        test_result["available_models"] = model_names
                        test_result["chat_models"] = [m for m in model_names if not model_kind(m, api_url)]
                        test_result["non_chat_models"] = [m for m in model_names if model_kind(m, api_url)]
                        _MODEL_CACHE.put(api_url, api_key, model_names)
                        test_result["model_cache"] = "refreshed"
            except Exception:
                pass
        else:
            test_result["status"] = "⚠️ 连接异常"
            # A Cloudflare 52x or other HTML error page is named, not quoted.
            summary = summarize_error_body(
                response.text, status=response.status_code, limit=100,
                content_type=response.headers.get("content-type", ""), server=response.headers.get("server", ""),
            )
            test_result["message"] = f"HTTP {response.status_code}: {summary}"
    except ValueError as e:  # GROK_API_URL / GROK_API_KEY missing
        test_result["status"] = "❌ 配置错误"
        test_result["message"] = str(e)
    except Exception as e:
        error = classify_exception("grok", e)
        test_result["status"] = {
            "timeout": "❌ 连接超时",
            "proxy_config": "❌ 本地代理环境错误",
            "network": "❌ 连接失败",
            "config": "❌ 配置错误",
        }.get(error.category, "❌ 测试失败")
        if isinstance(e, _RETRYABLE_TRANSPORT_ERRORS) and not str(e).strip():
            test_result["message"] = f"grok: {error.category} — {_transport_error_text(e)}"
        else:
            test_result["message"] = error.summary()
        test_result["error_category"] = error.category
        if "retried_after" in test_result:
            test_result["attempts"] = _CONNECTION_TEST_ATTEMPTS

    # After the connection test: a /models list that declares capabilities (OpenRouter) overrides the name.
    try:
        default_kind = model_kind(routing_base(config_info.get("GROK_MODEL") or ""), config.grok_api_url)
    except ValueError:  # GROK_API_URL missing
        default_kind = non_chat_kind(routing_base(config_info.get("GROK_MODEL") or ""))
    if default_kind:
        config_info.setdefault("config_warnings", []).append(
            f"GROK_MODEL={config_info['GROK_MODEL']}（来源: {config_info.get('GROK_MODEL_SOURCE')}）看起来不是对话/搜索模型"
            f"（{default_kind}），web_search 会被上游拒绝；请用 switch_model 或 GROK_MODEL 换成 chat_models 中的模型"
        )
    elif listed_models:
        # A configured model, or the built-in default when GROK_MODEL is unset, is not served by every gateway.
        current = config_info.get("GROK_MODEL") or ""
        base = routing_base(current)
        listed = current in listed_models or base_is_listed(base, listed_models)
        test_result["current_model_listed"] = listed
        if not listed:
            suggestions = suggest_models(base, listed_models)
            config_info.setdefault("config_warnings", []).append(
                f"GROK_MODEL={current}（来源: {config_info.get('GROK_MODEL_SOURCE')}）不在网关的 /models 列表中，"
                "web_search 很可能被上游拒绝；请用 switch_model 或在 MCP 配置里设置 GROK_MODEL 换成 chat_models 中的模型"
                + (f"，例如 {', '.join(suggestions)}" if suggestions else "")
            )
    config_info["connection_test"] = test_result
    return json.dumps(config_info, ensure_ascii=False, indent=2)


@mcp.tool(
    name=_tool_name("switch_model"),
    output_schema=None,
    description=_tool_description("""
    Switches the default Grok model used for search operations, persisting the setting.

    **Key Features:**
        - **Validation:** The name is stripped and checked against the proxy's /models list the same
          way as web_search(model=...): a thinking suffix such as "(high)" is accepted, an empty,
          over-long or unlisted name is rejected and nothing changes. An unsupported suffix on an unlisted
          model reports both problems at once. Non-chat models (TTS/voice/audio,
          image/video generation, embedding, moderation, realtime, rerank) are rejected even when
          /models lists them. When /models cannot be reached the switch is applied unverified, with a
          warning.
        - **Already active:** Switching to the model already in use changes nothing (no file write,
          GROK_MODEL_SOURCE stays as it was) and answers with already_active=true.
        - **Persistent Storage:** Model preference saved to ~/.config/grok-search/config.json (a private
          temp directory, with a warning, when that is not writable).
        - **Immediate Effect:** New model used for all subsequent operations in this process.

    **Edge Cases & Best Practices:**
        - Use get_config_info to see the available models and GROK_MODEL_SOURCE.
        - When the GROK_MODEL environment variable is set it takes precedence over the config file, so
          the switch lasts only for this process and GROK_MODEL wins again after a restart; the response
          carries a warning in that case. Switching back to the GROK_MODEL value restores
          GROK_MODEL_SOURCE=env.
    """),
    meta={"version": "1.7.0", "author": "guda.studio"},
)
async def switch_model(
    model: Annotated[str, "Model ID to switch to (e.g., 'grok-4.7', 'grok-4.20-multi-agent-0309(high)'). Must be non-empty."]
) -> str:
    import json

    def reply(**data) -> str:
        return json.dumps(data, ensure_ascii=False, indent=2)

    name = (model or "").strip()
    problem = model_name_problem(name)
    if problem:
        return reply(status="❌ 失败", message=f"切换模型失败: {problem}；未做任何修改")

    try:
        previous_model = config.grok_model
        warnings: list[str] = []
        check = None
        try:
            api_url, api_key = config.grok_api_url, config.grok_api_key
        except ValueError:
            warnings.append("GROK_API_URL / GROK_API_KEY 未配置，未能通过 /models 校验模型名")
        else:
            check = await check_model(name, default_model=previous_model, api_url=api_url, api_key=api_key, cache=_MODEL_CACHE)
            if not check.ok:
                detail = check.detail
                if check.suggestions:
                    detail += "；相近的可用模型: " + ", ".join(check.suggestions)
                return reply(status="❌ 失败", message=f"切换模型失败: {detail}；未做任何修改",
                             current_model=previous_model, model_check=check.to_dict())
            if check.status == "unverified":
                warnings.append(f"未能通过 /models 校验模型名（{check.detail}），已按原样切换")

        already_active = check.status == "default" if check is not None else config._apply_model_suffix(name) == previous_model
        if already_active:
            config_file = config.config_file
            result = {
                "status": "✅ 成功",
                "previous_model": previous_model,
                "current_model": previous_model,
                "message": f"{previous_model} 已是当前模型（来源: {config.grok_model_source}），无需切换；未做任何修改",
                "config_file": str(config_file) if config_file else None,
                "model_source": config.grok_model_source,
                "already_active": True,
            }
            if warnings:
                result["warning"] = "\n".join(warnings)
            return reply(**result)

        save_warning = config.set_model(name)
        current_model = config.grok_model
        config_file = config.config_file

        result = {
            "status": "✅ 成功",
            "previous_model": previous_model,
            "current_model": current_model,
            "message": f"模型已从 {previous_model} 切换到 {current_model}",
            "config_file": str(config_file) if config_file else None,
            "model_source": config.grok_model_source,
        }
        if check is not None:
            result["model_check"] = check.to_dict()
        if save_warning:
            warnings.append(save_warning)
        elif config.config_file_is_temporary:
            warnings.append(f"~/.config/grok-search 不可写，配置保存在临时目录 {config_file}，服务器重启后不会保留")
        env_model = (os.getenv("GROK_MODEL") or "").strip()
        if env_model and config._apply_model_suffix(env_model) != current_model:
            warnings.append(
                f"环境变量 GROK_MODEL={env_model} 已设置，它的优先级高于配置文件："
                f"本次切换只在当前进程有效，服务器重启后会恢复为 {env_model}。"
                f"要永久切换，请修改 MCP 配置里的 GROK_MODEL。"
            )
        if warnings:
            result["warning"] = "\n".join(warnings)
        return reply(**result)

    except ValueError as e:
        return reply(status="❌ 失败", message=f"切换模型失败: {str(e)}")
    except Exception as e:
        return reply(status="❌ 失败", message=f"未知错误: {redact(str(e) or type(e).__name__, 300)}")


_PROJECT_MARKERS = (".git", ".claude")


def _is_fs_root(path: Path) -> bool:
    return path == path.parent


def _find_project_root(start: Path) -> Optional[Path]:
    """Nearest directory with .git or .claude at or above start; never the home directory or a filesystem root."""
    try:
        home = Path.home().resolve()
    except Exception:
        home = None
    current = start
    while not _is_fs_root(current) and current != home:
        if any((current / marker).exists() for marker in _PROJECT_MARKERS):
            return current
        current = current.parent
    return None


def _is_home(path: Path) -> bool:
    try:
        return path == Path.home().resolve()
    except Exception:
        return False


def _not_a_project(path: Path) -> str:
    """Why path cannot be the project whose settings are changed ("" when it can)."""
    if _is_fs_root(path):
        return f"拒绝把文件系统根目录当作项目目录: {path}"
    if _is_home(path):
        return (f"拒绝把用户主目录当作项目目录: {path}。~/.claude/settings.json 是用户级设置，改动会影响所有项目；"
                "本工具只改项目级设置，未做任何修改。确需在所有项目屏蔽内置工具时，请自行编辑 ~/.claude/settings.json")
    return ""


def _resolve_project_dir(project_dir: str) -> tuple[Optional[Path], str, str]:
    """(directory, source, error). Order: argument, CLAUDE_PROJECT_DIR, nearest .git/.claude above cwd, the
    working directory itself (Claude Code starts MCP servers in the project directory). The home directory and a
    filesystem root are refused whatever their source."""
    if project_dir and project_dir.strip():
        path = Path(project_dir.strip()).expanduser()
        if not path.is_dir():
            return None, "argument", f"project_dir 不存在或不是目录: {path}"
        path = path.resolve()
        problem = _not_a_project(path)
        return (None, "argument", problem) if problem else (path, "argument", "")
    env_dir = os.getenv("CLAUDE_PROJECT_DIR", "").strip()
    if env_dir and Path(env_dir).is_dir():
        path = Path(env_dir).resolve()
        problem = _not_a_project(path)
        return (None, "CLAUDE_PROJECT_DIR", problem) if problem else (path, "CLAUDE_PROJECT_DIR", "")
    try:
        cwd = Path.cwd().resolve()
    except OSError:  # the working directory was removed or is not accessible
        cwd = None
    found = _find_project_root(cwd) if cwd is not None else None
    if found is not None:
        return found, "cwd", ""
    if cwd is not None and not _not_a_project(cwd):
        return cwd, "working_directory", ""
    return None, "", (
        "无法确定项目目录：未设置 CLAUDE_PROJECT_DIR，从当前目录向上到用户主目录之前都没有 .git 或 .claude，"
        "而工作目录本身是用户主目录、文件系统根目录或无法访问。请用 project_dir 参数传入项目根目录，未做任何修改。"
    )


_BUILTIN_WEB_TOOLS = ("WebFetch", "WebSearch")
# Accepted actions and what they do; "enable"/"disable" are refused because either reading
# (enable the override / disable the built-in tools) is plausible.
_TOGGLE_ACTIONS = {"on": "on", "block": "on", "off": "off", "allow": "off", "status": "status"}
_AMBIGUOUS_TOGGLE_ACTIONS = ("enable", "disable")


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _settings_real_path(root: Path, name: str) -> tuple[Optional[Path], str]:
    """Real path of <root>/.claude/<name>, or an error when a symlink would lead outside the project."""
    link = root / ".claude" / name
    real = Path(os.path.realpath(link))
    if not _is_inside(real, root):
        return None, f"{link} 经符号链接指向项目之外（{real}），拒绝跟随，未做任何修改"
    return real, ""


def _read_settings(path: Path) -> tuple[Optional[dict], str]:
    """({} when missing, the object, or None) and an error message."""
    if not path.exists():
        return {}, ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.loads(f.read())
        if not isinstance(data, dict):
            raise ValueError("top level is not a JSON object")
    except (OSError, ValueError) as e:
        return None, f"无法读取 {path}（{e}）"
    return data, ""


def _denied_web_tools(settings: Optional[dict]) -> list[str]:
    permissions = (settings or {}).get("permissions")
    deny = permissions.get("deny") if isinstance(permissions, dict) else None
    if not isinstance(deny, list):
        return []
    return [t for t in _BUILTIN_WEB_TOOLS if t in deny]


def _other_settings_denies(root: Path, project_file: Path) -> tuple[dict, list[str]]:
    """{file: denied tools} from settings.local.json and ~/.claude/settings.json (read only), and notes."""
    denies: dict = {}
    notes: list[str] = []
    candidates = []
    local, error = _settings_real_path(root, "settings.local.json")
    if local is None:
        notes.append(error.replace("，未做任何修改", "，未读取"))
    else:
        candidates.append(local)
    try:
        candidates.append(Path.home() / ".claude" / "settings.json")
    except Exception:
        pass
    for path in candidates:
        if Path(os.path.realpath(path)) == project_file:
            continue
        settings, error = _read_settings(path)
        if settings is None:
            notes.append(error)
            continue
        denied = _denied_web_tools(settings)
        if denied:
            denies[str(path)] = denied
    return denies, notes


def _short_path(path: Path, root: Path) -> str:
    """A settings path for messages: relative to the project, or ~/... for the user's own files."""
    for base, prefix in ((root, ""), (Path.home(), "~/")):
        try:
            return prefix + str(path.relative_to(base))
        except (ValueError, RuntimeError):
            continue
    return str(path)


def _write_settings(path: Path, settings: dict) -> None:
    """Replace the file through a temp file in the same directory (never through a symlink)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        mode = 0o644
    else:
        if not os.access(path, os.W_OK):  # a replace would succeed, but the file is meant to be read-only
            raise PermissionError(f"[Errno 13] Permission denied: '{path}'")
    fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@mcp.tool(
    name=_tool_name("toggle_builtin_tools"),
    output_schema=None,
    description=_tool_description("""
    Block or allow Claude Code's built-in WebSearch and WebFetch tools for a project.

    **Actions:**
        - "on" (alias "block"): add WebFetch and WebSearch to permissions.deny in
          <project>/.claude/settings.json, so this MCP server's tools are used instead.
        - "off" (alias "allow"): remove them from that file's deny list.
        - "status": report the current state without changing anything.
        Any other value (including the ambiguous "enable"/"disable") is rejected with an error.

    **Project directory:** project_dir if given, else CLAUDE_PROJECT_DIR, else the nearest directory
    containing .git or .claude above the server's working directory, else that working directory itself
    (Claude Code starts MCP servers in the project directory; project_dir_source "working_directory" and a
    warning say so). The home directory (whose .claude/settings.json is the user-level file of every
    project) and a filesystem root are refused from any source; nothing is written then.

    **Other scopes:** Claude Code also applies deny rules from .claude/settings.local.json and
    ~/.claude/settings.json. They are read (never changed) and reported in denied_elsewhere; "off"
    warns when one of them still blocks the tools, and effective shows the combined result.

    **Safety:** a .claude directory or settings file that is a symlink pointing outside the project is
    refused; the file is replaced atomically.
    """),
    meta={"version": "1.6.0", "author": "guda.studio"},
)
async def toggle_builtin_tools(
    action: Annotated[str, Field(
        description="'on' (block the built-in tools), 'off' (allow them again) or 'status' (check the current state). 'block' and 'allow' are accepted as aliases.",
        json_schema_extra={"enum": ["on", "off", "status", "block", "allow"]},
    )] = "status",
    project_dir: Annotated[str, "Project root whose .claude/settings.json is read or changed. Optional: CLAUDE_PROJECT_DIR, a .git/.claude directory above the server's working directory, or that working directory is used. Never the home directory."] = "",
) -> str:
    def reply(**data) -> str:
        return json.dumps(data, ensure_ascii=False, indent=2)

    requested = (action or "status").strip().lower()
    mode = _TOGGLE_ACTIONS.get(requested)
    if mode is None:
        error = (f"未知的 action: {action!r}。可用值: on（屏蔽内置 WebSearch/WebFetch，别名 block）、"
                 f"off（恢复内置工具，别名 allow）、status（只查看）。未做任何修改")
        if requested in _AMBIGUOUS_TOGGLE_ACTIONS:
            error += f"。'{requested}' 有歧义（是停用内置工具，还是停用本服务器的替代？），请改用 on（屏蔽内置工具）或 off（恢复内置工具）"
        return reply(blocked=None, error=error)

    root, source, error = _resolve_project_dir(project_dir)
    if root is None:
        return reply(blocked=None, project_dir=None, error=error)

    tools = list(_BUILTIN_WEB_TOOLS)
    link_path = root / ".claude" / "settings.json"
    settings_path, error = _settings_real_path(root, "settings.json")
    if settings_path is None:
        return reply(blocked=None, project_dir=str(root), file=str(link_path), error=error)
    settings, error = _read_settings(settings_path)
    if settings is None:
        return reply(blocked=None, project_dir=str(root), file=str(settings_path), error=f"{error}，未做任何修改")
    permissions = settings.setdefault("permissions", {})
    deny = permissions.setdefault("deny", []) if isinstance(permissions, dict) else None
    if not isinstance(deny, list):
        return reply(blocked=None, project_dir=str(root), file=str(settings_path),
                     error="permissions / permissions.deny 的类型不正确，未做任何修改")

    if mode == "on":
        for t in tools:
            if t not in deny:
                deny.append(t)
    elif mode == "off":
        deny[:] = [t for t in deny if t not in tools]
    if mode != "status":
        try:
            _write_settings(settings_path, settings)
        except OSError as e:
            return reply(blocked=None, project_dir=str(root), file=str(settings_path), error=f"写入 {settings_path} 失败: {e}")
    blocked = all(t in deny for t in tools)

    elsewhere, notes = _other_settings_denies(root, settings_path)
    denied_anywhere = {t for t in tools if t in deny} | {t for denied in elsewhere.values() for t in denied}
    effective = {t: ("blocked" if t in denied_anywhere else "allowed") for t in tools}
    other_files = "、".join(_short_path(Path(f), root) for f in elsewhere)
    this_file = _short_path(settings_path, root)
    still_denied = ", ".join(t for t in tools if t in denied_anywhere)
    warnings = list(notes)
    if mode == "on":
        msg = "官方工具已禁用"
    elif mode == "off":
        if elsewhere:
            msg = f"{this_file} 已不再屏蔽官方工具，但 {other_files} 仍然屏蔽 {still_denied}：这些工具仍不可用"
            warnings.append(f"要恢复这些工具，还需要从 {other_files} 的 permissions.deny 中删除 {still_denied}（本工具不修改这些文件）")
        else:
            msg = "官方工具已启用"
    elif blocked:
        msg = "官方工具当前已禁用"
    elif len(denied_anywhere) == len(tools):
        msg = f"官方工具当前已禁用（由 {other_files} 屏蔽，不是 {this_file}）"
    elif denied_anywhere:
        msg = f"官方工具部分被禁用: {still_denied}"
    else:
        msg = "官方工具当前已启用"

    result = {"blocked": blocked, "deny_list": deny, "file": str(settings_path), "project_dir": str(root),
              "project_dir_source": source, "effective": effective, "message": msg}
    if settings_path != link_path:
        result["symlink"] = str(link_path)
    if elsewhere:
        result["denied_elsewhere"] = elsewhere
    if source == "working_directory":
        warnings.insert(0, f"没有 CLAUDE_PROJECT_DIR，{root} 及其上层也没有 .git 或 .claude，按服务器的工作目录处理"
                           "（Claude Code 在项目目录中启动 MCP 服务器）；如果这不是项目根目录，请用 project_dir 指定")
    if warnings:
        result["warning"] = "\n".join(warnings)
    return reply(**result)


def register_planning_tools(app: FastMCP) -> None:
    """Six bookkeeping tools that record a client's search plan. Opt-in via GROK_PLANNING_TOOLS=true."""

    @app.tool(
        name=_tool_name("plan_intent"),
        output_schema=None,
        description=_tool_description("""
        Phase 1 of search planning: Analyze user intent. Call this FIRST to create a session.
        Returns session_id for subsequent phases. Required flow:
        plan_intent → plan_complexity → plan_sub_query(×N) → plan_search_term(×N) → plan_tool_mapping(×N) → plan_execution

        Required phases depend on complexity: Level 1 = phases 1-3; Level 2 = phases 1-5; Level 3 = all 6.
        Every plan_* call validates its input (enums, ranges, confidence 0.0-1.0) and returns
        {"error": ..., "invalid_fields": [...]} without recording anything when it is invalid.
        A recorded intent, complexity or execution phase is only replaced with is_revision=true.
        """),
    )
    async def plan_intent(
        thought: Annotated[str, "Reasoning for this phase"],
        core_question: Annotated[str, "Distilled core question in one sentence"],
        query_type: Annotated[str, "factual | comparative | exploratory | analytical"],
        time_sensitivity: Annotated[str, "realtime | recent | historical | irrelevant"],
        session_id: Annotated[str, "Empty for new session, or existing ID to revise"] = "",
        confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
        domain: Annotated[str, "Specific domain if identifiable"] = "",
        premise_valid: Annotated[Optional[bool], "False if the question contains a flawed assumption"] = None,
        ambiguities: Annotated[str, "Comma-separated unresolved ambiguities"] = "",
        unverified_terms: Annotated[str, "Comma-separated external terms to verify"] = "",
        is_revision: Annotated[bool, "True to overwrite existing intent"] = False,
    ) -> str:
        import json
        data = {"core_question": core_question, "query_type": query_type, "time_sensitivity": time_sensitivity}
        if domain:
            data["domain"] = domain
        if premise_valid is not None:
            data["premise_valid"] = premise_valid
        if ambiguities:
            data["ambiguities"] = _split_csv(ambiguities)
        if unverified_terms:
            data["unverified_terms"] = _split_csv(unverified_terms)
        return json.dumps(planning_engine.process_phase(
            phase="intent_analysis", thought=thought, session_id=session_id,
            is_revision=is_revision, confidence=confidence, phase_data=data,
        ), ensure_ascii=False, indent=2)

    @app.tool(
        name=_tool_name("plan_complexity"),
        output_schema=None,
        description="Phase 2: Assess search complexity (1-3). Controls required phases: Level 1 = phases 1-3; Level 2 = phases 1-5; Level 3 = all 6.",
    )
    async def plan_complexity(
        session_id: Annotated[str, "Session ID from plan_intent"],
        thought: Annotated[str, "Reasoning for complexity assessment"],
        level: Annotated[int, "Complexity 1-3"],
        estimated_sub_queries: Annotated[int, "Expected number of sub-queries (1-20)"],
        estimated_tool_calls: Annotated[int, "Expected total tool calls (1-50)"],
        justification: Annotated[str, "Why this complexity level"],
        confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
        is_revision: Annotated[bool, "True to overwrite"] = False,
    ) -> str:
        import json
        if not planning_engine.get_session(session_id):
            return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
        return json.dumps(planning_engine.process_phase(
            phase="complexity_assessment", thought=thought, session_id=session_id,
            is_revision=is_revision, confidence=confidence,
            phase_data={"level": level, "estimated_sub_queries": estimated_sub_queries,
                         "estimated_tool_calls": estimated_tool_calls, "justification": justification},
        ), ensure_ascii=False, indent=2)

    @app.tool(
        name=_tool_name("plan_sub_query"),
        output_schema=None,
        description="Phase 3: Add one sub-query. Call once per sub-query; data accumulates across calls. Set is_revision=true to replace all.",
    )
    async def plan_sub_query(
        session_id: Annotated[str, "Session ID from plan_intent"],
        thought: Annotated[str, "Reasoning for this sub-query"],
        id: Annotated[str, "Unique ID (e.g., 'sq1')"],
        goal: Annotated[str, "Sub-query goal"],
        expected_output: Annotated[str, "What success looks like"],
        boundary: Annotated[str, "What this excludes — mutual exclusion with siblings"],
        confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
        depends_on: Annotated[str, "Comma-separated prerequisite IDs"] = "",
        tool_hint: Annotated[str, "web_search | web_fetch | web_map"] = "",
        is_revision: Annotated[bool, "True to replace all sub-queries"] = False,
    ) -> str:
        import json
        if not planning_engine.get_session(session_id):
            return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
        item = {"id": id, "goal": goal, "expected_output": expected_output, "boundary": boundary}
        if depends_on:
            item["depends_on"] = _split_csv(depends_on)
        if tool_hint:
            item["tool_hint"] = tool_hint
        return json.dumps(planning_engine.process_phase(
            phase="query_decomposition", thought=thought, session_id=session_id,
            is_revision=is_revision, confidence=confidence, phase_data=item,
        ), ensure_ascii=False, indent=2)

    @app.tool(
        name=_tool_name("plan_search_term"),
        output_schema=None,
        description="Phase 4: Add one search term. Call once per term; data accumulates. First call must set approach.",
    )
    async def plan_search_term(
        session_id: Annotated[str, "Session ID from plan_intent"],
        thought: Annotated[str, "Reasoning for this search term"],
        term: Annotated[str, "Search query (max 8 words)"],
        purpose: Annotated[str, "Sub-query ID this serves (e.g., 'sq1')"],
        round: Annotated[int, "Execution round: 1=broad, 2+=targeted follow-up"],
        confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
        approach: Annotated[str, "broad_first | narrow_first | targeted (required on first call)"] = "",
        fallback_plan: Annotated[str, "Fallback if primary searches fail"] = "",
        is_revision: Annotated[bool, "True to replace all search terms"] = False,
    ) -> str:
        import json
        if not planning_engine.get_session(session_id):
            return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
        data = {"search_terms": [{"term": term, "purpose": purpose, "round": round}]}
        if approach:
            data["approach"] = approach
        if fallback_plan:
            data["fallback_plan"] = fallback_plan
        return json.dumps(planning_engine.process_phase(
            phase="search_strategy", thought=thought, session_id=session_id,
            is_revision=is_revision, confidence=confidence, phase_data=data,
        ), ensure_ascii=False, indent=2)

    @app.tool(
        name=_tool_name("plan_tool_mapping"),
        output_schema=None,
        description="Phase 5: Map a sub-query to a tool. Call once per mapping; data accumulates.",
    )
    async def plan_tool_mapping(
        session_id: Annotated[str, "Session ID from plan_intent"],
        thought: Annotated[str, "Reasoning for this mapping"],
        sub_query_id: Annotated[str, "Sub-query ID to map"],
        tool: Annotated[str, "web_search | web_fetch | web_map"],
        reason: Annotated[str, "Why this tool for this sub-query"],
        confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
        params_json: Annotated[str, "Optional JSON string for tool-specific params"] = "",
        is_revision: Annotated[bool, "True to replace all mappings"] = False,
    ) -> str:
        import json
        if not planning_engine.get_session(session_id):
            return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
        item = {"sub_query_id": sub_query_id, "tool": tool, "reason": reason}
        if params_json:
            try:
                item["params"] = json.loads(params_json)
            except json.JSONDecodeError as e:
                return json.dumps({"error": f"params_json is not valid JSON ({e}); nothing was recorded",
                                   "phase": "tool_selection"}, ensure_ascii=False)
        return json.dumps(planning_engine.process_phase(
            phase="tool_selection", thought=thought, session_id=session_id,
            is_revision=is_revision, confidence=confidence, phase_data=item,
        ), ensure_ascii=False, indent=2)

    @app.tool(
        name=_tool_name("plan_execution"),
        output_schema=None,
        description="Phase 6: Define execution order. parallel_groups: semicolon-separated groups of comma-separated IDs (e.g., 'sq1,sq2;sq3').",
    )
    async def plan_execution(
        session_id: Annotated[str, "Session ID from plan_intent"],
        thought: Annotated[str, "Reasoning for execution order"],
        parallel_groups: Annotated[str, "Parallel batches: 'sq1,sq2;sq3,sq4' (semicolon=groups, comma=IDs)"],
        sequential: Annotated[str, "Comma-separated IDs that must run in order"],
        estimated_rounds: Annotated[int, "Estimated execution rounds (at least 1)"],
        confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
        is_revision: Annotated[bool, "True to overwrite"] = False,
    ) -> str:
        import json
        if not planning_engine.get_session(session_id):
            return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
        parallel = [_split_csv(g) for g in parallel_groups.split(";") if g.strip()] if parallel_groups else []
        seq = _split_csv(sequential)
        return json.dumps(planning_engine.process_phase(
            phase="execution_order", thought=thought, session_id=session_id,
            is_revision=is_revision, confidence=confidence,
            phase_data={"parallel": parallel, "sequential": seq, "estimated_rounds": estimated_rounds},
        ), ensure_ascii=False, indent=2)


if config.planning_tools_enabled:
    register_planning_tools(mcp)


def main():
    import signal
    import threading

    # 信号处理（仅主线程）
    if threading.current_thread() is threading.main_thread():
        def handle_shutdown(signum, frame):
            _kill_matchers()
            os._exit(0)
        signal.signal(signal.SIGINT, handle_shutdown)
        if sys.platform != 'win32':
            signal.signal(signal.SIGTERM, handle_shutdown)

    # Windows 父进程监控
    if sys.platform == 'win32':
        import time
        import ctypes
        parent_pid = os.getppid()

        def is_parent_alive(pid):
            """Windows 下检查进程是否存活"""
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return True
            exit_code = ctypes.c_ulong()
            result = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            return result and exit_code.value == STILL_ACTIVE

        def monitor_parent():
            while True:
                if not is_parent_alive(parent_pid):
                    os._exit(0)
                time.sleep(2)

        threading.Thread(target=monitor_parent, daemon=True).start()

    # httpx cannot parse bracketed IPv6 entries in NO_PROXY; repair them before any client is built.
    global _SANITIZED_ENV
    _SANITIZED_ENV = sanitize_no_proxy_env()

    # os._exit skips interpreter shutdown (stdio reader threads could otherwise keep the process
    # alive), so a crash is logged and reported with a non-zero status first.
    exit_code = 0
    try:
        mcp.run(transport="stdio", show_banner=False)
    except KeyboardInterrupt:
        pass
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        if exit_code:
            _report_fatal_error()
    except BaseException:
        exit_code = 1
        _report_fatal_error()
    finally:
        _kill_matchers()
        os._exit(exit_code)


def _report_fatal_error() -> None:
    """Write the active exception's traceback to stderr and the log file before a hard exit."""
    import traceback
    try:
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
    except Exception:
        pass
    try:
        logger.critical("grok-search server crashed", exc_info=True)
        for handler in logger.handlers:
            handler.flush()
    except Exception:
        pass


if __name__ == "__main__":
    main()
