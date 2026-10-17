"""Deterministic post-verification of identifiers and links cited in a search answer.

What is checked:
* arXiv IDs (new style, old style such as hep-th/9711200, and arXiv's own DOIs
  10.48550/arXiv.<id>) are resolved in one batch through the arXiv export API.
  When arXiv cannot answer, DataCite (which registers arXiv's DOIs) supplies the record.
* DOIs are resolved through Crossref. A DOI Crossref does not know is looked up in the
  doi.org handle registry, the authority on whether a DOI exists, and its title is read
  from DataCite when DataCite registered it.
* Other cited URLs get a bounded GET (first 64 KB of decoded body) to record status and title.
  Before the request and before every redirect hop the host is resolved and the
  request is refused unless all addresses are public (see "destination guard").
  Behind a fake-ip resolver the real addresses come from DNS-over-HTTPS (fake_ip="doh").
* When the answer writes a paper title next to an arXiv ID or DOI, that title is
  compared with the registry title (title_checks).

Outcomes: "unresolved" means the registry or the site answered that the thing does not
exist (404/410, another 4xx from the page, a domain that does not exist). "lookup_failed"
means no answer could be obtained (429, 5xx, timeouts, network errors, a registry that is
cooling down after rate-limiting us); it says nothing about the citation.

What is NOT checked: whether a source supports the statement it is attached to.
"Resolved" means "exists / reachable", not "true". Every identifier or URL that
was not checked is listed in skipped with a reason. Nothing here depends on a model.
"""
import asyncio
import difflib
import email.utils
import html
import ipaddress
import re
import socket
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from typing import Iterator, Optional
from urllib.parse import quote, unquote, urlparse

import httpx

ARXIV_API = "https://export.arxiv.org/api/query"
CROSSREF_API = "https://api.crossref.org/works/"
DOI_HANDLE_API = "https://doi.org/api/handles/"
DATACITE_API = "https://api.datacite.org/dois/"
DEFAULT_DOH_URL = "https://cloudflare-dns.com/dns-query"

SCOPE = (
    "Existence and reachability only: arXiv IDs and DOIs are resolved against arXiv, Crossref and the doi.org "
    "registry (DataCite for titles), cited URLs are fetched when their host resolves to public addresses only, "
    "and paper titles written next to an identifier are compared with the registry title. unresolved = the "
    "registry or site said it does not exist (404/410, other 4xx, unknown domain); lookup_failed = no answer "
    "could be obtained, including 401/403/451 refusals of scripted requests (not evidence either way). Whether "
    "a source supports the sentence it is attached to is NOT checked."
)
MAX_SKIPPED_LISTED = 50
MAX_PATTERNS_LISTED = 20  # DOI patterns, listed after the other skipped entries


class LookupFailed(Exception):
    """A registry or site gave no usable answer (rate limit, 5xx, timeout, network error)."""


class RegistryCooldown(httpx.HTTPStatusError):
    """A registry rate-limited this process; lookups fail fast until its Retry-After has passed.

    Subclasses HTTPStatusError (with the status that started the cooldown) so callers that map
    HTTP errors, such as web_fetch, classify it as the rate limit it is.
    """

    def __init__(self, registry: str, url: str, status: int, remaining_s: float, started: bool = False):
        request = httpx.Request("GET", url)
        if started:
            text = f"{registry} HTTP {status} (rate-limited); cooling down for {remaining_s:.0f} s"
        else:
            text = f"{registry} is cooling down after HTTP {status}; {remaining_s:.0f} s left, not queried"
        super().__init__(text, request=request, response=httpx.Response(status, request=request))
        self.registry = registry
        self.status = status
        self.remaining_s = remaining_s


def _retry_after_s(response: httpx.Response) -> Optional[float]:
    """Seconds from a Retry-After header (delta-seconds or HTTP date), else None."""
    value = (response.headers.get("retry-after") or "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _describe(registry: str, exc: BaseException) -> str:
    """One-line reason for a failed registry request, with the status code when there is one."""
    if isinstance(exc, (RegistryCooldown, LookupFailed)):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{registry} HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return f"{registry} timed out ({type(exc).__name__})"
    if isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProxyError)):
        detail = str(exc).strip()
        return f"{registry} network error ({type(exc).__name__}{': ' + detail[:120] if detail else ''})"
    if isinstance(exc, (ET.ParseError, ValueError)):
        return f"{registry} returned an unreadable response ({type(exc).__name__})"
    return f"{registry} {type(exc).__name__}"


def _time_left(deadline: Optional[float]) -> float:
    return float("inf") if deadline is None else deadline - time.monotonic()


# ------------------------------------------------------------------ arXiv gate
# arXiv's API terms: no more than one request every three seconds, one connection at a time.
# Every arXiv call in this process (verification and web_fetch) passes through one gate that is
# held for the whole request and spaces the next request 3 s after the previous one finished.
# A 429/503 starts a process-wide cooldown (Retry-After, else ARXIV_COOLDOWN_S) during which
# lookups fail at once instead of queueing behind the gate. Other transient failures (406,
# 5xx, timeouts) get one retry when the caller's deadline leaves room for it.
_ARXIV_RETRY_STATUSES = {406, 408, 500, 502, 504}
_ARXIV_COOLDOWN_STATUSES = {429, 503}
ARXIV_RETRY_DELAY_S = 3.0
ARXIV_MIN_INTERVAL_S = 3.0
ARXIV_READ_TIMEOUT_S = 10.0
ARXIV_COOLDOWN_S = 60.0
REGISTRY_COOLDOWN_MAX_S = 900.0
_arxiv_gate: dict = {"loop": None, "lock": None, "last_start": 0.0}

# Resolved entries are cached for an hour: a fetch and a verification of the
# same paper, or repeated searches on one topic, should not re-query arXiv.
ARXIV_CACHE_TTL_S = 3600.0
_ARXIV_CACHE: dict[str, tuple[float, dict]] = {}


def _arxiv_state() -> dict:
    loop = asyncio.get_running_loop()
    if _arxiv_gate.get("loop") is not loop:
        # The gate belongs to one event loop; a new loop (or a reset to loop=None) starts clean.
        _arxiv_gate.update(loop=loop, lock=asyncio.Lock(), last_start=0.0, last_end=0.0,
                           cooldown_until=0.0, cooldown_status=0, inflight={})
    return _arxiv_gate


def _raise_if_arxiv_cooling(state: dict) -> None:
    remaining = state["cooldown_until"] - time.monotonic()
    if remaining > 0:
        raise RegistryCooldown("arXiv", ARXIV_API, state["cooldown_status"], remaining)


async def _arxiv_request(client: httpx.AsyncClient, params: dict, read_timeout: float) -> httpx.Response:
    state = _arxiv_state()
    async with state["lock"]:
        _raise_if_arxiv_cooling(state)
        wait = state["last_end"] + ARXIV_MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        state["last_start"] = time.monotonic()
        try:
            return await _registry_get(
                client,
                ARXIV_API,
                params=params,
                headers={"Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8"},
                timeout=httpx.Timeout(connect=10.0, read=read_timeout, write=10.0, pool=None),
            )
        finally:
            state["last_end"] = time.monotonic()


async def arxiv_get(client: httpx.AsyncClient, params: dict, attempts: int = 2, deadline: Optional[float] = None) -> httpx.Response:
    """GET the arXiv API through the process-wide gate (see above).

    Raises RegistryCooldown (an HTTPStatusError) on 429/503 and while cooling down, the last
    HTTPStatusError or transport error otherwise. deadline (time.monotonic()) bounds the read
    timeout and decides whether a retry still fits.
    """
    attempts = max(1, attempts)
    for attempt in range(attempts):
        state = _arxiv_state()
        _raise_if_arxiv_cooling(state)
        left = _time_left(deadline)
        if left <= 0:
            raise LookupFailed("timed out")
        can_retry = attempt + 1 < attempts and left > ARXIV_RETRY_DELAY_S + ARXIV_MIN_INTERVAL_S + 2.0
        try:
            response = await _arxiv_request(client, params, min(ARXIV_READ_TIMEOUT_S, max(1.0, left)))
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            if not can_retry:
                raise
            await asyncio.sleep(ARXIV_RETRY_DELAY_S)
            continue
        status = response.status_code
        if status in _ARXIV_COOLDOWN_STATUSES:
            seconds = _retry_after_s(response)
            seconds = min(REGISTRY_COOLDOWN_MAX_S, max(ARXIV_MIN_INTERVAL_S, ARXIV_COOLDOWN_S if seconds is None else seconds))
            state.update(cooldown_until=time.monotonic() + seconds, cooldown_status=status)
            raise RegistryCooldown("arXiv", ARXIV_API, status, seconds, started=True)
        if status in _ARXIV_RETRY_STATUSES and can_retry:
            await asyncio.sleep(ARXIV_RETRY_DELAY_S)
            continue
        response.raise_for_status()
        return response
    raise LookupFailed("arXiv: no attempt left")  # not reached: the last attempt returns or raises


