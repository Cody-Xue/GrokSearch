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
    from grok_search.sources import SourcesCache, merge_sources, new_session_id, sources_from_annotations, sources_from_text, split_answer_and_sources, url_identity as _url_identity
    from grok_search.planning import engine as planning_engine, _split_csv
    from grok_search.throttle import breaker, BreakerOpen
    from grok_search.verify import verify_answer
    from grok_search.fetching import fetch_page, url_problem
    from grok_search.citations import normalize_citations
    from grok_search.errors import ProviderError, classify_exception, redact, sanitize_no_proxy_env, summarize_error_body
    from grok_search.models import ModelListCache, check_model, model_name_problem
except ImportError:
    from .providers.grok import GrokResponse, GrokSearchProvider, GrokUpstreamError
    from .logger import log_info, log_error, logger
    from .config import config, mask_url
    from .sources import SourcesCache, merge_sources, new_session_id, sources_from_annotations, sources_from_text, split_answer_and_sources, url_identity as _url_identity
    from .planning import engine as planning_engine, _split_csv
    from .throttle import breaker, BreakerOpen
    from .verify import verify_answer
    from .fetching import fetch_page, url_problem
    from .citations import normalize_citations
    from .errors import ProviderError, classify_exception, redact, sanitize_no_proxy_env, summarize_error_body
    from .models import ModelListCache, check_model, model_name_problem

import asyncio
import json
import os
import re
import tempfile
from urllib.parse import urlparse

import httpx


def _package_version() -> str:
    try:
        from importlib.metadata import version as _version
        return _version("grok-search")
    except Exception:
        return "1.12.0"


mcp = FastMCP("grok-search", version=_package_version())

# Claude Code runs MCP tools concurrently only when they declare readOnlyHint;
# every tool that does not mutate anything is annotated so a parallel fan-out
# of searches is executed in parallel instead of one after another.
_READ_ONLY_WEB = {"readOnlyHint": True, "openWorldHint": True}
_READ_ONLY_LOCAL = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}

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


async def _run_engine(engine: str, query: str, limit: int) -> list[dict]:
    if engine == "tavily":
        results = await _call_tavily_search(query, limit)
    else:
        results = await _call_firecrawl_search(query, limit)
    return _engine_items(engine, results)


class _ExtraSources:
    """extra_sources strategy.

    1. Share: with both engines Tavily gets ceil(n/2) and Firecrawl floor(n/2); one engine gets n.
    2. Each engine is asked for its share + 2 (at most 20), in parallel with the Grok search, so
       duplicates can be absorbed without another round trip.
    3. Dedup: URLs Grok already cited and repeats across engines are dropped (scheme, "www.",
       fragment, trailing slash and tracking parameters are ignored).
    4. Selection: round-robin across engines, keeping each engine's own ranking.
    5. Backfill: if an engine failed or the selection is still short, every engine that answered
       and may have more results is asked once more for (previous request + shortfall + 2 + the
       duplicates skipped so far, at most 20), so new duplicates do not leave the request short again.
    """

    def __init__(self, query: str, wanted: int):
        self.query = query
        self.wanted = wanted
        self.engines = _extra_engines()
        self.shares = _split_shares(wanted, self.engines)
        self.items: dict[str, list[dict]] = {engine: [] for engine in self.engines}
        self.state: dict[str, dict] = {engine: {"share": self.shares.get(engine, 0)} for engine in self.engines}

    async def _ask(self, engine: str, limit: int, phase: str) -> None:
        state = self.state[engine]
        state[f"{phase}_asked"] = limit
        try:
            got = await _run_engine(engine, self.query, limit)
        except Exception as e:
            state["status"] = "error"
            state["error"] = classify_exception(engine, e).to_dict()
            return
        state["status"] = "ok"
        state[f"{phase}_received"] = len(got)
        state["exhausted"] = len(got) < limit
        known = {_url_identity(item["url"]) for item in self.items[engine]}
        for item in got:
            key = _url_identity(item["url"])
            if key not in known:
                known.add(key)
                self.items[engine].append(item)

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
                    key = _url_identity(item["url"])
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
        exclude = {_url_identity(url) for url in exclude_urls}
        picked, duplicates = self._select(exclude)
        for engine in self.engines:
            shortfall = self.wanted - len(picked)
            if shortfall <= 0:
                break
            state = self.state[engine]
            if state.get("status") == "error" or state.get("exhausted"):
                continue
            previous = state.get("first_asked", 0)
            limit = min(previous + shortfall + _EXTRA_MARGIN + duplicates, _EXTRA_MAX_PER_CALL)
            if limit <= previous:
                continue
            await self._ask(engine, limit, "backfill")
            picked, duplicates = self._select(exclude)
        for engine in self.engines:
            self.state[engine].setdefault("status", "not_needed")
            self.state[engine]["selected"] = sum(1 for item in picked if item["provider"] == engine)
        report = {"requested": self.wanted, "returned": len(picked), "duplicates_skipped": duplicates, "engines": self.state}
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


