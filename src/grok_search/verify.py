"""Deterministic post-verification of identifiers and links cited in a search answer.

What is checked:
* arXiv IDs are resolved in one batch through the arXiv export API.
* DOIs are resolved through Crossref.
* Other cited URLs get a bounded GET (first 64 KB) to record status and title.
  Before the request and before every redirect hop the host is resolved and the
  request is refused unless all addresses are public (see "destination guard").
* When the answer writes a paper title next to an arXiv ID or DOI, that title is
  compared with the registry title (title_checks).

What is NOT checked: whether a source supports the statement it is attached to.
"Resolved" means "exists / reachable", not "true". Every identifier or URL that
was not checked is listed in skipped with a reason. Nothing here depends on a model.
"""
import asyncio
import difflib
import html
import ipaddress
import re
import socket
import time
import unicodedata
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import unquote, urlparse

import httpx

ARXIV_API = "https://export.arxiv.org/api/query"
CROSSREF_API = "https://api.crossref.org/works/"

SCOPE = (
    "Existence and reachability only: arXiv IDs and DOIs are resolved against arXiv and Crossref, "
    "cited URLs are fetched when their host resolves to public addresses only, and paper titles written "
    "next to an identifier are compared with the registry title. Whether a source supports the sentence "
    "it is attached to is NOT checked."
)
MAX_SKIPPED_LISTED = 50

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


# ---------------------------------------------------------------- destination guard
# URL checks fetch addresses that come from the model's answer. Before the first request and
# before every redirect hop the host is resolved, and the request is refused unless every
# address is public (is_global; IPv4-mapped, 6to4 and NAT64 addresses are judged by the IPv4
# address they carry). Limits: the check resolves the name separately from the connection, so
# DNS rebinding between the two is not prevented; when an HTTP(S)/SOCKS proxy carries the
# request, the proxy decides the real destination; behind a fake-ip resolver (Clash/mihomo)
# the real destination is unknown, so such hosts are refused. Registry lookups (arXiv,
# Crossref) use fixed hosts and are not guarded.
FAKE_IP_NETWORK = ipaddress.IPv4Network("198.18.0.0/15")
NAT64_NETWORK = ipaddress.IPv6Network("64:ff9b::/96")
RESOLVE_TIMEOUT_S = 5.0
_NUMERIC_PART_RE = re.compile(r"^(?:0[xX][0-9a-fA-F]*|0[0-7]*|[1-9][0-9]*)$")


class BlockedAddress(Exception):
    """A URL check was refused because its host is, or resolves to, a non-public address."""

    def __init__(self, host: str, address: str, kind: str, literal: bool = False):
        self.host = host
        self.address = address
        self.kind = kind
        if host == address:
            subject = address
        elif literal:
            subject = f"{host} (= {address})"
        else:
            subject = f"{host} resolves to {address}, which"
        if kind == "fake_ip":
            text = f"{subject} is in 198.18.0.0/15, the fake-ip range of Clash/mihomo-style proxies, so the real destination cannot be checked"
        else:
            text = f"{subject} is not a public address"
        super().__init__(text)


class ResolveFailed(Exception):
    """The host of a URL check could not be resolved."""


def parse_numeric_ipv4(host: str) -> Optional[ipaddress.IPv4Address]:
    """An IPv4 literal in any notation inet_aton() accepts: 127.1, 2130706433, 0x7f.0.0.1, 0177.0.0.1."""
    parts = (host or "").split(".")
    if not 1 <= len(parts) <= 4 or not all(_NUMERIC_PART_RE.match(part) for part in parts):
        return None
    values = []
    for part in parts:
        if part[:2] in ("0x", "0X"):
            values.append(int(part[2:] or "0", 16))
        elif len(part) > 1 and part.startswith("0"):
            values.append(int(part, 8))
        else:
            values.append(int(part))
    *head, last = values
    if any(value > 255 for value in head) or last >= 256 ** (4 - len(head)):
        return None
    number = 0
    for value in head:
        number = number * 256 + value
    return ipaddress.IPv4Address(number * 256 ** (4 - len(head)) + last)


