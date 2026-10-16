![这是图片](./images/title.png)
<div align="center">

<!-- # Grok Search MCP -->

[English](./docs/README_EN.md) | 简体中文

**Grok-with-Tavily MCP，为 Claude Code 提供更完善的网络访问能力**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT) [![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/) [![FastMCP](https://img.shields.io/badge/FastMCP-2.0.0+-green.svg)](https://github.com/jlowin/fastmcp)

</div>

---

## 一、概述

Grok Search MCP 是一个基于 [FastMCP](https://github.com/jlowin/fastmcp) 构建的 MCP 服务器，采用**双引擎架构**：**Grok** 负责 AI 驱动的智能搜索，**Tavily** 负责高保真网页抓取与站点映射，各取所长为 Claude Code / Cherry Studio 等LLM Client提供完整的实时网络访问能力。

```
Claude ──MCP──► Grok Search Server
                  ├─ web_search  ───► Grok API（AI 搜索）
                  ├─ web_fetch   ───► Tavily Extract → Firecrawl Scrape（内容抓取，自动降级）
                  └─ web_map     ───► Tavily Map（站点映射）
```

### 功能特性

- **双引擎**：Grok 搜索 + Tavily 抓取/映射，互补协作
- **Firecrawl 托底**：Tavily 提取失败时自动降级到 Firecrawl Scrape，支持空内容自动重试
- **OpenAI 兼容接口**，支持任意 Grok 镜像站
- **自动时间注入**（每次搜索注入本地日期与时间上下文）
- **只读工具注解**：`web_search`/`web_fetch`/`web_map`/`get_sources`/`get_config_info` 声明 `readOnlyHint`，Claude Code 会并行执行同一轮里的多次调用，而不是逐个排队
- **并发上限 + 按模型熔断**：`GROK_MAX_CONCURRENCY` 限制同时在途的 Grok 请求；连续 429 或账号池耗尽时按模型熔断（同一模型的不同思考档位，如 `(high)`/`(low)`/`(8192)`，共用一套熔断状态）并返回结构化错误，冷却后自动探测恢复
- **失败必有回声**：上游流内错误帧、429、空响应都会变成 `error`/`error_type`/`retry_after_s` 字段，不再静默返回空正文
- **按次换模型**：`web_search(model=...)` 支持 CPA 风格的思考后缀（如 `grok-4.7(high)`、`grok-4.7(8192)`），按基础名称对照 `/models` 校验、请求时保留完整名称；模型列表缓存有有效期，获取失败不缓存
- **额外信源双引擎分配**：`extra_sources` 在 Tavily 与 Firecrawl 之间对半分配，与 Grok 已引用的链接及引擎之间去重，一方失败由另一方补位，过程写入 `extra_sources_report`
- **核验层（不是事实核验）**：回答里的 arXiv 号、DOI 经 arXiv API / Crossref 确认存在，被引用 URL 确认可访问，回答中写出的论文标题与登记标题自动比对（`title_checks`）；**不检查来源是否支持对应论断**（`claim_support: not_checked`）。没检查的条目连同原因列在 `skipped`；URL 请求前先解析域名，只有全部地址都是公网地址才访问（局限见 `web_search` 一节）
- **抓取质量门槛与快照分页**：Tavily 结果过短时回退 Firecrawl 取较长者；arXiv 摘要页直接走 arXiv API；超长页面首屏返回 `snapshot_id`，后续页按 `offset` + `snapshot_id` 固定读取同一份内容，不会错页；失败时逐个列出各后端的错误类别、HTTP 状态和脱敏详情
- **输出风格与引用规则**：默认 `concise`，`explanatory` 增加术语解释与类比。两种风格都只给来源直接陈述的事实挂引用；推断须标注“Inference:/推断：”，自写的解释与类比不挂引用；查不到的东西只表述为“本次检索未找到”；并附数字原样引用、不跨来源拼区间的硬规则
- 一键禁用 Claude Code 官方 WebSearch/WebFetch，强制路由到本工具
- 智能重试（支持 Retry-After 头解析 + 指数退避）
- 父进程监控（Windows 下自动检测父进程退出，防止僵尸进程）

### 效果展示
我们以在`cherry studio`中配置本MCP为例，展示了`claude-opus-4.6`模型如何通过本项目实现外部知识搜集，降低幻觉率。
![](./images/wogrok.png)
如上图，**为公平实验，我们打开了claude模型内置的搜索工具**，然而opus 4.6仍然相信自己的内部常识，不查询FastAPI的官方文档，以获取最新示例。
![](./images/wgrok.png)
如上图，当打开`grok-search MCP`时，在相同的实验条件下，opus 4.6主动调用多次搜索，以**获取官方文档，回答更可靠。** 


## 二、安装

### 前置条件

- Python 3.10+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)（推荐的 Python 包管理器）
- Claude Code

<details>
<summary><b>安装 uv</b></summary>

```bash
# Linux/macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

> Windows 用户**强烈推荐**在 WSL 中运行本项目。

</details>

### 一键安装
若之前安装过本项目，使用以下命令卸载旧版MCP。
```
claude mcp remove grok-search
```


将以下命令中的环境变量替换为你自己的值后执行。Grok 接口需为 OpenAI 兼容格式；Tavily 与 Firecrawl 为可选配置：`web_fetch` 至少需要其中之一，`web_map` 需要 Tavily。

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

<details> <summary>如果遇到 SSL / 证书验证错误</summary>

在部分企业网络或代理环境中，可能会出现类似错误：

certificate verify failed
self signed certificate in certificate chain

可以在 uvx 参数中添加 --native-tls，使其使用系统证书库：

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

除此之外，你还可以在`env`字段中配置更多环境变量

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `GROK_API_URL` | ✅ | - | Grok API 地址（OpenAI 兼容格式） |
| `GROK_API_KEY` | ✅ | - | Grok API 密钥 |
| `GROK_MODEL` | ❌ | `grok-4-fast` | 默认模型（设置后优先于 `~/.config/grok-search/config.json`） |
| `TAVILY_API_KEY` | ❌ | - | Tavily API 密钥（用于 web_fetch / web_map） |
| `TAVILY_API_URL` | ❌ | `https://api.tavily.com` | Tavily API 地址 |
| `TAVILY_ENABLED` | ❌ | `true` | 是否启用 Tavily；`false` 时 `web_fetch`、`web_map`、`extra_sources` 都不会调用 Tavily（即使配置了 key） |
| `FIRECRAWL_API_KEY` | ❌ | - | Firecrawl API 密钥（Tavily 失败时托底） |
| `FIRECRAWL_API_URL` | ❌ | `https://api.firecrawl.dev/v2` | Firecrawl API 地址 |
| `GROK_DEBUG` | ❌ | `false` | 调试模式 |
| `GROK_LOG_LEVEL` | ❌ | `INFO` | 日志级别 |
| `GROK_LOG_DIR` | ❌ | `logs` | 日志目录 |
| `GROK_RETRY_MAX_ATTEMPTS` | ❌ | `3` | 最大重试次数 |
| `GROK_RETRY_MULTIPLIER` | ❌ | `1` | 重试退避乘数 |
| `GROK_RETRY_MAX_WAIT` | ❌ | `10` | 重试最大等待秒数 |
| `GROK_RETRY_BUDGET_S` | ❌ | `45` | 单次调用重试总等待预算（秒），上游 Retry-After 超出预算时立即放弃；`0` 关闭 |
| `GROK_MAX_CONCURRENCY` | ❌ | `16` | 同时在途的 Grok 请求上限 |
| `GROK_BREAKER_THRESHOLD` | ❌ | `3` | 窗口内多少次 429 触发熔断 |
| `GROK_BREAKER_WINDOW_S` | ❌ | `60` | 熔断计数窗口（秒） |
| `GROK_BREAKER_COOLDOWN_S` | ❌ | `60` | 初始冷却时间（秒），探测失败后倍增 |
| `GROK_BREAKER_MAX_COOLDOWN_S` | ❌ | `300` | 冷却时间上限（秒） |
| `GROK_SEARCH_STYLE` | ❌ | `concise` | 输出风格：`concise` 或 `explanatory`（增加术语解释与类比，类比不挂引用） |
| `GROK_MODELS_CACHE_TTL_S` | ❌ | `600` | `/models` 列表缓存有效期（秒），用于校验 `web_search(model=...)`；获取失败不缓存，`get_config_info` 会刷新 |
| `GROK_VERIFY_IDS` | ❌ | `true` | 回答里的 arXiv 号与 DOI 是否自动核验 |
| `GROK_VERIFY_URLS` | ❌ | `true` | 被引用的 URL 是否做可达性与标题核验（请求前解析域名，非公网地址不访问；局限见 `web_search` 一节） |
| `GROK_VERIFY_TIMEOUT_S` | ❌ | `30` | 核验层总超时（秒）；超时未完成的条目以 `timed out` 列入 `unresolved`。所有 arXiv API 调用共用 3 秒间隔的闸门并缓存一小时 |
| `GROK_VERIFY_MAILTO` | ❌ | 空 | 核验请求的 User-Agent 联系邮箱（Crossref 礼貌池） |
| `GROK_PLANNING_TOOLS` | ❌ | `false` | 是否注册 `plan_*` 六个搜索规划工具 |
| `GROK_FETCH_MIN_CHARS` | ❌ | `2000` | Tavily 抓取结果低于此长度时回退 Firecrawl |
| `GROK_FETCH_MAX_CHARS` | ❌ | `40000` | `web_fetch` 单次默认返回字符数 |
| `GROK_FETCH_SNAPSHOT_TTL_S` | ❌ | `900` | `web_fetch` 分页快照的空闲有效期（秒），过期后需从 `offset=0` 重新抓取 |
| `TAVILY_EXTRACT_TIMEOUT_S` | ❌ | `30` | Tavily 提取超时（秒） |


### 验证安装

```bash
claude mcp list
```

🍟 显示连接成功后，我们**十分推荐**在 Claude 对话中输入 
```
调用 grok-search toggle_builtin_tools，关闭Claude Code's built-in WebSearch and WebFetch tools
```
工具将自动修改**项目级** `.claude/settings.json` 的 `permissions.deny`，一键禁用 Claude Code 官方的 WebSearch 和 WebFetch，从而迫使claude code调用本项目实现搜索！



## 三、MCP 工具介绍

<details>
<summary>默认提供 7 个 MCP 工具；设置 `GROK_PLANNING_TOOLS=true` 后另加 6 个 `plan_*` 规划工具（展开查看）</summary>

### `web_search` — AI 网络搜索

通过 Grok API 执行 AI 驱动的网络搜索，默认仅返回 Grok 的回答正文，并返回 `session_id` 以便后续获取信源。

`web_search` 输出不展开信源，仅返回 `sources_count`；信源会按 `session_id` 缓存在服务端，可用 `get_sources` 拉取。

参数 `instructions` 用于按次追加给搜索模型的要求（例如"至少覆盖 15 个不同域名"或"只列 arXiv 号和标题"）。返回值除 `content`/`sources_count` 外还有 `distinct_domains`（引用了多少个不同站点）；失败时有 `error`/`error_type`/`retry_after_s`，正文以 `[搜索失败]` 开头。

- **引用位置**：有原生引用时，相邻重复的引用标记（如 `[[1]](u)[[1]](u)`）会合并，`get_sources` 返回的 `start_index`/`end_index` 一律对应最终返回的正文。对不上的位置会重新定位（`position: relocated`）或去掉坐标（`position: removed`），上游原值保留在 `upstream_start_index`/`upstream_end_index`，统计见 `citation_positions`。
- **核验层 `verification`**：只检查标识符是否存在（arXiv / Crossref）、被引用链接能否访问，以及回答里写在 arXiv 号或 DOI 旁边的论文标题与登记标题是否一致（`title_checks`：`match` / `partial` / `possible_mismatch` / `not_stated`）。**不检查来源是否支持对应论断**，`claim_support` 固定为 `not_checked`；未检查的条目及原因（超出数量上限、URL 检查已关闭、非公网地址、非 http 链接）列在 `skipped`，汇总见 `summary`。
- **URL 检查会访问哪些地址**：请求前及每次重定向前先用系统解析器解析域名（含 hosts 文件与局域网名称），只要有一个地址不是公网地址就不访问并记入 `skipped`。回环、内网、链路本地、CGNAT（100.64.0.0/10）、`127.1`/`2130706433`/`0x7f.0.0.1` 这类数字写法，以及 IPv4 映射、6to4、NAT64 中内嵌的非公网地址都算在内。局限：检查时的解析与实际连接是两次独立解析，无法防御 DNS 重绑定；经 HTTP/SOCKS 代理请求时由代理决定真实目标；Clash/mihomo 的 fake-ip 模式下本地解析只得到 198.18.0.0/15，无法判断真实目标，这类 URL 会被跳过（可把相关域名加入 fake-ip-filter，或设 `GROK_VERIFY_URLS=false`）；本地解析不了的域名记为 `unresolved`（`dns lookup failed`）。arXiv / Crossref 查询访问固定域名，不经过此检查。
- **模型 `model`**：可带 CPA 思考后缀，如 `grok-4.7(high)`、`grok-4.7(8192)`、`grok-4.7(auto)`。与默认模型完全相同（含后缀）或基础名称相同时直接放行；其他模型按基础名称对照 `/models` 校验，请求时保留完整名称。列表获取失败时不拦截（`model_check.status = unverified`）。
- **额外信源 `extra_sources`**（0–20）：两个引擎都可用时 Tavily 分 ⌈n/2⌉、Firecrawl 分 ⌊n/2⌋，每个引擎多取 2 条备用；与 Grok 已引用的链接及引擎之间去重（忽略协议、`www.`、片段、末尾斜杠和跟踪参数），按轮流顺序选取；某个引擎失败或结果不足时，由另一个引擎补一次。结果附在正文末尾的 "Extra sources" 小节，过程见 `extra_sources_report`。
- **“没找到”的含义**：回答中的“本次检索未找到”只代表这次搜索没找到；需要确定结论时，直接到对应平台核实（例如用 `web_fetch` 打开仓库的 issue 搜索页）。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | string | ✅ | - | 搜索查询语句 |
| `platform` | string | ❌ | `""` | 聚焦平台（如 `"Twitter"`, `"GitHub, Reddit"`） |
| `model` | string | ❌ | `""` | 按次指定 Grok 模型 ID，可带思考后缀 |
| `extra_sources` | int | ❌ | `0` | 额外补充信源数量（0–20，Tavily/Firecrawl，0 关闭） |
| `instructions` | string | ❌ | `""` | 按次追加给搜索模型的要求 |
| `verify` | bool | ❌ | `true` | 是否附加核验层（同时受 `GROK_VERIFY_IDS` 控制） |

每次搜索自动注入本地日期、时间与时区上下文，以提升时效性搜索的准确度。

返回值（结构化字典）：
- `session_id`: 本次查询的会话 ID
- `content`: Grok 回答正文（无原生引用时剥离信源小节；有原生引用时保留原文，只合并重复标记）
- `sources_count` / `distinct_domains`: 已缓存的信源数量 / 不同站点数
- `citation_positions`、`verification`、`extra_sources_report`、`model_check`：见上文，按需出现

### `get_sources` — 获取信源

通过 `session_id` 获取对应 `web_search` 的全部信源。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `session_id` | string | ✅ | `web_search` 返回的 `session_id` |

返回值（结构化字典）：
- `session_id`
- `sources_count`
- `sources`: 信源列表（每项包含 `url`，可能包含 `title`/`description`/`provider`）

### `web_fetch` — 网页内容抓取

通过 Tavily Extract API 获取完整网页内容，返回 Markdown 格式。Tavily 失败或结果过短时自动降级到 Firecrawl Scrape；arXiv 摘要页直接走 arXiv API。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `url` | string | ✅ | - | 目标网页 URL |
| `max_chars` | int | ❌ | `0` | 本次最多返回的字符数，`0` 使用 `GROK_FETCH_MAX_CHARS` |
| `offset` | int | ❌ | `0` | 从第几个字符开始读；大于 0 时必须同时传 `snapshot_id` |
| `snapshot_id` | string | ❌ | `""` | 首屏返回的快照 ID，用于读取后续页 |

首行是元数据：`[web_fetch] source=… total_chars=… offset=… returned=… truncated=… [next_offset=… snapshot_id=… expires_in_s=…]`。页面超过一次能返回的长度时，服务端保存整页快照；后续用相同 `url` 加 `offset=next_offset` 与 `snapshot_id` 翻页，每一页都切自同一份内容。不带 `snapshot_id` 的 `offset>0` 会被拒绝；快照过期（默认空闲 900 秒）后返回 `error=snapshot_expired`，需从 `offset=0` 重新抓取。

失败时首行为 `[web_fetch] error=…`，随后逐行列出每个后端的错误类别（`auth`、`quota`、`rate_limit`、`timeout`、`proxy_config`、`network`、`upstream_error`、`bad_request`、`extract_failed`、`empty_content`、`disabled`、`not_configured` 等）、HTTP 状态和脱敏后的详情。

### `web_map` — 站点结构映射

通过 Tavily Map API 遍历网站结构，发现 URL 并生成站点地图。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `url` | string | ✅ | - | 起始 URL |
| `instructions` | string | ❌ | `""` | 自然语言过滤指令 |
| `max_depth` | int | ❌ | `1` | 最大遍历深度（1-5） |
| `max_breadth` | int | ❌ | `20` | 每页最大跟踪链接数（1-500） |
| `limit` | int | ❌ | `50` | 总链接处理数上限（1-500） |
| `timeout` | int | ❌ | `150` | 超时秒数（10-150） |
| `allow_external` | bool | ❌ | `true` | 是否包含站外链接（与 Tavily 默认一致）；设为 `false` 只保留起始站点 |
| `select_paths` | string[] | ❌ | - | 只保留路径匹配这些正则的 URL，如 `["/docs/.*"]` |
| `select_domains` | string[] | ❌ | - | 只在匹配这些正则的域名/子域名内遍历 |
| `exclude_paths` | string[] | ❌ | - | 跳过路径匹配这些正则的 URL |
| `exclude_domains` | string[] | ❌ | - | 跳过匹配这些正则的域名/子域名 |

`TAVILY_ENABLED=false` 时 `web_map` 不可用。结果较少或返回很快不一定是故障，可用上述参数与 `max_depth` 调整范围。

### `get_config_info` — 配置诊断

无需参数。显示所有配置状态（含 Tavily/Firecrawl 实际是否启用）、测试 Grok API 连接、返回响应时间和可用模型列表（API Key 自动脱敏），并刷新 `web_search(model=...)` 校验用的模型列表缓存。本地代理环境错误（如 `NO_PROXY` 中的 `[::1]`）会单独标为“本地代理环境错误”。

### `switch_model` — 模型切换

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model` | string | ✅ | 模型 ID（如 `"grok-4-fast"`, `"grok-2-latest"`） |

切换后配置持久化到 `~/.config/grok-search/config.json`，跨会话保持。

### `toggle_builtin_tools` — 工具路由控制

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `action` | string | ❌ | `"status"` | `"on"` 禁用官方工具 / `"off"` 启用官方工具 / `"status"` 查看状态 |
| `project_dir` | string | ❌ | `""` | 项目根目录；不传时按下述顺序确定 |

修改 `<项目目录>/.claude/settings.json` 的 `permissions.deny`，一键禁用 Claude Code 官方的 WebSearch 和 WebFetch。项目目录依次取：`project_dir` 参数 → 环境变量 `CLAUDE_PROJECT_DIR` → 从服务进程当前目录向上找到的、含 `.git` 或 `.claude` 的最近目录（不会取到用户主目录或文件系统根目录）。都找不到时不写任何文件，并提示传入 `project_dir`；配置文件无法解析时也不会覆盖。

### `plan_*` — 搜索规划（可选，默认不注册）

设置 `GROK_PLANNING_TOOLS=true` 后额外注册 6 个工具，按阶段记录复杂搜索的计划：`plan_intent` → `plan_complexity` → `plan_sub_query` → `plan_search_term` → `plan_tool_mapping` → `plan_execution`。它们只做记录，不发起搜索。
</details>

## 四、常见问题

<details>
<summary>
Q: 必须同时配置 Grok 和 Tavily 吗？
</summary>
A: Grok（`GROK_API_URL` + `GROK_API_KEY`）为必填，提供核心搜索能力。Tavily 和 Firecrawl 均为可选：配置 Tavily 后 `web_fetch` 优先使用 Tavily Extract，失败时降级到 Firecrawl Scrape；两者均未配置时 `web_fetch` 将返回配置错误提示。`web_map` 依赖 Tavily。设置 `TAVILY_ENABLED=false` 可以在保留 key 的情况下停用 Tavily：此时 `web_fetch` 只用 Firecrawl，`web_map` 不可用，`extra_sources` 只走 Firecrawl。
</details>

<details>
<summary>
Q: Grok API 地址需要什么格式？
</summary>
A: 需要 OpenAI 兼容格式的 API 地址（支持 `/chat/completions` 和 `/models` 端点）。如使用官方 Grok，需通过兼容 OpenAI 格式的镜像站访问。
</details>

<details>
<summary>
Q: 如何验证配置？
</summary>
A: 在 Claude 对话中说"显示 grok-search 配置信息"，将自动测试 API 连接并显示结果。
</details>

## 许可证

[MIT License](LICENSE)

---

<div align="center">

**如果这个项目对您有帮助，请给个 Star！**

[![Star History Chart](https://api.star-history.com/svg?repos=GuDaStudio/GrokSearch&type=date&legend=top-left)](https://www.star-history.com/#GuDaStudio/GrokSearch&type=date&legend=top-left)
</div>

## 变更记录

### v1.11.0（2026-10-16）

- `web_search(model=...)` 支持 CPA 思考后缀（如 `(high)`、`(8192)`）：默认模型及其同名档位直接放行，其他模型按基础名称校验、请求时保留完整名称；`/models` 列表缓存加有效期（`GROK_MODELS_CACHE_TTL_S`），失败不缓存，旧列表不再误拒新模型，`get_config_info` 会刷新缓存；缓存未命中触发的刷新同样遵守失败退避，并发的未命中共用一次刷新。
- `extra_sources` 修正配额：Tavily/Firecrawl 对半分配、与 Grok 引用及引擎之间去重、失败互相补位，过程写入 `extra_sources_report`。
- 默认输出风格改为 `concise`；提示词取消“每句话必须引用”，改为只给来源直接陈述的事实挂引用、推断明确标注、自写解释与类比不挂引用、“没找到”只表述为“本次检索未找到”。
- 核验层明确只做存在性与可达性检查（`scope`、`claim_support: not_checked`），新增论文标题比对 `title_checks`、未检查项 `skipped` 与 `summary`；URL 检查在请求前及每次重定向前解析域名，非公网地址不访问（局限见 `web_search` 一节）。
- `web_fetch` 改为快照分页（`snapshot_id`），翻页不再重新抓取；失败时按后端返回错误类别、HTTP 状态与脱敏详情。`TAVILY_ENABLED=false` 现在真正停用 Tavily（抓取、映射与额外信源）。
- 原生引用位置按最终正文校验：合并重复标记后重算位置，对不上的重新定位或去掉坐标（保留上游原值），统计见 `citation_positions`。
- `web_map` 新增 `allow_external`、`select_paths`、`select_domains`、`exclude_paths`、`exclude_domains`。
- `toggle_builtin_tools` 新增 `project_dir`，按参数 → `CLAUDE_PROJECT_DIR` → 最近的 `.git`/`.claude` 目录确定项目，找不到时不再写到文件系统根目录。
- 启动时修复 `NO_PROXY` 中 httpx 无法解析的方括号 IPv6 条目（如 `[::1]`），避免所有联网工具报 `Invalid port: ':1]'`；`get_config_info` 会把这类本地代理问题单独标出。
- 熔断器按基础模型名计数：同一模型的不同思考档位共用一套熔断状态，换档位不能绕过熔断。
- Grok 上游错误文本在工具输出、日志与熔断原因中统一脱敏（API Key、Bearer 令牌等）。
- README：安装地址改为本仓库，工具数量更正为默认 7 个 + 可选 6 个规划工具；英文 README 删除代码中并不存在的 `GUDA_*` 配置说明。

### v1.10.0（2026-09-17）

- 只读工具加 `readOnlyHint` 注解，Claude Code 可并行执行多次搜索与抓取。
- 新增并发信号量、按模型分键的熔断器（连续 429 或账号池耗尽即熔断，半开探测恢复）、按时间计算的重试预算。
- 上游流内 `event: error` 帧、429、空响应统一转为结构化错误字段，不再静默返回空正文；错误无条件写入日志。
- `web_search` 新增 `instructions`、`verify` 参数与 `distinct_domains`、`verification` 返回字段；`extra_sources` 结果附在正文末尾。
- 系统提示词拆为策略段与风格段，`GROK_SEARCH_STYLE` 可切换；新增引用完整性硬规则。
- `web_fetch` 新增长度门槛回退、`max_chars`/`offset` 分页与 arXiv 摘要页适配，Tavily 提取超时降为 30 秒。
- `plan_*` 六个规划工具改为 `GROK_PLANNING_TOOLS=true` 时才注册。
- `switch_model` 在 `GROK_MODEL` 环境变量存在时给出警告；`get_config_info` 增加熔断状态与服务器版本。
- 删除未被调用的 Grok fetch/describe/rank 代码路径及相关提示词；`build/`、`*.egg-info` 不再入库。