def _counts(cited: list[dict], extra: list[dict], total: list[dict]) -> dict:
    return {
        "sources_count": len(cited),
        "distinct_domains": _distinct_domains(cited),
        "extra_sources_count": len(extra),
        "total_sources_count": len(total),
    }


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
    name="web_search",
    output_schema=None,
    annotations=_READ_ONLY_WEB,
    description="""
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
    - sources_count / distinct_domains: how many sources Grok cited and on how many distinct sites (native
      citations; when the upstream sends none, the markers, links and URLs written in the answer).
      extra_sources hits are not included: extra_sources_count counts them and total_sources_count is
      cited + extra (the length of the get_sources list)
    - citation_positions: native citation offsets that were valid, relocated or removed after repeated
      markers were collapsed; offsets returned by get_sources always refer to this content and count
      Unicode code points (not UTF-16 units)
    - incomplete / finish_reason / native_finish_reason (only when the upstream cut the answer short, e.g.
      output cap or content filter): the answer is partial; a one-line note is appended after it
    - usage (when the upstream reports it): prompt_tokens, completion_tokens, reasoning_tokens
    - verification (when enabled): existence and reachability only, for the arXiv IDs / DOIs in the
      answer and the URLs Grok cited (extra sources are not checked). Identifiers are resolved against
      arXiv, Crossref and the doi.org registry (DataCite), URLs are fetched, and paper titles written next
      to an identifier are compared with the registry title (title_checks: match / partial /
      possible_mismatch / not_stated). It does NOT check whether a source supports the sentence it is
      attached to (claim_support = "not_checked"). Treat unresolved (the registry or site said it does not
      exist: 404/410, other 4xx except 401/403/451, unknown domain) and possible_mismatch entries as
      suspect; treat lookup_failed (rate limit, 5xx, timeout, network, or a site that refuses scripted
      requests with 401/403/451) as unknown, not as evidence against the citation; skipped lists what was
      not checked and why (including DOI patterns written in prose, such as "10.48550/arXiv.*").
    - extra_sources_report (when extra_sources > 0): share, results, errors and backfill per engine
    - model_check (when model is given and differs from the default, also on failure): how the name was validated
    - error / error_type / retry_after_s / status / exhausted (failures only). error_type is one of:
      config (GROK_API_URL / GROK_API_KEY missing or invalid), proxy_config, invalid_model, rate_limit
      (exhausted=true: this model's account pool is blocked), circuit_open, timeout (no complete answer in
      time, or gateway 522/524), network (connection/proxy failure), truncated_stream, empty_response,
      incomplete (the upstream stopped before any content, e.g. finish_reason length or content_filter;
      finish_reason / native_finish_reason / usage are included; rephrase or narrow the query instead of
      repeating it), auth (the gateway has no usable credential for the model), http_<status>, unknown, or an error code
      passed through from the upstream stream (for example invalid_request_error, model_not_found,
      server_error, upstream_error). Wait retry_after_s seconds or switch model before calling again
    """,
    meta={"version": "2.3.0", "author": "guda.studio"},
)
async def web_search(
    query: Annotated[str, "Clear, self-contained natural-language search query."],
    platform: Annotated[str, "Target platform to focus on (e.g., 'Twitter', 'GitHub', 'Reddit'). Leave empty for general web search."] = "",
    model: Annotated[str, "Optional model ID for this request only. This value is used ONLY when user explicitly provided. A thinking suffix such as 'grok-4(high)' or 'grok-4(8192)' is accepted: the base name is checked against /models and the full name is sent."] = "",
    extra_sources: Annotated[int, Field(description="Number of additional results from the independent engines (Tavily/Firecrawl), 0-20, appended as an 'Extra sources' section and counted in extra_sources_count, not in sources_count. With both engines active the share is split (Tavily ceil(n/2), Firecrawl floor(n/2)); URLs Grok already cited are skipped and a failed engine is backfilled by the other. Default 0.", ge=0, le=_EXTRA_SOURCES_MAX)] = 0,
    instructions: Annotated[str, "Optional per-call instructions for the searcher, e.g. 'list at least 15 distinct sources' or 'return arXiv IDs with titles, no analogies'."] = "",
    verify: Annotated[bool, "Attach the verification layer: identifier existence, link reachability and title comparison (NOT whether sources support the claims). Default true; identifier lookups follow GROK_VERIFY_IDS and URL checks GROK_VERIFY_URLS (both off: no verification)."] = True,
) -> dict:
    session_id = new_session_id()
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
    extra_job = _ExtraSources(query, wanted_extra) if wanted_extra > 0 else None
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
        # heading was split off) and its inline markers, Markdown links and URLs are the cited sources.
        answer, _, _ = normalize_citations(answer, [])
        grok_sources = merge_sources(sources_from_text(answer), grok_sources)
    for item in grok_sources:
        item.setdefault("provider", "grok")
    cited_sources = merge_sources(native_sources, grok_sources)

    extra: list[dict] = []
    extra_report = None
    if extra_job is not None:
        extra, extra_report = await extra_job.finish(item["url"] for item in cited_sources)
    all_sources = merge_sources(cited_sources, extra)

    await _SOURCES_CACHE.set(session_id, all_sources)
    # Counts describe what Grok cited; extra-source hits are reported separately.
    counts = _counts(cited_sources, extra, all_sources)

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
                      "usage", "dropped_events"):
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
            # Only what Grok cited is verified; extra_sources hits are search results, not citations.
            result["verification"] = await verify_answer(
                grok_answer,
                cited_sources,
                check_ids=config.verify_ids,
                check_urls=config.verify_urls,
                timeout_s=config.verify_timeout_s,
                mailto=config.verify_mailto,
                fake_ip=config.verify_fake_ip,
                doh_url=config.verify_doh_url,
            )
        except Exception as e:
            result["verification"] = {"error": f"{type(e).__name__}: {e}", "claim_support": "not_checked"}

    return result