# ------------------------------------------------------------- identifiers
# New-style arXiv IDs (YYMM.NNNNN). Accepted next to CJK text, before a sentence-final period,
# ".pdf" or a version, and after "arXiv." (DataCite DOI form); rejected inside longer numbers.
_ARXIV_RE = re.compile(
    r"(?:(?<=[aA][rR][xX][iI][vV]\.)|(?<![0-9A-Za-z.]))"
    r"(\d{2})(\d{2})\.(\d{4,5})(?:v\d+)?(?![0-9A-Za-z_])(?!\.\d)"
)
# Old-style IDs (archive[.XX]/YYMMNNN, 1991-2007), limited to real archive names.
_ARXIV_ARCHIVES = (
    "astro-ph|cond-mat|gr-qc|hep-ex|hep-lat|hep-ph|hep-th|math-ph|nlin|nucl-ex|nucl-th|physics|quant-ph|"
    "math|cs|q-bio|q-fin|stat|acc-phys|adap-org|alg-geom|ao-sci|atom-ph|bayes-an|chao-dyn|chem-ph|cmp-lg|"
    "comp-gas|dg-ga|funct-an|mtrl-th|patt-sol|plasm-ph|q-alg|solv-int|supr-con"
)
_ARXIV_OLD_RE = re.compile(
    r"(?:(?<=[aA][rR][xX][iI][vV]\.)|(?<![0-9A-Za-z.\-]))"
    rf"({_ARXIV_ARCHIVES})(?:\.[A-Za-z]{{2}})?/(\d{{2}})(\d{{2}})(\d{{3}})(?:v\d+)?(?![0-9A-Za-z_])(?!\.\d)",
    re.I,
)
# DOI characters: printable ASCII except quotes, brackets, <>, backtick, | and backslash, so that
# CJK text and full-width punctuation end a DOI. Balanced (...) groups (SICI-style Elsevier/Lancet
# DOIs) and SICI <...:...> or [...:...] groups (Wiley "<143::AID-ASL56>", AMS "<0437:TNYRP>", ESA and
# BioOne "[0933:TEOTWA]") are kept; a group without a ":" ("<id>", "[1]", "[^1]"), a closing tag
# ("</dc:identifier>") or an autolink ("<https://...>") ends the DOI. Only a letter or digit directly
# before the prefix blocks a DOI ("210.1234/abc", "x10.1234/abc"); a "." ("1234-1250.10.1234/abc",
# "...10.1234/abc"), a CJK character, an italic "_" and a "-" that does not follow a letter or digit
# ("<!--10.1234/abc-->") are allowed ("_10.1234/abc_", but not "name_10.1234/abc" or "name-10.1234/abc"),
# and so are a "DOI" label without a separator ("DOI10.1234/abc", "DOI-10.1234/abc") and a percent-encoded
# character, whose hex digits are not a word ("url=https%3A%2F%2Fdoi.org%2F10.1234/abc", "q=doi%3A10.1234").
# A DOI right after a percent-encoded character is (part of) a query value ("url?url=https%3A%2F%2Fdoi.org%2F
# 10.1234%2Fabc&usg=...", "scholar?q=doi%3A10.1234%2Fabc+survey"): a literal "&" starts the next parameter and
# a "+" is a space, so both end it (group "qv"; other DOIs are group "doi"). A "," or ";" directly followed by
# another DOI or link separates two DOIs ("10.1/a,10.2/b"), and a markdown-escaped "\_" stays inside the DOI.
# The "/" after the prefix may be percent-encoded, also twice (a redirect to such a link):
# "10.1109%2FFAKE.2099.12345", "10.1109%252FFAKE.2099.12345".
_DOI_CHAR = r"[!#-&*-;=?-Z^_a-z~]"
_DOI_TOKEN_CHAR = r"[!#-&*+\--:=?-Z^_a-z~]"  # _DOI_CHAR without "," and ";"
_DOI_QUERY_CHAR = r"[!#-%*,-;=?-Z^_a-z~]"  # _DOI_CHAR without "&" and "+"
_DOI_QUERY_TOKEN_CHAR = r"[!#-%*\--:=?-Z^_a-z~]"  # _DOI_TOKEN_CHAR without "&" and "+"
_DOI_PREFIX = r"10\.\d{4,9}(?:/|%(?:25)?2[Ff])"
_DOI_JOIN = rf"[,;](?!(?i:doi:|https?:)|{_DOI_PREFIX})"
# The start page, ":" and title code of a SICI item; never a closing tag's "/" or a "<https://...>" autolink.
_SICI_ITEM = rf"[0-9A-Za-z\-]{{0,40}}:(?!//){_DOI_CHAR}{{0,40}}"
_DOI_GROUPS = rf"{_DOI_JOIN}|\\_|<{_SICI_ITEM}>|\[{_SICI_ITEM}\]"
_PCT = r"%[0-9A-Fa-f]{2}"
_DOI_RE = re.compile(
    r"(?=10\.)"  # checked first: the lookbehinds below are slower to try at every position of the text
    rf"(?:(?<=[Dd][Oo][Ii])|(?<={_PCT})|(?<![0-9A-Za-z]))"
    rf"(?:(?<=[Dd][Oo][Ii][-_])|(?<={_PCT}[-_])|(?<![0-9A-Za-z][-_]))"
    rf"(?:(?<={_PCT})(?P<qv>{_DOI_PREFIX}(?:{_DOI_QUERY_TOKEN_CHAR}|\({_DOI_QUERY_CHAR}{{0,60}}\)|{_DOI_GROUPS})+)"
    rf"|(?P<doi>{_DOI_PREFIX}(?:{_DOI_TOKEN_CHAR}|\({_DOI_CHAR}{{0,60}}\)|{_DOI_GROUPS})+))"
)
_DOI_PCT_SLASH_RE = re.compile(r"(10\.\d{4,9})%(?:25)?2[Ff]")
# Inline word-break hints inside a DOI ("10.1109/FAKE.<wbr>2099.12345", a zero-width space a web page puts
# into a long DOI) are removed before extraction: <wbr>, soft hyphen, zero-width space, word joiner and BOM,
# as characters or HTML entities. One directly before a DOI becomes a space, so it still separates the DOI
# from a preceding word ("doi\u200b10.1234/abc").
_WORD_BREAK_RE = re.compile(
    r"(?i)(?:<wbr\s*/?>|&(?:shy|ZeroWidthSpace|NoBreak|#0*(?:173|8203|8288|65279)|#x0*(?:ad|200b|2060|feff));"
    rf"|[\u00ad\u200b\u2060\ufeff])(?=({_DOI_PREFIX})?)"
)
# Unicode hyphens (U+2010 hyphen, U+2011 non-breaking hyphen, which some models write for every "-" of a
# hyphenated word) are "-" inside a DOI ("10.1038/s41586" U+2011 "021" U+2011 ...). En and em dashes are dashes,
# not hyphens, and stay out. One directly before a DOI prefix is kept: "word" U+2010 "10.1234/abc" is a DOI, while
# "word-10.1234/abc" is not.
_UNICODE_HYPHEN_RE = re.compile(rf"[\u2010\u2011](?!{_DOI_PREFIX})")
_DOI_SHAPE_RE = re.compile(r"10\.\d{4,9}/\S+")
_DOI_TAIL_RE = re.compile(
    r"(?i)(?:\.(?:pdf|html?|epub|xml)|/(?:full|abstract|pdf|epdf|pdfdirect|pdfft|meta|html|fulltext|full-text|"
    r"references|figures|tables?|citations?|suppl(?:ementary)?|summary|metrics))$"
)
# bioRxiv/medRxiv (10.1101) page URLs append the version and a view: 2020.03.22.002386v1.full,
# 2020.03.22.002386v1.full.pdf+html
_RXIV_TAIL_RE = re.compile(
    r"(?i)(?:v\d+)?(?:\.(?:full|abstract|supplementary-material|article-info|article-metrics|full-text|pdf\+html))+$|v\d+$"
)
_ARXIV_DOI_RE = re.compile(r"(?i)^10\.48550/arxiv\.(.+)$")
_ATOM = {"a": "http://www.w3.org/2005/Atom"}


def _old_style_id(m: re.Match) -> Optional[str]:
    yy, mm = int(m.group(2)), int(m.group(3))
    if not (1 <= mm <= 12) or not (yy >= 91 or yy <= 7):
        return None
    return f"{m.group(1).lower()}/{m.group(2)}{m.group(3)}{m.group(4)}"


def extract_arxiv_ids(text: str) -> list[str]:
    found: list[tuple[int, str]] = []
    for m in _ARXIV_RE.finditer(text or ""):
        yy, mm = int(m.group(1)), int(m.group(2))
        if not (1 <= mm <= 12) or yy < 7:  # new-style identifiers begin at 0704
            continue
        found.append((m.start(), f"{m.group(1)}{m.group(2)}.{m.group(3)}"))
    for m in _ARXIV_OLD_RE.finditer(text or ""):
        idv = _old_style_id(m)
        if idv:
            found.append((m.start(), idv))
    ids: list[str] = []
    for _, idv in sorted(found):
        if idv not in ids:
            ids.append(idv)
    return ids


# Real DOIs are far shorter (Crossref's longest are a few hundred characters with a publisher tail). The cap
# applies once the query, fragment or HTML entity is cut off, so a DOI link carrying a long tracking query or
# text fragment is still a DOI; a longer token is listed in skipped. Without the cap _RXIV_TAIL_RE would cost
# quadratic time on ".full" * n + "X".
DOI_MAX_CHARS = 400
# Real DOIs carry two or three stacked tails at most ("...002386v1.full.pdf", ".../full/pdf/"); the bound
# keeps a token of hundreds of repeated tails ("v1v1v1...") from costing one pass per tail.
DOI_MAX_TAIL_PASSES = 6
DOI_PATTERN_REASON = "DOI pattern/prefix, not an identifier (not looked up)"
DOI_TOO_LONG_REASON = f"DOI-shaped text longer than {DOI_MAX_CHARS} characters (not looked up)"
DOI_DOT_SEGMENT_REASON = "DOI with a '.' or '..' path segment (not looked up: it would change the registry path)"


def _has_dot_segment(doi: str) -> bool:
    # "10.1234/../../10.5555/x" would be normalised by the HTTP client into another registry path
    # (https://doi.org/10.5555/x, the resolver, which redirects to the DOI's landing page).
    return any(segment in (".", "..") for segment in doi.split("/"))


# A DOI written in text or a link ends at its query ("?"), fragment ("#") or an HTML entity ("&nbsp;").
_DOI_CUT_RE = re.compile(r"&(?:[A-Za-z][A-Za-z0-9]{1,31}|#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6});|[?#]")
# A run of "?" or "#" standing for characters is a placeholder, not a query or fragment:
# "10.48550/arXiv.2301.?????", "10.48550/arXiv.2301.#####", "10.1038/s41586-0??-?????-?".
_DOI_RUN_INNER_RE = re.compile(r"([?#])\1+[./_-][0-9A-Za-z?#]")
_DOI_RUN_END_RE = re.compile(r"([?#])\1{2,}(?=$|[./_-])")