def literal_address(host: str):
    """The IP address of an IP-literal host (any IPv4 notation, IPv6 with or without brackets), else None."""
    host = (host or "").strip().strip("[]")
    ipv4 = parse_numeric_ipv4(host)
    if ipv4 is not None:
        return ipv4
    try:
        return ipaddress.IPv6Address(host.split("%", 1)[0])
    except ValueError:
        return None


def address_problem(address) -> Optional[str]:
    """None for a public address, else "fake_ip" or "non_public"."""
    ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.version == 4 and ip in FAKE_IP_NETWORK:
        return "fake_ip"
    embedded = []
    if ip.version == 6:
        if ip.sixtofour is not None:
            embedded.append(ip.sixtofour)
        if ip in NAT64_NETWORK:
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if not ip.is_global or any(not inner.is_global for inner in embedded):
        return "non_public"
    return None


async def resolve_host(host: str, port: int) -> list[str]:
    """Addresses from the system resolver (hosts file, search domains and LAN name resolution included)."""
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout=RESOLVE_TIMEOUT_S)
    return list(dict.fromkeys(str(info[4][0]).split("%", 1)[0] for info in infos))


async def check_destination(host: str, port: int) -> None:
    """Raise BlockedAddress unless every address of host is public, ResolveFailed when it does not resolve."""
    literal = literal_address(host)
    if literal is not None:
        kind = address_problem(literal)
        if kind:
            raise BlockedAddress(host, str(literal), kind, literal=True)
        return
    try:
        addresses = await resolve_host(host, port)
    except (OSError, asyncio.TimeoutError, UnicodeError, ValueError) as e:
        raise ResolveFailed(f"{host}: {type(e).__name__}" + (f" {e}" if str(e) else "")) from e
    if not addresses:
        raise ResolveFailed(f"{host}: no addresses")
    for address in addresses:
        kind = address_problem(address)
        if kind:
            raise BlockedAddress(host, address, kind)


def _host_of(url) -> Optional[str]:
    try:
        return httpx.URL(str(url)).raw_host.decode("ascii", "replace")
    except Exception:
        return None


async def _guard_destination(request: httpx.Request) -> None:
    """httpx request hook: runs before the first request and before every redirect hop."""
    url = request.url
    await check_destination(url.raw_host.decode("ascii", "replace"), url.port or (443 if url.scheme == "https" else 80))


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
    except BlockedAddress as e:
        first = _host_of(url)
        where = "redirect target " if first is not None and e.host.lower() != first.lower() else ""
        return {"url": url, "status": None, "blocked": True, "error": f"blocked: {where}{e}"}
    except ResolveFailed as e:
        return {"url": url, "status": None, "error": f"dns lookup failed: {e}"}
    except Exception as e:
        return {"url": url, "status": None, "error": type(e).__name__}
    title = ""
    m = _TITLE_RE.search(buf.decode("utf-8", "replace"))
    if m:
        title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()[:200]
    return {"url": url, "status": status, "title": title}


# --------------------------------------------------------------- title checks
_LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]+")
_STRONG_TITLE_PATTERNS = (
    re.compile(r'"([^"\n]{4,400}?)"'),
    re.compile(r"“([^”\n]{4,400}?)”"),
    re.compile(r"《([^》\n]{2,400}?)》"),
    re.compile(r"「([^」\n]{2,400}?)」"),
    re.compile(r"『([^』\n]{2,400}?)』"),
    re.compile(r"«([^»\n]{4,400}?)»"),
    re.compile(r"\[([^\[\]\n]{4,400}?)\]\(https?://[^)\s]+\)"),
    re.compile(r"(?i)(?:\btitled\b|\btitle(?:\s+is)?\b|标题(?:为|是)?|题为|题目(?:为|是)?)\s*[:：]?\s*(.{4,400}?)(?=\s*(?:[.;。；\n]|\[\[|$))"),
)
_EMPHASIS_PATTERNS = (
    re.compile(r"\*\*([^*\n]{4,400}?)\*\*"),
    re.compile(r"(?<![\w*])\*([^*\n]{4,400}?)\*(?![\w*])"),
    re.compile(r"(?<![\w_])_([^_\n]{4,400}?)_(?![\w_])"),
)
_ID_LEAD_RE = re.compile(r"(?i)[\s(（\[]*(?:arxiv|doi)?\s*(?:id|号)?\s*[:：]?\s*$")
_BOUNDARY_RE = re.compile(r"[.;。；|](?:\s|$)|[。；|]|\n")
_TRIM = " *_\"'“”《》()（）[]-—:：,，"