@mcp.tool(
    name="get_sources",
    annotations=_READ_ONLY_LOCAL,
    description="""
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
    """,
    meta={"version": "1.1.0", "author": "guda.studio"},
)
async def get_sources(
    session_id: Annotated[str, "Session ID from previous web_search call."]
) -> dict:
    sources = await _SOURCES_CACHE.get(session_id)
    if sources is None:
        return {
            "session_id": session_id,
            "sources": [],
            "sources_count": 0,
            "error": "session_id_not_found_or_expired",
        }
    extra_count = sum(1 for item in sources if isinstance(item, dict) and item.get("provider") in _EXTRA_PROVIDERS)
    return {
        "session_id": session_id,
        "sources": sources,
        "sources_count": len(sources),
        "cited_sources_count": len(sources) - extra_count,
        "extra_sources_count": extra_count,
    }


async def _call_tavily_search(query: str, max_results: int = 6) -> list[dict]:
    """Tavily Search for extra_sources. Raises ProviderError on failure, also when disabled or unconfigured."""
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


async def _call_firecrawl_search(query: str, limit: int = 14) -> list[dict]:
    """Firecrawl Search for extra_sources. Raises ProviderError on failure."""
    import httpx
    api_key = config.firecrawl_api_key
    if not api_key:
        raise ProviderError("firecrawl", "not_configured", "FIRECRAWL_API_KEY is not set")
    endpoint = f"{config.firecrawl_api_url.rstrip('/')}/search"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
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
    name="web_fetch",
    output_schema=None,
    annotations=_READ_ONLY_WEB,
    description="""
    Fetches a URL and returns its content as Markdown. Read-only: several fetches may run in parallel.

    Backends (all run in the cloud): arXiv abstract pages use the arXiv API (complete metadata and abstract;
    a requested version such as .../abs/1706.03762v1 is honoured; cached for an hour; when the API is slow or
    throttled the extractors take over within GROK_FETCH_ARXIV_BUDGET_S, default 8 s, and the API is skipped
    for 2 minutes: `arxiv-api:cooldown`). Other pages use Tavily Extract, falling back to Firecrawl when the
    extract is missing or too short.

    The first line is a metadata header:
    `[web_fetch] source=<backend> total_chars=N offset=O returned=R truncated=true|false [next_offset=X snapshot_id=S expires_in_s=T] [attempts=...] [warning=interstitial] [final_url=<url> title="<title>"]`
    The default page is GROK_FETCH_MAX_CHARS (40,000) characters, or GROK_FETCH_MAX_CHARS_CJK (20,000) when
    more than 30% of it is CJK text (about one token per character), to stay under the ~25k-token MCP result
    limit; an explicit max_chars is used as given.
    A page longer than one response is kept as a server-side snapshot. To read on, call again with the
    same url, `offset=next_offset` and `snapshot_id=S`: every page then comes from exactly the same text.
    offset > 0 without snapshot_id is refused; when a snapshot has expired, start again at offset 0.
    final_url and title appear when Firecrawl was redirected. `warning=interstitial` plus a `注意:` line mean
    the text is a login wall or anti-bot check (a redirect to a sign-in page, titles like "安全验证" or
    "Just a moment..."), not the requested page: use another source.

    On failure the first line is `[web_fetch] error=...`, followed by one line per backend with an error
    category (auth, quota, rate_limit, timeout, proxy_config, network, upstream_error, bad_request,
    extract_failed, empty_content, disabled, not_configured), the HTTP status and a redacted detail.
    A page that itself answered HTTP >= 400 counts as failed ("target page answered HTTP 404"), and the
    first line adds `page_status=404`. Not an absolute http(s) URL with a host, localhost, or a
    loopback/private/link-local/CGNAT IP literal: refused locally without any backend call, as
    `[web_fetch] error=invalid_url reason=<malformed|missing_scheme|unsupported_scheme|missing_host|local_address>`
    (host names are not resolved). The cloud backends cannot reach your machine or LAN.

    **Edge Cases & Best Practices:**
        - Ensure URL is complete and accessible (not behind authentication or paywalls).
        - May not capture dynamically loaded content requiring JavaScript execution.
    """,
    meta={"version": "1.6.0", "author": "guda.studio"},
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
    for key, values in (("select_paths", select_paths), ("select_domains", select_domains),
                        ("exclude_paths", exclude_paths), ("exclude_domains", exclude_domains)):
        cleaned = [v.strip() for v in values or [] if isinstance(v, str) and v.strip()]
        if cleaned:
            body[key] = cleaned
    try:
        async with httpx.AsyncClient(timeout=float(timeout + 10)) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            return json.dumps({
                "base_url": data.get("base_url", ""),
                "results": data.get("results", []),
                "response_time": data.get("response_time", 0)
            }, ensure_ascii=False, indent=2)
    except Exception as e:
        error = classify_exception("tavily", e)
        if isinstance(e, httpx.TimeoutException):
            title = f"映射超时: 请求超过{timeout}秒"
        elif isinstance(e, httpx.HTTPStatusError):
            title = f"映射失败: Tavily Map 返回 HTTP {e.response.status_code}"
        else:
            title = "映射失败: Tavily Map 未能完成"
        return _map_failure(error, title)