def _split_doi_token(raw: str) -> tuple[str, str, bool]:
    """(token, rest, placeholder): raw up to its query, fragment or HTML entity, and what follows.

    placeholder is True when the cut is a run of "?" or "#" standing for characters. The "#" check character
    of a SICI DOI ("...<143::AID-ASL56>3.0.CO;2-#") belongs to the DOI.
    """
    if "?" not in raw and "#" not in raw and "&" not in raw:
        return raw, "", False
    sici_close = None
    for m in _DOI_CUT_RE.finditer(raw):
        i = m.start()
        if raw[i] == "#" and raw.endswith(";2-", 0, i):
            if sici_close is None:
                closes = [j for j in (raw.find(">"), raw.find("]")) if j >= 0]
                sici_close = min(closes) if closes else -1
            if 0 <= sici_close < i:
                if i + 1 < len(raw) and raw[i + 1].isalnum():
                    return raw[:i + 1], raw[i + 1:], False  # the check character, then a fragment
                continue
        if _DOI_RUN_INNER_RE.match(raw, i) or (raw[i - 1] in "./_-" and _DOI_RUN_END_RE.match(raw, i)):
            return raw[:i], raw[i:], True
        return raw[:i], raw[i:], False
    return raw, "", False


def _strip_doi(token: str) -> str:
    """Strip what surrounding text or a publisher URL glues onto a DOI (token is already cut, see above)."""
    doi = token
    if "%" in doi:
        unquoted = unquote(doi)
        if _DOI_SHAPE_RE.fullmatch(unquoted):
            doi = unquoted
    for _ in range(DOI_MAX_TAIL_PASSES):
        before = doi
        doi = doi.rstrip(".,;:!?*_~/")
        doi = _DOI_TAIL_RE.sub("", doi)
        if doi.lower().startswith("10.1101/"):
            doi = _RXIV_TAIL_RE.sub("", doi)
        if doi == before:
            break
    return doi


def _clean_doi(raw: str) -> Optional[str]:
    """The DOI (or DOI pattern) in one DOI-shaped token; None for a prefix alone or a token listed in skipped."""
    kind, value, _ = _classify_doi(raw)
    return None if kind == "skip" or not _DOI_SHAPE_RE.fullmatch(value) else value


def extract_dois(text: str) -> list[str]:
    return scan_doi_mentions(text)[0]


# DOI patterns written in prose ("the 10.48550/arXiv.* form", "10.48550/arXiv.<id>", "10.1038/s41586-…",
# "10.48550/arXiv.YYMM.NNNNN", "10.1145/3XXXXXX.3XXXXXX") are not identifiers: looking them up only produces
# a false "not found". A mention is a pattern only when that is unambiguous; when in doubt it is a DOI, since
# a false "unresolved" on a template is harmless and a fabricated DOI hidden as a pattern is not. So a
# concrete DOI followed by markdown, HTML, a footnote star or a prose ellipsis ("**DOI 10.1/x1.**", "10.1/x1<br>",
# "10.1/x1.<h3>", "*... 10.1/x1.*", "10.1/x1/*", "10.1/x1....", "10.1/x1.…", "10.1/x1-->") is looked up.
#
# Placeholder segments, between separators and maybe after digits: a run of X (any case, at least three
# letters: "XXX", "3XXXXXX"), a run of one Y, M, D or N of at least four letters ("YYYY", "NNNNN"), or runs of
# two or more of these letters mixed ("YYMM", "YYYYMMDD", "yyyy-mm"). Three equal letters other than X are a
# journal code more often than a placeholder (IEEE Microwave Magazine: "10.1109/MMM.2014.2356092").
_DOI_SEGMENT_RE = re.compile(r"[./_\-()]")
_DOI_TEMPLATE_HINT_RE = re.compile(r"(?i)([xymdn])\1")
_DIGIT_RE = re.compile(r"\d")
# A wildcard "*" after "/", "-" or "." when the suffix has no digit ("10.1371/journal.pone.*",
# "10.1038/nature/*"); after a suffix with a digit ("10.1/x1.*", "10.1/x1/*") it is emphasis or a footnote star.
_DOI_WILDCARD_RE = re.compile(r"[./-]\*\.?$")
# A placeholder ("<id>", "<NNNNN>", "{编号}") after the token. Markup is not one: closing tags, comments,
# tags with attributes and the element names below.
_DOI_PLACEHOLDER_RE = re.compile(r"<([^\s<>/!?'\"=&]{1,30})>|\{[^\s{}]{1,30}\}")
_MARKUP_TAGS = frozenset("""
    a abbr acronym address area article aside audio b base bdi bdo big blockquote body br button canvas caption
    center cite code col colgroup data datalist dd del details dfn dialog dir div dl dt em embed fieldset figcaption
    figure font footer form h1 h2 h3 h4 h5 h6 head header hgroup hr html i iframe img input ins kbd label legend li
    link main map mark math menu meta meter nav nobr noscript object ol optgroup option output p param picture pre
    progress q rp rt ruby s samp script search section select slot small source span strike strong style sub
    summary sup svg table tbody td template textarea tfoot th thead time title tr track tt u ul var video wbr
    ref references reflist nowiki gallery poem citation doi pub-id ext-link xref fn note footnote
""".split())
# How far back on the line to look for the markdown emphasis a trailing "*" may close.
_DOI_LOOKBACK = 200


def _emphasis_closer(raw: str, before: str) -> int:
    """How many trailing "*" of raw close markdown emphasis opened earlier on the same line."""
    stars = len(raw) - len(raw.rstrip("*"))
    if not stars:
        return 0
    line = re.sub(r"^\s*[*+-]\s+", "", before[before.rfind("\n") + 1:])  # a list bullet opens nothing
    still_open: list[int] = []
    for run in re.findall(r"\*+", line):
        if still_open and still_open[-1] == len(run):
            still_open.pop()
        else:
            still_open.append(len(run))
    return min(stars, sum(still_open))


def _template_segment(segment: str) -> bool:
    letters = segment.lstrip("0123456789").lower()
    if len(letters) < 3 or not set(letters) <= set("xymdn"):
        return False
    runs = re.findall(r"x+|y+|m+|d+|n+", letters)
    if len(runs) == 1:
        return letters[0] == "x" or len(letters) >= 4
    return all(len(run) >= 2 for run in runs)


def _doi_placeholder(doi: str, token: str, before: str = "", after: str = "") -> bool:
    """Whether a DOI-shaped token is a template rather than a DOI.

    token is the mention as written (query and fragment cut off), before and after the text around it.
    """
    if doi.lower() == "10.48550/arxiv":
        return True  # arXiv's DOI prefix on its own
    if doi.endswith("-"):
        return True  # cut at a separator: "10.1038/s41586-"
    suffix = doi.split("/", 1)[1]
    digitless = not _DIGIT_RE.search(suffix)
    if digitless and _ARXIV_DOI_RE.match(doi):
        return True  # 10.48550/arXiv.<letters only>
    if _DOI_TEMPLATE_HINT_RE.search(suffix) and any(_template_segment(seg) for seg in _DOI_SEGMENT_RE.split(suffix)):
        return True
    if token.endswith("*"):
        token = token[:len(token) - _emphasis_closer(token, before)]
    if digitless and _DOI_WILDCARD_RE.search(token):
        return True
    # An ellipsis marks a template after "-" or "/" or after a suffix with no digit ("nature…"); after a
    # concrete DOI it is prose ("10.1/x1...", "10.1/x1....", "10.1/x1.…").
    stem = token.rstrip(".")
    if (len(token) - len(stem) >= 3 or after.startswith("…")) and (digitless or stem.endswith(("-", "/"))):
        return True
    placeholder = _DOI_PLACEHOLDER_RE.match(after)
    if placeholder and (placeholder.group(1) or "").lower() not in _MARKUP_TAGS:
        return digitless or token.endswith((".", "/", "_", "-"))
    return False


def _classify_doi(raw: str, before: str = "", after: str = "") -> tuple[str, str, str]:
    """("doi" | "pattern" | "skip", value, reason) for one DOI-shaped token; reason is set for "skip"."""
    if "\\" in raw:
        raw = raw.replace("\\_", "_")  # markdown-escaped underscore: 10.1007/978-3-319-24277-4\_9
    encoded_slash = _DOI_PCT_SLASH_RE.match(raw)
    if encoded_slash:
        raw = encoded_slash.group(1) + "/" + raw[encoded_slash.end():]  # https://doi.org/10.1109%2FFAKE.2099.12345
    token, rest, placeholder = _split_doi_token(raw)
    if len(token) > DOI_MAX_CHARS:
        return "skip", token[:80] + "…", DOI_TOO_LONG_REASON
    if rest:
        after = html.unescape(rest[:40]) + after
    if token.endswith("-") and (after.startswith(">") or token.endswith("--")):
        # An arrow, a comment close or an ASCII dash ("10.1/x1->", "10.1/x1-->", "(10.1/x1-- see also)"), not a
        # cut at a separator: only a single trailing "-" ("10.1038/s41586-") is a template.
        token = token.rstrip("-")
    elif token.endswith("-") and not after.startswith("…") and _DOI_SEGMENT_RE.search(token.split("/", 1)[-1].rstrip("-")):
        # A stray hyphen after a DOI that already has several segments ("10.1109/FAKE.2099.12345- (IEEE)",
        # including a Unicode hyphen made "-") is punctuation; only a one-segment suffix ("10.1038/s41586-") is cut
        # at a separator and stays a template.
        token = token.rstrip("-")
    doi = _strip_doi(token)
    # Checked after the strip: a sentence's "10.1234/abc/." is 10.1234/abc, an inner "/../" is refused.
    if "/." in doi and _has_dot_segment(doi):
        return "skip", doi[:200], DOI_DOT_SEGMENT_REASON
    if not _DOI_SHAPE_RE.fullmatch(doi):
        return "pattern", doi.split("/", 1)[0] + "/", ""  # a prefix alone: "10.1038/", "10.1038/*"
    if placeholder or _doi_placeholder(doi, token, before, after):
        return "pattern", doi, ""
    return "doi", doi, ""


