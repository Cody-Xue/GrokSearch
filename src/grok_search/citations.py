"""Validate and clean native citation positions in a web_search answer.

Grok-compatible proxies return URL citations as annotations whose
start_index/end_index point at inline markers such as "[[1]](https://example.com/)"
in the answer. The offsets are only useful if they match the text the caller
receives, so every answer goes through normalize_citations() before it is returned:

1. Duplicate markers are collapsed: inside a run of adjacent markers (only
   spaces or tabs between them) a marker whose URL already appeared in that run
   is removed, e.g. "[[1]](u)[[1]](u)[[2]](v)" -> "[[1]](u)[[2]](v)".
2. Offsets are recomputed for the cleaned text.
3. An annotation whose span is exactly a marker for the same URL (and label),
   or exactly one Markdown link to that URL, is kept with shifted offsets.
4. Any other annotation is relocated to the next unclaimed marker for the same
   URL in reading order (label match preferred) -- this repairs offsets that
   use another unit or scale -- or, when there is no such marker, loses its
   coordinates. The upstream values are kept as upstream_start_index /
   upstream_end_index and "position" says "relocated" or "removed".
"""
import bisect
import copy
import json
import re
from dataclasses import dataclass

# The URL keeps one level of balanced parentheses; square brackets end it, so a long run of unclosed
# markers cannot make one match attempt scan the rest of the answer.
MARKER_RE = re.compile(
    r"\[\[(?P<label>[^\[\]\s]{1,16})\]\]"
    r"\((?P<url>https?://(?:[^\s()\[\]]|\([^\s()\[\]]*\))+)\)"
)
_RUN_GAP_RE = re.compile(r"[ \t]*")


@dataclass(frozen=True)
class Marker:
    start: int
    end: int
    label: str
    url: str


def find_markers(text: str) -> list[Marker]:
    return [Marker(m.start(), m.end(), m.group("label"), m.group("url")) for m in MARKER_RE.finditer(text or "")]


