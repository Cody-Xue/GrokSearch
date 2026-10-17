![Image](../images/title.png)
<div align="center">

<!-- # Grok Search MCP -->

English | [简体中文](../README.md)

**Grok-with-Tavily MCP, providing enhanced web access for Claude Code**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT) [![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/) [![FastMCP](https://img.shields.io/badge/FastMCP-2.11.0+-green.svg)](https://github.com/jlowin/fastmcp)

</div>

---

## 1. Overview

Grok Search MCP is an MCP server built on [FastMCP](https://github.com/jlowin/fastmcp), featuring a **dual-engine architecture**: **Grok** handles AI-driven intelligent search, while **Tavily** handles high-fidelity web content extraction and site mapping. Together they provide complete real-time web access for LLM clients such as Claude Code and Cherry Studio.

```
Claude --MCP--> Grok Search Server
                  ├─ web_search  ---> Grok API (AI Search)
                  ├─ web_fetch   ---> direct download (text/code files) -> Tavily Extract -> Firecrawl Scrape (extraction with fallback)
                  └─ web_map     ---> Tavily Map (Site Mapping)
```

### Features

- **Dual Engine**: Grok search + Tavily extraction/mapping, complementary collaboration
- **Firecrawl fallback**: when Tavily extraction fails, web_fetch falls back to Firecrawl Scrape, which is retried automatically when it returns empty content
- **OpenAI-compatible interface**, supports any Grok mirror endpoint
- **Automatic time injection** (local date, time and an ASCII time zone are prepended to every search: the UTC offset such as `UTC+08:00`, plus the IANA name when it can be determined)
- **Read-only tool annotations**: `web_search`/`web_fetch`/`web_map`/`get_sources`/`get_config_info` declare `readOnlyHint`, so Claude Code runs several calls from one turn in parallel instead of one after another
- **Concurrency cap + per-model circuit breaker**: `GROK_MAX_CONCURRENCY` bounds in-flight Grok requests; repeated 429s or an exhausted account pool open a breaker for that model (thinking-level suffixes such as `(high)`/`(low)`/`(8192)` share one breaker) and return a structured error, with half-open probing to recover. CLIProxyAPI / xAI pool-exhaustion answers (503 `auth_unavailable`, 429 `model_cooldown`, `free-usage-exhausted`, ...) open the breaker on the first call; calls queued for a concurrency slot check the breaker again once they get one
- **Failures are never silent**: in-stream error frames, HTTP 429, empty responses, timeouts, network failures and streams that close mid-answer become `error`/`error_type`/`retry_after_s` fields instead of an empty answer; answers the upstream cut short or filtered are flagged `incomplete`. By design, tool failures are returned as normal results (MCP `isError=false`): check the `error` field or the first line
- **Bounded search time**: `GROK_READ_TIMEOUT_S` (per read) and `GROK_SEARCH_TIMEOUT_S` (whole call, including queueing, retries and waits) make a search end with `timeout` even when the gateway stalls
- **Two upstream APIs**: `GROK_API_MODE=chat` (default, `/chat/completions`) or `responses` (`/responses`, the Responses API). CLIProxyAPI sends nothing on the chat endpoint until the whole answer is generated, while the Responses endpoint streams events as they happen (headers within 2-3 s in tests), so long multi-agent searches are no longer exposed to the read timeout and Cloudflare's 100 s limit; xAI also reports the cost of each call (`usage.cost_usd`) and its search count, and the result lists the queries Grok searched with (`search_queries`)
- **Per-call models**: `web_search(model=...)` accepts CPA-style thinking suffixes (e.g. `grok-4.7(high)`, `grok-4.7(8192)`), validates the base name against `/models` and sends the full name; the model list is cached with a TTL and failures are not cached; non-chat models (TTS/voice, image, embedding, ...) are rejected locally by name, and suggestions list chat models only (same family first)
- **Dual-engine extra sources**: `extra_sources` is split between Tavily and Firecrawl, deduplicated against Grok's citations and across engines, and a failed engine is backfilled by the other (`extra_sources_report`); with `platform` set, the engines search only that platform's domains (Tavily `include_domains`, Firecrawl `site:`) and off-platform hits are dropped
- **Verification layer (not fact-checking)**: arXiv IDs and DOIs are confirmed to exist via the arXiv API, Crossref, the doi.org handle registry and DataCite (this covers arXiv's own `10.48550/arXiv.<id>` DOIs, Zenodo, and DOIs of other agencies such as Wanfang/ISTIC), URLs Grok cited are confirmed reachable, and paper titles written in the answer are compared with the registry title (`title_checks`). It does **not** check whether a source supports the claim (`claim_support: not_checked`). `unresolved` means the registry or site said it does not exist (treat as suspect); `lookup_failed` means no answer was obtained (rate limit, 5xx, timeout, a bot check or login wall such as Reddit's "Prove your humanity") and is not evidence against the citation; anything not checked is listed in `skipped` with a reason. Only sources Grok cited are checked, not `extra_sources` hits. Cited URLs are fetched only when their host resolves to public addresses; behind a Clash/mihomo fake-ip resolver the real address is looked up via DNS-over-HTTPS first (limits: see `web_search`). Compact output by default (`GROK_VERIFY_DETAIL=compact`): only entries that need attention are listed, all counts are kept
- **Verbatim text files, fresh content and snapshot paging**: raw.githubusercontent.com, gist, GitHub/GitLab/Codeberg/Bitbucket file pages and URLs with a code/config/text extension are downloaded directly on your machine and returned byte for byte (`source=direct`; public addresses only, at most 5 MB, no API key sent); Firecrawl is asked for a fresh copy by default (`maxAge=0`) and the header shows the backend's cache state (`cache=`); for ordinary pages a short Tavily extract or a bot-check page falls back to Firecrawl; arXiv abstract pages use the arXiv API (a versioned link returns that version), or DataCite's record when the API is down; long pages return a `snapshot_id` and every later page is cut from the same snapshot; CJK-heavy pages default to 20,000 characters per page; a page that itself answers HTTP >= 400 counts as failed (`page_status=`), a Firecrawl redirect shows `final_url=`/`title=` in the header, and login walls / anti-bot pages are flagged `warning=interstitial`; non-http(s) URLs and local/LAN IP literals are refused locally (`error=invalid_url`) without calling a paid backend; failures list each backend with error category, HTTP status and a redacted detail
- **Output style and citation rules**: `concise` by default, `explanatory` adds term definitions and analogies. Both styles cite only statements a source makes, label inferences ("Inference:"), never cite self-written explanations or analogies, report absence as "not found in this search", and quote numbers verbatim without merging figures across sources
- One-click disable Claude Code's built-in WebSearch/WebFetch, force routing to this tool
- Smart retry (Retry-After honoured on every status + exponential backoff; deterministic errors are not retried; Cloudflare 520/521/523/530 are retried once, 522/524 are treated as timeouts)
- Parent process monitoring (auto-detects parent process exit on Windows, prevents zombie processes)

### Demo

Using `cherry studio` with this MCP configured, here's how `claude-opus-4.6` leverages this project for external knowledge retrieval, reducing hallucination rates.

![](../images/wogrok.png)
As shown above, **for a fair experiment, we enabled Claude's built-in search tools**, yet Opus 4.6 still relied on its internal knowledge without consulting FastAPI's official documentation for the latest examples.

![](../images/wgrok.png)
As shown above, with `grok-search MCP` enabled under the same experimental conditions, Opus 4.6 proactively made multiple search calls to **retrieve official documentation, producing more reliable answers.**


## 2. Installation

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (recommended Python package manager)
- Claude Code

<details>
<summary><b>Install uv</b></summary>

```bash
# Linux/macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

> Windows users are **strongly recommended** to run this project in WSL.

</details>

### One-Click Install

If you have previously installed this project, remove the old MCP first:
```
claude mcp remove grok-search
```

Replace the environment variables in the following command with your own values. The Grok endpoint must be OpenAI-compatible; Tavily and Firecrawl are optional: `web_fetch` needs at least one of them and `web_map` needs Tavily.

```bash
claude mcp add-json grok-search --scope user '{
  "type": "stdio",
  "command": "uvx",
  "args": [
    "--from",
    "git+https://github.com/Cody-Xue/GrokSearch@main",
    "grok-search"
  ],
  "env": {
    "GROK_API_URL": "https://your-api-endpoint.com/v1",
    "GROK_API_KEY": "your-grok-api-key",
    "TAVILY_API_KEY": "tvly-your-tavily-key",
    "TAVILY_API_URL": "https://api.tavily.com"
  }
}'
```

> **Pin a version (recommended)**: `@main` is a branch, so uvx resolves it against GitHub on every start: you always run the latest main and get fixes first, but each start takes a few seconds longer, the server cannot start when GitHub is unreachable, and changes you have not reviewed (including incompatible ones) take effect on the next start. For everyday use, pin a release tag or commit: replace `git+https://github.com/Cody-Xue/GrokSearch@main` above with `git+https://github.com/Cody-Xue/GrokSearch@v1.13.0` (use a tag that exists in the repository; until one is published, use `@<commit SHA>`). Every run then uses the same code, is reproducible and, once cached, starts without GitHub; the trade-off is that you upgrade by editing the version. Both forms work; the examples below keep `@main`.

<details> <summary>If you hit SSL / certificate verification errors</summary>

In some corporate networks or proxy environments you may see errors such as:

certificate verify failed
self signed certificate in certificate chain

Add `--native-tls` to the uvx arguments so that uv uses the system certificate store:

```bash
claude mcp add-json grok-search --scope user '{
  "type": "stdio",
  "command": "uvx",
  "args": [
    "--native-tls",
    "--from",
    "git+https://github.com/Cody-Xue/GrokSearch@main",
    "grok-search"
  ],
  "env": {
    "GROK_API_URL": "https://your-api-endpoint.com/v1",
    "GROK_API_KEY": "your-grok-api-key",
    "TAVILY_API_KEY": "tvly-your-tavily-key",
    "TAVILY_API_URL": "https://api.tavily.com"
  }
}'
```

</details>

You can also configure additional environment variables in the `env` field:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GROK_API_URL` | Yes | - | Grok API endpoint (OpenAI-compatible). Surrounding whitespace and a trailing `/` are ignored (`https://host/v1/` works) |
| `GROK_API_KEY` | Yes | - | Grok API key; surrounding whitespace is ignored |
| `GROK_MODEL` | No | `grok-4-fast` | Default model (takes precedence over `~/.config/grok-search/config.json` and `switch_model` when set; a blank value counts as unset). Cost note: multi-agent / high-reasoning tiers (e.g. `grok-4.20-multi-agent-0309(high)`) were observed to use 0.5-1.6M prompt tokens per search; for routine lookups use a lighter model such as `grok-4.7` (about 19k-133k per search in this release's live tests), set here or per call with `web_search(model=...)` |
| `TAVILY_API_KEY` | No | - | Tavily API key (for web_fetch / web_map / extra_sources) |
| `TAVILY_API_URL` | No | `https://api.tavily.com` | Tavily API endpoint |
| `TAVILY_ENABLED` | No | `true` | Enable Tavily; with `false` neither `web_fetch`, `web_map` nor `extra_sources` calls Tavily, even when a key is set |
| `FIRECRAWL_API_KEY` | No | - | Firecrawl API key (fallback when Tavily fails) |
| `FIRECRAWL_API_URL` | No | `https://api.firecrawl.dev/v2` | Firecrawl API endpoint |
| `GROK_DEBUG` | No | `false` | Debug mode (logs full prompts and answers). Progress messages (e.g. `Begin Fetch`, `Fetch Finished`) are written to the log and sent to the client only when it is on; warnings and errors are always written and sent; a failing client notification (e.g. a closed connection) never breaks a tool |
| `GROK_LOG_LEVEL` | No | `INFO` | Log level |
| `GROK_LOG_DIR` | No | `logs` | Log directory; a relative path is under `~/.config/grok-search`. If it is unusable, logs go to a private temp directory, never the working directory. One file per day (`grok_search_YYYYMMDD.log`); a running server switches files after midnight |
| `GROK_LOG_RETENTION_DAYS` | No | `7` | Number of daily log files kept (today included); older ones are deleted at start-up and at each day change |
| `GROK_RETRY_MAX_ATTEMPTS` | No | `3` | Max retry attempts |
| `GROK_RETRY_MULTIPLIER` | No | `1` | Retry backoff multiplier |
| `GROK_RETRY_MAX_WAIT` | No | `10` | Max retry wait in seconds |
| `GROK_RETRY_BUDGET_S` | No | `45` | Total retry-wait budget per call (seconds); a Retry-After beyond the budget fails fast. `0` disables. Counts only the waits between retries; the whole call is bounded by `GROK_SEARCH_TIMEOUT_S` |
| `GROK_API_MODE` | No | `chat` | Upstream API: `chat` calls `/chat/completions`; `responses` calls `/responses` (the Responses API: streamed events, `store: false`; supported by xAI and CLIProxyAPI, not necessarily by other gateways). Thinking suffixes such as `(high)` become the reasoning effort on both APIs (the gateway converts them). An unknown value falls back to `chat` and is listed in `config_warnings` |
| `GROK_READ_TIMEOUT_S` | No | `180` | Longest silence (per read, seconds, minimum 5) while waiting for Grok. Buffering gateways such as CLIProxyAPI send nothing until the answer is complete, so this also caps one attempt. A read timeout is retried at most once, and only if another full wait fits `GROK_SEARCH_TIMEOUT_S` |
| `GROK_SEARCH_TIMEOUT_S` | No | `300` | Wall-clock limit (seconds) for one `web_search` Grok call: waiting for a concurrency slot, every attempt and every retry wait. `0` disables it. When exceeded, `error_type` is `timeout` |
| `GROK_MAX_CONCURRENCY` | No | `16` | Maximum in-flight Grok requests |
| `GROK_BREAKER_THRESHOLD` | No | `3` | 429s within the window that open the breaker |
| `GROK_BREAKER_WINDOW_S` | No | `60` | Breaker counting window (seconds) |
| `GROK_BREAKER_COOLDOWN_S` | No | `60` | Initial cooldown (seconds), doubled after a failed probe |
| `GROK_BREAKER_MAX_COOLDOWN_S` | No | `300` | Cooldown ceiling (seconds) |
| `GROK_SEARCH_STYLE` | No | `concise` | Output style: `concise` or `explanatory` (adds term definitions and uncited analogies) |
| `GROK_MODELS_CACHE_TTL_S` | No | `600` | Lifetime (seconds) of the cached `/models` list used to validate `web_search(model=...)`; failures are not cached and `get_config_info` refreshes it |
| `GROK_VERIFY_IDS` | No | `true` | Resolve arXiv IDs and DOIs found in the answer (identifier lookups only; URL checks follow `GROK_VERIFY_URLS`; with both `false` no verification block is attached) |
| `GROK_VERIFY_URLS` | No | `true` | Check reachability and title of the URLs Grok cited, independent of `GROK_VERIFY_IDS` (hosts are resolved first and non-public addresses are not fetched; limits: see `web_search`) |
| `GROK_VERIFY_TIMEOUT_S` | No | `30` | Overall verification timeout (seconds); lookups still pending at the deadline are listed under `lookup_failed` (reason `timed out`), and the last 5 s (at most 20%) of the budget is kept for the DataCite fallback of arXiv IDs. All arXiv API calls (`web_fetch` included) share one gate: one connection at a time, each request starting 3 s after the previous one finished; a 429/503 starts a process-wide cooldown (Retry-After, else 60 s) during which arXiv lookups fail fast into `lookup_failed`; concurrent lookups of the same ID share one request; 10 s read timeout. Verification lookups and `web_fetch` abstract pages are cached for an hour, and a paper fetched with `web_fetch` is not looked up again when it is verified (the verification cache holds no abstract, so a `web_fetch` after a verification still makes one request) |
| `GROK_VERIFY_MAILTO` | No | empty | Contact e-mail sent only to the registries (arXiv / Crossref / doi.org / DataCite), never to cited sites or the DoH service. It joins the Crossref polite pool: up to 3 concurrent Crossref requests per process instead of 1. Strongly recommended |
| `GROK_VERIFY_FAKE_IP` | No | `doh` | What to do when the local resolver returns only fake-ip addresses (198.18.0.0/15): `doh` = resolve the name via DNS-over-HTTPS through the configured proxy and fetch through the proxy only if all real addresses are public; `skip` = do not fetch (listed in `skipped`); `allow` = fetch through the proxy without the check |
| `GROK_VERIFY_DOH_URL` | No | `https://cloudflare-dns.com/dns-query` | DoH JSON endpoint used by `GROK_VERIFY_FAKE_IP=doh` (Cloudflare/Google JSON format, e.g. `https://dns.google/resolve`), reached through the configured proxy; `web_fetch`'s direct download uses both settings too |
| `GROK_VERIFY_DETAIL` | No | `compact` | Detail of the verification block: `compact` lists only what needs attention (every `unresolved` and `lookup_failed` entry, `title_checks` other than `match`, `urls_unusual`, the first 10 `skipped` entries plus `skipped_omitted`), keeps every count in `checked`/`summary` and uses a one-sentence scope; `full` also lists every resolved arXiv/DOI record, every reachable URL with status and title, matching title checks and the full scope text (the v1.12.0 output). An unrecognised value falls back to `compact` and is listed in `config_warnings` |
| `GROK_PLANNING_TOOLS` | No | `false` | Register the six `plan_*` planning tools |
| `GROK_FETCH_MIN_CHARS` | No | `2000` | Fall back to Firecrawl when the Tavily extract of an ordinary web page is shorter than this. Not applied to direct downloads, code-file fallbacks or arXiv abstract fallbacks (they can legitimately be short) |
| `GROK_FETCH_MAX_CHARS` | No | `40000` | Default characters returned per `web_fetch` call |
| `GROK_FETCH_MAX_CHARS_CJK` | No | `20000` | Default `web_fetch` page size when more than 30% of the page is CJK text (CJK is about one token per character), never above `GROK_FETCH_MAX_CHARS`. It keeps a page under the ~25k-token MCP result limit of Claude Code |
| `GROK_FETCH_ARXIV_BUDGET_S` | No | `8` | Overall time limit (seconds) for `web_fetch`'s arXiv API path, covering the 3 s gate and one retry. After a rate-limit, timeout, network or 5xx failure the arXiv API is skipped for 2 minutes (`attempts=arxiv-api:cooldown`). When the arXiv API fails, the abstract page is built from DataCite's record first (free), then Firecrawl and Tavily are tried |
| `GROK_FETCH_SNAPSHOT_TTL_S` | No | `900` | Idle lifetime (seconds) of a `web_fetch` paging snapshot; afterwards paging restarts at `offset=0` |
| `GROK_FETCH_DIRECT` | No | `true` | Download text/code files (raw, gist, file pages, code/config/text extensions) directly from the origin on your machine and return them verbatim. The origin sees your IP; with `false` Firecrawl (first) and Tavily read the raw URL instead, and damaged copies are flagged |
| `GROK_FETCH_MAX_AGE_S` | No | `0` | Oldest Firecrawl cache entry accepted (seconds, sent as `maxAge`). `0` always scrapes fresh; without it Firecrawl serves cached copies up to 2 days old. A cache hit costs the same 1 credit as a fresh scrape ([docs](https://docs.firecrawl.dev/features/fast-scraping)) |
| `GROK_MAP_REGEX_TIMEOUT_S` | No | `2` | Time limit (seconds, 0.5-60, waiting for a free matcher included) of `web_map`'s local `select_domains` matching, which runs in a separate process that is killed when the limit is reached; the results then come back unfiltered with a `scope_note` |
| `TAVILY_EXTRACT_TIMEOUT_S` | No | `30` | Tavily extract timeout (seconds) |

Booleans accept `true`/`1`/`yes`/`on` and `false`/`0`/`no`/`off` (any case). A blank value keeps the default; an unrecognised value (including an unparseable number) also keeps the default and is listed in `config_warnings` of `get_config_info`. For example `TAVILY_ENABLED=disabled` keeps Tavily on; use `false`.



### Verify Installation

```bash
claude mcp list
```

After confirming a successful connection, we **highly recommend** typing the following in a Claude conversation:
```
Call grok-search toggle_builtin_tools to disable Claude Code's built-in WebSearch and WebFetch tools
```
This (`action=on`) will automatically modify the **project-level** `.claude/settings.json` `permissions.deny`, disabling Claude Code's built-in WebSearch and WebFetch, forcing Claude Code to use this project for searches!



## 3. MCP Tools

<details>
<summary>Seven MCP tools by default; `GROK_PLANNING_TOOLS=true` adds six `plan_*` planning tools (click to expand)</summary>

By design, every tool returns its failures as normal results (MCP `isError=false`), so the model can read the structured error fields and any partial results already obtained: for `web_search` check the `error`/`error_type` fields and the `[搜索失败]` prefix, for `web_fetch`/`web_map` the `error=` in the first line.

### `web_search` — AI Web Search

Executes AI-driven web search via Grok API. By default it returns only Grok's answer and a `session_id` for retrieving sources later.

`web_search` does not expand sources in the response; it only returns counts. Sources are cached server-side by `session_id` and can be fetched with `get_sources`. `sources_count` / `distinct_domains` count only the sources Grok cited (not `extra_sources`); `extra_sources_count` counts the extra hits and `total_sources_count` is the sum (the length of the `get_sources` list).

`instructions` adds per-call requirements for the searcher (e.g. "cover at least 15 distinct domains" or "list arXiv IDs with titles only"). On failure the result carries `error`/`error_type`/`retry_after_s` (plus `status` and `exhausted` when applicable) and `content` always starts with `[搜索失败]`; extra-source hits found before the failure are still listed after it, and `model_check` is returned when `model` was given.

- **Citation positions**: with native citations, adjacent repeated markers (e.g. `[[1]](u)[[1]](u)`) are collapsed and the `start_index`/`end_index` returned by `get_sources` always refer to the content that is returned, counted in Unicode code points (not UTF-16 units). Offsets that do not match are relocated (`position: relocated`) or lose their coordinates (`position: removed`); the upstream values are kept in `upstream_start_index`/`upstream_end_index` and summarized in `citation_positions`.
- **Upstream errors and retries**:
  - `error_type` is one of: `bad_request` (the query is empty or only whitespace / invisible / control characters: refused locally, `content` starts with `[搜索失败] 查询为空…`, and nothing is sent to Grok, `/models` or the extra-source engines, so it costs nothing), `config` (`GROK_API_URL`/`GROK_API_KEY` missing or invalid), `proxy_config` (broken local proxy environment), `invalid_model`, `rate_limit` (`exhausted: true` means this model's account pool is blocked: wait `retry_after_s` or switch model), `circuit_open`, `timeout` (no complete answer within `GROK_SEARCH_TIMEOUT_S`, a read timeout, or Cloudflare 522/524), `network` (connection, proxy or TLS failure; the message is never empty), `truncated_stream` (the stream closed without `finish_reason` or `[DONE]`; it is retried), `empty_response`, `auth` (the gateway has no usable credential for the model, e.g. CPA `auth_not_found`; not retried), `http_<status>`, `unknown`, or an error code passed through from the upstream stream (e.g. `invalid_request_error`, `model_not_found`, `server_error`, `upstream_error`).
  - `rate_limit` with `exhausted: true` also covers CLIProxyAPI 503 `auth_unavailable` / "no auth available", 429 `model_cooldown` / "cooling down" and xAI `free-usage-exhausted`, and opens the breaker on the first call. `retry_after_s` comes from Retry-After on any status, or from CPA `reset_seconds` in error frames.
  - An exhausted pool that announces a recovery time within `GROK_RETRY_BUDGET_S` and the overall limit is waited out once; otherwise the call fails fast with `retry_after_s` and is not retried blindly. Deterministic errors (`retryable: false`, `authentication_error`, `auth_not_found`) are not retried.
  - Cloudflare: 520/521/523/530 are retried once; 522/524 are reported as `timeout`, and 524 is never re-run (the search already ran ~100 s; narrow the query or use a lighter model). Cloudflare pages are named, not quoted.
  - Circuit breaker: calls waiting for a `GROK_MAX_CONCURRENCY` slot re-check the breaker once they get one; only the half-open probe call can release the probe slot.
- **`verification`**: checks the arXiv IDs / DOIs in the answer and the URLs Grok cited (`extra_sources` hits are not checked) for existence and reachability only, and whether a paper title written next to an identifier matches the registry title (`title_checks`: `match` / `partial` / `possible_mismatch` / `not_stated`). It does **not** check whether a source supports the statement it is attached to (`claim_support` is always `not_checked`).
  - arXiv IDs include old-style IDs (e.g. `hep-th/9711200`) and `10.48550/arXiv.<id>` DOIs. DOIs go to Crossref first; on a Crossref 404 the doi.org handle registry decides whether the DOI exists and DataCite supplies the title.
  - `unresolved` (treat as suspect): the registry said not found, the page answered 404/410 or another 4xx, or the domain does not exist (NXDOMAIN). 401/403/451 carry the note "access denied to automated requests; the page may still exist".
  - `lookup_failed` (unknown, not evidence against the citation): 429, 5xx, timeouts, network errors, registry cooldowns, bot challenges, the verification time limit; reasons include the HTTP status. `summary` also counts `lookup_failed`. Bot checks and login walls answered with 2xx/3xx (Reddit's JS challenge "Prove your humanity", PyPI's Fastly "Client Challenge", Baidu 安全验证, redirects to `/login`) are `lookup_failed` too, with the reason `bot check / interstitial (page may exist)`, and are never counted as reachable; the detector is shared with `web_fetch`.
  - An unresolved DOI under the 10.5555 prefix (Crossref's test prefix, shown by the ACM DL for unregistered items) stays in `unresolved`, with a hint to check the paper by its title.
  - `skipped`: what was not checked and why (over the limit, URL checks disabled, identifier checks disabled (`GROK_VERIFY_IDS=false`), non-public address, non-http link, arxiv.org/doi.org URLs without a recognisable identifier, DOI-shaped text over 400 characters or with `.`/`..` path segments; DOI templates written in the answer, such as `10.48550/arXiv.<id>` or `10.1145/3XXXXXX.3XXXXXX`, come last, at most 20 of them).
  - `title_checks` only use a title written for this identifier: the nearest explicit title, never another paper's; the previous line counts when the identifier starts its line, also when it is written as a Markdown link after its label (e.g. `- arXiv: [1706.03762](https://arxiv.org/abs/1706.03762)`); author-year text, venue names, labels ending in `:` and spans under 3 words are not titles.
  - **Compact output (default)**: with `GROK_VERIFY_DETAIL=compact` the block carries `detail: "compact"` and a one-sentence `scope` of 112 characters (`claim_support` stays `not_checked`; the lists are explained in this section), and lists only what needs attention: every `unresolved` and `lookup_failed` entry, `title_checks` other than `match` (`partial` / `possible_mismatch` / `not_stated`), `urls_unusual` (reachable URLs whose final status is not 200, e.g. a 202/203 bot check; only when there are some), the first 10 `skipped` entries (`skipped_omitted` counts the rest) and `timed_out` when set. `checked` and `summary` keep every count (`urls_reachable`, `identifiers_found`, `title_match`, ...). In compact mode the `arxiv`, `doi` and `urls` keys are absent, not empty. `GROK_VERIFY_DETAIL=full` restores the v1.12.0 output. On 3 live searches the verification blocks went from 18,756 to 7,352 characters in total (-61%) and the whole `web_search` results from 29,021 to 17,617 (-39%); `lookup_failed` is never capped, so an answer citing many sites that refuse scripts (such as Reddit) still gives a sizeable block.
- **Which URLs are fetched**: before the request and before every redirect the host is resolved with the system resolver (hosts file and LAN names included); if any address is not public the URL is not fetched and goes to `skipped`. This covers loopback, private, link-local and CGNAT (100.64.0.0/10) addresses, numeric forms such as `127.1`, `2130706433` or `0x7f.0.0.1`, and non-public IPv4 addresses inside IPv4-mapped, 6to4 and NAT64 addresses. With a Clash/mihomo fake-ip resolver every name resolves to 198.18.0.0/15: with `GROK_VERIFY_FAKE_IP=doh` (default) the real addresses come from DNS-over-HTTPS through the configured proxy, the same all-public rule applies, and the page is fetched through the proxy; `skip` and `allow` are described in the env table. Redirects are followed hop by hop and redirect bodies are never read; at most 64 KB of decoded body is read (gzip/deflate inflated with a limit, other encodings not decoded). Proxy 502/503/504 answers go to `lookup_failed` (from the proxy or the site). Names that do not resolve locally are listed as `unresolved` (`dns lookup failed`). Limits: the check and the connection resolve the name separately, so DNS rebinding is not prevented; when an HTTP/SOCKS proxy carries the request the proxy decides the real destination (and may use different, e.g. split-horizon, DNS); DoH reveals cited host names to the DoH provider. Registry (arXiv / Crossref / doi.org / DataCite) and DoH lookups go to fixed hosts and are not subject to this check.
- **`model`**: CPA thinking suffixes are accepted, e.g. `grok-4.7(high)`, `grok-4.7(8192)`, `grok-4.7(auto)`. The default model (with its suffix) and other levels of the same base model pass directly; other names are validated by their base name against `/models` and sent in full. If the list cannot be fetched the name is not blocked (`model_check.status = unverified`). With an OpenRouter URL a per-call model also gets `:online`, and an explicit `model:online` is accepted.
  - Non-chat models are recognised by name (whole name parts such as tts, voice, audio, speech, whisper, transcribe, image, imagine, video, embed/embedding, moderation, realtime, rerank) and rejected locally as `invalid_model` with `model_check.reason` `not_chat_model`; no search request is sent (at most the `/models` list is read for suggestions, and the rejection also applies when `/models` is down). Example: `grok-tts` -> "'grok-tts' is not a chat/search model (audio/speech model, name contains 'tts')".
  - Suggestions list chat models only, same family first, then by name similarity, e.g. `grok-4.8` -> `grok-4.7`. An invalid result's `model_check.reason` is `bad_name`, `unsupported_suffix`, `not_listed` or `not_chat_model`.
  - The configured default (`GROK_MODEL` or the config file) is not blocked; `get_config_info` warns about it in `config_warnings`. This is the way to use a real chat model whose name happens to contain one of those parts.
  - **Cost**: multi-agent / high-reasoning tiers (such as the default `grok-4.20-multi-agent-0309(high)`) were observed to use 0.5-1.6M prompt tokens per search (about 1.59M with `platform=GitHub`); `grok-4.7` used about 19k-133k per search in this release's live tests. For routine lookups use a lighter model: set it as `GROK_MODEL`, or pass `web_search(model="grok-4.7")` per call. `usage.prompt_tokens` shows the actual use of each call; when the upstream reports a cost (xAI; seen on both APIs, always in Responses mode) `usage.cost_usd` gives it in US dollars: in this release's tests about $0.5-0.7 per search for the default model and $0.02-0.05 for `grok-4.7`.
- **`extra_sources`** (0-20, values outside the range are rejected by parameter validation): with both engines active Tavily gets ceil(n/2) and Firecrawl floor(n/2), each asked for 2 spare results; URLs already cited by Grok and repeats across engines are dropped (scheme, `www.`, fragment, trailing slash and tracking parameters ignored) and results are picked round-robin. If an engine fails or comes up short, the other one backfills once (shortfall + a margin of 2 + the duplicates seen, at most 20). The hits are appended as an "Extra sources" section with one plain-text line per item (title, link, engine, a preview of at most 300 characters); details are in `extra_sources_report`.
  - **Per-platform search**: when `platform` is a known name (GitHub, GitLab, Reddit, X/Twitter, Stack Overflow, Hacker News, YouTube, arXiv, Wikipedia, 知乎/Zhihu, Bilibili, 微博, CSDN, 掘金, V2EX, Hugging Face, PyPI, npm, Medium, 小红书, 豆瓣, 微信公众号 and others, about 40) or a host name (e.g. `docs.python.org`), the engines search only the platform's main domains: Tavily with `include_domains`, Firecrawl with `site:` appended to the query ([docs](https://docs.firecrawl.dev/api-reference/endpoint/search)). Results are then filtered by host suffix (`githubhelp.com` is not GitHub); off-platform hits are dropped and counted in `off_platform_skipped`, and the backfill asks for that many more, so fewer than requested may come back. Short-link and content hosts (githubusercontent.com, redd.it, youtu.be, b23.tv, weibo.cn, hf.co) are accepted by the local filter only and not sent to the engines (live, Firecrawl leaked off-platform hits for `site:github.com OR site:githubusercontent.com`). Several platforms are comma-separated (e.g. `"GitHub, Reddit"`); extra words next to a known name (e.g. `GitHub issues`) are added to the engine query. An unknown platform is not filtered; its text is added to the engine query. `extra_sources_report.platform` records `requested`, `applied` (`domain_filter` / `query_hint` / `none`), `domains`, `also_accepted`, `engines`, `off_platform_skipped`, `query_hint` and `unresolved`. Live with `platform="GitHub"`, `extra_sources=6`: all 6 on github.com.
- **"Not found"**: "not found in this search" only means this search did not find it. When a definite answer matters, check the platform directly (for example `web_fetch` the repository's issue search page).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | Yes | - | Search query |
| `platform` | string | No | `""` | Focus platform (e.g., `"Twitter"`, `"GitHub, Reddit"`); Grok gets a "focus on" hint, and a known platform or host name also restricts the `extra_sources` domains (see above); whitespace only counts as unset |
| `model` | string | No | `""` | Per-request Grok model ID, thinking suffix allowed; non-chat models are rejected locally |
| `extra_sources` | int | No | `0` | Extra sources via Tavily/Firecrawl (0-20, 0 disables; counted in `extra_sources_count`, not in `sources_count`) |
| `instructions` | string | No | `""` | Per-call requirements for the searcher |
| `verify` | bool | No | `true` | Attach the verification layer; identifier lookups follow `GROK_VERIFY_IDS` and URL checks `GROK_VERIFY_URLS`; both off = no verification; the detail follows `GROK_VERIFY_DETAIL` (default `compact`) |

Local date, time and timezone context is prepended to every search to improve time-sensitive queries. The time zone is written as an ASCII UTC offset (e.g. `UTC+08:00`), followed by the IANA name when one can be determined from `TZ`, `/etc/localtime`, `/etc/timezone` or the zone object and matches the current offset (e.g. `UTC+08:00 (Asia/Shanghai)`). Localized zone names are no longer used: on Chinese Windows they used to arrive as `Timezone: ??????`, and an undecodable name made the whole search fail.

Return value (structured dict):
- `session_id`: search session ID
- `content`: the answer. Without native citations the full text is kept and only a pure link list under a sources heading is removed; with native citations the original text is kept and only repeated markers are collapsed. When the answer was cut short a one-line `[回答不完整] …` note follows it, before the "Extra sources" section
- `sources_count` / `distinct_domains`: sources Grok cited / distinct sites among them (without native citations: the markers, Markdown links and URLs written in the answer)
- `extra_sources_count` / `total_sources_count`: extra-source hits / cited + extra
- `incomplete` / `finish_reason` / `native_finish_reason`: present only when the upstream cut the answer short (`finish_reason` `length` or `content_filter`, or `native_finish_reason` other than `stop`)
- `usage`: `{prompt_tokens, completion_tokens, reasoning_tokens}` when the upstream reports token usage, plus `cached_tokens`, `cost_usd` (what this call was billed, in US dollars) and `web_search_calls` / `x_search_calls` (searches Grok ran itself) when the upstream provides them (multi-agent models can reach 0.5-1.6M prompt tokens per search; see "Cost" above)
- `search_queries`: in Responses mode, the queries Grok searched with (at most 10); useful to judge whether a "not found in this search" used the right terms
- `dropped_events`: present only when SSE frames could not be decoded
- `error` / `error_type` / `retry_after_s` / `status` / `exhausted`: failures only, see above
- `citation_positions`, `verification`, `extra_sources_report`, `model_check`: see above, present when applicable

### `get_sources` — Retrieve Sources

Retrieves the full cached source list for a previous `web_search` call.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session_id` | string | Yes | `session_id` returned by `web_search` |

Return value (structured dict):
- `session_id`
- `sources_count`: the whole list (`web_search`'s `total_sources_count`)
- `cited_sources_count` / `extra_sources_count`: how many of them Grok cited / came from `extra_sources`
- `sources`: the sources Grok cited (`provider: grok`) first, then the `extra_sources` hits (`provider: tavily` / `firecrawl`), which Grok did not cite. Each item includes `url` and may include `title`/`description`/`provider`/`citations`. `start_index`/`end_index` in `citations` are Unicode code-point offsets into the content returned by `web_search`; an extra source's `description` is a whitespace-collapsed snippet of at most 500 characters ending with `…` when cut (use `web_fetch` for the full page)

### `web_fetch` — Web Content Extraction

Fetches web pages as Markdown and text files verbatim. The backend depends on the URL:

- **Text/code files**: raw.githubusercontent.com, gist.githubusercontent.com, GitHub/GitLab/Codeberg/Bitbucket file pages (e.g. `github.com/<o>/<r>/blob/<ref>/<path>`, read from their raw URL), `/raw/` URLs, and URLs ending in a code/config/text extension (`.py` `.h` `.c` `.rs` `.go` `.js` `.ts` `.toml` `.yaml` `.json` `.md` `.txt` `.csv` `.xml` `.sql` `.lock` ...) are downloaded directly (no double fetch) and returned byte for byte (`source=direct`, with `content_type=` and `status=` in the header, and `final_url=` when a file page was rewritten to its raw URL). Only `text/*` (not HTML) and JSON/XML/YAML/TOML/JS types are accepted. The direct download runs on your machine: the request and every redirect pass the same address check as citation verification (public addresses only; fake-ip handled per `GROK_VERIFY_FAKE_IP`/`GROK_VERIFY_DOH_URL`), at most 5 MB after decompression, 30 s timeout, and no API key is sent. The origin sees your IP; `GROK_FETCH_DIRECT=false` turns it off. When it fails or is off, Firecrawl (first) and Tavily read the raw URL, and visibly damaged code (collapsed newlines, lost indentation, `_` escaped as `\_`, stripped `#include <...>` targets) is marked `warning=degraded_code degraded=<reasons>` and should not be quoted. A URL that answers with HTML is fetched as a web page. A URL that only ends in such an extension (e.g. `bundlephobia.com/package/chart.js`) and could not be downloaded directly (proxy, failure, `GROK_FETCH_DIRECT=false`) is read like a page, Tavily first: a copy shorter than `GROK_FETCH_MIN_CHARS` goes on to Firecrawl (one extra call), whose content type decides whether it is a file (Firecrawl's text/plain copy is then preferred and Tavily's is checked for damage) or a page; a longer copy with few Markdown links is taken as the file and checked for damage. GitHub's raw CDN caches branch URLs for about 5 minutes (`cache-control: max-age=300`), so a branch URL can lag a push; commit-pinned URLs are always exact.
- **arXiv abstract pages**: the arXiv API; a versioned link (e.g. `/abs/1706.03762v1`) returns that version with `version: v1` in the front matter (`vN (latest)` when no version was requested). When the arXiv API is down, the page is built from DataCite's record of `10.48550/arXiv.<id>` (`source=datacite`, free: title, all authors, abstract, categories, comments, first/last submission dates; latest version only, skipped when another version was requested), then Firecrawl and Tavily are tried, with no length threshold and no double fetch.
- **Other pages**: Tavily Extract; Firecrawl Scrape when the extract is missing, shorter than `GROK_FETCH_MIN_CHARS` or a bot check.

**Freshness**: Firecrawl gets `maxAge` from `GROK_FETCH_MAX_AGE_S` (default `0`: always scraped fresh; without it Firecrawl serves cached copies up to 2 days old, see the [docs](https://docs.firecrawl.dev/features/fast-scraping)). `cache=` in the header is the backend's cache state: `hit`/`miss` is Firecrawl's reported `cacheState` (with `cached_at=` on a hit), `bypassed` means `maxAge=0` scraped it now, and `possible` means Tavily, which caches by URL and whose Extract API has no freshness parameter (see the [docs](https://docs.tavily.com/documentation/api-reference/endpoint/extract)), so a recently changed page may be stale. `cached=true` refers to this server's paging snapshot.

The cloud backends cannot reach your machine or LAN, so before any backend call web_fetch refuses such URLs: no http(s) scheme, no host, `localhost`/`*.localhost`, and loopback/private/link-local/CGNAT IP literals. The result is `[web_fetch] error=invalid_url reason=<malformed|missing_scheme|unsupported_scheme|missing_host|local_address>`; host names are not resolved for this check.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `url` | string | Yes | - | Complete public http(s) URL |
| `max_chars` | int | No | `0` | Maximum characters in this call; `0` uses `GROK_FETCH_MAX_CHARS`, or `GROK_FETCH_MAX_CHARS_CJK` when more than 30% of the page is CJK |
| `offset` | int | No | `0` | Character offset; values above 0 require `snapshot_id` |
| `snapshot_id` | string | No | `""` | Snapshot ID from the first page, used to read the following pages |

The first line is a metadata header: `[web_fetch] source=… total_chars=… offset=… returned=… truncated=… [next_offset=… snapshot_id=… expires_in_s=…] [cached=true] [content_type=… status=…] [cache=hit|miss|bypassed|possible cached_at=…] [attempts=…] [warning=interstitial|degraded_code|size_limit] [degraded=…] [final_url=… title="…"]`. More lines may follow it before the blank line: a `注意:` line explains a warning, and a backend that failed while another one answered gets a `- <backend>: <category> …` line. A page longer than one response is stored as a server-side snapshot; call again with the same `url`, `offset=next_offset` and `snapshot_id` and every page is cut from the same text. `offset>0` without `snapshot_id` is refused; once a snapshot expires (900 s idle by default) the tool answers `error=snapshot_expired` and paging restarts at `offset=0`.

`final_url` and `title` appear when Firecrawl was redirected; `title` is always the last field and is JSON-quoted. `warning=interstitial` plus a `注意:` line mean the text is a login wall or anti-bot page (e.g. Zhihu 安全验证, Cloudflare "Just a moment...", Reddit "Prove your humanity"), not the requested page. The detector is shared with the verification layer: the whole title and each side of ` - ` / ` | ` are matched, and challenge markers in the body are checked; ordinary articles about CAPTCHAs are not flagged. Tavily results are checked too (a Tavily bot check makes web_fetch try Firecrawl), and such a page never replaces a real extract.

On failure the first line is `[web_fetch] error=…`, followed by one line per backend with the error category (`auth`, `quota`, `rate_limit`, `timeout`, `proxy_config`, `network`, `upstream_error`, `bad_request`, `unsupported_site`, `blocked`, `not_text`, `extract_failed`, `empty_content`, `http_error`, `disabled`, `not_configured`, ...), the HTTP status and a redacted detail. `unsupported_site` means the provider does not support or may not fetch the site (e.g. Firecrawl's 403 "we do not support this site" for Reddit, Tavily's "URL is not supported"), not a key problem; `auth` is used only for 401 or a 403 that mentions API keys, tokens or credentials, and a 403 without explanation is `http_error`; `blocked` means the direct download's destination is, or resolves to, a non-public address; `not_text` means the target is not a plain-text resource (it was fetched as a web page instead). When the page itself answered HTTP >= 400 (404, 403, 5xx...), that backend counts as failed with detail `target page answered HTTP <status>` and category `extract_failed` (`rate_limit` for 429, `upstream_error` for 5xx), and the first line adds `page_status=<status>`.

### `web_map` — Site Structure Mapping

Traverses website structure via Tavily Map API, discovering URLs and generating a site map.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `url` | string | Yes | - | Starting URL |
| `instructions` | string | No | `""` | Natural language filtering instructions |
| `max_depth` | int | No | `1` | Max traversal depth (1-5) |
| `max_breadth` | int | No | `20` | Max links to follow per page (1-500) |
| `limit` | int | No | `50` | Total link processing limit (1-500) |
| `timeout` | int | No | `150` | Timeout in seconds (10-150) |
| `allow_external` | bool | No | `true` | Include links to other sites (Tavily's default). `false` keeps only URLs on the start URL's host (`www.<host>` counts as the same host); other subdomains such as `app.` or `status.` are removed client-side, because Tavily treats subdomains of the same site as internal. With `select_domains` given, its regexes decide instead |
| `select_paths` | string[] | No | - | Keep only URLs whose path matches these regexes, e.g. `["/docs/.*"]` |
| `select_domains` | string[] | No | - | Crawl only domains/subdomains matching these regexes (Python syntax; at most 20 after removing duplicates, each at most 300 characters); with `allow_external=false` the results are also filtered client-side by host (`re.search`), with `allow_external=true` they only steer the crawl |
| `exclude_paths` | string[] | No | - | Skip URLs whose path matches these regexes |
| `exclude_domains` | string[] | No | - | Skip domains/subdomains matching these regexes |

`web_map` is unavailable when `TAVILY_ENABLED=false`. Few results or a fast answer are not necessarily a fault; use the parameters above and `max_depth` to adjust the scope.

On success the output is JSON `{base_url, results, response_time, scope, filtered_out}`: `scope` is `exact_host` (`allow_external=false`), `select_domains` (`allow_external=false` with `select_domains`) or `unfiltered` (`allow_external=true`, Tavily's list as it is), and `filtered_out` counts the URLs removed client-side. The `select_domains` regexes come from the caller, so they are matched in a separate Python process (`-I -S` with no environment variables, so no API keys or proxy settings; at most 2 at a time, and waiting for one counts toward the limit; the process ends without the server's help too: on Linux/macOS it ends itself at the limit and its CPU time is limited, on Linux its memory is limited to 512 MB and it ends as soon as the server process does, even a killed one; on Windows it runs in a Job Object that limits its memory to 512 MB and ends it with the server) that is killed after `GROK_MAP_REGEX_TIMEOUT_S` (default 2 s): a pattern that backtracks or allocates for a long time (`^(\w+\.?)*example\.org$`, `(?:\b){50000000}x`) never blocks the server or other tools. When matching times out or fails, Tavily's list comes back unfiltered (`scope: unfiltered`) with `scope_note: "local regex filter timed out; results not filtered (…)"` or `"local regex filter failed; …"`; this is not an error. The child process is started only when `allow_external=false` is combined with `select_domains`; the exact-host rule is a plain string comparison. `select_paths`, `exclude_paths` and `exclude_domains` are applied by Tavily only. Tavily's `limit` applies before the client-side filter, so `allow_external=false` can return fewer URLs than `limit`. Tavily's parameter semantics: [Map docs](https://docs.tavily.com/documentation/api-reference/endpoint/map). Live with `https://www.firecrawl.dev` and `allow_external=false`: 8 links on other subdomains removed, only `firecrawl.dev`/`www.firecrawl.dev` left.

On failure the first line is `[web_map] error=<category> provider=tavily [status=<HTTP status>]` (categories as in `web_fetch`, including `unsupported_site` and `http_error`), followed by the Chinese message and a redacted detail line; success output is JSON (see above) and is not duplicated in `structuredContent`. `file://`, `localhost` and local/LAN IP literals are refused locally (`[web_map] error=invalid_url reason=…`); bare domains such as `docs.example.com` still go to Tavily. More than 20 `select_domains` patterns, one longer than 300 characters or one that is not a valid Python regex is refused before Tavily is called: `[web_map] error=bad_request reason=too_many_patterns|pattern_too_long|invalid_regex`.

### `get_config_info` — Configuration Diagnostics

No parameters required. Displays all configuration status (including whether Tavily/Firecrawl are actually active), tests Grok API connection, returns response time and available model list (API keys auto-masked), and refreshes the model list used to validate `web_search(model=...)`. Local proxy problems (such as `[::1]` in `NO_PROXY`) are reported as such.

- `GROK_MODEL_SOURCE` is `env` | `switch_model` | `config` | `default`; `GROK_MODEL_NOTE` explains how long a `switch_model` choice lasts.
- It also lists `GROK_CONFIG_FILE`, `GROK_LOG_RETENTION_DAYS`, `GROK_RETRY_MULTIPLIER`, `GROK_RETRY_MAX_WAIT`, `GROK_API_MODE`, `GROK_READ_TIMEOUT_S`, `GROK_SEARCH_TIMEOUT_S`, the `GROK_VERIFY_*` settings (including `GROK_VERIFY_FAKE_IP`, `GROK_VERIFY_DOH_URL` and `GROK_VERIFY_DETAIL`), the `GROK_FETCH_*` settings (including `GROK_FETCH_DIRECT` and `GROK_FETCH_MAX_AGE_S`) and `GROK_MAP_REGEX_TIMEOUT_S`; `config_warnings` lists environment values that were not understood and config-file or log-directory problems.
- Keys show at most a quarter of their characters (at most the last 4 below 24 characters); credentials in URLs (userinfo, query values) are shown as `***`.
- `available_models` in `connection_test` is unchanged; `chat_models` lists the models `web_search`/`switch_model` accept and `non_chat_models` the TTS/voice/audio, image/video generation, embedding, moderation, realtime and rerank models recognised by name (always rejected). When `GROK_MODEL` itself looks like a non-chat model, `config_warnings` says so.
- The connection test retries once after a transport error (connection reset or refused, proxy tunnel closed; `retried_after`/`attempts`) and explains an empty error as `proxy tunnel closed or upstream unreachable (ConnectError)`; Cloudflare error pages are named, not quoted.

### `switch_model` — Model Switching

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `model` | string | Yes | Model ID (e.g., `"grok-4-fast"`, `"grok-2-latest"`) |

- The name is stripped and checked against `/models` like `web_search(model=...)`. Thinking suffixes such as `(high)` are allowed. Empty, over-long (more than 200 characters) or unlisted names and non-chat models (e.g. `grok-tts`, even when `/models` is unreachable) are rejected with chat-model suggestions and nothing changes. If `/models` is unreachable other names are switched unverified with a warning.
- Switching to the model already in use changes nothing: the result has `already_active: true`, `config.json` is not written and `GROK_MODEL_SOURCE` stays as it was (e.g. `env`). Switching back to the `GROK_MODEL` value restores `GROK_MODEL_SOURCE=env`, with no `GROK_MODEL_NOTE` and no warning.
- The choice is saved to `~/.config/grok-search/config.json`, or to a private temp directory (not kept after a restart, with a warning) when that is not writable.
- If `GROK_MODEL` is set in the MCP `env` it takes precedence: the switch lasts only for the running process and `GROK_MODEL` wins after a restart. To change the model permanently, edit `GROK_MODEL`.

### `toggle_builtin_tools` — Tool Routing Control

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `action` | string | No | `"status"` | `"on"` (alias `"block"`) disable built-in tools / `"off"` (alias `"allow"`) enable built-in tools again / `"status"` check status; any other value (including the ambiguous `enable`/`disable`) is rejected |
| `project_dir` | string | No | `""` | Project root; when empty it is resolved as described below |

Modifies `<project>/.claude/settings.json` `permissions.deny` to disable Claude Code's built-in WebSearch and WebFetch. The project is taken from `project_dir`, else `CLAUDE_PROJECT_DIR`, else the nearest directory containing `.git` or `.claude` above the server's working directory (never the home directory or a filesystem root). If none applies nothing is written and the tool asks for `project_dir`; an unreadable settings file is never overwritten.

Deny rules in `.claude/settings.local.json` and `~/.claude/settings.json` are read, never changed, and reported in `denied_elsewhere`, with the combined result in `effective`; `off` warns when one of them still blocks the tools. A `.claude` directory or settings file that is a symlink pointing outside the project is refused; writes are atomic; a read-only `settings.json` is not changed. (The old, undocumented `disable` used to re-enable the built-in tools; it is now rejected.)

### `plan_*` — Search Planning (optional, off by default)

With `GROK_PLANNING_TOOLS=true` six more tools record a staged plan for complex searches: `plan_intent` → `plan_complexity` → `plan_sub_query` → `plan_search_term` → `plan_tool_mapping` → `plan_execution`. They only record the plan and do not search. Inputs are validated first (enums, `level` 1-3, ranges, confidence 0-1); errors return `{error, phase, invalid_fields}` and nothing is recorded. `plan_intent`, `plan_complexity` and `plan_execution` need `is_revision=true` to overwrite an existing phase.
</details>

## 4. FAQ

<details>
<summary>
Q: Must I configure both Grok and Tavily?
</summary>
A: Grok (`GROK_API_URL` + `GROK_API_KEY`) is required and provides the core search capability. Tavily and Firecrawl are optional: for ordinary web pages `web_fetch` prefers Tavily Extract and falls back to Firecrawl Scrape; text/code files are downloaded directly and need neither (they are used only when the direct download fails); for other pages it returns a configuration error when neither is configured; `web_map` needs Tavily. `TAVILY_ENABLED=false` disables Tavily while keeping the key: `web_fetch` and `extra_sources` then use Firecrawl only and `web_map` is unavailable.
</details>

<details>
<summary>
Q: What format does the Grok API URL need?
</summary>
A: An OpenAI-compatible API endpoint (supporting `/chat/completions` and `/models` endpoints). If using official Grok, access it through an OpenAI-compatible mirror. Surrounding whitespace and a trailing `/` are ignored: before v1.12.0 a trailing slash made `get_config_info` report success while every `web_search` returned HTTP 404.
</details>

<details>
<summary>
Q: What does `proxy tunnel closed or upstream unreachable (ConnectError)` mean?
</summary>
A: The local proxy (`HTTP_PROXY`/`HTTPS_PROXY`) closed the tunnel or could not reach the gateway. Check that the proxy is running, that its rules let the gateway's domain through, and that `GROK_API_URL` is reachable.
</details>

<details>
<summary>
Q: Why is `isError` false when a tool fails?
</summary>
A: By design. Failures are returned as normal results so the model can read the structured error fields and any partial results (such as extra sources). For `web_search` check `error`/`error_type` and the `[搜索失败]` prefix; for `web_fetch`/`web_map` check `error=` in the first line.
</details>

<details>
<summary>
Q: Why did one search use hundreds of thousands or over a million prompt tokens?
</summary>
A: That is how the upstream bills, not something this server inflates: multi-agent / high-reasoning tiers (such as `grok-4.20-multi-agent-0309(high)`) run many internal searches and every turn re-bills the accumulated context as prompt tokens; 0.5-1.6M per search was observed. For routine lookups use a lighter model (e.g. `grok-4.7`, about 19k-133k per search in this release's live tests): set it as `GROK_MODEL`, or pass `web_search(model="grok-4.7")` per call. `usage.prompt_tokens` shows the actual use of each call. Blank queries are now refused locally and cost nothing.
</details>

<details>
<summary>
Q: How to verify configuration?
</summary>
A: Say "Show grok-search configuration info" in a Claude conversation to automatically test the API connection and display results.
</details>

## License

[MIT License](../LICENSE)

---

<div align="center">

**If this project helps you, please give it a Star!**

[![Star History Chart](https://api.star-history.com/svg?repos=GuDaStudio/GrokSearch&type=date&legend=top-left)](https://www.star-history.com/#GuDaStudio/GrokSearch&type=date&legend=top-left)
</div>

## Changelog

### v1.13.0 (2026-10-17)

- New: `GROK_API_MODE=chat|responses` (default `chat`, unchanged behaviour). `responses` uses `/responses` (the Responses API): the request carries `input` and `store: false`; the event stream is parsed for text deltas, citation annotations (offsets shifted per text part) and `response.completed` / `incomplete` / `failed`. `incomplete` maps to `finish_reason` (`max_output_tokens` -> `length`); `failed` and error events are classified like before (exhausted pool, rate limit, deterministic errors); a stream that ends without a final event is retried as `truncated_stream`. Retries, the breaker, the call time limit and citation checks work as in chat mode.
- Measured on CLIProxyAPI: the Responses endpoint returns headers within 2-3 s and keeps streaming search progress and text, while the chat endpoint waits for the complete answer; a `(high)` suffix becomes `reasoning.effort: high` on both.
- `usage` gains `cached_tokens`, `cost_usd` (xAI's `cost_in_usd_ticks`; 1 USD = 10^10 ticks) and `web_search_calls` / `x_search_calls`; results in Responses mode gain `search_queries`.
- Version 1.13.0; the pinned install example is `@v1.13.0`.

### v1.12.1 (2026-10-17)

**web_fetch**

- Fix: source code and text files are no longer corrupted. Raw/gist URLs, GitHub/GitLab/Codeberg/Bitbucket file pages (read from their raw URL), `/raw/` URLs and URLs with a code/config/text extension are downloaded directly and returned byte for byte (`source=direct`, with `content_type=`/`status=` in the header). Before, they went through Tavily, which converts text/plain as if it were HTML: `#include <assert.h>` became `#include  // assert()`, `_` became `\_`, newlines or indentation were lost and long code spans between `<` and `>` were deleted as tags, with no hint in the header. Live, Python.h (raw and blob page), v1.12.0's verify.py (82,163 characters) and pyproject.toml are byte-identical to curl. When the direct download fails or is off (new setting `GROK_FETCH_DIRECT=false`), Firecrawl reads the raw URL first and damaged copies are marked `warning=degraded_code degraded=<reasons>`. The direct download only reaches public addresses (the same check as verification), is capped at 5 MB and sends no API key.
- Fix: branch raw URLs no longer return stale cached copies (in the report, main's pyproject.toml came back as 1.11.0 three times). The direct download reads the origin; Firecrawl requests carry `maxAge` (new setting `GROK_FETCH_MAX_AGE_S`, default `0` = fresh; before, Firecrawl's default 2-day cache applied) and the header shows `cache=hit|miss|bypassed` and `cached_at=`; Tavily has no freshness control and its results say `cache=possible`.
- Fix: small files are no longer fetched twice (`tavily:short(792),firecrawl:ok`). Text files take the direct path (no double fetch), where `GROK_FETCH_MIN_CHARS` does not apply; a URL that only ends in a code-like extension and could not be downloaded directly is read like a page: a short extractor copy goes on to Firecrawl (one extra call, as for any short page in v1.12.0; bundlephobia.com/package/chart.js: a 521-character Tavily stub, a 3,424-character Firecrawl page), whose content type decides whether it is a file; when the arXiv API is down the abstract page is built from DataCite's record (free, `source=datacite`) before Firecrawl and Tavily are tried, with no length threshold.
- Fix: Firecrawl's 403 "we do not support this site" (e.g. Reddit) is reported as the new category `unsupported_site` instead of `auth`, which suggested a bad key. Only 401 or a 403 that mentions API keys, tokens or credentials is `auth`; a 403 without explanation is `http_error`; Tavily's "URL is not supported" moves from `bad_request` to `unsupported_site`. When one backend fails and another answers, the failure is explained below the header.
- Bot-check detection is one detector shared by `web_fetch` and the verification layer: the whole title and each side of ` - ` / ` | ` are matched (so "Reddit - Prove your humanity" is caught), with new wordings such as prove your humanity, verify you are human, are you a robot, Client Challenge, 安全验证/验证码/人机验证, plus challenge markers in the body; Tavily results are checked as well and a bot check makes web_fetch try Firecrawl. Ordinary articles about CAPTCHAs are not flagged (the body-length and challenge-text safeguards are kept).

**Verification layer**

- Fix: bot checks and login walls answered with 2xx/3xx (Reddit, PyPI, Baidu, redirects to `/login`) go to `lookup_failed` with `bot check / interstitial (page may exist)` and are no longer counted as reachable. Live, every Reddit thread cited by a `platform=Reddit` search landed there.
- The verification block is compact by default (new setting `GROK_VERIFY_DETAIL=compact`): the scope text shrinks from 587 to 112 characters and only entries that need attention are listed (every `unresolved`/`lookup_failed` entry, title checks other than `match`, `urls_unusual`, the first 10 `skipped` entries), with every count kept in `summary`. On 3 live searches the blocks went from 18,756 to 7,352 characters in total (-61%) and the whole `web_search` results from 29,021 to 17,617 (-39%). `GROK_VERIFY_DETAIL=full` restores the v1.12.0 output.
- Fix: title checks read the title on the line above in Grok's usual "- Title: X" plus "- arXiv: [id](url)" layout (it was `not_stated`, which also hid wrong titles).
- An unresolved DOI under the 10.5555 prefix (Crossref's test prefix, used by the ACM DL for unregistered items) carries a hint to check the paper by its title.

**web_search**

- Fix: the time context gives the time zone as an ASCII UTC offset (e.g. `UTC+08:00`) plus the IANA name when it can be determined and matches (e.g. `(Asia/Shanghai)`). This fixes `Timezone: ??????` on Chinese Windows and searches failing with UnicodeEncodeError when the zone name could not be decoded. Live, `grok-4.7` repeated it as "2026-10-17, UTC-07:00 (America/Los_Angeles)".
- Fix: a blank query (including ideographic spaces, zero-width and control characters) is refused locally as `bad_request` and is no longer sent to Grok, `/models` or the extra-source engines (in the report one blank query used 500,905 prompt tokens); a whitespace-only `platform` counts as unset.
- Fix: `extra_sources` follows `platform`: known platforms map to domains (Tavily `include_domains`, Firecrawl `site:`), off-platform hits are dropped locally, and `extra_sources_report.platform` explains what was applied; an unknown platform is added to the engine query. Live with `platform="GitHub"`, `extra_sources=6`: all 6 on github.com (the report had 3 of 6 elsewhere).
- Fix: non-chat models (e.g. `grok-tts`, image or embedding models) are rejected locally as `invalid_model` (`model_check.reason: not_chat_model`) instead of failing upstream with HTTP 400; suggestions list chat models only, same family first (`grok-4.8` -> `grok-4.7`; `grok-tts` used to be suggested).
- Docs: multi-agent / high-reasoning models use 0.5-1.6M prompt tokens per search; use a lighter model such as `grok-4.7` for routine lookups.

**web_map, admin tools and logging**

- Fix: `allow_external=false` keeps only URLs on the start URL's host; other subdomains (`app.`, `status.`, ...) are removed client-side, and with `select_domains` its regexes decide. New output fields `scope` and `filtered_out`. The regexes are matched in a separate process (no inherited environment, at most 2 at a time) that is killed after `GROK_MAP_REGEX_TIMEOUT_S` (new setting, default 2 s, waiting included; on Linux/macOS the process has its own alarm and CPU-time limit, on Linux a memory limit too and it ends with the server; on Windows a Job Object limits its memory and ends it with the server), so a pathological pattern cannot block the server (live: `get_config_info` answers in milliseconds while `web_map` runs `^(\w+\.?)*example\.org$`); on a timeout or failure the list comes back unfiltered with a `scope_note`. At most 20 patterns of at most 300 characters; invalid ones are refused locally as `bad_request`.
- Fix: `switch_model` to the model already in use changes nothing (`already_active: true`) and `GROK_MODEL_SOURCE` stays `env`; switching back to the `GROK_MODEL` value restores `env`.
- Fix: `log_info` progress messages reach the client only with `GROK_DEBUG`; every client notification is guarded, so a closed connection no longer breaks a tool.
- `get_config_info` gains `chat_models`/`non_chat_models` and lists `GROK_VERIFY_DETAIL`, `GROK_FETCH_DIRECT`, `GROK_FETCH_MAX_AGE_S` and `GROK_MAP_REGEX_TIMEOUT_S`.
- Tool meta versions: `web_search` 2.4.0, `web_fetch` 1.7.0, `web_map` 1.6.0, `get_config_info` 1.7.0, `switch_model` 1.6.0.

**Packaging and docs**

- Version 1.12.1; the pinned install example is `@v1.12.1`; the v1.12.0 changelog entry gets its release date.

### v1.12.0 (2026-10-16)

**web_search and the Grok upstream**

- Fix: a `GROK_API_URL` with a trailing slash made every `web_search` fail with HTTP 404 while `get_config_info` reported success; surrounding whitespace in the URL and key is ignored too.
- New: `GROK_READ_TIMEOUT_S` and `GROK_SEARCH_TIMEOUT_S`. A stalled gateway no longer holds a search (and a concurrency slot) for ~12 minutes or forever.
- Fix: timeouts and transport failures are reported as `timeout`/`network` with a useful message instead of `unknown: ReadTimeout` or a bare `ConnectError`.
- Fix: CLIProxyAPI pool exhaustion (503 `auth_unavailable`, 429 `model_cooldown`, xAI `free-usage-exhausted`) sets `exhausted=true` and `retry_after_s` and opens the breaker on the first call; an announced recovery time within the budget is waited out once. Deterministic gateway credential failures are not retried (`error_type: auth`).
- Fix: Retry-After is honoured on 503 and every other status.
- Fix: Cloudflare 52x errors are named; 522/524 are timeouts; 520/521/523/530 are retried once.
- Fix: the SSE parser no longer drops frames containing U+0085/U+2028/U+2029 and joins multi-line `data:` events; lone UTF-16 surrogates in the query no longer crash the request.
- New: `incomplete`/`finish_reason`/`native_finish_reason`/`usage` fields. Streams that close without `finish_reason` or `[DONE]` are retried as `truncated_stream`.
- Fix: circuit breaker – queued calls re-check after getting a slot; only the probe owner can release the half-open probe.
- `sources_count`/`distinct_domains` count only the sources Grok cited; new `extra_sources_count` and `total_sources_count`. `get_sources` gains `cited_sources_count`/`extra_sources_count`; extra-source snippets are capped at 500 characters.
- Without native citations the full answer is kept (it could be mistaken for a sources section and removed) and the markers, links and URLs in it count as citations; URLs with balanced parentheses are no longer cut; answer parsing is linear-time.
- Every failure starts with `[搜索失败]`; extra-source hits are still shown when Grok fails and `model_check` is returned on failure too; `extra_sources` 0-20 is enforced by the schema; backfill asks for a margin; the "Extra sources" section has one plain line per item.
- With OpenRouter a per-call model also gets `:online`.

**Verification layer**

- DOIs and arXiv IDs are extracted exactly as written: parenthesised Lancet/Elsevier DOIs, Chinese punctuation or text right after a DOI, URL tails `?`/`#`/`.pdf`, bioRxiv `vN.full`, sentence-final periods, `.pdf` links, `arXiv.<id>` and old-style IDs.
- New `lookup_failed` list: `unresolved` only holds registry/site denials; timed-out, rate-limited and 5xx lookups move to `lookup_failed`.
- DOIs not in Crossref are checked via the doi.org handle registry and DataCite. Crossref runs at concurrency 1 per process (3 with `GROK_VERIFY_MAILTO`), with one retry after Retry-After.
- arXiv: one connection at a time, 3 s after completion, process-wide 429/503 cooldown, shared in-flight lookups, 10 s read timeout, DataCite fallback, status codes in reasons.
- `title_checks` no longer flag author-year link text, journal names, bold labels or a neighbouring paper's title.
- Only Grok-cited sources are verified. `GROK_VERIFY_IDS=false` now only disables identifier lookups; URL checks follow `GROK_VERIFY_URLS`.
- New `GROK_VERIFY_FAKE_IP` / `GROK_VERIFY_DOH_URL`: URL checks work behind Clash fake-ip.
- URL checks read at most 64 KB decoded and never read redirect bodies (fixes a gzip-bomb/regex stall that could freeze the server); `GROK_VERIFY_MAILTO` is sent only to registries.

**web_fetch / web_map**

- 404/403/5xx error pages rendered by Firecrawl are no longer returned as content (`page_status=`) and no longer replace a short Tavily extract.
- After a Firecrawl redirect the header shows `final_url`/`title`; login walls and anti-bot pages (e.g. Zhihu 安全验证) are flagged `warning=interstitial` with a note line.
- Versioned arXiv abs links return the requested version; abstract pages are cached for an hour and fill the verification cache; a slow or throttled arXiv API is abandoned after `GROK_FETCH_ARXIV_BUDGET_S` (default 8 s) and skipped for 2 minutes.
- Non-http(s) URLs and local/LAN IP literals are refused locally (`error=invalid_url`) without calling paid backends.
- CJK-heavy pages default to 20,000 characters per page (`GROK_FETCH_MAX_CHARS_CJK`).
- `web_map`: `output_schema=None`, so no duplicated `structuredContent`, and a stable `[web_map] error=<category> provider=tavily [status=…]` failure header.

**Configuration and admin tools**

- `switch_model` validates the name (empty, over-long and unlisted names are rejected) and reports the true model source (`GROK_MODEL_SOURCE`/`GROK_MODEL_NOTE`).
- Config and logs are never written into the project working directory; a malformed `config.json` is ignored with a warning; unrecognised env values keep their default and appear in `config_warnings`.
- Daily log files with retention (`GROK_LOG_RETENTION_DAYS`, default 7).
- Stricter key/URL masking (at most a quarter of a key, URL credentials as `***`, also in breaker messages); the `get_config_info` connection test retries once after a transport error and lists the new timeout and verification settings.
- `toggle_builtin_tools`: `block`/`allow` aliases, ambiguous `enable`/`disable` rejected, deny rules from other settings files reported, symlinks leading outside the project refused, atomic writes.
- `plan_*` inputs are validated; overwriting a phase needs `is_revision=true`.
- The server exits with status 1 and prints the traceback when it crashes.

**Security**

- Error bodies are redacted before they are shortened; `xai-` keys, Basic/bare Authorization values, `?key=` and URL userinfo are redacted too.

**Packaging and docs**

- Dependency floor raised to `fastmcp>=2.11.0` (the `meta` argument used to register the tools exists only from 2.11.0; older versions cannot import the server).
- Install docs recommend pinning a tag or commit; tool failures returned as normal results (`isError=false`) are documented; the English README is back in parity (SSL `--native-tls` variant, Firecrawl empty-content retry).

### v1.11.0 (2026-10-16)

- `web_search(model=...)` accepts CPA thinking suffixes such as `(high)` or `(8192)`: the default model and other levels of it pass directly, other names are validated by base name and sent in full. The `/models` list is cached with a TTL (`GROK_MODELS_CACHE_TTL_S`), failures are not cached, a stale list no longer rejects new models, and `get_config_info` refreshes the cache. A refresh after a cache miss also honours the failure backoff, and concurrent misses share one refresh.
- `extra_sources` quota fixed: Tavily/Firecrawl split the share, duplicates with Grok's citations and across engines are dropped, and a failed engine is backfilled by the other; see `extra_sources_report`.
- The default output style is now `concise`. The prompt no longer demands a citation for every sentence: only statements a source makes are cited, inferences are labelled, self-written explanations and analogies carry no citation, and absence is reported as "not found in this search".
- The verification layer states that it checks existence and reachability only (`scope`, `claim_support: not_checked`) and gained paper-title comparison (`title_checks`), `skipped` and `summary`; URL checks resolve the host before the request and before every redirect and skip non-public addresses (limits: see `web_search`).
- `web_fetch` pages from snapshots (`snapshot_id`) instead of refetching; failures report category, HTTP status and a redacted detail per backend. `TAVILY_ENABLED=false` now really disables Tavily (fetch, map and extra sources).
- Native citation offsets are checked against the final answer: repeated markers are collapsed and offsets recomputed; offsets that do not match are relocated or dropped (upstream values kept), see `citation_positions`.
- `web_map` gained `allow_external`, `select_paths`, `select_domains`, `exclude_paths` and `exclude_domains`.
- `toggle_builtin_tools` gained `project_dir` and resolves the project via argument → `CLAUDE_PROJECT_DIR` → nearest `.git`/`.claude`; it no longer writes to a filesystem root.
- At startup, bracketed IPv6 entries in `NO_PROXY` (such as `[::1]`) that httpx cannot parse are rewritten, so the web tools no longer fail with `Invalid port: ':1]'`; `get_config_info` names local proxy problems explicitly.
- The circuit breaker counts per base model: all thinking levels of a model share one breaker state, so switching levels does not bypass it.
- Grok upstream error text is redacted (API keys, bearer tokens) in tool output, logs and breaker reasons.
- README: install from this repository, 7 default tools + 6 optional planning tools; the English README no longer documents `GUDA_*` settings that the code does not support.

### v1.10.0 (2026-09-17)

- Read-only tools carry `readOnlyHint`, so Claude Code executes parallel searches and fetches concurrently.
- Added a concurrency semaphore, a per-model circuit breaker (repeated 429s or an exhausted account pool open it; half-open probing closes it) and a time-based retry budget.
- In-stream `event: error` frames, HTTP 429 and empty responses become structured error fields instead of a silent empty answer; errors are always logged.
- `web_search` gained `instructions` and `verify` parameters plus `distinct_domains` and `verification` result fields; `extra_sources` hits are appended to the answer.
- The system prompt is split into a strategy block and a switchable style block (`GROK_SEARCH_STYLE`); citation-integrity rules were added.
- `web_fetch` gained a length gate with Firecrawl fallback, `max_chars`/`offset` paging and an arXiv abstract adapter; the Tavily extract timeout is now 30 s.
- The six `plan_*` tools are registered only when `GROK_PLANNING_TOOLS=true`.
- `switch_model` warns when `GROK_MODEL` is pinned by the environment; `get_config_info` reports breaker state and the server version.
- Removed the unused Grok fetch/describe/rank code paths and prompts; `build/` and `*.egg-info` are no longer committed.