def _doi_text(text: str) -> str:
    """text with word-break hints removed and Unicode hyphens made "-", ready for _DOI_RE."""
    text = _WORD_BREAK_RE.sub(lambda m: " " if m.group(1) else "", text)
    if "\u2010" in text or "\u2011" in text:
        text = _UNICODE_HYPHEN_RE.sub("-", text)
    return text


def _doi_tokens(text: str) -> Iterator[tuple[str, str, str]]:
    """(token, before, after) for each DOI-shaped token in text (see _doi_text); before is only set when the token
    has a "*".

    A token inside a percent-encoded query value (_DOI_RE group "qv") is decoded and read as text again, so that
    an encoded "?", "#" or space ends the DOI as in plain text ("10.1234%2Fabc%3Futm_source%3D...") and every DOI
    of an encoded list is found ("doi%3A10.1234%2Fabc%20doi%3A10.5678%2Fdef"). A token whose decoded text holds no
    DOI is kept as written.
    """
    for m in _DOI_RE.finditer(text):
        after = text[m.end():m.end() + 40]
        if m["qv"] and "%" in m["qv"]:
            decoded = _doi_text(unquote(m["qv"]))
            inner = list(_DOI_RE.finditer(decoded))
            if inner:
                for n in inner:
                    yield n["qv"] or n["doi"], "", decoded[n.end():n.end() + 40] + after
                continue
        raw = m["qv"] or m["doi"]
        yield raw, text[max(0, m.start() - _DOI_LOOKBACK):m.start()] if "*" in raw else "", after  # for _emphasis_closer


def scan_doi_mentions(text: str) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """(DOIs, DOI patterns, skipped) in text.

    Patterns are templates written in prose; skipped holds (value, reason) for DOI-shaped text that is not
    looked up for another reason (too long, a dot segment). Nothing DOI-shaped is dropped silently. A DOI
    written once as a pattern and once concretely is a DOI.
    """
    found: dict[str, dict[str, tuple[str, str]]] = {"doi": {}, "pattern": {}, "skip": {}}
    for raw, before, after in _doi_tokens(_doi_text(text or "")):
        kind, value, reason = _classify_doi(raw, before, after)
        found[kind].setdefault(value.lower(), (value, reason))
    dois = [value for value, _ in found["doi"].values()]
    patterns = [value for key, (value, _) in found["pattern"].items() if key not in found["doi"]]
    return dois, patterns, list(found["skip"].values())


def extract_doi_mentions(text: str) -> tuple[list[str], list[str]]:
    """(DOIs, DOI patterns): the second list holds prose templates that are not looked up."""
    dois, patterns, _ = scan_doi_mentions(text)
    return dois, patterns


def arxiv_id_from_doi(doi: str) -> Optional[str]:
    """The arXiv ID behind arXiv's own DataCite DOI (10.48550/arXiv.<id>), else None."""
    if not _ARXIV_DOI_RE.match(doi or ""):
        return None
    ids = extract_arxiv_ids(doi)
    return ids[0] if ids else None


def _arxiv_id_from_url(url: str) -> Optional[str]:
    try:
        path = unquote(urlparse(url).path)
    except ValueError:
        return None
    ids = extract_arxiv_ids(path)
    return ids[0] if ids else None


def _doi_from_url(url: str) -> Optional[str]:
    try:
        path = unquote(urlparse(url).path).lstrip("/")
    except ValueError:
        return None
    if path.lower().startswith("doi:"):
        path = path[4:]
    dois = extract_dois(path)
    return dois[0] if dois and path.startswith("10.") else None


