# Native citation regression tests

Run from the repository root:

```sh
uv run --extra dev pytest -q
```

The tests use mocked HTTP responses and an in-process MCP client; no API keys or
network requests are needed. `fixtures/grok_annotations.json` contains 13 actual
URL annotations captured from a Python 3.14 search, covering 7 distinct URLs.
The original answer and credentials are not included. SSE envelopes are rebuilt
around these captured annotations during testing.

`web_search` collects both `choices[0].delta.annotations` and non-streaming
`choices[0].message.annotations`. `get_sources` keeps one entry per URL. Native
entries use `provider: "grok"` and retain repeated citation locations in a
`citations` list. Numeric upstream titles become `label`; document titles stay
in `title`. Both flat and nested `url_citation` shapes are accepted.

When native citations exist, `web_search.content` keeps the original answer (whitespace and any
sources section included) except that adjacent repeated citation markers are collapsed;
every offset is then checked against that content. Offsets that point exactly at a
`[[n]](url)` marker for the same URL (or at exactly one Markdown link to it) are kept and shifted
if markers were collapsed; others are relocated to the next unclaimed marker for that URL
or lose their coordinates. The upstream values stay in `upstream_start_index`/`upstream_end_index`
and `position` says `relocated` or `removed`. Existing text-source extraction remains the fallback for APIs
without annotations. Native sources, text sources, and optional extra search
results are merged by URL, with native citation metadata taking precedence.
