import time

import pytest

from grok_search.citations import find_markers, normalize_citations
from grok_search.sources import (
    merge_sources,
    sources_from_annotations,
    sources_from_text,
    split_answer_and_sources,
    url_identity,
)
from grok_search.utils import extract_unique_urls

WIKI = "https://en.wikipedia.org/wiki/Python_(programming_language)"


def urls(sources):
    return [item["url"] for item in sources]


# --- answer text is never cut unless it is a pure source list ---------------------------------------

LINK_LIST = (
    "Open httpx issues about memory growth:\n\n"
    "- [#3734 SSL context memory usage](https://github.com/encode/httpx/issues/3734): create_ssl_context() can hold 100s of MB.\n"
    "- [#3782 BoundSyncStream reference cycle](https://github.com/encode/httpx/issues/3782): Response objects are not collected.\n"
    "- [Discussion #3272](https://github.com/encode/httpx/discussions/3272): workaround is to reuse one Client."
)
ONLY_LIST = (
    "- [PEP 703](https://peps.python.org/pep-0703/) makes the GIL optional (free-threaded build).\n"
    "- [PEP 744](https://peps.python.org/pep-0744/) adds an experimental JIT.\n"
    "- [PEP 667](https://peps.python.org/pep-0667/) defines locals() semantics."
)
TABLE = (
    "Comparison:\n\n| Project | Licence | Link |\n|---|---|---|\n"
    "| httpx | BSD-3 | [repo](https://github.com/encode/httpx) |\n"
    "| requests | Apache-2.0 | [repo](https://github.com/psf/requests) |"
)
TEXT_AFTER_HEADING = (
    "Python 3.13 was released on 2024-10-07.\n\n## Sources\n- https://docs.python.org/3/whatsnew/3.13.html\n\n"
    "Inference: the free-threaded build is still experimental."
)


@pytest.mark.parametrize("text, expected", [
    (LINK_LIST, ["https://github.com/encode/httpx/issues/3734", "https://github.com/encode/httpx/issues/3782", "https://github.com/encode/httpx/discussions/3272"]),
    (ONLY_LIST, ["https://peps.python.org/pep-0703/", "https://peps.python.org/pep-0744/", "https://peps.python.org/pep-0667/"]),
    (TABLE, ["https://github.com/encode/httpx", "https://github.com/psf/requests"]),
    (TEXT_AFTER_HEADING, ["https://docs.python.org/3/whatsnew/3.13.html"]),
])
def test_answer_text_is_kept_and_links_are_only_harvested(text, expected):
    answer, sources = split_answer_and_sources(text)
    assert answer == text
    assert urls(sources) == expected


@pytest.mark.parametrize("block", [
    "Sources:\n- [Example](https://example.test/a)\n- https://example.test/b",
    "## References\n1. https://example.test/a\n2. <https://example.test/b>.",
    "**Sources:**\n[1] https://example.test/a\n[2]: [B](https://example.test/b)",
    "### 来源\n- [[1]](https://example.test/a)\n- [[2]](https://example.test/b)",
])
def test_pure_link_list_under_a_sources_heading_is_split_off(block):
    answer, sources = split_answer_and_sources("The answer.\n\n" + block)
    assert answer == "The answer."
    assert urls(sources) == ["https://example.test/a", "https://example.test/b"]


def test_heading_list_with_annotations_or_prose_is_kept():
    text = "The answer.\n\n## Sources\n- [Docs](https://example.test/a) - official documentation\n- https://example.test/b"
    answer, sources = split_answer_and_sources(text)
    assert answer == text and urls(sources) == ["https://example.test/a", "https://example.test/b"]


def test_a_text_made_only_of_a_source_list_is_not_emptied():
    text = "## Sources\n- https://example.test/a\n- https://example.test/b"
    answer, sources = split_answer_and_sources(text)
    assert answer == text and len(sources) == 2