def _is_host(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _parse_arxiv_feed(text: str, wanted: list[str]) -> dict[str, dict]:
    root = ET.fromstring(text)
    found: dict[str, dict] = {}
    for entry in root.findall("a:entry", _ATOM):
        raw_id = (entry.findtext("a:id", default="", namespaces=_ATOM) or "").split("/abs/")[-1]
        base = re.sub(r"v\d+$", "", raw_id)
        title = re.sub(r"\s+", " ", (entry.findtext("a:title", default="", namespaces=_ATOM) or "").strip())
        if base not in wanted or not title:
            continue
        found[base] = {
            "id": base,
            "title": title,
            "published": (entry.findtext("a:published", default="", namespaces=_ATOM) or "")[:10],
            "authors": [a.findtext("a:name", default="", namespaces=_ATOM) for a in entry.findall("a:author", _ATOM)][:3],
        }
    return found


def _consume_exception(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()


async def _resolve_arxiv(ids: list[str], client: httpx.AsyncClient, deadline: Optional[float] = None):
    """(found, not_found, failed) for ids. failed maps an ID to the exception that prevented an answer.

    IDs already being looked up by a concurrent verification are not requested twice: the
    second caller waits for the first caller's answer.
    """
    state = _arxiv_state()
    loop = asyncio.get_running_loop()
    found: dict[str, dict] = {}
    failed: dict[str, BaseException] = {}
    shared: dict[str, asyncio.Future] = {}
    own: dict[str, asyncio.Future] = {}
    now = time.monotonic()
    for idv in dict.fromkeys(ids):
        cached = _ARXIV_CACHE.get(idv)
        if cached and now - cached[0] < ARXIV_CACHE_TTL_S:
            found[idv] = cached[1]
        elif idv in state["inflight"]:
            shared[idv] = state["inflight"][idv]
        else:
            future = loop.create_future()
            future.add_done_callback(_consume_exception)
            state["inflight"][idv] = future
            own[idv] = future
    pending = list(own)
    try:
        for start in range(0, len(pending), 50):
            chunk = pending[start:start + 50]
            try:
                response = await arxiv_get(client, {"id_list": ",".join(chunk), "max_results": len(chunk)}, deadline=deadline)
                entries = _parse_arxiv_feed(response.text, chunk)
            except Exception as e:
                for idv in chunk:
                    own[idv].set_exception(e)
                continue
            for idv in chunk:
                entry = entries.get(idv)
                if entry:
                    _ARXIV_CACHE[idv] = (time.monotonic(), entry)
                own[idv].set_result(entry)
    finally:
        for idv, future in own.items():
            if not future.done():
                future.set_exception(LookupFailed("arXiv lookup was cancelled"))
            if state["inflight"].get(idv) is future:
                del state["inflight"][idv]
    for idv, future in {**own, **shared}.items():
        try:
            entry = await asyncio.shield(future)
        except Exception as e:
            failed[idv] = e
            continue
        if entry:
            found[idv] = entry
    not_found = [i for i in dict.fromkeys(ids) if i not in found and i not in failed]
    return found, not_found, failed


async def lookup_arxiv(ids: list[str], client: httpx.AsyncClient) -> tuple[dict, list[str]]:
    """(found, not_found). Raises when arXiv could not answer for one of the IDs."""
    found, not_found, failed = await _resolve_arxiv(ids, client)
    if failed:
        raise next(iter(failed.values()))
    return found, not_found


# ------------------------------------------------------------ registry requests
# Registry lookups go to fixed hosts with the DOI or ID in the path. Redirects are not followed
# (the doi.org resolver answers a DOI path with a redirect to the publisher, which is not a
# registry), and the body is read up to REGISTRY_MAX_BYTES after decoding.
REGISTRY_MAX_BYTES = 8 * 1024 * 1024


def _registry_url(base: str, doi: str) -> str:
    if _has_dot_segment(doi):
        raise ValueError(f"DOI with a '.' or '..' path segment is not looked up: {doi[:80]}")
    return base + quote(doi, safe=_DOI_PATH_SAFE)


async def _registry_get(client: httpx.AsyncClient, url: str, *, params=None, headers=None, timeout=None) -> httpx.Response:
    request = client.build_request(
        "GET", url, params=params, headers={"Accept-Encoding": "gzip, deflate", **(headers or {})},
        **({"timeout": timeout} if timeout is not None else {}),
    )
    response = await client.send(request, stream=True, follow_redirects=False)
    try:
        body = await _read_capped(response, REGISTRY_MAX_BYTES + 1)
    finally:
        await response.aclose()
    if len(body) > REGISTRY_MAX_BYTES:
        raise LookupFailed(f"{request.url.host} answered with more than {REGISTRY_MAX_BYTES // 2**20} MB")
    dropped = ("content-encoding", "content-length", "transfer-encoding")
    kept = [(k, v) for k, v in response.headers.multi_items() if k.lower() not in dropped]
    return httpx.Response(response.status_code, headers=kept, content=body, request=request)


# ---------------------------------------------------------------- DOIs
# Crossref's public pool allows one concurrent request per client (three in the polite pool,
# which a mailto in the User-Agent selects). All DOI lookups in this process share that limit;
# a 429/503 is retried once after its Retry-After and otherwise starts a short shared cooldown.
CROSSREF_MIN_INTERVAL_S = 0.2
CROSSREF_RETRY_AFTER_MAX_S = 10.0
CROSSREF_COOLDOWN_S = 30.0
_crossref_gate: dict = {"loop": None}
_DOI_PATH_SAFE = "/:;()@!$&*+,=-._~"


def _crossref_state(mailto: str) -> dict:
    loop = asyncio.get_running_loop()
    limit = 3 if mailto else 1
    if _crossref_gate.get("loop") is not loop or _crossref_gate.get("limit") != limit:
        _crossref_gate.update(loop=loop, sem=asyncio.Semaphore(limit), limit=limit, next_start=0.0,
                              cooldown_until=0.0, cooldown_status=0)
    return _crossref_gate


async def _crossref_get(doi: str, client: httpx.AsyncClient, mailto: str, deadline: Optional[float]) -> httpx.Response:
    state = _crossref_state(mailto)
    url = _registry_url(CROSSREF_API, doi)
    async with state["sem"]:
        for attempt in range(2):
            remaining = state["cooldown_until"] - time.monotonic()
            if remaining > 0:
                raise RegistryCooldown("Crossref", url, state["cooldown_status"], remaining)
            now = time.monotonic()
            slot = max(now, state["next_start"])
            state["next_start"] = slot + CROSSREF_MIN_INTERVAL_S / state["limit"]
            if slot > now:
                await asyncio.sleep(slot - now)
            response = await _registry_get(client, url)
            if response.status_code not in (429, 503):
                return response
            retry_after = _retry_after_s(response)
            wait = 1.0 if retry_after is None else retry_after
            if attempt == 0 and wait <= CROSSREF_RETRY_AFTER_MAX_S and _time_left(deadline) > wait + 2.0:
                # The slot is held while waiting, so the other lookups pause too.
                await asyncio.sleep(wait)
                continue
            seconds = min(REGISTRY_COOLDOWN_MAX_S, max(1.0, CROSSREF_COOLDOWN_S if retry_after is None else retry_after))
            state.update(cooldown_until=time.monotonic() + seconds, cooldown_status=response.status_code)
            return response
    return response  # not reached


def _datacite_info(doi: str, payload: dict) -> dict:
    attributes = ((payload or {}).get("data") or {}).get("attributes") or {}
    titles = [t for t in attributes.get("titles") or [] if isinstance(t, dict) and t.get("title")]
    main = [t for t in titles if not t.get("titleType")] or titles
    container = attributes.get("container") or {}
    publisher = attributes.get("publisher")
    if isinstance(publisher, dict):
        publisher = publisher.get("name")
    types = attributes.get("types") or {}
    return {
        "doi": doi,
        "title": re.sub(r"\s+", " ", main[0]["title"]).strip() if main else "",
        "container": (container.get("title") if isinstance(container, dict) else "") or publisher or "",
        "type": types.get("resourceTypeGeneral") or types.get("resourceType") or "",
        "issued": str(attributes.get("publicationYear") or ""),
        "registry": "datacite",
    }


async def _datacite_record(doi: str, client: httpx.AsyncClient) -> Optional[dict]:
    """DataCite's record for doi; None when DataCite does not have it. Raises LookupFailed otherwise."""
    try:
        response = await _registry_get(client, _registry_url(DATACITE_API, doi), headers={"Accept": "application/vnd.api+json"})
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return _datacite_info(doi, response.json())
    except Exception as e:
        raise LookupFailed(_describe("DataCite", e)) from e


async def _doi_outside_crossref(doi: str, client: httpx.AsyncClient) -> Optional[dict]:
    """After a Crossref 404: does the DOI exist at all (doi.org handle), and what is its title (DataCite)?"""
    try:
        response = await _registry_get(client, _registry_url(DOI_HANDLE_API, doi))
        payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as e:
        raise LookupFailed(f"not in Crossref; {_describe('doi.org handle lookup', e)}") from e
    code = payload.get("responseCode") if isinstance(payload, dict) else None
    if code == 100:
        return None  # the handle registry says the DOI does not exist
    if response.status_code != 200 or code != 1:
        raise LookupFailed(f"not in Crossref; doi.org handle lookup HTTP {response.status_code}" + (f" responseCode {code}" if code is not None else ""))
    info = {"doi": doi, "title": "", "container": "", "type": "", "issued": "", "registry": "doi.org"}
    try:
        record = await _datacite_record(doi, client)
    except LookupFailed:
        record = None
    if record:
        info.update(record)
    return info


async def lookup_doi(doi: str, client: httpx.AsyncClient, *, mailto: str = "", deadline: Optional[float] = None) -> Optional[dict]:
    """Registry record for doi; None only when the DOI does not exist. Raises LookupFailed when unknown."""
    try:
        response = await _crossref_get(doi, client, mailto, deadline)
    except RegistryCooldown as e:
        raise LookupFailed(str(e)) from e
    except Exception as e:
        raise LookupFailed(_describe("Crossref", e)) from e
    if response.status_code == 404:
        return await _doi_outside_crossref(doi, client)
    if response.status_code != 200:
        hint = " (rate-limited)" if response.status_code == 429 else ""
        raise LookupFailed(f"Crossref HTTP {response.status_code}{hint}")
    try:
        message = response.json().get("message", {})
    except ValueError as e:
        raise LookupFailed(_describe("Crossref", e)) from e
    issued = (message.get("issued", {}) or {}).get("date-parts") or [[]]
    return {
        "doi": doi,
        "title": (message.get("title") or [""])[0],
        "container": (message.get("container-title") or [""])[0],
        "type": message.get("type", ""),
        "issued": "-".join(str(x) for x in issued[0] if x is not None) if issued and issued[0] else "",
    }


# ---------------------------------------------------------------- destination guard
# URL checks fetch addresses that come from the model's answer. Before the first request and
# before every redirect hop the host is resolved, and the request is refused unless every
# address is public (is_global; IPv4-mapped, 6to4 and NAT64 addresses are judged by the IPv4
# address they carry). Registry lookups (arXiv, Crossref, doi.org, DataCite, DoH) use fixed
# hosts and are not guarded.
#
# Fake-ip resolvers (Clash/mihomo) answer every name with an address in 198.18.0.0/15 and let
# the proxy pick the real destination. What happens to such hosts is set by fake_ip:
#   doh   (default in the server) resolve the name again through DNS-over-HTTPS (sent through
#         the configured proxy), apply the same all-public rule to the real addresses, then let
#         the proxy carry the request;
#   skip  refuse the host (listed in skipped);
#   allow fetch through the proxy without any check.
# Limits: the check resolves the name separately from the connection, so DNS rebinding between
# the two is not prevented; when an HTTP(S)/SOCKS proxy or a fake-ip TUN carries the request,
# the proxy resolves the name itself (possibly with different DNS, e.g. split-horizon) and
# decides the real destination. A refused or failed check never fetches anything.
FAKE_IP_NETWORK = ipaddress.IPv4Network("198.18.0.0/15")
NAT64_NETWORK = ipaddress.IPv6Network("64:ff9b::/96")
RESOLVE_TIMEOUT_S = 5.0
FAKE_IP_MODES = ("doh", "skip", "allow")
DOH_TIMEOUT_S = 5.0
DOH_CACHE_TTL_MAX_S = 300.0
_DOH_CACHE: dict[str, tuple[float, list[str]]] = {}
_NUMERIC_PART_RE = re.compile(r"^(?:0[xX][0-9a-fA-F]*|0[0-7]*|[1-9][0-9]*)$")


class BlockedAddress(Exception):
    """A URL check was refused because its host is, or resolves to, a non-public address."""

    def __init__(self, host: str, address: str, kind: str, literal: bool = False, via: str = ""):
        self.host = host
        self.address = address
        self.kind = kind
        if host == address:
            subject = address
        elif literal:
            subject = f"{host} (= {address})"
        else:
            subject = f"{host} resolves to {address}{f' ({via})' if via else ''}, which"
        if kind == "fake_ip":
            text = f"{subject} is in 198.18.0.0/15, the fake-ip range of Clash/mihomo-style proxies, so the real destination cannot be checked"
        else:
            text = f"{subject} is not a public address"
        super().__init__(text)


class ResolveFailed(Exception):
    """The host of a URL check could not be resolved. definitive: the name does not exist."""

    def __init__(self, message: str, definitive: bool = True):
        super().__init__(message)
        self.definitive = definitive


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


# getaddrinfo errors that mean "this name does not exist" rather than "the resolver did not answer".
_DEFINITIVE_EAI = {getattr(socket, name) for name in ("EAI_NONAME", "EAI_NODATA") if hasattr(socket, name)}


class DohResolver:
    """Resolves names through a DNS-over-HTTPS JSON endpoint (Cloudflare / Google format).

    The client is an ordinary httpx client, so the query goes through the configured proxy.
    Answers are cached for their TTL, at most DOH_CACHE_TTL_MAX_S.
    """

    def __init__(self, url: str, client: httpx.AsyncClient):
        self.url = url or DEFAULT_DOH_URL
        self.client = client
        self._locks: dict[str, asyncio.Lock] = {}

    async def _query(self, host: str, rtype: str) -> tuple[str, list[str], float]:
        """(status, addresses, ttl) with status "ok" | "nxdomain"; raises ResolveFailed(definitive=False)."""
        try:
            response = await self.client.get(
                self.url, params={"name": host, "type": rtype}, headers={"Accept": "application/dns-json"}, timeout=DOH_TIMEOUT_S,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as e:
            raise ResolveFailed(f"{host}: {_describe('DNS-over-HTTPS', e)}", definitive=False) from e
        status = payload.get("Status") if isinstance(payload, dict) else None
        if status == 3:
            return "nxdomain", [], 60.0
        if status != 0:
            raise ResolveFailed(f"{host}: DNS-over-HTTPS answered status {status}", definitive=False)
        wanted = 1 if rtype == "A" else 28
        answers = [a for a in payload.get("Answer") or [] if isinstance(a, dict) and a.get("type") == wanted and a.get("data")]
        ttl = min([float(a.get("TTL") or 0) for a in answers] or [60.0])
        return "ok", [str(a["data"]).strip() for a in answers], ttl

    async def resolve(self, host: str) -> list[str]:
        key = host.lower().rstrip(".")
        async with self._locks.setdefault(key, asyncio.Lock()):  # URLs on one host share one lookup
            return await self._resolve(host, key)

    async def _resolve(self, host: str, key: str) -> list[str]:
        cached = _DOH_CACHE.get(key)
        if cached and cached[0] > time.monotonic():
            if not cached[1]:
                raise ResolveFailed(f"{host}: no such domain (NXDOMAIN via DNS-over-HTTPS)")
            return list(cached[1])
        (s4, a4, t4), (s6, a6, t6) = await asyncio.gather(self._query(key, "A"), self._query(key, "AAAA"))
        addresses = list(dict.fromkeys(a4 + a6))
        if len(_DOH_CACHE) > 512:
            _DOH_CACHE.clear()
        _DOH_CACHE[key] = (time.monotonic() + max(1.0, min(DOH_CACHE_TTL_MAX_S, t4, t6)), addresses)
        if not addresses:
            if "nxdomain" in (s4, s6):
                raise ResolveFailed(f"{host}: no such domain (NXDOMAIN via DNS-over-HTTPS)")
            raise ResolveFailed(f"{host}: no addresses (DNS-over-HTTPS)")
        return addresses


async def check_destination(host: str, port: int, fake_ip: str = "skip", doh: Optional[DohResolver] = None) -> None:
    """Raise BlockedAddress unless every address of host is public, ResolveFailed when it does not resolve."""
    literal = literal_address(host)
    if literal is not None:
        kind = address_problem(literal)
        if kind:
            raise BlockedAddress(host, str(literal), kind, literal=True)
        return
    try:
        addresses = await resolve_host(host, port)
    except socket.gaierror as e:
        raise ResolveFailed(f"{host}: {type(e).__name__}" + (f" {e}" if str(e) else ""), definitive=e.errno in _DEFINITIVE_EAI) from e
    except (UnicodeError, ValueError) as e:
        raise ResolveFailed(f"{host}: {type(e).__name__}" + (f" {e}" if str(e) else "")) from e
    except (OSError, asyncio.TimeoutError) as e:
        raise ResolveFailed(f"{host}: {type(e).__name__}" + (f" {e}" if str(e) else ""), definitive=False) from e
    if not addresses:
        raise ResolveFailed(f"{host}: no addresses")
    fake = []
    for address in addresses:
        kind = address_problem(address)
        if kind == "non_public":
            raise BlockedAddress(host, address, kind)
        if kind == "fake_ip":
            fake.append(address)
    if not fake or fake_ip == "allow":
        return
    if fake_ip == "doh" and doh is not None:
        for address in await doh.resolve(host):
            try:
                kind = address_problem(address)
            except ValueError:
                raise ResolveFailed(f"{host}: DNS-over-HTTPS returned {address!r}", definitive=False)
            if kind:
                raise BlockedAddress(host, address, kind, via="DNS-over-HTTPS")
        return
    raise BlockedAddress(host, fake[0], "fake_ip")


def _host_of(url) -> Optional[str]:
    try:
        return httpx.URL(str(url)).raw_host.decode("ascii", "replace")
    except Exception:
        return None


def _destination_guard(fake_ip: str = "skip", doh: Optional[DohResolver] = None):
    async def guard(request: httpx.Request) -> None:
        """httpx request hook: runs before the first request and before every redirect hop."""
        url = request.url
        await check_destination(url.raw_host.decode("ascii", "replace"), url.port or (443 if url.scheme == "https" else 80), fake_ip, doh)
    return guard


_guard_destination = _destination_guard("skip")


# ---------------------------------------------------------------- URL checks
URL_MAX_REDIRECTS = 10
_TITLE_OPEN_RE = re.compile(r"<title\b", re.I)


async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    """At most max_bytes of the DECODED body. gzip/deflate are inflated here with an output limit
    (httpx's own decoder has none); other content codings are not decoded at all."""
    if response.is_stream_consumed:  # body already loaded (in-memory transports)
        return response.content[:max_bytes]
    coding = (response.headers.get("content-encoding") or "identity").strip().lower()
    if coding in ("", "identity"):
        decoder = None
    elif coding in ("gzip", "x-gzip", "deflate"):
        decoder = zlib.decompressobj(zlib.MAX_WBITS | 32)  # zlib or gzip header, detected
    else:
        return b""
    out = bytearray()
    raw_total = 0
    async for chunk in response.aiter_raw():
        raw_total += len(chunk)
        if decoder is None:
            out += chunk[:max_bytes - len(out)]
        else:
            data = chunk
            try:
                while data and len(out) < max_bytes and not decoder.eof:
                    out += decoder.decompress(data, max_bytes - len(out))
                    data = decoder.unconsumed_tail
            except zlib.error:
                break
        if len(out) >= max_bytes or raw_total >= max_bytes * 16 or (decoder is not None and decoder.eof):
            break
    return bytes(out[:max_bytes])


def _extract_title(text: str) -> str:
    """Text of the first <title> element, in linear time and bounded size."""
    m = _TITLE_OPEN_RE.search(text)
    if not m:
        return ""
    close = text.find(">", m.end(), m.end() + 300)
    if close == -1:
        return ""
    end = text.find("<", close + 1, close + 1 + 2000)
    raw = text[close + 1:end if end != -1 else close + 1 + 2000]
    return html.unescape(" ".join(raw.split())).strip()[:200]


def _decode(body: bytes, charset: Optional[str]) -> str:
    try:
        return body.decode(charset or "utf-8", "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def _proxied(url: str) -> bool:
    """Whether the environment routes url through an HTTP(S) proxy (as httpx's trust_env does)."""
    try:
        parsed = urlparse(url)
        proxies = urllib.request.getproxies_environment()
        if not (proxies.get(parsed.scheme) or proxies.get("all")):
            return False
        return not urllib.request.proxy_bypass_environment(parsed.hostname or "", proxies)
    except Exception:
        return False


async def check_url(url: str, client: httpx.AsyncClient, max_bytes: int = 65536) -> dict:
    """status and title of url. Redirects are followed here, hop by hop, without reading their bodies.

    On failure: error, plus transient=True when the failure says nothing about the page
    (timeouts, network errors, resolver failures) and blocked=True when the guard refused it.
    """
    try:
        request = client.build_request("GET", url, headers={"Accept-Encoding": "gzip, deflate"})
        response = await client.send(request, stream=True, follow_redirects=False)
        try:
            hops = 0
            while response.next_request is not None and hops < URL_MAX_REDIRECTS:
                next_request = response.next_request
                await response.aclose()
                hops += 1
                response = await client.send(next_request, stream=True, follow_redirects=False)
            if response.next_request is not None:
                return {"url": url, "status": None, "transient": True, "error": f"more than {URL_MAX_REDIRECTS} redirects"}
            status = response.status_code
            body = await _read_capped(response, max_bytes) if status < 400 else b""
            charset = response.charset_encoding
            challenge = response.headers.get("cf-mitigated", "").lower() == "challenge"
        finally:
            await response.aclose()
    except BlockedAddress as e:
        first = _host_of(url)
        where = "redirect target " if first is not None and e.host.lower() != first.lower() else ""
        return {"url": url, "status": None, "blocked": True, "error": f"blocked: {where}{e}"}
    except ResolveFailed as e:
        return {"url": url, "status": None, "transient": not e.definitive, "error": f"dns lookup failed: {e}"}
    except httpx.TimeoutException as e:
        return {"url": url, "status": None, "transient": True, "error": f"timed out ({type(e).__name__})"}
    except Exception as e:
        detail = str(e).strip()
        return {"url": url, "status": None, "transient": True, "error": type(e).__name__ + (f": {detail[:120]}" if detail else "")}
    info = {"url": url, "status": status, "title": _extract_title(_decode(body, charset)) if body else ""}
    if challenge:
        info["challenge"] = True
    return info


def _url_outcome(info: dict) -> tuple[str, str]:
    """("urls" | "unresolved" | "lookup_failed" | "skipped", reason) for a check_url result."""
    status = info.get("status")
    if info.get("blocked"):
        return "skipped", info["error"]
    if status is None:
        return ("lookup_failed" if info.get("transient") else "unresolved"), info.get("error", "unreachable")
    if status < 400:
        return "urls", ""
    reason = f"status {status}"
    if status in (408, 425, 429) or status >= 500:
        if status in (502, 503, 504) and _proxied(info["url"]):
            reason += " (from the proxy or the site)"
        return "lookup_failed", reason
    if info.get("challenge"):
        return "lookup_failed", reason + " (bot challenge)"
    if status in (401, 403, 451):
        # Many publishers (Reuters, NYTimes, ScienceDirect, Zhihu, ...) refuse scripted requests outright:
        # the refusal says nothing about whether the page exists, so it is not counted against the citation.
        return "lookup_failed", reason + " (access denied to automated requests; the page may still exist)"
    return "unresolved", reason


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
    re.compile(r"(?i)(?:\btitled\b|\btitle\s+is\b|\btitle\s*[:：]|标题(?:为|是)?|题为|题目(?:为|是)?)\s*[:：]?\s*(.{4,400}?)(?=\s*(?:[.;](?:\s|$)|[。；\n]|\[\[|$))"),
)
_EMPHASIS_PATTERNS = (
    re.compile(r"\*\*([^*\n]{4,400}?)\*\*"),
    re.compile(r"(?<![\w*])\*([^*\n]{4,400}?)\*(?![\w*])"),
    re.compile(r"(?<![\w_])_([^_\n]{4,400}?)_(?![\w_])"),
)
# Each token is followed by its own \s*, so the pattern cannot backtrack polynomially on long whitespace.
_ID_LEAD_RE = re.compile(r"(?i)\s*(?:[(（\[]\s*)*(?:(?:arxiv|doi)\s*)?(?:(?:id|号)\s*)?(?:[:：]\s*)?$")
_BOUNDARY_RE = re.compile(r"[.;。；|](?:\s|$)|[。；|]|\n")
_SENTENCE_RE = re.compile(r"[.!?;](?:\s|$)|[。！？；]")
_ABBREVIATION_RE = re.compile(r"(?i)\b(?:et al|e\.g|i\.e|vs|no|vol|pp|cf|fig|eq|ch|sec|ed|eds)\.")
# Text that may precede an identifier at the start of its line ("- arXiv:", "**DOI:** https://doi.org/").
_LINE_LEAD_RE = re.compile(
    r"(?i)[\s>*_~`#+\-•·\d.,;:：，、)(（）\[\]【】]*"
    r"(?:(?:arxiv|biorxiv|medrxiv|doi|pmid|id|url|link|pdf|preprint|paper|论文|链接|预印本|编号|号)(?:[\s*_]*[:：])?[\s*_,;，、]*)*"
)
_URL_TAIL_RE = re.compile(r"(?:https?://|www\.)\S*$")
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uf900-\ufaff]")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}[a-z]?\b")
_VENUE_RE = re.compile(
    r"(?i)^(?:the\s+)?(?:proceedings\b|journal\s+of\b|transactions\s+on\b|annals\s+of\b|advances\s+in\s+neural\b|"
    r"conference\s+on\b|international\s+conference\b|new\s+england\s+journal\b|nucleic\s+acids\s+research\b|physical\s+review\b)"
)
TITLE_MAX_DISTANCE = 200
_TRIM = " *_\"'“”《》()（）[]-—:：,，"


def _norm_title(text: str) -> str:
    text = html.unescape(text or "")
    text = _LATEX_CMD_RE.sub(" ", text).replace("$", " ")
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"[\W_]+", " ", text)
    return " ".join(text.split())