def _norm_title(text: str) -> str:
    text = html.unescape(text or "")
    text = _LATEX_CMD_RE.sub(" ", text).replace("$", " ")
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"[\W_]+", " ", text)
    return " ".join(text.split())


def _identifier_windows(answer: str, identifier: str, radius: int = 300) -> list[tuple[str, int, int]]:
    """(window, id_start, id_end) for every occurrence of identifier, clipped to its line."""
    windows = []
    lowered, needle = answer.lower(), identifier.lower()
    pos = lowered.find(needle)
    while pos != -1:
        line_start = answer.rfind("\n", 0, pos) + 1
        line_end = answer.find("\n", pos)
        line_end = len(answer) if line_end == -1 else line_end
        start = max(line_start, pos - radius)
        end = min(line_end, pos + len(identifier) + radius)
        windows.append((answer[start:end], pos - start, pos - start + len(identifier)))
        pos = lowered.find(needle, pos + len(needle))
    return windows


def _title_candidates(window: str, id_start: int, id_end: int, identifier: str, registry_len: int):
    """Yield (candidate, strong). Strong candidates are explicitly marked as titles."""
    ident = identifier.lower()
    for pattern in _STRONG_TITLE_PATTERNS:
        for m in pattern.finditer(window):
            cand = m.group(1).strip()
            if ident not in cand.lower():
                yield cand, True
    for pattern in _EMPHASIS_PATTERNS:
        for m in pattern.finditer(window):
            cand = m.group(1).strip()
            if ident in cand.lower():
                continue
            # Bold is also used for whole sentences; only short, unpunctuated spans count as titles.
            strong = not re.search(r"[.!?。！？]$", cand) and len(cand) <= max(80, int(registry_len * 1.5))
            yield cand, strong
    before = _BOUNDARY_RE.split(window[:id_start])[-1]
    before = _ID_LEAD_RE.sub("", before).strip(_TRIM)
    if before:
        yield before, False
    after = re.sub(r"^(?:v\d+)?[)\]）]*\s*[:：—–-]*\s*", "", window[id_end:])
    after = _BOUNDARY_RE.split(after)[0].strip(_TRIM)
    if after:
        yield after, False