def test_structured_sources_call_is_split_but_prose_in_parentheses_is_kept():
    answer, sources = split_answer_and_sources('Answer.\nsources([{"title": "A", "url": "https://example.test/a"}])')
    assert answer == "Answer." and sources == [{"url": "https://example.test/a", "title": "A"}]
    prose = "Answer.\nCitation (see [the docs](https://example.test/d) for details)"
    answer, sources = split_answer_and_sources(prose)
    assert answer == prose and urls(sources) == ["https://example.test/d"]


def test_details_block_is_split_only_when_it_holds_nothing_but_links():
    pure = "Answer.\n<details><summary>Sources</summary>\n\n- https://example.test/a\n- [B](https://example.test/b)\n</details>"
    answer, sources = split_answer_and_sources(pure)
    assert answer == "Answer." and set(urls(sources)) == {"https://example.test/a", "https://example.test/b"}
    mixed = "Answer.\n<details><summary>More</summary>\nSee https://example.test/a and https://example.test/b for the benchmark.\n</details>"
    answer, _ = split_answer_and_sources(mixed)
    assert answer == mixed


# --- URLs from answer text ------------------------------------------------------------------------------

def test_balanced_parentheses_are_kept_and_closing_brackets_dropped():
    text = f"Python is a language.\n\n## Sources\n- [Python (programming language)]({WIKI})\n- [Docs](https://docs.python.org/3/)"
    _, sources = split_answer_and_sources(text)
    assert urls(sources) == [WIKI, "https://docs.python.org/3/"]
    _, bracketed = split_answer_and_sources("Answer.\n\nSources:\n- [https://example.com/a]\n- [https://example.com/b]")
    assert urls(bracketed) == ["https://example.com/a", "https://example.com/b"]
    assert extract_unique_urls(f"see {WIKI}, or (https://docs.python.org/3/). **https://example.com/x**") == [
        WIKI, "https://docs.python.org/3/", "https://example.com/x"]


def test_text_sources_do_not_duplicate_a_native_url():
    text = f"Python is a language.[[1]]({WIKI})\n\n## Sources\n- [Python]({WIKI})\n- [Docs](https://docs.python.org/3/)"
    native = sources_from_annotations([{"type": "url_citation", "url": WIKI, "title": "1", "start_index": 21, "end_index": 21 + len(f"[[1]]({WIKI})")}])
    _, text_sources = split_answer_and_sources(text)
    assert urls(merge_sources(native, text_sources)) == [WIKI, "https://docs.python.org/3/"]


def test_merge_sources_compares_by_identity_and_keeps_both_citations():
    first = {"url": "https://www.example.test/a/", "provider": "grok", "citations": [{"label": "1", "start_index": 0, "end_index": 5}]}
    second = {"url": "http://example.test/a?utm_source=x#top", "title": "A", "citations": [{"label": "2", "start_index": 9, "end_index": 14}]}
    merged = merge_sources([first], [second, {"url": "https://example.test/b"}])
    assert urls(merged) == ["https://www.example.test/a/", "https://example.test/b"]
    assert merged[0]["title"] == "A" and [c["label"] for c in merged[0]["citations"]] == ["1", "2"]
    assert len(first["citations"]) == 1 and "title" not in first  # inputs untouched
    assert url_identity("https://www.Example.test/a/?utm_medium=m&b=2&a=1") == "example.test/a?a=1&b=2"


# --- fallback sources (no native annotations) ----------------------------------------------------------

def test_inline_markers_links_and_urls_become_sources_with_marker_offsets():
    text = (
        f"Python[[1]]({WIKI}) is popular.[[2]](https://example.test/b) Again[[1]]({WIKI}).\n"
        "See [the docs](https://docs.python.org/3/) and https://example.test/c.\n"
        "```bash\ncurl https://api.example.test/v1/items\n```\n"
        "Run `pip install https://example.test/pkg.whl` first."
    )
    sources = sources_from_text(text)
    assert urls(sources) == [WIKI, "https://example.test/b", "https://docs.python.org/3/", "https://example.test/c"]
    assert sources[2]["title"] == "the docs" and all(item["provider"] == "grok" for item in sources)
    markers = find_markers(text)
    assert [(c["label"], c["start_index"], c["end_index"]) for c in sources[0]["citations"]] == [
        ("1", markers[0].start, markers[0].end), ("1", markers[2].start, markers[2].end)]
    for source in sources:
        for c in source.get("citations", []):
            assert text[c["start_index"]:c["end_index"]] == f"[[{c['label']}]]({source['url']})"