def _tight(between: str) -> bool:
    """Only punctuation, list markers and labels such as "arXiv:" between a candidate and an identifier."""
    return len(between) <= 40 and bool(_LINE_LEAD_RE.fullmatch(_URL_TAIL_RE.sub("", between)))


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _find_all(haystack_lower: str, needle: str) -> list[tuple[int, int]]:
    needle = needle.lower()
    spans, pos = [], haystack_lower.find(needle) if needle else -1
    while pos != -1:
        spans.append((pos, pos + len(needle)))
        pos = haystack_lower.find(needle, pos + len(needle))
    return spans


def _identifier_windows(answer: str, identifier: str, radius: int = 300, separators: Optional[list] = None,
                        same_paper: Optional[list] = None) -> list[tuple[int, int, int, int, Optional[int], Optional[int]]]:
    """(window_start, window_end, id_start, id_end, prev_separator_end, next_separator_start) per occurrence.

    A window is the identifier's line, extended to the previous line when the identifier starts its
    line, clipped to radius and to the nearest occurrence of another identifier (a separator).
    """
    separators = separators or []
    windows = []
    for pos, end in _find_all(answer.lower(), identifier):
        line_start = answer.rfind("\n", 0, pos) + 1
        line_end = answer.find("\n", end)
        line_end = len(answer) if line_end == -1 else line_end
        start = line_start
        prefix = answer[line_start:pos] if pos - line_start <= 300 else "-" * 301
        for s, e in sorted(same_paper or [], reverse=True):
            if line_start <= s and e <= pos:
                prefix = prefix[:s - line_start] + prefix[e - line_start:]
        prefix = _URL_TAIL_RE.sub("", prefix)
        if line_start > 0 and len(prefix) <= 80 and _LINE_LEAD_RE.fullmatch(prefix):
            prev_start = answer.rfind("\n", 0, line_start - 1) + 1
            if answer[prev_start:line_start - 1].strip():
                start = prev_start
        prev_sep = max((e for s, e in separators if e <= pos), default=None)
        next_sep = min((s for s, e in separators if s >= end), default=None)
        start = max(start, pos - radius, prev_sep if prev_sep is not None else 0)
        stop = min(line_end, end + radius, next_sep if next_sep is not None else len(answer))
        windows.append((start, stop, pos, end, prev_sep, next_sep))
    return windows


