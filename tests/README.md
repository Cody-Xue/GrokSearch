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

Citation offsets are passed through in the upstream API's indexing convention.
When native citations exist, `web_search.content` preserves the original answer,
including whitespace and any sources section, so removing text does not shift
those offsets. Existing text-source extraction remains the fallback for APIs
without annotations. Native sources, text sources, and optional extra search
results are merged by URL, with native citation metadata taking precedence.