# --- linear time on hostile input (all of these took >30 s before) -----------------------------------

N = 100_000
HOSTILE = {
    "newline run": lambda: split_answer_and_sources("a" + "\n" * N + "a"),
    "'#' + spaces": lambda: split_answer_and_sources("a\n#" + " " * N + "x"),
    "last line of '['": lambda: split_answer_and_sources("answer\n" + "[" * N),
    "unclosed sources( lines": lambda: split_answer_and_sources("answer\n" + "sources(\n" * (N // 9)),
    "heading + dots": lambda: split_answer_and_sources("a\n## Sources\n- https://a.example/" + "." * N + " y"),
    # Polynomial in the run length with side-by-side optional whitespace runs (400 spaces took 21 s).
    "heading word + spaces": lambda: split_answer_and_sources("a\n\nSources" + " " * N + "x\n\nmore"),
    "heading word + space/tab": lambda: split_answer_and_sources("a\n\nSources" + " \t" * (N // 2) + "x"),
    "## 参考 + spaces": lambda: split_answer_and_sources("回答。\n\n## 参考" + " " * N + "。\n"),
    "heading with spaced tokens": lambda: split_answer_and_sources(
        "a\n**" + " " * (N // 4) + "Sources" + " " * (N // 4) + "**" + " " * (N // 4) + ":" + " " * (N // 4) + "x"),
    "unclosed fences": lambda: sources_from_text("````\n" * (N // 5)),
    "unclosed markers": lambda: sources_from_text("[[1]](http://(b)" * (N // 16)),
    "whitespace link span": lambda: normalize_citations(" " * N + "[a](https://example.com/p)", [{"url": "https://example.com/p", "citations": [{"start_index": 0, "end_index": N}]}]),
    "misplaced annotations": lambda: normalize_citations(
        ("x [[1]](https://example.com/p)") * 4500,
        sources_from_annotations([{"type": "url_citation", "url": "https://example.com/p", "title": "1", "start_index": 10**7 + i, "end_index": 10**7 + i + 1} for i in range(4500)])),
}


@pytest.mark.parametrize("name", list(HOSTILE))
def test_hostile_answers_parse_in_linear_time(name):
    started = time.perf_counter()
    HOSTILE[name]()
    assert time.perf_counter() - started < 2.0, name


def test_relocation_still_prefers_the_matching_label():
    u = "https://example.test/u"
    text = f"a[[1]]({u}) b[[2]]({u}) c[[1]]({u})"
    sources = [{"url": u, "citations": [{"label": "1", "start_index": 900, "end_index": 901}, {"label": "1", "start_index": 950, "end_index": 951}, {"label": "3", "start_index": 990, "end_index": 991}]}]
    _, out, report = normalize_citations(text, sources)
    markers = find_markers(text)
    spans = [(c["start_index"], c["end_index"]) for c in out[0]["citations"]]
    assert spans == [(markers[0].start, markers[0].end), (markers[2].start, markers[2].end), (markers[1].start, markers[1].end)]
    assert report["relocated"] == 3


@pytest.mark.parametrize("line", [
    "Sources", "## Sources", "**Sources:**", "**Sources**:", "Sources (official)  :  ", "## 参考资料：",
    "### **References** ", "__来源__", "Citations\t:\t**", "  #  ** 引用 ** （节选） ： **  ",
])
def test_heading_forms_still_recognised(line):
    text = "Answer body.\n\n" + line + "\n- https://example.com/a\n"
    answer, sources = split_answer_and_sources(text)
    assert answer == "Answer body." and [s["url"] for s in sources] == ["https://example.com/a"]


def test_heading_with_trailing_text_is_not_a_heading():
    answer, _ = split_answer_and_sources("Answer.\n\nSources" + " " * 2000 + "x\n- https://example.com/a\n")
    assert answer.startswith("Answer.\n\nSources")