def _title_candidates(answer: str, window: tuple, identifier: str, registry_len: int):
    """Yield (candidate, start, end, strong). Strong candidates are explicitly marked as titles."""
    wstart, wend, pos, end = window[:4]
    text = answer[wstart:wend]
    ident = identifier.lower()

    def overlaps_identifier(m: re.Match) -> bool:
        return ident in m.group(1).lower() or (wstart + m.start(1) < end and wstart + m.end(1) > pos)

    for pattern in _STRONG_TITLE_PATTERNS:
        for m in pattern.finditer(text):
            if not overlaps_identifier(m):
                yield m.group(1).strip(), wstart + m.start(), wstart + m.end(), True
    for pattern in _EMPHASIS_PATTERNS:
        for m in pattern.finditer(text):
            cand = m.group(1).strip().strip("\"'“”《》「」『』«»").strip()
            if overlaps_identifier(m) or not cand:
                continue
            # Bold is also used for whole sentences; only short, unpunctuated spans count as titles.
            strong = not re.search(r"[.!?。！？]$", cand) and len(cand) <= max(80, int(registry_len * 1.5))
            yield cand, wstart + m.start(), wstart + m.end(), strong
    before = _BOUNDARY_RE.split(answer[wstart:pos])[-1]
    before = _ID_LEAD_RE.sub("", before).strip(_TRIM)
    if before:
        yield before, pos - len(before), pos, False
    after = re.sub(r"^(?:v\d+)?[)\]）]*\s*[:：—–-]*\s*", "", answer[end:wend])
    after = _BOUNDARY_RE.split(after)[0].strip(_TRIM)
    if after:
        yield after, end, end + len(after), False


def _looks_like_non_title(cand: str, norm: str, container: str) -> bool:
    """Labels ("bioRxiv preprint:"), author-year ("Leviathan et al., 2023"), venues ("*Nature*", "ICML 2023")."""
    stripped = cand.strip(" *_")
    if stripped.endswith((":", "：")):
        return True
    if re.search(r"(?i)\bet\s+al\b|等人", stripped):
        return True
    words = len(norm.split())
    if words <= 4 and _YEAR_RE.search(stripped):
        return True
    if _VENUE_RE.match(stripped):
        return True
    if container and _ratio(norm, _norm_title(container)) >= 0.8:
        return True
    if _CJK_RE.search(norm):
        return len(_CJK_RE.findall(norm)) < 4
    return words < 3


def compare_title(answer: str, identifier: str, registry_title: str, others=(), container: str = "") -> Optional[dict]:
    """Compare the title the answer gives for identifier with the registry title.

    status: match | partial | possible_mismatch | not_stated. None when the
    identifier does not occur in the answer text at all. others lists (identifier,
    registry_title) for the other identifiers in the answer: a candidate is only tied to this
    identifier when no other paper's identifier sits between them and it is not closer to one,
    and a candidate that matches another paper's registry title is never reported.
    """
    answer = answer or ""
    lowered = answer.lower()
    own_spans = _find_all(lowered, identifier)
    if not own_spans:
        return None
    entry = {"registry_title": registry_title}
    registry = _norm_title(registry_title)
    if not registry:
        return {**entry, "status": "not_stated"}
    separators, same_paper, other_titles = [], [], []
    for other_id, other_title in others or ():
        if not other_id or other_id.lower() == identifier.lower():
            continue
        other_norm = _norm_title(other_title)
        spans = [(s, e) for s, e in _find_all(lowered, other_id)
                 if not any(s < oe and os_ < e for os_, oe in own_spans)]
        if other_norm and _ratio(other_norm, registry) >= 0.9:
            same_paper += spans  # another identifier of the same paper is not a separator
            continue
        separators += spans
        if other_norm:
            other_titles.append(other_norm)
    windows = _identifier_windows(answer, identifier, separators=separators, same_paper=same_paper)
    if any(registry in _norm_title(answer[w[0]:w[1]]) for w in windows):
        return {**entry, "status": "match"}
    short = _norm_title(registry_title.split(":", 1)[0]) if ":" in registry_title else ""
    registry_cjk = bool(_CJK_RE.search(registry))
    scored = []
    for window in windows:
        wstart, wend, pos, end, prev_sep, next_sep = window
        for cand, cstart, cend, strong in _title_candidates(answer, window, identifier, len(registry_title)):
            norm = _norm_title(cand)
            if len(norm) < 3 or bool(_CJK_RE.search(norm)) != registry_cjk:
                continue
            ratio = _ratio(norm, registry)
            if short and norm == short:
                ratio = max(ratio, 0.95)  # "SWE-Review" for "SWE-Review: Closing the Loop"
            if any(_ratio(norm, other) >= 0.6 and _ratio(norm, other) > ratio for other in other_titles):
                continue  # another identifier's title
            # A candidate between two identifiers belongs to the one it is written against
            # ("Title (arXiv:Y)"), else to the nearer one.
            if cend <= pos:
                distance, between = pos - cend, answer[cend:pos]
                if prev_sep is not None and not _tight(between) and (
                        _tight(answer[prev_sep:cstart]) or cstart - prev_sep < distance):
                    continue
            elif cstart >= end:
                distance, between = cstart - end, answer[end:cstart]
                if next_sep is not None and not _tight(between) and (
                        _tight(answer[cend:next_sep]) or next_sep - cend < distance):
                    continue
            else:
                distance, between = 0, ""
            if strong and (distance > TITLE_MAX_DISTANCE or _SENTENCE_RE.search(_ABBREVIATION_RE.sub("", between))
                           or _looks_like_non_title(cand, norm, container)):
                strong = False
            scored.append((ratio, strong, cand, distance))
    if not scored:
        return {**entry, "status": "not_stated"}
    # A near-exact title anywhere next to the identifier is a match. Otherwise an explicitly marked
    # title decides; unmarked text next to the identifier can only make it partial.
    best = max(scored, key=lambda x: (x[0], x[1]))
    explicit = [x for x in scored if x[1]]
    if best[0] >= 0.9:
        status, pick = "match", best
    elif explicit:
        best_explicit = max(explicit, key=lambda x: x[0])
        if best_explicit[0] >= 0.6:
            status, pick = "partial", best_explicit
        else:
            status, pick = "possible_mismatch", min(explicit, key=lambda x: (x[3], -x[0]))
    elif best[0] >= 0.6:
        status, pick = "partial", best
    else:
        return {**entry, "status": "not_stated"}
    return {**entry, "status": status, "answer_title": pick[2][:200], "similarity": round(pick[0], 2)}


# ------------------------------------------------------------------- driver
MAX_DATACITE_FALLBACK = 20
# Part of the verification budget kept for the DataCite fallback when arXiv is slow or its one
# allowed connection is busy with other searches (at most 20 % of the budget).
ARXIV_FALLBACK_RESERVE_S = 5.0


