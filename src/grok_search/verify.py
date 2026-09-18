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
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import unquote, urlparse

import httpx

ARXIV_API = "https://export.arxiv.org/api/query"
CROSSREF_API = "https://api.crossref.org/works/"

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
    for start in range(0, len(ids), 50):
        chunk = ids[start:start + 50]
        response = await client.get(ARXIV_API, params={"id_list": ",".join(chunk), "max_results": len(chunk)})
        response.raise_for_status()
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
    timeout_s: float = 20.0,
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

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), follow_redirects=True, headers={"User-Agent": user_agent}) as client:

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

    result["arxiv"].sort(key=lambda x: x["id"])
    result["doi"].sort(key=lambda x: x["doi"])
    result["checked"] = {"arxiv": len(arxiv_ids), "doi": len(dois), "urls": len(urls) if check_urls else 0}
    return result
