![Image](../images/title.png)
<div align="center">

<!-- # Grok Search MCP -->

English | [简体中文](../README.md)

**Grok-with-Tavily MCP, providing enhanced web access for Claude Code**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT) [![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/) [![FastMCP](https://img.shields.io/badge/FastMCP-2.0.0+-green.svg)](https://github.com/jlowin/fastmcp)

</div>

---

## 1. Overview

Grok Search MCP is an MCP server built on [FastMCP](https://github.com/jlowin/fastmcp), featuring a **dual-engine architecture**: **Grok** handles AI-driven intelligent search, while **Tavily** handles high-fidelity web content extraction and site mapping. Together they provide complete real-time web access for LLM clients such as Claude Code and Cherry Studio.

```
Claude --MCP--> Grok Search Server
                  ├─ web_search  ---> Grok API (AI Search)
                  ├─ web_fetch   ---> Tavily Extract -> Firecrawl Scrape (extraction with fallback)
                  └─ web_map     ---> Tavily Map (Site Mapping)
```

### Features

- **Dual Engine**: Grok search + Tavily extraction/mapping, complementary collaboration
- **OpenAI-compatible interface**, supports any Grok mirror endpoint
- **Automatic time injection** (local date and time context is prepended to every search)
- **Read-only tool annotations**: `web_search`/`web_fetch`/`web_map`/`get_sources`/`get_config_info` declare `readOnlyHint`, so Claude Code runs several calls from one turn in parallel instead of one after another
- **Concurrency cap + per-model circuit breaker**: `GROK_MAX_CONCURRENCY` bounds in-flight Grok requests; repeated 429s or an exhausted account pool open a breaker for that model (thinking-level suffixes such as `(high)`/`(low)`/`(8192)` share one breaker) and return a structured error, with half-open probing to recover
- **Failures are never silent**: in-stream error frames, HTTP 429 and empty responses become `error`/`error_type`/`retry_after_s` fields instead of an empty answer
- **Per-call models**: `web_search(model=...)` accepts CPA-style thinking suffixes (e.g. `grok-4.7(high)`, `grok-4.7(8192)`), validates the base name against `/models` and sends the full name; the model list is cached with a TTL and failures are not cached
- **Dual-engine extra sources**: `extra_sources` is split between Tavily and Firecrawl, deduplicated against Grok's citations and across engines, and a failed engine is backfilled by the other (`extra_sources_report`)
- **Verification layer (not fact-checking)**: arXiv IDs and DOIs are confirmed to exist via the arXiv API / Crossref, cited URLs are confirmed reachable, and paper titles written in the answer are compared with the registry title (`title_checks`). It does **not** check whether a source supports the claim (`claim_support: not_checked`); anything not checked is listed in `skipped` with a reason. Cited URLs are fetched only when their host resolves to public addresses (limits: see `web_search`)
- **Fetch quality gate and snapshot paging**: short Tavily extracts fall back to Firecrawl and the longer result wins; arXiv abstract pages use the arXiv API; long pages return a `snapshot_id` and every later page is cut from the same snapshot; failures list each backend with error category, HTTP status and a redacted detail
- **Output style and citation rules**: `concise` by default, `explanatory` adds term definitions and analogies. Both styles cite only statements a source makes, label inferences ("Inference:"), never cite self-written explanations or analogies, report absence as "not found in this search", and quote numbers verbatim without merging figures across sources
- One-click disable Claude Code's built-in WebSearch/WebFetch, force routing to this tool
- Smart retry (Retry-After header parsing + exponential backoff)
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

You can also configure additional environment variables in the `env` field:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GROK_API_URL` | Yes | - | Grok API endpoint (OpenAI-compatible) |
| `GROK_API_KEY` | Yes | - | Grok API key |
| `GROK_MODEL` | No | `grok-4-fast` | Default model (takes precedence over `~/.config/grok-search/config.json` when set) |
| `TAVILY_API_KEY` | No | - | Tavily API key (for web_fetch / web_map / extra_sources) |
| `TAVILY_API_URL` | No | `https://api.tavily.com` | Tavily API endpoint |
| `TAVILY_ENABLED` | No | `true` | Enable Tavily; with `false` neither `web_fetch`, `web_map` nor `extra_sources` calls Tavily, even when a key is set |
| `FIRECRAWL_API_KEY` | No | - | Firecrawl API key (fallback when Tavily fails) |
| `FIRECRAWL_API_URL` | No | `https://api.firecrawl.dev/v2` | Firecrawl API endpoint |
| `GROK_DEBUG` | No | `false` | Debug mode |
| `GROK_LOG_LEVEL` | No | `INFO` | Log level |
| `GROK_LOG_DIR` | No | `logs` | Log directory |
| `GROK_RETRY_MAX_ATTEMPTS` | No | `3` | Max retry attempts |
| `GROK_RETRY_MULTIPLIER` | No | `1` | Retry backoff multiplier |
| `GROK_RETRY_MAX_WAIT` | No | `10` | Max retry wait in seconds |
| `GROK_RETRY_BUDGET_S` | No | `45` | Total retry-wait budget per call (seconds); a Retry-After beyond the budget fails fast. `0` disables |
| `GROK_MAX_CONCURRENCY` | No | `16` | Maximum in-flight Grok requests |
| `GROK_BREAKER_THRESHOLD` | No | `3` | 429s within the window that open the breaker |
| `GROK_BREAKER_WINDOW_S` | No | `60` | Breaker counting window (seconds) |
| `GROK_BREAKER_COOLDOWN_S` | No | `60` | Initial cooldown (seconds), doubled after a failed probe |
| `GROK_BREAKER_MAX_COOLDOWN_S` | No | `300` | Cooldown ceiling (seconds) |
| `GROK_SEARCH_STYLE` | No | `concise` | Output style: `concise` or `explanatory` (adds term definitions and uncited analogies) |
| `GROK_MODELS_CACHE_TTL_S` | No | `600` | Lifetime (seconds) of the cached `/models` list used to validate `web_search(model=...)`; failures are not cached and `get_config_info` refreshes it |
| `GROK_VERIFY_IDS` | No | `true` | Resolve arXiv IDs and DOIs found in the answer |
| `GROK_VERIFY_URLS` | No | `true` | Check reachability and title of cited URLs (hosts are resolved first and non-public addresses are not fetched; limits: see `web_search`) |
| `GROK_VERIFY_TIMEOUT_S` | No | `30` | Overall verification timeout (seconds); lookups still pending at the deadline are listed under `unresolved` as `timed out`. All arXiv API calls share a 3-second spacing gate and a one-hour cache |
| `GROK_VERIFY_MAILTO` | No | empty | Contact e-mail for the verification User-Agent (Crossref polite pool) |
| `GROK_PLANNING_TOOLS` | No | `false` | Register the six `plan_*` planning tools |
| `GROK_FETCH_MIN_CHARS` | No | `2000` | Fall back to Firecrawl when the Tavily extract is shorter than this |
| `GROK_FETCH_MAX_CHARS` | No | `40000` | Default characters returned per `web_fetch` call |
| `GROK_FETCH_SNAPSHOT_TTL_S` | No | `900` | Idle lifetime (seconds) of a `web_fetch` paging snapshot; afterwards paging restarts at `offset=0` |
| `TAVILY_EXTRACT_TIMEOUT_S` | No | `30` | Tavily extract timeout (seconds) |



### Verify Installation

```bash
claude mcp list
```

After confirming a successful connection, we **highly recommend** typing the following in a Claude conversation:
```
Call grok-search toggle_builtin_tools to disable Claude Code's built-in WebSearch and WebFetch tools
```
This will automatically modify the **project-level** `.claude/settings.json` `permissions.deny`, disabling Claude Code's built-in WebSearch and WebFetch, forcing Claude Code to use this project for searches!



## 3. MCP Tools

<details>
<summary>Seven MCP tools by default; `GROK_PLANNING_TOOLS=true` adds six `plan_*` planning tools (click to expand)</summary>

### `web_search` — AI Web Search

Executes AI-driven web search via Grok API. By default it returns only Grok's answer and a `session_id` for retrieving sources later.

`web_search` does not expand sources in the response; it only returns `sources_count`. Sources are cached server-side by `session_id` and can be fetched with `get_sources`.

`instructions` adds per-call requirements for the searcher (e.g. "cover at least 15 distinct domains" or "list arXiv IDs with titles only"). Besides `content` and `sources_count` the result carries `distinct_domains`; on failure it carries `error`/`error_type`/`retry_after_s` and `content` starts with `[搜索失败]`.

- **Citation positions**: with native citations, adjacent repeated markers (e.g. `[[1]](u)[[1]](u)`) are collapsed and the `start_index`/`end_index` returned by `get_sources` always refer to the content that is returned. Offsets that do not match are relocated (`position: relocated`) or lose their coordinates (`position: removed`); the upstream values are kept in `upstream_start_index`/`upstream_end_index` and summarized in `citation_positions`.
- **`verification`**: checks only that identifiers exist (arXiv / Crossref), that cited links are reachable, and whether a paper title written next to an arXiv ID or DOI matches the registry title (`title_checks`: `match` / `partial` / `possible_mismatch` / `not_stated`). It does **not** check whether a source supports the statement it is attached to (`claim_support` is always `not_checked`). Whatever was not checked is listed in `skipped` with a reason (over the limit, URL checks disabled, non-public address, non-http link); totals are in `summary`.
- **Which URLs are fetched**: before the request and before every redirect the host is resolved with the system resolver (hosts file and LAN names included); if any address is not public the URL is not fetched and goes to `skipped`. This covers loopback, private, link-local and CGNAT (100.64.0.0/10) addresses, numeric forms such as `127.1`, `2130706433` or `0x7f.0.0.1`, and non-public IPv4 addresses inside IPv4-mapped, 6to4 and NAT64 addresses. Limits: the check and the connection resolve the name separately, so DNS rebinding is not prevented; when an HTTP/SOCKS proxy carries the request the proxy decides the real destination; with a Clash/mihomo fake-ip resolver every name resolves to 198.18.0.0/15, the real destination cannot be checked and such URLs are skipped (add the domains to fake-ip-filter or set `GROK_VERIFY_URLS=false`); names that do not resolve locally are listed as `unresolved` (`dns lookup failed`). arXiv and Crossref lookups go to fixed hosts and are not subject to this check.
- **`model`**: CPA thinking suffixes are accepted, e.g. `grok-4.7(high)`, `grok-4.7(8192)`, `grok-4.7(auto)`. The default model (with its suffix) and other levels of the same base model pass directly; other names are validated by their base name against `/models` and sent in full. If the list cannot be fetched the name is not blocked (`model_check.status = unverified`).
- **`extra_sources`** (0-20): with both engines active Tavily gets ceil(n/2) and Firecrawl floor(n/2), each asked for 2 spare results; URLs already cited by Grok and repeats across engines are dropped (scheme, `www.`, fragment, trailing slash and tracking parameters ignored) and results are picked round-robin. If an engine fails or comes up short, the other one backfills once. The hits are appended as an "Extra sources" section; details are in `extra_sources_report`.
- **"Not found"**: "not found in this search" only means this search did not find it. When a definite answer matters, check the platform directly (for example `web_fetch` the repository's issue search page).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | Yes | - | Search query |
| `platform` | string | No | `""` | Focus platform (e.g., `"Twitter"`, `"GitHub, Reddit"`) |
| `model` | string | No | `""` | Per-request Grok model ID, thinking suffix allowed |
| `extra_sources` | int | No | `0` | Extra sources via Tavily/Firecrawl (0-20, 0 disables) |
| `instructions` | string | No | `""` | Per-call requirements for the searcher |
| `verify` | bool | No | `true` | Attach the verification layer (also gated by `GROK_VERIFY_IDS`) |

Local date, time and timezone context is prepended to every search to improve time-sensitive queries.

Return value (structured dict):
- `session_id`: search session ID
- `content`: the answer (without native citations the sources section is removed; with native citations the original text is kept and only repeated markers are collapsed)
- `sources_count` / `distinct_domains`: cached sources / distinct sites
- `citation_positions`, `verification`, `extra_sources_report`, `model_check`: see above, present when applicable

### `get_sources` — Retrieve Sources

Retrieves the full cached source list for a previous `web_search` call.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session_id` | string | Yes | `session_id` returned by `web_search` |

Return value (structured dict):
- `session_id`
- `sources_count`
- `sources`: source list (each item includes `url`, may include `title`/`description`/`provider`)

### `web_fetch` — Web Content Extraction

Extracts complete web content via Tavily Extract API, returning Markdown. When Tavily fails or returns too little, Firecrawl Scrape takes over; arXiv abstract pages use the arXiv API.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `url` | string | Yes | - | Target webpage URL |
| `max_chars` | int | No | `0` | Maximum characters in this call; `0` uses `GROK_FETCH_MAX_CHARS` |
| `offset` | int | No | `0` | Character offset; values above 0 require `snapshot_id` |
| `snapshot_id` | string | No | `""` | Snapshot ID from the first page, used to read the following pages |

The first line is a metadata header: `[web_fetch] source=… total_chars=… offset=… returned=… truncated=… [next_offset=… snapshot_id=… expires_in_s=…]`. A page longer than one response is stored as a server-side snapshot; call again with the same `url`, `offset=next_offset` and `snapshot_id` and every page is cut from the same text. `offset>0` without `snapshot_id` is refused; once a snapshot expires (900 s idle by default) the tool answers `error=snapshot_expired` and paging restarts at `offset=0`.

On failure the first line is `[web_fetch] error=…`, followed by one line per backend with the error category (`auth`, `quota`, `rate_limit`, `timeout`, `proxy_config`, `network`, `upstream_error`, `bad_request`, `extract_failed`, `empty_content`, `disabled`, `not_configured`, ...), the HTTP status and a redacted detail.

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
| `allow_external` | bool | No | `true` | Include links to other sites (Tavily's default); `false` keeps only the starting site |
| `select_paths` | string[] | No | - | Keep only URLs whose path matches these regexes, e.g. `["/docs/.*"]` |
| `select_domains` | string[] | No | - | Crawl only domains/subdomains matching these regexes |
| `exclude_paths` | string[] | No | - | Skip URLs whose path matches these regexes |
| `exclude_domains` | string[] | No | - | Skip domains/subdomains matching these regexes |

`web_map` is unavailable when `TAVILY_ENABLED=false`. Few results or a fast answer are not necessarily a fault; use the parameters above and `max_depth` to adjust the scope.

### `get_config_info` — Configuration Diagnostics

No parameters required. Displays all configuration status (including whether Tavily/Firecrawl are actually active), tests Grok API connection, returns response time and available model list (API keys auto-masked), and refreshes the model list used to validate `web_search(model=...)`. Local proxy problems (such as `[::1]` in `NO_PROXY`) are reported as such.

### `switch_model` — Model Switching

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `model` | string | Yes | Model ID (e.g., `"grok-4-fast"`, `"grok-2-latest"`) |

Settings persist to `~/.config/grok-search/config.json` across sessions.

### `toggle_builtin_tools` — Tool Routing Control

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `action` | string | No | `"status"` | `"on"` disable built-in tools / `"off"` enable built-in tools / `"status"` check status |
| `project_dir` | string | No | `""` | Project root; when empty it is resolved as described below |

Modifies `<project>/.claude/settings.json` `permissions.deny` to disable Claude Code's built-in WebSearch and WebFetch. The project is taken from `project_dir`, else `CLAUDE_PROJECT_DIR`, else the nearest directory containing `.git` or `.claude` above the server's working directory (never the home directory or a filesystem root). If none applies nothing is written and the tool asks for `project_dir`; an unreadable settings file is never overwritten.

### `plan_*` — Search Planning (optional, off by default)

With `GROK_PLANNING_TOOLS=true` six more tools record a staged plan for complex searches: `plan_intent` → `plan_complexity` → `plan_sub_query` → `plan_search_term` → `plan_tool_mapping` → `plan_execution`. They only record the plan and do not search.
</details>

## 4. FAQ

<details>
<summary>
Q: Must I configure both Grok and Tavily?
</summary>
A: Grok (`GROK_API_URL` + `GROK_API_KEY`) is required and provides the core search capability. Tavily and Firecrawl are optional: `web_fetch` prefers Tavily Extract and falls back to Firecrawl Scrape, and returns a configuration error when neither is configured; `web_map` needs Tavily. `TAVILY_ENABLED=false` disables Tavily while keeping the key: `web_fetch` and `extra_sources` then use Firecrawl only and `web_map` is unavailable.
</details>

<details>
<summary>
Q: What format does the Grok API URL need?
</summary>
A: An OpenAI-compatible API endpoint (supporting `/chat/completions` and `/models` endpoints). If using official Grok, access it through an OpenAI-compatible mirror.
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