async def verify_answer(
    answer: str,
    sources: list[dict] | None,
    *,
    check_ids: bool = True,
    check_urls: bool = True,
    timeout_s: float = 30.0,
    mailto: str = "",
    max_urls: int = 30,
    max_dois: int = 30,
    fake_ip: str = "skip",
    doh_url: str = DEFAULT_DOH_URL,
) -> dict:
    answer = answer or ""
    deadline = time.monotonic() + timeout_s
    fake_ip = fake_ip if fake_ip in FAKE_IP_MODES else "skip"
    answer_arxiv = extract_arxiv_ids(answer)
    arxiv_ids = list(answer_arxiv)
    answer_dois: list[str] = []
    dois: list[str] = []
    doi_keys: set[str] = set()
    answer_doi_list, doi_patterns, doi_skipped = scan_doi_mentions(answer)
    for doi in answer_doi_list:
        mapped = arxiv_id_from_doi(doi)
        if mapped:
            if mapped not in arxiv_ids:
                arxiv_ids.append(mapped)
        else:
            answer_dois.append(doi)
            dois.append(doi)
            doi_keys.add(doi.lower())
    urls: list[str] = []
    skipped: list[dict] = [{"kind": "doi", "value": value, "reason": reason} for value, reason in doi_skipped]
    # Patterns are listed after everything else and capped on their own, so that a list of templates never
    # hides an over-limit or blocked entry.
    pattern_skipped = [{"kind": "doi", "value": d, "reason": DOI_PATTERN_REASON} for d in doi_patterns]
    for source in sources or []:
        url = (source or {}).get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        url = url.strip()
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
        except ValueError:
            skipped.append({"kind": "url", "value": url, "reason": "malformed URL (not checked)"})
            continue
        if parsed.scheme not in ("http", "https"):
            skipped.append({"kind": "url", "value": url, "reason": "unsupported scheme (only http/https are checked)"})
            continue
        if _is_host(host, "arxiv.org"):
            idv = _arxiv_id_from_url(url)
            if idv is None:
                skipped.append({"kind": "url", "value": url, "reason": "arxiv.org URL without a recognisable paper ID (not checked)"})
            elif idv not in arxiv_ids:
                arxiv_ids.append(idv)
            continue
        if _is_host(host, "doi.org"):
            doi = _doi_from_url(url)
            mapped = arxiv_id_from_doi(doi) if doi else None
            if doi is None:
                skipped.append({"kind": "url", "value": url, "reason": "doi.org URL without a recognisable DOI (not checked)"})
            elif mapped:
                if mapped not in arxiv_ids:
                    arxiv_ids.append(mapped)
            elif doi.lower() not in doi_keys:
                dois.append(doi)
                doi_keys.add(doi.lower())
            continue
        literal = literal_address(host)
        problem = address_problem(literal) if literal is not None else None
        if problem:
            skipped.append({"kind": "url", "value": url, "reason": f"blocked: {BlockedAddress(host, str(literal), problem, literal=True)} (not fetched)"})
            continue
        if url not in urls:
            urls.append(url)
    if not check_ids:
        skipped += [{"kind": "arxiv", "value": i, "reason": "identifier checks disabled (GROK_VERIFY_IDS=false)"} for i in arxiv_ids]
        skipped += [{"kind": "doi", "value": d, "reason": "identifier checks disabled (GROK_VERIFY_IDS=false)"} for d in dois]
        arxiv_ids, dois = [], []
    skipped += [{"kind": "url", "value": u, "reason": f"over the per-answer limit of {max_urls} URL checks"} for u in urls[max_urls:]]
    skipped += [{"kind": "doi", "value": d, "reason": f"over the per-answer limit of {max_dois} DOI lookups"} for d in dois[max_dois:]]
    urls = urls[:max_urls]
    dois = dois[:max_dois]
    if not check_urls:
        skipped += [{"kind": "url", "value": u, "reason": "URL checks disabled (GROK_VERIFY_URLS=false)"} for u in urls]

    result: dict = {"scope": SCOPE, "claim_support": "not_checked", "arxiv": [], "doi": [], "urls": [], "unresolved": [], "lookup_failed": []}
    user_agent = "grok-search-verify/1.11"
    doi_sem = asyncio.Semaphore(5)
    url_sem = asyncio.Semaphore(5)

    timeout = httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=None)
    headers = {"User-Agent": user_agent}
    # The contact address selects Crossref's polite pool; it goes to the registries only, never to cited sites.
    registry_headers = {"User-Agent": user_agent + (f" (mailto:{mailto})" if mailto else "")}
    # Registry lookups go to fixed hosts and never follow redirects; only the URL checks, whose targets come
    # from the answer, are guarded. URL checks follow redirects themselves (check_url) so that no redirect
    # body is read unbounded.
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=registry_headers) as client, \
            httpx.AsyncClient(timeout=httpx.Timeout(DOH_TIMEOUT_S), headers=headers) as doh_client, \
            httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=headers,
                              event_hooks={"request": [_destination_guard(fake_ip, DohResolver(doh_url, doh_client) if fake_ip == "doh" else None)]}) as url_client:

        async def from_datacite(idv: str, failed: dict):
            # arXiv could not answer; DataCite registers arXiv's DOIs and usually still can.
            if _time_left(deadline) <= 0.1:
                return
            try:
                async with doi_sem:
                    record = await _datacite_record(f"10.48550/arXiv.{idv}", client)
            except LookupFailed:
                return
            if record and record.get("title"):
                entry = {"id": idv, "title": record["title"], "published": record.get("issued", ""), "authors": [], "registry": "datacite"}
                _ARXIV_CACHE[idv] = (time.monotonic(), entry)
                result["arxiv"].append(entry)
                failed.pop(idv, None)

        async def run_arxiv():
            if not arxiv_ids:
                return
            lookup = asyncio.ensure_future(_resolve_arxiv(arxiv_ids, client, deadline))
            reserve = min(ARXIV_FALLBACK_RESERVE_S, timeout_s * 0.2)
            try:
                await asyncio.wait({lookup}, timeout=max(0.0, _time_left(deadline) - reserve))
            finally:
                if not lookup.done():
                    lookup.cancel()
            await asyncio.wait({lookup})
            if lookup.cancelled():
                now = time.monotonic()
                cached = {i: _ARXIV_CACHE[i][1] for i in arxiv_ids if i in _ARXIV_CACHE and now - _ARXIV_CACHE[i][0] < ARXIV_CACHE_TTL_S}
                found, missing = cached, []
                failed = {i: LookupFailed("timed out") for i in arxiv_ids if i not in cached}
            else:
                found, missing, failed = lookup.result()
            result["arxiv"] += list(found.values())
            result["unresolved"] += [{"kind": "arxiv", "value": i, "reason": "not found"} for i in missing]
            await asyncio.gather(*(from_datacite(i, failed) for i in list(failed)[:MAX_DATACITE_FALLBACK]))
            result["lookup_failed"] += [{"kind": "arxiv", "value": i, "reason": _describe("arXiv", e)} for i, e in failed.items()]

        async def run_doi(doi: str):
            async with doi_sem:
                try:
                    info = await lookup_doi(doi, client, mailto=mailto, deadline=deadline)
                except LookupFailed as e:
                    result["lookup_failed"].append({"kind": "doi", "value": doi, "reason": str(e)})
                    return
                except Exception as e:
                    result["lookup_failed"].append({"kind": "doi", "value": doi, "reason": _describe("DOI lookup", e)})
                    return
            if info:
                result["doi"].append(info)
            else:
                result["unresolved"].append({"kind": "doi", "value": doi, "reason": "not found"})

        async def run_url(url: str):
            async with url_sem:
                info = await check_url(url, url_client)
            outcome, reason = _url_outcome(info)
            if outcome == "urls":
                result["urls"].append(info)
            elif outcome == "skipped":
                skipped.append({"kind": "url", "value": url, "reason": reason})
            else:
                result[outcome].append({"kind": "url", "value": url, "reason": reason})

        tasks = [run_arxiv()] + [run_doi(d) for d in dois]
        if check_urls:
            tasks += [run_url(u) for u in urls]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout_s)
        except asyncio.TimeoutError:
            result["timed_out"] = True
            # Whatever the cancelled lookups had not settled is reported explicitly rather than dropped.
            seen = {(u["kind"], u["value"]) for u in result["unresolved"] + result["lookup_failed"] + skipped}
            done_arxiv = {x["id"] for x in result["arxiv"]}
            done_doi = {x["doi"] for x in result["doi"]}
            done_url = {x["url"] for x in result["urls"]}
            for kind, values, done in (("arxiv", arxiv_ids, done_arxiv), ("doi", dois, done_doi), ("url", urls if check_urls else [], done_url)):
                for value in values:
                    if value not in done and (kind, value) not in seen:
                        result["lookup_failed"].append({"kind": kind, "value": value, "reason": "timed out"})

    result["arxiv"].sort(key=lambda x: x["id"])
    result["doi"].sort(key=lambda x: x["doi"])

    registry_titles = {x["id"].lower(): x.get("title", "") for x in result["arxiv"]}
    registry_titles.update({x["doi"].lower(): x.get("title", "") for x in result["doi"]})
    in_text = [(i, registry_titles.get(i.lower(), "")) for i in answer_arxiv + answer_dois]
    answer_doi_keys = {d.lower() for d in answer_dois}
    title_checks: list[dict] = []
    for entry in result["arxiv"]:
        if entry["id"] in answer_arxiv:
            check = compare_title(answer, entry["id"], entry.get("title", ""), others=in_text)
            if check:
                title_checks.append({"kind": "arxiv", "id": entry["id"], **check})
    for entry in result["doi"]:
        if entry["doi"].lower() in answer_doi_keys:
            check = compare_title(answer, entry["doi"], entry.get("title", ""), others=in_text, container=entry.get("container", ""))
            if check:
                title_checks.append({"kind": "doi", "id": entry["doi"], **check})
    result["title_checks"] = title_checks
    result["skipped"] = skipped[:MAX_SKIPPED_LISTED] + pattern_skipped[:MAX_PATTERNS_LISTED]
    result["checked"] = {"arxiv": len(arxiv_ids), "doi": len(dois), "urls": len(urls) if check_urls else 0}
    statuses = [t["status"] for t in title_checks]
    result["summary"] = {
        "identifiers_found": len(result["arxiv"]) + len(result["doi"]),
        "urls_reachable": len(result["urls"]),
        "unresolved": len(result["unresolved"]),
        "lookup_failed": len(result["lookup_failed"]),
        "skipped": len(skipped) + len(pattern_skipped),
        "title_match": statuses.count("match"),
        "title_partial": statuses.count("partial"),
        "title_possible_mismatch": statuses.count("possible_mismatch"),
        "title_not_stated": statuses.count("not_stated"),
    }
    return result
