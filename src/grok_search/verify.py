"""Deterministic post-verification of identifiers cited in a search answer.

* arXiv IDs are resolved in one batch through the arXiv export API.
* DOIs are resolved through Crossref.
* Other cited URLs get a bounded GET (first 64 KB) to record status and title.

Nothing here depends on a model; the caller can compare the returned titles
with what the answer claims and treat `unresolved` entries as suspect.
"""
import asyncio
import html
import re
import time
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import unquote, urlparse

import httpx

ARXIV_API = "https://export.arxiv.org/api/query"
CROSSREF_API = "https://api.crossref.org/works/"

# The arXiv API is reachable but flaky from some networks (sporadic 406 / connect
# timeouts between otherwise healthy responses). One retry after the 3-second
# spacing arXiv asks for turns most of those into successes.
_ARXIV_RETRY_STATUSES = {406, 408, 429, 500, 502, 503, 504}
ARXIV_RETRY_DELAY_S = 3.0

# arXiv's terms ask for at least 3 seconds between API calls from one client.
# Concurrent searches each verify their own answer, so every arXiv call in
# this process passes through one gate that spaces request starts; a burst of
# parallel lookups otherwise earns "Rate exceeded." and long stalls for all.
ARXIV_MIN_INTERVAL_S = 3.0
_arxiv_gate: dict = {"loop": None, "lock": None, "last_start": 0.0}

# Resolved entries are cached for an hour: a fetch and a verification of the
# same paper, or repeated searches on one topic, should not re-query arXiv.
ARXIV_CACHE_TTL_S = 3600.0
_ARXIV_CACHE: dict[str, tuple[float, dict]] = {}


async def _arxiv_slot() -> None:
    loop = asyncio.get_running_loop()
    if _arxiv_gate["loop"] is not loop:
        _arxiv_gate["loop"] = loop
        _arxiv_gate["lock"] = asyncio.Lock()
    async with _arxiv_gate["lock"]:
        wait = _arxiv_gate["last_start"] + ARXIV_MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _arxiv_gate["last_start"] = time.monotonic()


async def arxiv_get(client: httpx.AsyncClient, params: dict, attempts: int = 2) -> httpx.Response:
    """GET the arXiv API through the spacing gate, with one spaced retry on transient failures."""
    last_exc: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            await _arxiv_slot()
            response = await client.get(
                ARXIV_API,
                params=params,
                headers={"Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8"},
            )
            if response.status_code in _ARXIV_RETRY_STATUSES and attempt + 1 < attempts:
                last_exc = httpx.HTTPStatusError(f"arXiv API returned {response.status_code}", request=response.request, response=response)
                await asyncio.sleep(ARXIV_RETRY_DELAY_S)
                continue
            response.raise_for_status()
            return response
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            last_exc = e
            if attempt + 1 < attempts:
                await asyncio.sleep(ARXIV_RETRY_DELAY_S)
                continue
            raise
    assert last_exc is not None
    raise last_exc

_ARXIV_RE = re.compile(r"(?<![\w.])(\d{2})(\d{2})\.(\d{4,5})(?:v\d+)?(?![\w.])")
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>()\[\]{}]+)")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_ATOM = {"a": "http://www.w3.org/2005/Atom"}


def extract_arxiv_ids(text: str) -> list[str]:
    ids: list[str] = []
    for m in _ARXIV_RE.finditer(text or ""):
        yy, mm = int(m.group(1)), int(m.group(2))
        if not (1 <= mm <= 12) or yy < 7:  # new-style identifiers begin at 0704
            continue
        idv = f"{m.group(1)}{m.group(2)}.{m.group(3)}"
        if idv not in ids:
            ids.append(idv)
    return ids


def extract_dois(text: str) -> list[str]:
    out: list[str] = []
    for m in _DOI_RE.finditer(text or ""):
        doi = m.group(1).rstrip(".,;:*_`")
        if doi and doi not in out:
            out.append(doi)
    return out


def _arxiv_id_from_url(url: str) -> Optional[str]:
    m = _ARXIV_RE.search(url)
    if not m:
        return None
    return extract_arxiv_ids(m.group(0))[0] if extract_arxiv_ids(m.group(0)) else None


async def lookup_arxiv(ids: list[str], client: httpx.AsyncClient) -> tuple[dict, list[str]]:
    found: dict[str, dict] = {}
    now = time.monotonic()
    pending: list[str] = []
    for idv in ids:
        cached = _ARXIV_CACHE.get(idv)
        if cached and now - cached[0] < ARXIV_CACHE_TTL_S:
            found[idv] = cached[1]
        else:
            pending.append(idv)
    for start in range(0, len(pending), 50):
        chunk = pending[start:start + 50]
        response = await arxiv_get(client, {"id_list": ",".join(chunk), "max_results": len(chunk)})
        root = ET.fromstring(response.text)
        for entry in root.findall("a:entry", _ATOM):
            raw_id = (entry.findtext("a:id", default="", namespaces=_ATOM) or "").split("/abs/")[-1]
            base = re.sub(r"v\d+$", "", raw_id)
            title = re.sub(r"\s+", " ", (entry.findtext("a:title", default="", namespaces=_ATOM) or "").strip())
            if base not in chunk or not title:
                continue
            found[base] = {
                "id": base,
                "title": title,
                "published": (entry.findtext("a:published", default="", namespaces=_ATOM) or "")[:10],
                "authors": [a.findtext("a:name", default="", namespaces=_ATOM) for a in entry.findall("a:author", _ATOM)][:3],
            }
            _ARXIV_CACHE[base] = (time.monotonic(), found[base])
    unresolved = [i for i in ids if i not in found]
    return found, unresolved