def _url_key(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _collapse_duplicates(text: str, markers: list[Marker]) -> tuple[list[tuple[int, int]], dict[int, int]]:
    """Deletion spans for repeated markers, and {removed marker index: kept marker index}."""
    deletions: list[tuple[int, int]] = []
    twin_of: dict[int, int] = {}
    seen_in_run: dict[str, int] = {}
    for i, marker in enumerate(markers):
        same_run = i > 0 and _RUN_GAP_RE.fullmatch(text[markers[i - 1].end:marker.start]) is not None
        if not same_run:
            seen_in_run = {}
        key = _url_key(marker.url)
        if key in seen_in_run:
            deletions.append((markers[i - 1].end, marker.end))  # the marker and the gap before it
            twin_of[i] = seen_in_run[key]
        else:
            seen_in_run[key] = i
    return deletions, twin_of


def _shifter(deletions: list[tuple[int, int]]):
    """pos -> position after the (sorted, non-overlapping) deletions are applied; O(log n) per call."""
    ends = [end for _, end in deletions]
    removed = [0]
    for start, end in deletions:
        removed.append(removed[-1] + end - start)

    def shift(pos: int) -> int:
        return pos - removed[bisect.bisect_right(ends, pos)]

    return shift


def _shift(pos: int, deletions: list[tuple[int, int]]) -> int:
    return _shifter(deletions)(pos)


def _apply(text: str, deletions: list[tuple[int, int]]) -> str:
    if not deletions:
        return text
    parts: list[str] = []
    last = 0
    for start, end in deletions:
        parts.append(text[last:start])
        last = end
    parts.append(text[last:])
    return "".join(parts)


def _is_link_span(text: str, start: int, end: int, url: str) -> bool:
    """True when text[start:end] is exactly one Markdown link to url, optionally wrapped in brackets."""
    if not (0 <= start < end <= len(text)) or not url:
        return False
    # No two whitespace runs are adjacent, so an all-whitespace span is rejected in linear time.
    pattern = r"\s*(?:[(\[]\s*)?\[[^\[\]]*\]\(\s*" + re.escape(_url_key(url)) + r"/?\s*\)\s*(?:[)\]]\s*)?"
    return re.fullmatch(pattern, text[start:end]) is not None


def _positional(occ) -> bool:
    return isinstance(occ, dict) and type(occ.get("start_index")) is int and type(occ.get("end_index")) is int


def normalize_citations(content: str, sources: list[dict]) -> tuple[str, list[dict], dict]:
    """Return (cleaned content, sources with checked offsets, report)."""
    text = content or ""
    markers = find_markers(text)
    deletions, twin_of = _collapse_duplicates(text, markers)
    cleaned = _apply(text, deletions)
    shift = _shifter(deletions)
    by_span = {(m.start, m.end): i for i, m in enumerate(markers)}
    # Markers per URL (and per URL + label) in reading order, for relocation without rescanning.
    by_url: dict[str, list[int]] = {}
    by_url_label: dict[tuple[str, str], list[int]] = {}
    for i, m in enumerate(markers):
        by_url.setdefault(_url_key(m.url), []).append(i)
        by_url_label.setdefault((_url_key(m.url), m.label), []).append(i)
    cursor: dict = {}

    def first_unclaimed(key, indices: list[int] | None) -> int | None:
        # claimed only grows, so each list is walked at most once in total.
        if not indices:
            return None
        pos = cursor.get(key, 0)
        while pos < len(indices) and indices[pos] in claimed:
            pos += 1
        cursor[key] = pos
        return indices[pos] if pos < len(indices) else None

    def final_span(i: int) -> tuple[int, int]:
        kept = markers[twin_of.get(i, i)]
        return shift(kept.start), shift(kept.end)

    report = {
        "markers": len(markers) - len(twin_of),
        "duplicate_markers_removed": len(twin_of),
        "checked": 0,
        "valid": 0,
        "relocated": 0,
        "removed": 0,
    }
    out = copy.deepcopy(sources or [])
    claimed: set[int] = set()
    pending: list[tuple[dict, int, dict, str]] = []

    for source in out:
        if not isinstance(source, dict) or not isinstance(source.get("citations"), list):
            continue
        url = source.get("url", "")
        url_key = _url_key(url)
        citations = source["citations"]
        for idx, occ in enumerate(citations):
            if not _positional(occ):
                continue
            report["checked"] += 1
            start, end, label = occ["start_index"], occ["end_index"], occ.get("label")
            hit = by_span.get((start, end))
            if hit is not None and _url_key(markers[hit].url) == url_key and label in (None, markers[hit].label):
                claimed.add(hit)
                new_start, new_end = final_span(hit)
                citations[idx] = {**occ, "start_index": new_start, "end_index": new_end}
                report["valid"] += 1
            elif hit is None and _is_link_span(text, start, end, url):
                citations[idx] = {**occ, "start_index": shift(start), "end_index": shift(end)}
                report["valid"] += 1
            else:
                pending.append((source, idx, occ, url_key))

    for source, idx, occ, url_key in sorted(pending, key=lambda p: (p[2]["start_index"], p[2]["end_index"])):
        label = occ.get("label")
        rest = {k: v for k, v in occ.items() if k not in ("start_index", "end_index")}
        upstream = {"upstream_start_index": occ["start_index"], "upstream_end_index": occ["end_index"]}
        target = None
        if label is not None:
            target = first_unclaimed(("label", url_key, label), by_url_label.get((url_key, label)))
        if target is None:
            target = first_unclaimed(("url", url_key), by_url.get(url_key))
        if target is not None:
            claimed.add(target)
            new_start, new_end = final_span(target)
            source["citations"][idx] = {**rest, "start_index": new_start, "end_index": new_end, "position": "relocated", **upstream}
            report["relocated"] += 1
        else:
            source["citations"][idx] = {**rest, "position": "removed", **upstream}
            report["removed"] += 1

    for source in out:
        if not isinstance(source, dict) or not isinstance(source.get("citations"), list):
            continue
        seen: set = set()
        unique: list = []
        for occ in source["citations"]:
            if _positional(occ):
                key = ("at", occ.get("label"), occ["start_index"], occ["end_index"])
            else:
                key = ("raw", json.dumps(occ, sort_keys=True, default=str))
            if key in seen:
                continue
            seen.add(key)
            unique.append(occ)
        source["citations"] = unique
    return cleaned, out, report
