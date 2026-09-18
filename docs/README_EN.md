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
                  ├─ web_fetch   ---> Tavily Extract (Content Extraction)
                  └─ web_map     ---> Tavily Map (Site Mapping)
```

### Features

- **Dual Engine**: Grok search + Tavily extraction/mapping, complementary collaboration
- **OpenAI-compatible interface**, supports any Grok mirror endpoint
- **Automatic time injection** (local date and time context is prepended to every search)
- **Read-only tool annotations**: `web_search`/`web_fetch`/`web_map`/`get_sources`/`get_config_info` declare `readOnlyHint`, so Claude Code runs several calls from one turn in parallel instead of one after another
- **Concurrency cap + per-model circuit breaker**: `GROK_MAX_CONCURRENCY` bounds in-flight Grok requests; repeated 429s or an exhausted account pool open a breaker for that model tier and return a structured error, with half-open probing to recover
- **Failures are never silent**: in-stream error frames, HTTP 429 and empty responses become `error`/`error_type`/`retry_after_s` fields instead of an empty answer
- **Citation verification layer**: arXiv IDs, DOIs and cited URLs in the answer are resolved through the arXiv API, Crossref and the live page; misses are listed under `unresolved`
- **Fetch quality gate and paging**: short Tavily extracts fall back to Firecrawl and the longer result wins; arXiv abstract pages use the arXiv API; `max_chars`/`offset` page through long documents
- **Output style switch**: `GROK_SEARCH_STYLE=concise` drops term definitions and analogies; both styles carry hard rules to quote numbers verbatim and never merge figures across sources
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

Replace the environment variables in the following command with your own values. The Grok endpoint must be OpenAI-compatible; Tavily is optional — `web_fetch` and `web_map` will be unavailable without it.

#### GuDa Users (Recommended)

GuDa users only need to set `GUDA_API_KEY` to access all services — API URLs are automatically derived:

```bash
claude mcp add-json grok-search --scope user '{
  "type": "stdio",
  "command": "uvx",
  "args": [
    "--from",
    "git+https://github.com/GuDaStudio/GrokSearch@grok-with-tavily",
    "grok-search"
  ],
  "env": {
    "GUDA_API_KEY": "your-guda-api-key"
  }
}'
```

#### Custom Configuration

To use your own API endpoints, configure each service separately:

```bash
claude mcp add-json grok-search --scope user '{
  "type": "stdio",
  "command": "uvx",
  "args": [
    "--from",
    "git+https://github.com/GuDaStudio/GrokSearch@grok-with-tavily",
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
| `GUDA_API_KEY` | No | - | GuDa API key (auto-derives all service URLs and keys when set) |
| `GUDA_BASE_URL` | No | `https://code.guda.studio` | GuDa service base URL |
| `GROK_API_URL` | No | `{GUDA_BASE_URL}/grok/v1` | Grok API endpoint (OpenAI-compatible), overrides GuDa-derived value |
| `GROK_API_KEY` | No | `{GUDA_API_KEY}` | Grok API key, overrides GuDa-derived value |
| `GROK_MODEL` | No | `grok-4.20-beta` | Default model (takes precedence over `~/.config/grok-search/config.json` when set) |
| `TAVILY_API_KEY` | No | `{GUDA_API_KEY}` | Tavily API key (for web_fetch / web_map) |
| `TAVILY_API_URL` | No | `{GUDA_BASE_URL}/tavily` | Tavily API endpoint |
| `TAVILY_ENABLED` | No | `true` | Enable Tavily |
| `FIRECRAWL_API_KEY` | No | `{GUDA_API_KEY}` | Firecrawl API key (fallback when Tavily fails) |
| `FIRECRAWL_API_URL` | No | `{GUDA_BASE_URL}/firecrawl` | Firecrawl API endpoint |
| `GROK_DEBUG` | No | `false` | Debug mode |
| `GROK_LOG_LEVEL` | No | `INFO` | Log level |
| `GROK_LOG_DIR` | No | `logs` | Log directory |
| `GROK_RETRY_MAX_ATTEMPTS` | No | `3` | Max retry attempts |
| `GROK_RETRY_MULTIPLIER` | No | `1` | Retry backoff multiplier |
| `GROK_RETRY_MAX_WAIT` | No | `10` | Max retry wait in seconds |
| `GROK_RETRY_BUDGET_S` | No | `45` | Total retry-wait budget per call (seconds); a Retry-After beyond the budget fails fast. `0` disables |
| `GROK_MAX_CONCURRENCY` | No | `4` | Maximum in-flight Grok requests |
| `GROK_BREAKER_THRESHOLD` | No | `3` | 429s within the window that open the breaker |
| `GROK_BREAKER_WINDOW_S` | No | `60` | Breaker counting window (seconds) |
| `GROK_BREAKER_COOLDOWN_S` | No | `60` | Initial cooldown (seconds), doubled after a failed probe |
| `GROK_BREAKER_MAX_COOLDOWN_S` | No | `300` | Cooldown ceiling (seconds) |
| `GROK_SEARCH_STYLE` | No | `explanatory` | Output style: `explanatory` or `concise` |
| `GROK_VERIFY_IDS` | No | `true` | Resolve arXiv IDs and DOIs found in the answer |
| `GROK_VERIFY_URLS` | No | `true` | Check reachability and title of cited URLs |
| `GROK_VERIFY_TIMEOUT_S` | No | `30` | Overall verification timeout (seconds); lookups still pending at the deadline are listed under `unresolved` as `timed out`. All arXiv API calls share a 3-second spacing gate and a one-hour cache |
| `GROK_VERIFY_MAILTO` | No | empty | Contact e-mail for the verification User-Agent (Crossref polite pool) |
| `GROK_PLANNING_TOOLS` | No | `false` | Register the six `plan_*` planning tools |
| `GROK_FETCH_MIN_CHARS` | No | `2000` | Fall back to Firecrawl when the Tavily extract is shorter than this |
| `GROK_FETCH_MAX_CHARS` | No | `40000` | Default characters returned per `web_fetch` call |
| `TAVILY_EXTRACT_TIMEOUT_S` | No | `30` | Tavily extract timeout (seconds) |

> **Note**: When `GUDA_API_KEY` is set, all `GROK_API_URL`/`GROK_API_KEY`/`TAVILY_*`/`FIRECRAWL_*` variables become optional as they are auto-derived from `GUDA_BASE_URL`. Explicitly set variables take higher priority.


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
<summary>This project provides eight MCP tools (click to expand)</summary>

### `web_search` — AI Web Search

Executes AI-driven web search via Grok API. By default it returns only Grok's answer and a `session_id` for retrieving sources later.

`web_search` does not expand sources in the response; it only returns `sources_count`. Sources are cached server-side by `session_id` and can be fetched with `get_sources`.

New parameters: `instructions` (per-call requirements for the searcher, e.g. "cover at least 15 distinct domains" or "list arXiv IDs with titles only") and `verify` (default `true`; resolves arXiv IDs, DOIs and cited URLs found in the answer). Besides `content` and `sources_count` the result carries `distinct_domains` and `verification` (`arxiv`/`doi`/`urls` plus an `unresolved` list); on failure it carries `error`/`error_type`/`retry_after_s` and `content` starts with `[搜索失败]`. With `extra_sources > 0` the independent Tavily/Firecrawl hits are appended to the answer as an "Extra sources" section.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | Yes | - | Search query |
| `platform` | string | No | `""` | Focus platform (e.g., `"Twitter"`, `"GitHub, Reddit"`) |
| `model` | string | No | `null` | Per-request Grok model ID |
| `extra_sources` | int | No | `0` | Extra sources via Tavily/Firecrawl (0 disables) |

Local date, time and timezone context is prepended to every search to improve time-sensitive queries.

Return value (structured dict):
- `session_id`: search session ID
- `content`: answer only (sources removed)
- `sources_count`: cached sources count

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

Extracts complete web content via Tavily Extract API, returning Markdown format.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `url` | string | Yes | Target webpage URL |

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

### `get_config_info` — Configuration Diagnostics

No parameters required. Displays all configuration status, tests Grok API connection, returns response time and available model list (API keys auto-masked).

### `switch_model` — Model Switching

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `model` | string | Yes | Model ID (e.g., `"grok-4-fast"`, `"grok-2-latest"`) |

Settings persist to `~/.config/grok-search/config.json` across sessions.

### `toggle_builtin_tools` — Tool Routing Control

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `action` | string | No | `"status"` | `"on"` disable built-in tools / `"off"` enable built-in tools / `"status"` check status |

Modifies project-level `.claude/settings.json` `permissions.deny` to disable Claude Code's built-in WebSearch and WebFetch.

### `search_planning` — Search Planning

A structured multi-phase planning scaffold to generate an executable search plan before running complex searches.
</details>

## 4. FAQ

<details>
<summary>
Q: Must I configure both Grok and Tavily?
</summary>
A: Set `GUDA_API_KEY` to get full Grok + Tavily + Firecrawl service. Without GuDa, Grok (`GROK_API_URL` + `GROK_API_KEY`) is required and provides the core search capability. Tavily is optional — without it, `web_fetch` and `web_map` will return configuration error messages.
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

[MIT License](LICENSE)

---

<div align="center">

**If this project helps you, please give it a Star!**

[![Star History Chart](https://api.star-history.com/svg?repos=GuDaStudio/GrokSearch&type=date&legend=top-left)](https://www.star-history.com/#GuDaStudio/GrokSearch&type=date&legend=top-left)
</div>

## Changelog

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
