import ast
import json
import re
import uuid
from collections import OrderedDict
from typing import Any
from urllib.parse import urlparse

import asyncio

from .citations import MARKER_RE
from .utils import URL_BODY, clean_url, extract_unique_urls


# Every pattern below keeps whitespace inside one line ([ \t], never \s across newlines) and bounds link
# titles, so a pathological answer (thousands of blank lines, "#" plus spaces, runs of "[") is parsed
# in linear time instead of stalling the event loop. Each optional [ \t]* run is also tied to the
# token that must follow it: two optional runs side by side ("[ \t]*(?:\*\*)?[ \t]*") let the engine
# try every way of splitting one run of spaces between them, which is polynomial per line.
_MD_LINK_PATTERN = re.compile(r"\[([^\[\]\n]{1,500})\]\((https?://" + URL_BODY + r")\)")
_BARE_URL_PATTERN = re.compile(r"https?://" + URL_BODY)
_SOURCES_HEADING_PATTERN = re.compile(
    r"(?im)^[ \t]*"
    r"(?:#{1,6}[ \t]*)?"
    r"(?:(?:\*\*|__)[ \t]*)?"
    r"(sources?|references?|citations?|信源|参考资料|参考|引用|来源列表|来源)"
    r"(?:[ \t]*(?:\*\*|__))?"
    r"(?:[ \t]*[（(][^)）\n]*[)）])?"
    r"(?:[ \t]*[:：])?"
    r"(?:[ \t]*(?:\*\*|__))?"
    r"[ \t]*\r?$"
)
_SOURCES_FUNCTION_PATTERN = re.compile(
    r"(?im)^[ \t]*(sources|source|citations|citation|references|reference|citation_card|source_cards|source_card)[ \t]*\("
)
# A line that is nothing but a link: a bare URL, "[title](url)" or a "[[n]](url)" marker, optionally
# bulleted or numbered (trailing punctuation is stripped before matching).
_PURE_LINK_LINE = re.compile(
    r"(?:(?:[-*+•]|\d{1,3}[.)]|\[\d{1,3}\]:?)[ \t]+)?"
    r"(?:\*\*|__)?"
    r"(?:<?https?://" + URL_BODY + r">?"
    r"|\[[^\[\]\n]{1,500}\]\(https?://" + URL_BODY + r"\)"
    r"|\[\[[^\[\]\s]{1,16}\]\]\(https?://" + URL_BODY + r"\))"
    r"(?:\*\*|__)?"
)
_LIST_BULLET = re.compile(r"(?:[-*+•]|\d{1,3}[.)])[ \t]*")
_FENCE = re.compile(r"[ \t]{0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_FUNCTION_CALL_ATTEMPTS = 3
_TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src"}


def url_identity(url: str) -> str:
    """Key used to deduplicate sources: scheme, "www.", fragment, trailing slash and tracking parameters are ignored."""
    raw = (url or "").strip()
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return raw
    if host.startswith("www."):
        host = host[4:]
    if port and port not in (80, 443):
        host = f"{host}:{port}"
    params = sorted(
        part for part in parsed.query.split("&")
        if part and not (
            part.split("=", 1)[0].lower().startswith("utm_")
            or part.split("=", 1)[0].lower() in _TRACKING_PARAMS
        )
    )
    return host + parsed.path.rstrip("/") + ("?" + "&".join(params) if params else "")


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


class SourcesCache:
    def __init__(self, max_size: int = 256):
        self._max_size = max_size
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[str, list[dict]] = OrderedDict()

    async def set(self, session_id: str, sources: list[dict]) -> None:
        async with self._lock:
            self._cache[session_id] = sources
            self._cache.move_to_end(session_id)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    async def get(self, session_id: str) -> list[dict] | None:
        async with self._lock:
            sources = self._cache.get(session_id)
            if sources is None:
                return None
            self._cache.move_to_end(session_id)
            return sources


def merge_sources(*source_lists: list[dict]) -> list[dict]:
    """Concatenate source lists, dropping repeats of the same document (compared by url_identity).

    The first entry wins; citation locations and a missing title of a later duplicate are merged into it
    (on a copy, the inputs are not modified), so two spellings of one URL do not count twice.
    """
    merged: list[dict] = []
    index: dict[str, int] = {}
    for sources in source_lists:
        for item in sources or []:
            url = item.get("url") if isinstance(item, dict) else None
            if not isinstance(url, str) or not url.strip():
                continue
            key = url_identity(url)
            if key not in index:
                index[key] = len(merged)
                merged.append(item)
                continue
            kept = merged[index[key]]
            known = kept.get("citations") or []
            more = [occ for occ in item.get("citations") or [] if occ not in known]
            title = item.get("title") if not kept.get("title") else None
            if more or title:
                kept = dict(kept)
                if more:
                    kept["citations"] = list(known) + more
                if title:
                    kept["title"] = title
                merged[index[key]] = kept
    return merged


def sources_from_annotations(annotations: list[dict]) -> list[dict]:
    """Group native URL citations without losing repeated citation locations.

    Accept both flat Grok annotations and nested Chat Completions citations.
    Numeric titles are citation labels, not document titles. Offsets are kept
    as supplied by the upstream API and refer to the unmodified answer.
    """
    by_url: dict[str, dict] = {}
    seen: set[tuple] = set()
    for annotation in annotations:
        if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
            continue
        citation = annotation.get("url_citation", annotation)
        if not isinstance(citation, dict):
            continue
        url = citation.get("url")
        if not isinstance(url, str) or not url.strip().startswith(("https://", "http://")):
            continue
        url = url.strip()
        source = by_url.setdefault(url, {"url": url, "provider": "grok", "citations": []})
        occurrence = {}
        title = citation.get("title")
        if isinstance(title, str) and title.strip():
            title = title.strip()
            if title.isdecimal():
                occurrence["label"] = title
            else:
                source.setdefault("title", title)
        for key in ("start_index", "end_index"):
            value = citation.get(key)
            if type(value) is int and value >= 0:
                occurrence[key] = value
        key = (url, tuple(sorted(occurrence.items())))
        if occurrence and key not in seen:
            seen.add(key)
            source["citations"].append(occurrence)
    return list(by_url.values())


def split_answer_and_sources(text: str) -> tuple[str, list[dict]]:
    """Return (answer, sources) for a model-written source list at the end of the answer.

    Sources are harvested from a trailing sources(...) call, a "Sources"/"来源" heading, a closing
    <details> block or a trailing run of link lines. Answer text is only removed when the block is
    unambiguously nothing but sources -- a structured sources(...) payload, or a sources heading / details
    block followed only by link lines up to the end -- and something is left after removing it.
    Otherwise the answer is returned unchanged and the URLs are only harvested.
    """
    raw = (text or "").strip()
    if not raw:
        return "", []

    for splitter in (_split_function_call_sources, _split_heading_sources, _split_details_block_sources, _split_tail_link_block):
        split = splitter(raw)
        if split:
            answer, sources = split
            return (answer if answer else raw), sources

    return raw, []


def sources_from_text(text: str) -> list[dict]:
    """Sources cited in an answer that arrived without native url_citation annotations.

    Inline markers ("[[1]](url)") become citations with code-point offsets into `text`, Markdown links
    contribute their title, bare URLs are taken as they are. Fenced code blocks and inline code are skipped,
    so example URLs in code are not reported as sources. Order follows the first mention.
    """
    masked = _mask_code(text or "")
    hits: list[tuple[int, int, str, str, str]] = []
    for m in MARKER_RE.finditer(masked):
        hits.append((m.start(), -m.end(), "marker", m.group("url"), m.group("label")))
    for m in _MD_LINK_PATTERN.finditer(masked):
        hits.append((m.start(), -m.end(), "link", m.group(2), m.group(1)))
    for m in _BARE_URL_PATTERN.finditer(masked):
        hits.append((m.start(), 0, "url", m.group(), ""))
    hits.sort(key=lambda hit: (hit[0], hit[1]))

    by_key: dict[str, dict] = {}
    out: list[dict] = []
    for start, neg_end, kind, url, extra in hits:
        url = clean_url(url)
        if not url.partition("://")[2]:
            continue
        key = url_identity(url)
        source = by_key.get(key)
        if source is None:
            source = by_key[key] = {"url": url, "provider": "grok"}
            out.append(source)
        if kind == "marker":
            source.setdefault("citations", []).append({"label": extra, "start_index": start, "end_index": -neg_end})
        elif kind == "link":
            title = extra.strip()
            if title and not title.isdecimal() and title != url:
                source.setdefault("title", title)
    return out


def _mask_code(text: str) -> str:
    """Blank out fenced code blocks and inline code spans, keeping every offset (and line break) in place."""
    out: list[str] = []
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        m = _FENCE.match(line)
        if fence is None and not m:
            out.append(_INLINE_CODE.sub(lambda c: " " * len(c.group()), line))
            continue
        if fence is None:
            fence = m.group(1)
        elif m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not line[m.end():].strip():
            fence = None
        out.append("".join(ch if ch in "\r\n" else " " for ch in line))
    return "".join(out)


def _split_function_call_sources(text: str) -> tuple[str, list[dict]] | None:
    matches = list(_SOURCES_FUNCTION_PATTERN.finditer(text))
    # Only the last few calls can end exactly at the end of the text; trying every earlier one would
    # rescan the rest of the answer for each of them.
    for m in reversed(matches[-_FUNCTION_CALL_ATTEMPTS:]):
        open_paren_idx = m.end() - 1
        extracted = _extract_balanced_call_at_end(text, open_paren_idx)
        if not extracted:
            continue

        close_paren_idx, args_text = extracted
        sources, structured = _parse_sources_payload(args_text)
        if not sources:
            continue

        # A JSON / literal payload is machine output and can go; prose inside "sources (...)" stays.
        answer = text[: m.start()].rstrip() if structured else text
        return answer, sources

    return None


def _extract_balanced_call_at_end(text: str, open_paren_idx: int) -> tuple[int, str] | None:
    if open_paren_idx < 0 or open_paren_idx >= len(text) or text[open_paren_idx] != "(":
        return None

    depth = 1
    in_string: str | None = None
    escape = False

    for idx in range(open_paren_idx + 1, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == in_string:
                in_string = None
            continue

        if ch in ("'", '"'):
            in_string = ch
            continue

        if ch == "(":
            depth += 1
            continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                if text[idx + 1 :].strip():
                    return None
                args_text = text[open_paren_idx + 1 : idx]
                return idx, args_text

    return None


def _split_heading_sources(text: str) -> tuple[str, list[dict]] | None:
    matches = list(_SOURCES_HEADING_PATTERN.finditer(text))
    if not matches:
        return None

    # The last heading that has links after it. Later headings had none, so only the stretch up to the
    # next heading needs scanning: every character is read once however many headings there are.
    segment_end = len(text)
    for m in reversed(matches):
        sources = _extract_sources_from_text(text[m.start():segment_end])
        segment_end = m.start()
        if not sources:
            continue
        tail = [line.strip() for line in text[m.end():].splitlines() if line.strip()]
        if tail and all(_is_pure_link_line(line) for line in tail):
            return text[: m.start()].rstrip(), sources
        return text, sources
    return None


def _split_tail_link_block(text: str) -> tuple[str, list[dict]] | None:
    """Harvest a trailing run of link lines. Without a sources heading it may be part of the answer
    (a list of linked items, a table), so the text is never cut here."""
    lines = text.splitlines()
    idx = len(lines) - 1
    while idx >= 0 and not lines[idx].strip():
        idx -= 1
    if idx < 0:
        return None

    tail_end = idx
    link_like_count = 0
    while idx >= 0:
        line = lines[idx].strip()
        if not line:
            idx -= 1
            continue
        if not _has_link(line):
            break
        link_like_count += 1
        idx -= 1

    if link_like_count < 2:
        return None

    sources = _extract_sources_from_text("\n".join(lines[idx + 1 : tail_end + 1]))
    if not sources:
        return None
    return text, sources


def _split_details_block_sources(text: str) -> tuple[str, list[dict]] | None:
    lower = text.lower()
    close_idx = lower.rfind("</details>")
    if close_idx == -1:
        return None
    tail = text[close_idx + len("</details>") :].strip()
    if tail:
        return None

    open_idx = lower.rfind("<details", 0, close_idx)
    if open_idx == -1:
        return None

    block_text = text[open_idx : close_idx + len("</details>")]
    sources = _extract_sources_from_text(block_text)
    if len(sources) < 2:
        return None

    inner = block_text
    summary_end = inner.lower().find("</summary>")
    if summary_end != -1:
        inner = inner[summary_end + len("</summary>") :]
    inner = re.sub(r"<[^<>]*>", "\n", inner)
    lines = [line.strip() for line in inner.splitlines() if line.strip()]
    if lines and all(_is_pure_link_line(line) for line in lines):
        return text[:open_idx].rstrip(), sources
    return text, sources


def _is_pure_link_line(line: str) -> bool:
    return _PURE_LINK_LINE.fullmatch(line.strip().rstrip(" \t.,;:")) is not None


def _has_link(line: str) -> bool:
    """A line that carries a link: it starts with a URL (after a bullet) or contains a Markdown link."""
    stripped = line.strip()
    bullet = _LIST_BULLET.match(stripped)
    if bullet:
        stripped = stripped[bullet.end():]
    if not stripped:
        return False
    if stripped.startswith(("http://", "https://")):
        return True
    return _MD_LINK_PATTERN.search(stripped) is not None


def _parse_sources_payload(payload: str) -> tuple[list[dict], bool]:
    """(sources, structured): structured is True when the payload parsed as JSON or a Python literal."""
    payload = (payload or "").strip().rstrip(";")
    if not payload:
        return [], False

    data: Any = None
    try:
        data = json.loads(payload)
    except Exception:
        try:
            data = ast.literal_eval(payload)
        except Exception:
            data = None

    if data is None:
        return _extract_sources_from_text(payload), False

    if isinstance(data, dict):
        for key in ("sources", "citations", "references", "urls"):
            if key in data:
                return _normalize_sources(data[key]), True
        return _normalize_sources(data), True

    return _normalize_sources(data), True


def _normalize_sources(data: Any) -> list[dict]:
    items: list[Any]
    if isinstance(data, (list, tuple)):
        items = list(data)
    elif isinstance(data, dict):
        items = [data]
    else:
        items = [data]

    normalized: list[dict] = []
    seen: set[str] = set()

    for item in items:
        if isinstance(item, str):
            for url in extract_unique_urls(item):
                if url not in seen:
                    seen.add(url)
                    normalized.append({"url": url})
            continue

        if isinstance(item, (list, tuple)) and len(item) >= 2:
            title, url = item[0], item[1]
            if isinstance(url, str) and url.startswith(("http://", "https://")) and url not in seen:
                seen.add(url)
                out: dict = {"url": url}
                if isinstance(title, str) and title.strip():
                    out["title"] = title.strip()
                normalized.append(out)
            continue

        if isinstance(item, dict):
            url = item.get("url") or item.get("href") or item.get("link")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                continue
            if url in seen:
                continue
            seen.add(url)
            out: dict = {"url": url}
            title = item.get("title") or item.get("name") or item.get("label")
            if isinstance(title, str) and title.strip():
                out["title"] = title.strip()
            desc = item.get("description") or item.get("snippet") or item.get("content")
            if isinstance(desc, str) and desc.strip():
                out["description"] = desc.strip()
            normalized.append(out)
            continue

    return normalized


def _extract_sources_from_text(text: str) -> list[dict]:
    """Markdown links (with their title) and bare URLs, in reading order, each URL once."""
    hits: list[tuple[int, str, str]] = []
    for m in _MD_LINK_PATTERN.finditer(text or ""):
        hits.append((m.start(), m.group(2), m.group(1)))
    for m in _BARE_URL_PATTERN.finditer(text or ""):
        hits.append((m.start(), m.group(), ""))
    hits.sort(key=lambda hit: hit[0])

    sources: list[dict] = []
    by_url: dict[str, dict] = {}
    for _, url, title in hits:
        url = clean_url(url)
        if not url.partition("://")[2]:
            continue
        source = by_url.get(url)
        if source is None:
            source = by_url[url] = {"url": url}
            sources.append(source)
        title = (title or "").strip()
        if title and "title" not in source:
            source["title"] = title
    return sources