async def lookup_doi(doi: str, client: httpx.AsyncClient) -> Optional[dict]:
    response = await client.get(CROSSREF_API + doi)
    if response.status_code != 200:
        return None
    message = response.json().get("message", {})
    issued = (message.get("issued", {}) or {}).get("date-parts") or [[]]
    return {
        "doi": doi,
        "title": (message.get("title") or [""])[0],
        "container": (message.get("container-title") or [""])[0],
        "type": message.get("type", ""),
        "issued": "-".join(str(x) for x in issued[0]) if issued and issued[0] else "",
    }


async def check_url(url: str, client: httpx.AsyncClient, max_bytes: int = 65536) -> dict:
    try:
        async with client.stream("GET", url) as response:
            buf = b""
            if response.status_code < 400:
                async for chunk in response.aiter_bytes():
                    buf += chunk
                    if len(buf) >= max_bytes:
                        break
            status = response.status_code
    except Exception as e:
        return {"url": url, "status": None, "error": type(e).__name__}
    title = ""
    m = _TITLE_RE.search(buf.decode("utf-8", "replace"))
    if m:
        title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()[:200]
    return {"url": url, "status": status, "title": title}


async def verify_answer(
    answer: str,
    sources: list[dict] | None,
    *,
    check_urls: bool = True,
    timeout_s: float = 30.0,
    mailto: str = "",
    max_urls: int = 30,
    max_dois: int = 30,
) -> dict:
    arxiv_ids = extract_arxiv_ids(answer)
    dois = extract_dois(answer)
    urls: list[str] = []
    for source in sources or []:
        url = (source or {}).get("url")
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host.endswith("arxiv.org"):
            idv = _arxiv_id_from_url(url)
            if idv and idv not in arxiv_ids:
                arxiv_ids.append(idv)
            continue
        if host.endswith("doi.org"):
            doi = unquote(parsed.path.lstrip("/")).rstrip("/")
            if doi and doi not in dois:
                dois.append(doi)
            continue
        if url not in urls:
            urls.append(url)
    urls = urls[:max_urls]
    dois = dois[:max_dois]

    result: dict = {"arxiv": [], "doi": [], "urls": [], "unresolved": []}
    user_agent = "grok-search-verify/1.10" + (f" (mailto:{mailto})" if mailto else "")
    sem = asyncio.Semaphore(5)

    # arXiv answers slowly (15 s and more) once it has throttled a client, so reads get more room than connects.
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=None), follow_redirects=True, headers={"User-Agent": user_agent}) as client:

        async def run_arxiv():
            if not arxiv_ids:
                return
            try:
                found, missing = await lookup_arxiv(arxiv_ids, client)
                result["arxiv"] = list(found.values())
                result["unresolved"] += [{"kind": "arxiv", "value": i, "reason": "not found"} for i in missing]
            except Exception as e:
                result["unresolved"] += [{"kind": "arxiv", "value": i, "reason": f"lookup failed: {type(e).__name__}"} for i in arxiv_ids]

        async def run_doi(doi: str):
            async with sem:
                try:
                    info = await lookup_doi(doi, client)
                except Exception as e:
                    result["unresolved"].append({"kind": "doi", "value": doi, "reason": f"lookup failed: {type(e).__name__}"})
                    return
            if info:
                result["doi"].append(info)
            else:
                result["unresolved"].append({"kind": "doi", "value": doi, "reason": "not found"})

        async def run_url(url: str):
            async with sem:
                info = await check_url(url, client)
            status = info.get("status")
            if status and status < 400:
                result["urls"].append(info)
            else:
                reason = f"status {status}" if status else info.get("error", "unreachable")
                result["unresolved"].append({"kind": "url", "value": url, "reason": reason})

        tasks = [run_arxiv()] + [run_doi(d) for d in dois]
        if check_urls:
            tasks += [run_url(u) for u in urls]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout_s)
        except asyncio.TimeoutError:
            result["timed_out"] = True
            # Whatever the cancelled lookups had not settled is reported explicitly rather than dropped.
            seen = {(u["kind"], u["value"]) for u in result["unresolved"]}
            done_arxiv = {x["id"] for x in result["arxiv"]}
            done_doi = {x["doi"] for x in result["doi"]}
            done_url = {x["url"] for x in result["urls"]}
            for kind, values, done in (("arxiv", arxiv_ids, done_arxiv), ("doi", dois, done_doi), ("url", urls if check_urls else [], done_url)):
                for value in values:
                    if value not in done and (kind, value) not in seen:
                        result["unresolved"].append({"kind": kind, "value": value, "reason": "timed out"})

    result["arxiv"].sort(key=lambda x: x["id"])
    result["doi"].sort(key=lambda x: x["doi"])
    result["checked"] = {"arxiv": len(arxiv_ids), "doi": len(dois), "urls": len(urls) if check_urls else 0}
    return result