def compare_title(answer: str, identifier: str, registry_title: str) -> Optional[dict]:
    """Compare the title the answer gives for identifier with the registry title.

    status: match | partial | possible_mismatch | not_stated. None when the
    identifier does not occur in the answer text at all.
    """
    windows = _identifier_windows(answer or "", identifier)
    if not windows:
        return None
    entry = {"registry_title": registry_title}
    registry = _norm_title(registry_title)
    if not registry:
        return {**entry, "status": "not_stated"}
    if any(registry in _norm_title(window) for window, _, _ in windows):
        return {**entry, "status": "match"}
    short = _norm_title(registry_title.split(":", 1)[0]) if ":" in registry_title else ""
    scored = []
    for window, start, end in windows:
        for cand, strong in _title_candidates(window, start, end, identifier, len(registry_title)):
            norm = _norm_title(cand)
            if len(norm) < 3:
                continue
            ratio = difflib.SequenceMatcher(None, norm, registry).ratio()
            if short and norm == short:
                ratio = max(ratio, 0.95)  # "SWE-Review" for "SWE-Review: Closing the Loop"
            scored.append((ratio, strong, cand))
    if not scored:
        return {**entry, "status": "not_stated"}
    best = max(scored, key=lambda x: (x[0], x[1]))
    if best[0] >= 0.9:
        status, pick = "match", best
    elif best[0] >= 0.6:
        status, pick = "partial", best
    else:
        strong = [x for x in scored if x[1]]
        if not strong:
            return {**entry, "status": "not_stated"}
        status, pick = "possible_mismatch", max(strong, key=lambda x: x[0])
    return {**entry, "status": status, "answer_title": pick[2][:200], "similarity": round(pick[0], 2)}


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
    answer = answer or ""
    answer_arxiv = extract_arxiv_ids(answer)
    answer_dois = extract_dois(answer)
    arxiv_ids = list(answer_arxiv)
    dois = list(answer_dois)
    urls: list[str] = []
    skipped: list[dict] = []
    for source in sources or []:
        url = (source or {}).get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        url = url.strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            skipped.append({"kind": "url", "value": url, "reason": "unsupported scheme (only http/https are checked)"})
            continue
        host = (parsed.hostname or "").lower()
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
        literal = literal_address(host)
        problem = address_problem(literal) if literal is not None else None
        if problem:
            skipped.append({"kind": "url", "value": url, "reason": f"blocked: {BlockedAddress(host, str(literal), problem, literal=True)} (not fetched)"})
            continue
        if url not in urls:
            urls.append(url)
    skipped += [{"kind": "url", "value": u, "reason": f"over the per-answer limit of {max_urls} URL checks"} for u in urls[max_urls:]]
    skipped += [{"kind": "doi", "value": d, "reason": f"over the per-answer limit of {max_dois} DOI lookups"} for d in dois[max_dois:]]
    urls = urls[:max_urls]
    dois = dois[:max_dois]
    if not check_urls:
        skipped += [{"kind": "url", "value": u, "reason": "URL checks disabled (GROK_VERIFY_URLS=false)"} for u in urls]

    result: dict = {"scope": SCOPE, "claim_support": "not_checked", "arxiv": [], "doi": [], "urls": [], "unresolved": []}
    user_agent = "grok-search-verify/1.11" + (f" (mailto:{mailto})" if mailto else "")
    sem = asyncio.Semaphore(5)

    # arXiv answers slowly (15 s and more) once it has throttled a client, so reads get more room than connects.
    timeout = httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=None)
    headers = {"User-Agent": user_agent}
    # Registry lookups go to fixed hosts; only the URL checks, whose targets come from the answer, are guarded.
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client, \
            httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers,
                              event_hooks={"request": [_guard_destination]}) as url_client:

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
                info = await check_url(url, url_client)
            status = info.get("status")
            if info.get("blocked"):
                skipped.append({"kind": "url", "value": url, "reason": info["error"]})
            elif status and status < 400:
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
            seen = {(u["kind"], u["value"]) for u in result["unresolved"] + skipped}
            done_arxiv = {x["id"] for x in result["arxiv"]}
            done_doi = {x["doi"] for x in result["doi"]}
            done_url = {x["url"] for x in result["urls"]}
            for kind, values, done in (("arxiv", arxiv_ids, done_arxiv), ("doi", dois, done_doi), ("url", urls if check_urls else [], done_url)):
                for value in values:
                    if value not in done and (kind, value) not in seen:
                        result["unresolved"].append({"kind": kind, "value": value, "reason": "timed out"})

    result["arxiv"].sort(key=lambda x: x["id"])
    result["doi"].sort(key=lambda x: x["doi"])

    title_checks: list[dict] = []
    for entry in result["arxiv"]:
        if entry["id"] in answer_arxiv:
            check = compare_title(answer, entry["id"], entry.get("title", ""))
            if check:
                title_checks.append({"kind": "arxiv", "id": entry["id"], **check})
    for entry in result["doi"]:
        if entry["doi"] in answer_dois:
            check = compare_title(answer, entry["doi"], entry.get("title", ""))
            if check:
                title_checks.append({"kind": "doi", "id": entry["doi"], **check})
    result["title_checks"] = title_checks
    result["skipped"] = skipped[:MAX_SKIPPED_LISTED]
    result["checked"] = {"arxiv": len(arxiv_ids), "doi": len(dois), "urls": len(urls) if check_urls else 0}
    statuses = [t["status"] for t in title_checks]
    result["summary"] = {
        "identifiers_found": len(result["arxiv"]) + len(result["doi"]),
        "urls_reachable": len(result["urls"]),
        "unresolved": len(result["unresolved"]),
        "skipped": len(skipped),
        "title_match": statuses.count("match"),
        "title_partial": statuses.count("partial"),
        "title_possible_mismatch": statuses.count("possible_mismatch"),
        "title_not_stated": statuses.count("not_stated"),
    }
    return result