@mcp.tool(
    name="web_map",
    output_schema=None,
    annotations=_READ_ONLY_WEB,
    description="""
    Maps a website's structure by traversing it like a graph, discovering URLs and generating a comprehensive site map.

    **Key Features:**
        - **Graph Traversal:** Explores website structure starting from root URL.
        - **Depth & Breadth Control:** Configure traversal limits to balance coverage and performance.
        - **Scope Control:** allow_external (Tavily's default is true, so links to other sites are
          included), select_paths / select_domains to keep only matching URLs, exclude_paths /
          exclude_domains to drop matching URLs (all regular expressions).
        - **Instruction Filtering:** Use natural language to focus crawler on specific content types.

    **Output:** on success a JSON object {base_url, results, response_time}. On failure the first line is
    `[web_map] error=<category> provider=tavily [status=<HTTP status>]` (categories as in web_fetch: auth,
    quota, rate_limit, timeout, network, upstream_error, bad_request, disabled, not_configured, ...), then a
    short message and a redacted detail line. Local/LAN addresses and non-http(s) URLs are refused locally
    with `[web_map] error=invalid_url reason=...` (Tavily runs in the cloud and cannot reach them).

    **Edge Cases & Best Practices:**
        - Start with low max_depth (1-2) for initial exploration, increase if needed.
        - Use allow_external=false or select_domains to stay on the starting site.
        - Use instructions to filter for specific content (e.g., "only documentation pages").
        - Large sites may hit timeout limits; adjust timeout and limit parameters accordingly.
    """,
    meta={"version": "1.5.0", "author": "guda.studio"},
)
async def web_map(
    url: Annotated[str, "Root URL to begin the mapping (e.g., 'https://docs.example.com')."],
    instructions: Annotated[str, "Natural language instructions for the crawler to filter or focus on specific content."] = "",
    max_depth: Annotated[int, Field(description="Maximum depth of mapping from the base URL.", ge=1, le=5)] = 1,
    max_breadth: Annotated[int, Field(description="Maximum number of links to follow per page.", ge=1, le=500)] = 20,
    limit: Annotated[int, Field(description="Total number of links to process before stopping.", ge=1, le=500)] = 50,
    timeout: Annotated[int, Field(description="Maximum time in seconds for the operation.", ge=10, le=150)] = 150,
    allow_external: Annotated[bool, "Include links to other domains in the results (Tavily's default is true). Set false to stay on the starting site."] = True,
    select_paths: Annotated[list[str] | None, "Regex patterns; only URLs whose path matches are returned, e.g. ['/docs/.*']."] = None,
    select_domains: Annotated[list[str] | None, "Regex patterns restricting the crawl to matching domains or subdomains, e.g. ['^docs\\.example\\.com$']."] = None,
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
    name="get_config_info",
    output_schema=None,
    annotations=_READ_ONLY_WEB,
    description="""
    Returns current Grok Search MCP server configuration, circuit-breaker state, and tests API connectivity.

    **Key Features:**
        - **Configuration Check:** Verifies environment variables and current settings, including which
          optional engines (Tavily, Firecrawl) are actually active.
        - **Breaker State:** Shows which model tiers are currently cooling down after rate limits.
        - **Connection Test:** Sends request to /models endpoint to validate API access; a transport
          failure (connection reset or refused, proxy tunnel closed) is retried once.
        - **Model Discovery:** Lists all available models from the API and refreshes the cached list
          that web_search(model=...) is validated against.
        - **Model Source:** GROK_MODEL_SOURCE is env, switch_model (set in this process), config
          (config file) or default; GROK_MODEL_NOTE says how long a switch_model choice lasts.
        - **Warnings:** config_warnings lists environment values that were not understood (their
          default was used) and config-file or log-directory problems.

    **Edge Cases & Best Practices:**
        - Use this tool first when debugging connection or configuration issues.
        - API keys are masked (at most a quarter of a key is shown) and credentials in URLs are hidden.
        - Connection test timeout is 10 seconds per attempt; network issues may cause delays.
    """,
    meta={"version": "1.6.0", "author": "guda.studio"},
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
                        test_result["available_models"] = model_names
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

    config_info["connection_test"] = test_result
    return json.dumps(config_info, ensure_ascii=False, indent=2)


@mcp.tool(
    name="switch_model",
    output_schema=None,
    description="""
    Switches the default Grok model used for search operations, persisting the setting.

    **Key Features:**
        - **Validation:** The name is stripped and checked against the proxy's /models list the same
          way as web_search(model=...): a thinking suffix such as "(high)" is accepted, an empty,
          over-long or unlisted name is rejected and nothing changes. When /models cannot be reached
          the switch is applied unverified, with a warning.
        - **Persistent Storage:** Model preference saved to ~/.config/grok-search/config.json (a private
          temp directory, with a warning, when that is not writable).
        - **Immediate Effect:** New model used for all subsequent operations in this process.

    **Edge Cases & Best Practices:**
        - Use get_config_info to see the available models and GROK_MODEL_SOURCE.
        - When the GROK_MODEL environment variable is set it takes precedence over the config file, so
          the switch lasts only for this process and GROK_MODEL wins again after a restart; the response
          carries a warning in that case.
    """,
    meta={"version": "1.5.0", "author": "guda.studio"},
)
async def switch_model(
    model: Annotated[str, "Model ID to switch to (e.g., 'grok-4-fast', 'grok-4.20-multi-agent-0309(high)'). Must be non-empty."]
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
        if check is not None and check.status != "default":
            result["model_check"] = check.to_dict()
        if save_warning:
            warnings.append(save_warning)
        elif config.config_file_is_temporary:
            warnings.append(f"~/.config/grok-search 不可写，配置保存在临时目录 {config_file}，服务器重启后不会保留")
        env_model = (os.getenv("GROK_MODEL") or "").strip()
        if env_model:
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


def _resolve_project_dir(project_dir: str) -> tuple[Optional[Path], str, str]:
    """(directory, source, error). Order: argument, CLAUDE_PROJECT_DIR, nearest .git/.claude above cwd."""
    if project_dir and project_dir.strip():
        path = Path(project_dir.strip()).expanduser()
        if not path.is_dir():
            return None, "argument", f"project_dir 不存在或不是目录: {path}"
        path = path.resolve()
        if _is_fs_root(path):
            return None, "argument", f"拒绝把文件系统根目录当作项目目录: {path}"
        return path, "argument", ""
    env_dir = os.getenv("CLAUDE_PROJECT_DIR", "").strip()
    if env_dir and Path(env_dir).is_dir() and not _is_fs_root(Path(env_dir).resolve()):
        return Path(env_dir).resolve(), "CLAUDE_PROJECT_DIR", ""
    try:
        found = _find_project_root(Path.cwd().resolve())
    except OSError:  # the working directory was removed or is not accessible
        found = None
    if found is not None:
        return found, "cwd", ""
    return None, "", (
        "无法确定项目目录：未设置 CLAUDE_PROJECT_DIR，且从当前目录向上到用户主目录之前都没有 .git 或 .claude。"
        "请用 project_dir 参数传入项目根目录，未做任何修改。"
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
    name="toggle_builtin_tools",
    output_schema=None,
    description="""
    Block or allow Claude Code's built-in WebSearch and WebFetch tools for a project.

    **Actions:**
        - "on" (alias "block"): add WebFetch and WebSearch to permissions.deny in
          <project>/.claude/settings.json, so this MCP server's tools are used instead.
        - "off" (alias "allow"): remove them from that file's deny list.
        - "status": report the current state without changing anything.
        Any other value (including the ambiguous "enable"/"disable") is rejected with an error.

    **Project directory:** project_dir if given, else CLAUDE_PROJECT_DIR, else the nearest directory
    containing .git or .claude above the server's working directory (never the home directory or a
    filesystem root). If none applies, nothing is written and the tool asks for project_dir.

    **Other scopes:** Claude Code also applies deny rules from .claude/settings.local.json and
    ~/.claude/settings.json. They are read (never changed) and reported in denied_elsewhere; "off"
    warns when one of them still blocks the tools, and effective shows the combined result.

    **Safety:** a .claude directory or settings file that is a symlink pointing outside the project is
    refused; the file is replaced atomically.
    """,
    meta={"version": "1.5.0", "author": "guda.studio"},
)
async def toggle_builtin_tools(
    action: Annotated[str, Field(
        description="'on' (block the built-in tools), 'off' (allow them again) or 'status' (check the current state). 'block' and 'allow' are accepted as aliases.",
        json_schema_extra={"enum": ["on", "off", "status", "block", "allow"]},
    )] = "status",
    project_dir: Annotated[str, "Project root whose .claude/settings.json is read or changed. Optional when CLAUDE_PROJECT_DIR or a .git/.claude directory above the server's working directory identifies the project."] = "",
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
    try:
        if root == Path.home().resolve():
            warnings.append("project_dir 是用户主目录：~/.claude/settings.json 是用户级设置，会影响所有项目")
    except Exception:
        pass
    if warnings:
        result["warning"] = "\n".join(warnings)
    return reply(**result)


def register_planning_tools(app: FastMCP) -> None:
    """Six bookkeeping tools that record a client's search plan. Opt-in via GROK_PLANNING_TOOLS=true."""

    @app.tool(
        name="plan_intent",
        output_schema=None,
        description="""
        Phase 1 of search planning: Analyze user intent. Call this FIRST to create a session.
        Returns session_id for subsequent phases. Required flow:
        plan_intent → plan_complexity → plan_sub_query(×N) → plan_search_term(×N) → plan_tool_mapping(×N) → plan_execution

        Required phases depend on complexity: Level 1 = phases 1-3; Level 2 = phases 1-5; Level 3 = all 6.
        Every plan_* call validates its input (enums, ranges, confidence 0.0-1.0) and returns
        {"error": ..., "invalid_fields": [...]} without recording anything when it is invalid.
        A recorded intent, complexity or execution phase is only replaced with is_revision=true.
        """,
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
        name="plan_complexity",
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
        name="plan_sub_query",
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
        name="plan_search_term",
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
        name="plan_tool_mapping",
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
        name="plan_execution",
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
