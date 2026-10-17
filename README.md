![这是图片](./images/title.png)
<div align="center">

<!-- # Grok Search MCP -->

[English](./docs/README_EN.md) | 简体中文

**Grok-with-Tavily MCP，为 Claude Code 提供更完善的网络访问能力**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT) [![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/) [![FastMCP](https://img.shields.io/badge/FastMCP-2.11.0+-green.svg)](https://github.com/jlowin/fastmcp)

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
- **并发上限 + 按模型熔断**：`GROK_MAX_CONCURRENCY` 限制同时在途的 Grok 请求；连续 429 或账号池耗尽时按模型熔断（同一模型的不同思考档位，如 `(high)`/`(low)`/`(8192)`，共用一套熔断状态）并返回结构化错误，冷却后自动探测恢复。CLIProxyAPI / xAI 的账号池耗尽响应（503 `auth_unavailable`、429 `model_cooldown`、`free-usage-exhausted` 等）第一次就会熔断；排队等并发名额的调用拿到名额后会再检查一次熔断状态
- **失败必有回声**：上游流内错误帧、429、空响应、超时、网络故障和中途断开的流都会变成 `error`/`error_type`/`retry_after_s` 字段，不再静默返回空正文；被上游截断或过滤的回答标记 `incomplete`。工具失败按设计以普通结果返回（MCP `isError=false`），请以 `error` 字段或首行判断
- **搜索时长有上限**：`GROK_READ_TIMEOUT_S`（单次读取）与 `GROK_SEARCH_TIMEOUT_S`（整次调用，含排队、重试与等待）保证网关卡住时搜索也会结束并返回 `timeout`
- **按次换模型**：`web_search(model=...)` 支持 CPA 风格的思考后缀（如 `grok-4.7(high)`、`grok-4.7(8192)`），按基础名称对照 `/models` 校验、请求时保留完整名称；模型列表缓存有有效期，获取失败不缓存
- **额外信源双引擎分配**：`extra_sources` 在 Tavily 与 Firecrawl 之间对半分配，与 Grok 已引用的链接及引擎之间去重，一方失败由另一方补位，过程写入 `extra_sources_report`
- **核验层（不是事实核验）**：回答里的 arXiv 号、DOI 经 arXiv API / Crossref / doi.org 登记处 / DataCite 确认存在（包括 arXiv 自己的 `10.48550/arXiv.<id>` DOI、Zenodo 以及万方/ISTIC 等其他登记机构的 DOI），Grok 引用的 URL 确认可访问，回答中写出的论文标题与登记标题自动比对（`title_checks`）；**不检查来源是否支持对应论断**（`claim_support: not_checked`）。`unresolved` 表示登记处或网站明确答复不存在（应视为可疑）；`lookup_failed` 表示没拿到答复（限流、5xx、超时），不能当作反对引用的证据；没检查的条目连同原因列在 `skipped`。只核验 Grok 引用的来源，不核验 `extra_sources` 的结果；URL 请求前先确认目标地址都是公网地址，Clash/mihomo fake-ip 环境下先经 DNS-over-HTTPS 查出真实地址（局限见 `web_search` 一节）
- **抓取质量门槛与快照分页**：Tavily 结果过短时回退 Firecrawl 取较长者；arXiv 摘要页直接走 arXiv API（带版本号的链接返回该版本）；超长页面首屏返回 `snapshot_id`，后续页按 `offset` + `snapshot_id` 固定读取同一份内容，不会错页；CJK 为主的页面默认每页 20,000 字符；页面本身返回 HTTP ≥ 400 时算作失败（`page_status=`），Firecrawl 被重定向时首行给出 `final_url=`/`title=`，登录墙与反爬验证页标记 `warning=interstitial`；非 http(s) 链接和本机/局域网 IP 直接在本地拒绝（`error=invalid_url`），不调用付费后端；失败时逐个列出各后端的错误类别、HTTP 状态和脱敏详情
- **输出风格与引用规则**：默认 `concise`，`explanatory` 增加术语解释与类比。两种风格都只给来源直接陈述的事实挂引用；推断须标注“Inference:/推断：”，自写的解释与类比不挂引用；查不到的东西只表述为“本次检索未找到”；并附数字原样引用、不跨来源拼区间的硬规则
- 一键禁用 Claude Code 官方 WebSearch/WebFetch，强制路由到本工具
- 智能重试（任何状态码都解析 Retry-After + 指数退避；确定性错误不重试；Cloudflare 520/521/523/530 重试一次，522/524 按超时处理）
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

> **固定版本（推荐）**：`@main` 是分支，uvx 每次启动都会到 GitHub 解析它：总是运行主分支的最新代码，修复来得最快；代价是每次启动多花几秒，GitHub 不可达时服务器无法启动，而且未经你审阅的改动（包括不兼容的改动）会在下次启动时直接生效。日常使用建议固定到发布标签或提交，把上面的 `git+https://github.com/Cody-Xue/GrokSearch@main` 换成 `git+https://github.com/Cody-Xue/GrokSearch@v1.12.0`（以仓库实际存在的标签为准，打标签之前可用 `@<提交 SHA>`）：每次运行的都是同一份代码，可复现，缓存后启动不再依赖 GitHub；代价是升级要手动改版本号。两种写法都可以，下文示例沿用 `@main`。

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
| `GROK_API_URL` | ✅ | - | Grok API 地址（OpenAI 兼容格式）；首尾空白与末尾的 `/` 会被忽略（`https://host/v1/` 也可用） |
| `GROK_API_KEY` | ✅ | - | Grok API 密钥；首尾空白会被忽略 |
| `GROK_MODEL` | ❌ | `grok-4-fast` | 默认模型（设置后优先于 `~/.config/grok-search/config.json` 与 `switch_model`；空值视为未设置） |
| `TAVILY_API_KEY` | ❌ | - | Tavily API 密钥（用于 web_fetch / web_map） |
| `TAVILY_API_URL` | ❌ | `https://api.tavily.com` | Tavily API 地址 |
| `TAVILY_ENABLED` | ❌ | `true` | 是否启用 Tavily；`false` 时 `web_fetch`、`web_map`、`extra_sources` 都不会调用 Tavily（即使配置了 key） |
| `FIRECRAWL_API_KEY` | ❌ | - | Firecrawl API 密钥（Tavily 失败时托底） |
| `FIRECRAWL_API_URL` | ❌ | `https://api.firecrawl.dev/v2` | Firecrawl API 地址 |
| `GROK_DEBUG` | ❌ | `false` | 调试模式（日志会记录完整的提示词与回答） |
| `GROK_LOG_LEVEL` | ❌ | `INFO` | 日志级别 |
| `GROK_LOG_DIR` | ❌ | `logs` | 日志目录；相对路径位于 `~/.config/grok-search` 下，不可用时改用私有临时目录（绝不写到工作目录）。每天一个文件 `grok_search_YYYYMMDD.log`，运行中的服务器过午夜自动换文件 |
| `GROK_LOG_RETENTION_DAYS` | ❌ | `7` | 保留的每日日志文件个数（含当天），更旧的在启动和换日时删除 |
| `GROK_RETRY_MAX_ATTEMPTS` | ❌ | `3` | 最大重试次数 |
| `GROK_RETRY_MULTIPLIER` | ❌ | `1` | 重试退避乘数 |
| `GROK_RETRY_MAX_WAIT` | ❌ | `10` | 重试最大等待秒数 |
| `GROK_RETRY_BUDGET_S` | ❌ | `45` | 单次调用重试总等待预算（秒），上游 Retry-After 超出预算时立即放弃；`0` 关闭。只计重试之间的等待，整次调用由 `GROK_SEARCH_TIMEOUT_S` 限定 |
| `GROK_READ_TIMEOUT_S` | ❌ | `180` | 等待 Grok 回答时单次读取允许的最长静默（秒，最小 5）。CLIProxyAPI 这类缓冲型网关在回答生成完之前不发送任何字节，所以它也限定了单次尝试的时长。读取超时最多重试一次，且只在剩余的 `GROK_SEARCH_TIMEOUT_S` 还够再等一整次时重试 |
| `GROK_SEARCH_TIMEOUT_S` | ❌ | `300` | 一次 `web_search` 的 Grok 调用总时限（秒）：等待并发名额、每次尝试与每次重试等待都计入；`0` 关闭。超时返回 `error_type: timeout` |
| `GROK_MAX_CONCURRENCY` | ❌ | `16` | 同时在途的 Grok 请求上限 |
| `GROK_BREAKER_THRESHOLD` | ❌ | `3` | 窗口内多少次 429 触发熔断 |
| `GROK_BREAKER_WINDOW_S` | ❌ | `60` | 熔断计数窗口（秒） |
| `GROK_BREAKER_COOLDOWN_S` | ❌ | `60` | 初始冷却时间（秒），探测失败后倍增 |
| `GROK_BREAKER_MAX_COOLDOWN_S` | ❌ | `300` | 冷却时间上限（秒） |
| `GROK_SEARCH_STYLE` | ❌ | `concise` | 输出风格：`concise` 或 `explanatory`（增加术语解释与类比，类比不挂引用） |
| `GROK_MODELS_CACHE_TTL_S` | ❌ | `600` | `/models` 列表缓存有效期（秒），用于校验 `web_search(model=...)`；获取失败不缓存，`get_config_info` 会刷新 |
| `GROK_VERIFY_IDS` | ❌ | `true` | 是否核验回答里的 arXiv 号与 DOI（只管标识符查询；URL 检查由 `GROK_VERIFY_URLS` 控制；两者都为 `false` 时不附加核验层） |
| `GROK_VERIFY_URLS` | ❌ | `true` | Grok 引用的 URL 是否做可达性与标题核验，与 `GROK_VERIFY_IDS` 相互独立（请求前解析域名，非公网地址不访问；局限见 `web_search` 一节） |
| `GROK_VERIFY_TIMEOUT_S` | ❌ | `30` | 核验层总超时（秒）；到时仍未完成的查询列入 `lookup_failed`（原因 `timed out`），预算的最后 5 秒（至多 20%）留给 arXiv 号的 DataCite 兜底查询。所有 arXiv API 调用（含 `web_fetch`）共用一道闸门：同一时间只有一个连接，上一个请求结束 3 秒后才发下一个；429/503 触发全进程冷却（按 Retry-After，否则 60 秒），期间 arXiv 查询直接列入 `lookup_failed`；同一编号的并发查询共用一次请求，读取超时 10 秒。核验查询与 `web_fetch` 的摘要页都缓存一小时，`web_fetch` 抓过的论文核验时不再查询 arXiv（核验缓存不含摘要，核验后再抓取仍会请求一次） |
| `GROK_VERIFY_MAILTO` | ❌ | 空 | 联系邮箱，只发给登记处（arXiv / Crossref / doi.org / DataCite），不发给被引用的网站或 DoH 服务；设置后进入 Crossref 礼貌池，每个进程的 Crossref 并发从 1 提到 3。强烈建议设置 |
| `GROK_VERIFY_FAKE_IP` | ❌ | `doh` | 本地解析只得到 fake-ip 地址（198.18.0.0/15）时的处理：`doh` 经配置的代理用 DNS-over-HTTPS 查出真实地址，全部是公网地址才经代理访问；`skip` 不访问（列入 `skipped`）；`allow` 不检查，直接经代理访问 |
| `GROK_VERIFY_DOH_URL` | ❌ | `https://cloudflare-dns.com/dns-query` | `GROK_VERIFY_FAKE_IP=doh` 使用的 DoH JSON 接口（Cloudflare/Google JSON 格式，如 `https://dns.google/resolve`），经配置的代理访问 |
| `GROK_PLANNING_TOOLS` | ❌ | `false` | 是否注册 `plan_*` 六个搜索规划工具 |
| `GROK_FETCH_MIN_CHARS` | ❌ | `2000` | Tavily 抓取结果低于此长度时回退 Firecrawl |
| `GROK_FETCH_MAX_CHARS` | ❌ | `40000` | `web_fetch` 单次默认返回字符数 |
| `GROK_FETCH_MAX_CHARS_CJK` | ❌ | `20000` | 页面中 CJK 字符超过 30% 时 `web_fetch` 的默认每页字符数（不超过 `GROK_FETCH_MAX_CHARS`），避免单次结果超过 Claude Code 约 25k token 的 MCP 输出上限 |
| `GROK_FETCH_ARXIV_BUDGET_S` | ❌ | `8` | `web_fetch` 走 arXiv API 的总时限（秒，含 3 秒闸门与一次重试）；限流、超时、网络或 5xx 失败后 2 分钟内跳过 arXiv API，直接用 Tavily/Firecrawl（`attempts=arxiv-api:cooldown`） |
| `GROK_FETCH_SNAPSHOT_TTL_S` | ❌ | `900` | `web_fetch` 分页快照的空闲有效期（秒），过期后需从 `offset=0` 重新抓取 |
| `TAVILY_EXTRACT_TIMEOUT_S` | ❌ | `30` | Tavily 提取超时（秒） |

布尔值接受 `true`/`1`/`yes`/`on` 与 `false`/`0`/`no`/`off`（不区分大小写）；空值保持默认；无法识别的值（包括无法解析的数字）同样保持默认，并列入 `get_config_info` 的 `config_warnings`。例如 `TAVILY_ENABLED=disabled` 不会停用 Tavily，要写 `false`。


### 验证安装

```bash
claude mcp list
```

🍟 显示连接成功后，我们**十分推荐**在 Claude 对话中输入 
```
调用 grok-search toggle_builtin_tools，关闭Claude Code's built-in WebSearch and WebFetch tools
```
工具（`action=on`）将自动修改**项目级** `.claude/settings.json` 的 `permissions.deny`，一键禁用 Claude Code 官方的 WebSearch 和 WebFetch，从而迫使claude code调用本项目实现搜索！



## 三、MCP 工具介绍

<details>
<summary>默认提供 7 个 MCP 工具；设置 `GROK_PLANNING_TOOLS=true` 后另加 6 个 `plan_*` 规划工具（展开查看）</summary>

所有工具的失败都以普通结果返回（MCP `isError=false`，属有意设计），这样模型能读到结构化的错误字段和已经拿到的部分结果：`web_search` 看 `error`/`error_type` 字段与 `[搜索失败]` 前缀，`web_fetch`/`web_map` 看首行的 `error=`。

### `web_search` — AI 网络搜索

通过 Grok API 执行 AI 驱动的网络搜索，默认仅返回 Grok 的回答正文，并返回 `session_id` 以便后续获取信源。

`web_search` 输出不展开信源，只返回计数；信源会按 `session_id` 缓存在服务端，可用 `get_sources` 拉取。`sources_count` / `distinct_domains` 只统计 Grok 引用的来源（不含 `extra_sources`）；`extra_sources_count` 是额外信源条数，`total_sources_count` 是两者之和（即 `get_sources` 列表的长度）。

参数 `instructions` 用于按次追加给搜索模型的要求（例如"至少覆盖 15 个不同域名"或"只列 arXiv 号和标题"）。失败时有 `error`/`error_type`/`retry_after_s`（按需还有 `status`、`exhausted`），正文一律以 `[搜索失败]` 开头；失败前已找到的额外信源仍附在后面，指定了 `model` 时 `model_check` 也照常返回。

- **引用位置**：有原生引用时，相邻重复的引用标记（如 `[[1]](u)[[1]](u)`）会合并，`get_sources` 返回的 `start_index`/`end_index` 一律对应最终返回的正文，按 Unicode 码点计（不是 UTF-16 单元）。对不上的位置会重新定位（`position: relocated`）或去掉坐标（`position: removed`），上游原值保留在 `upstream_start_index`/`upstream_end_index`，统计见 `citation_positions`。
- **上游错误与重试**：
  - `error_type` 取值：`config`（`GROK_API_URL`/`GROK_API_KEY` 缺失或无效）、`proxy_config`（本地代理环境错误）、`invalid_model`、`rate_limit`（`exhausted: true` 表示该模型的账号池被封，等待 `retry_after_s` 或换模型）、`circuit_open`、`timeout`（`GROK_SEARCH_TIMEOUT_S` 内没拿到完整回答、读取超时，或 Cloudflare 522/524）、`network`（连接、代理或 TLS 故障，消息不会为空）、`truncated_stream`（流在既没有 `finish_reason` 也没有 `[DONE]` 时关闭，会重试）、`empty_response`、`auth`（网关没有该模型可用的凭据，如 CPA `auth_not_found`，不重试）、`http_<状态码>`、`unknown`，以及上游流内透传的错误码（如 `invalid_request_error`、`model_not_found`、`server_error`、`upstream_error`）。
  - `rate_limit` + `exhausted: true` 也涵盖 CLIProxyAPI 的 503 `auth_unavailable`/“no auth available”、429 `model_cooldown`/“cooling down” 和 xAI 的 `free-usage-exhausted`，第一次就会熔断。`retry_after_s` 取自任何状态码的 Retry-After 头，或 CPA 错误帧里的 `reset_seconds`。
  - 账号池耗尽且给出的恢复时间在 `GROK_RETRY_BUDGET_S` 与总时限之内时，只等一次再试；否则立即失败并给出 `retry_after_s`，不盲目重试。确定性错误（`retryable: false`、`authentication_error`、`auth_not_found`）不重试。
  - Cloudflare：520/521/523/530 重试一次；522/524 按 `timeout` 报告，524 不会重跑（这次搜索已经跑了约 100 秒，可缩小问题或换更轻的模型）。Cloudflare 错误页只给出名称，不引用 HTML。
  - 熔断器：排队等 `GROK_MAX_CONCURRENCY` 名额的调用拿到名额后会再检查一次熔断状态；半开探测名额只能由探测调用自己释放。
- **核验层 `verification`**：检查回答里的 arXiv 号 / DOI 和 Grok 引用的 URL（`extra_sources` 的结果不检查），只看是否存在、能否访问，以及回答里写在标识符旁边的论文标题与登记标题是否一致（`title_checks`：`match` / `partial` / `possible_mismatch` / `not_stated`）。**不检查来源是否支持对应论断**，`claim_support` 固定为 `not_checked`。
  - arXiv 号包括旧式编号（如 `hep-th/9711200`）和 `10.48550/arXiv.<id>` 形式的 DOI；DOI 先查 Crossref，Crossref 返回 404 时由 doi.org 登记处判断是否存在、由 DataCite 提供标题。
  - `unresolved`（视为可疑）：登记处答复不存在、页面返回 404/410 或其他 4xx、域名不存在（NXDOMAIN）。401/403/451 附注“拒绝自动访问，页面可能仍存在”。
  - `lookup_failed`（结果未知，不能当作反对引用的证据）：429、5xx、超时、网络错误、登记处冷却、人机验证页、核验总时限到期；原因里写明 HTTP 状态。`summary` 中也有 `lookup_failed` 计数。
  - `skipped`：未检查的条目及原因（超出数量上限、URL 检查已关闭、标识符检查已关闭（`GROK_VERIFY_IDS=false`）、非公网地址、非 http 链接、arxiv.org/doi.org 链接里没有可识别的编号、超过 400 字符或含 `.`/`..` 路径段的 DOI 形文本；回答里写的 DOI 模板如 `10.48550/arXiv.<id>`、`10.1145/3XXXXXX.3XXXXXX` 排在最后，最多列 20 条）。
  - `title_checks` 只使用为该标识符写的标题：离它最近的明确标题，不会取到另一篇论文的标题；标识符位于行首时上一行也算；作者-年份文字、期刊名、以冒号结尾的标签和不足 3 个词的片段不算标题。
- **URL 检查会访问哪些地址**：请求前及每次重定向前先用系统解析器解析域名（含 hosts 文件与局域网名称），只要有一个地址不是公网地址就不访问并记入 `skipped`。回环、内网、链路本地、CGNAT（100.64.0.0/10）、`127.1`/`2130706433`/`0x7f.0.0.1` 这类数字写法，以及 IPv4 映射、6to4、NAT64 中内嵌的非公网地址都算在内。Clash/mihomo fake-ip 模式下本地解析只得到 198.18.0.0/15：`GROK_VERIFY_FAKE_IP=doh`（默认）时经配置的代理用 DNS-over-HTTPS 查出真实地址，同样要求全部是公网地址，再经代理访问；`skip`、`allow` 见环境变量表。重定向逐跳跟随，从不读取重定向的响应体；正文最多读取解码后的 64 KB（gzip/deflate 限量解压，其他编码不解码）。经代理得到的 502/503/504 记为 `lookup_failed`（来自代理或网站）。本地解析不了的域名记为 `unresolved`（`dns lookup failed`）。局限：检查时的解析与实际连接是两次独立解析，无法防御 DNS 重绑定；经 HTTP/SOCKS 代理请求时由代理决定真实目标（代理可能使用不同的、例如分流的 DNS）；DoH 模式会让 DoH 服务商看到被引用的域名。登记处（arXiv / Crossref / doi.org / DataCite）与 DoH 查询访问固定域名，不经过此检查。
- **模型 `model`**：可带 CPA 思考后缀，如 `grok-4.7(high)`、`grok-4.7(8192)`、`grok-4.7(auto)`。与默认模型完全相同（含后缀）或基础名称相同时直接放行；其他模型按基础名称对照 `/models` 校验，请求时保留完整名称。列表获取失败时不拦截（`model_check.status = unverified`）。OpenRouter 地址下按次指定的模型同样会自动加 `:online`，也可以显式写 `model:online`。
- **额外信源 `extra_sources`**（0–20，超出范围由参数校验拒绝）：两个引擎都可用时 Tavily 分 ⌈n/2⌉、Firecrawl 分 ⌊n/2⌋，每个引擎多取 2 条备用；与 Grok 已引用的链接及引擎之间去重（忽略协议、`www.`、片段、末尾斜杠和跟踪参数），按轮流顺序选取；某个引擎失败或结果不足时，由另一个引擎补一次（补取数量为缺口 + 2 条余量 + 已遇到的重复条数，最多 20）。结果附在正文末尾的 "Extra sources" 小节，每条一行纯文本（标题、链接、引擎、最多 300 字符的摘要）；过程见 `extra_sources_report`。
- **“没找到”的含义**：回答中的“本次检索未找到”只代表这次搜索没找到；需要确定结论时，直接到对应平台核实（例如用 `web_fetch` 打开仓库的 issue 搜索页）。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | string | ✅ | - | 搜索查询语句 |
| `platform` | string | ❌ | `""` | 聚焦平台（如 `"Twitter"`, `"GitHub, Reddit"`） |
| `model` | string | ❌ | `""` | 按次指定 Grok 模型 ID，可带思考后缀 |
| `extra_sources` | int | ❌ | `0` | 额外补充信源数量（0–20，Tavily/Firecrawl，0 关闭；计入 `extra_sources_count`，不计入 `sources_count`） |
| `instructions` | string | ❌ | `""` | 按次追加给搜索模型的要求 |
| `verify` | bool | ❌ | `true` | 是否附加核验层；标识符查询受 `GROK_VERIFY_IDS` 控制，URL 检查受 `GROK_VERIFY_URLS` 控制，两者都关闭时不核验 |

每次搜索自动注入本地日期、时间与时区上下文，以提升时效性搜索的准确度。

返回值（结构化字典）：
- `session_id`: 本次查询的会话 ID
- `content`: Grok 回答正文。没有原生引用时保留全文，只剥离信源标题下的纯链接列表；有原生引用时保留原文，只合并重复标记。回答被截断时紧接正文附一行 `[回答不完整] …` 说明，"Extra sources" 小节在其后
- `sources_count` / `distinct_domains`: Grok 引用的信源数量 / 不同站点数（没有原生引用时，按正文中的引用标记、Markdown 链接和 URL 统计）
- `extra_sources_count` / `total_sources_count`: 额外信源数量 / 两者合计
- `incomplete` / `finish_reason` / `native_finish_reason`: 仅在上游提前结束生成时出现（`finish_reason` 为 `length` 或 `content_filter`，或 `native_finish_reason` 不是 `stop`），表示回答不完整
- `usage`: 上游报告用量时给出 `{prompt_tokens, completion_tokens, reasoning_tokens}`
- `dropped_events`: 仅在有 SSE 帧无法解码时出现
- `error` / `error_type` / `retry_after_s` / `status` / `exhausted`: 仅失败时出现，见上文
- `citation_positions`、`verification`、`extra_sources_report`、`model_check`：见上文，按需出现

### `get_sources` — 获取信源

通过 `session_id` 获取对应 `web_search` 的全部信源。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `session_id` | string | ✅ | `web_search` 返回的 `session_id` |

返回值（结构化字典）：
- `session_id`
- `sources_count`: 列表总条数（即 `web_search` 的 `total_sources_count`）
- `cited_sources_count` / `extra_sources_count`: 其中 Grok 引用的 / 额外信源的条数
- `sources`: 信源列表，先列 Grok 引用的（`provider: grok`），再列 `extra_sources` 的结果（`provider: tavily` / `firecrawl`，Grok 并未引用）。每项包含 `url`，可能包含 `title`/`description`/`provider`/`citations`。`citations` 中的 `start_index`/`end_index` 是对 `web_search` 返回正文的 Unicode 码点偏移；额外信源的 `description` 是折叠空白后最多 500 字符的摘要（截断时以 `…` 结尾），全文请用 `web_fetch`

### `web_fetch` — 网页内容抓取

通过 Tavily Extract API 获取完整网页内容，返回 Markdown 格式。Tavily 失败或结果过短时自动降级到 Firecrawl Scrape；arXiv 摘要页直接走 arXiv API，带版本号的链接（如 `/abs/1706.03762v1`）返回该版本，front matter 中有 `version: v1`（未指定版本时为 `vN (latest)`）。

所有后端都在云端运行，因此调用前先在本地拒绝它们访问不到的 URL：没有 http(s) 协议、没有主机名、`localhost`/`*.localhost`，以及回环/内网/链路本地/CGNAT 的 IP 字面量。结果为 `[web_fetch] error=invalid_url reason=<malformed|missing_scheme|unsupported_scheme|missing_host|local_address>`，不调用任何后端；此检查不解析域名。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `url` | string | ✅ | - | 完整的公网 http(s) URL |
| `max_chars` | int | ❌ | `0` | 本次最多返回的字符数，`0` 使用 `GROK_FETCH_MAX_CHARS`，页面 CJK 字符超过 30% 时使用 `GROK_FETCH_MAX_CHARS_CJK` |
| `offset` | int | ❌ | `0` | 从第几个字符开始读；大于 0 时必须同时传 `snapshot_id` |
| `snapshot_id` | string | ❌ | `""` | 首屏返回的快照 ID，用于读取后续页 |

首行是元数据：`[web_fetch] source=… total_chars=… offset=… returned=… truncated=… [next_offset=… snapshot_id=… expires_in_s=…] [attempts=…] [warning=interstitial] [final_url=… title="…"]`。页面超过一次能返回的长度时，服务端保存整页快照；后续用相同 `url` 加 `offset=next_offset` 与 `snapshot_id` 翻页，每一页都切自同一份内容。不带 `snapshot_id` 的 `offset>0` 会被拒绝；快照过期（默认空闲 900 秒）后返回 `error=snapshot_expired`，需从 `offset=0` 重新抓取。

Firecrawl 被重定向时首行出现 `final_url` 与 `title`（`title` 总在最后，且是 JSON 引号字符串）。`warning=interstitial` 加一行 `注意:` 表示内容是登录墙或反爬验证页（如知乎“安全验证”、Cloudflare “Just a moment...”），不是所请求的页面；这类页面不会替换 Tavily 的抽取结果。

失败时首行为 `[web_fetch] error=…`，随后逐行列出每个后端的错误类别（`auth`、`quota`、`rate_limit`、`timeout`、`proxy_config`、`network`、`upstream_error`、`bad_request`、`extract_failed`、`empty_content`、`disabled`、`not_configured` 等）、HTTP 状态和脱敏后的详情。页面本身返回 HTTP ≥ 400（404、403、5xx…）时该后端算作失败，详情为 `target page answered HTTP <状态码>`，类别为 `extract_failed`（429 为 `rate_limit`，5xx 为 `upstream_error`），首行加 `page_status=<状态码>`。

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

失败时首行为 `[web_map] error=<类别> provider=tavily [status=<HTTP 状态>]`，随后是中文说明和一行脱敏详情；成功时输出不变（JSON），且不再在 `structuredContent` 中重复一份。`file://`、`localhost` 和本机/局域网 IP 在本地拒绝（`[web_map] error=invalid_url reason=…`）；`docs.example.com` 这样的裸域名仍交给 Tavily。

### `get_config_info` — 配置诊断

无需参数。显示所有配置状态（含 Tavily/Firecrawl 实际是否启用）、测试 Grok API 连接、返回响应时间和可用模型列表（API Key 自动脱敏），并刷新 `web_search(model=...)` 校验用的模型列表缓存。本地代理环境错误（如 `NO_PROXY` 中的 `[::1]`）会单独标为“本地代理环境错误”。

- `GROK_MODEL_SOURCE` 为 `env` | `switch_model` | `config` | `default`，`GROK_MODEL_NOTE` 说明 `switch_model` 的选择能保持多久。
- 还会列出 `GROK_CONFIG_FILE`、`GROK_LOG_RETENTION_DAYS`、`GROK_RETRY_MULTIPLIER`、`GROK_RETRY_MAX_WAIT`、`GROK_READ_TIMEOUT_S`、`GROK_SEARCH_TIMEOUT_S`、各项 `GROK_VERIFY_*`（含 `GROK_VERIFY_FAKE_IP`、`GROK_VERIFY_DOH_URL`）与 `GROK_FETCH_*`；`config_warnings` 列出无法识别的环境变量值以及配置文件、日志目录的问题。
- API Key 最多显示四分之一（不足 24 个字符时最多显示末 4 位）；URL 中的凭据（用户信息、查询参数值）显示为 `***`。
- 连接测试遇到传输错误（连接重置或拒绝、代理隧道关闭）时重试一次（`retried_after`/`attempts`）；空错误信息会写成 `proxy tunnel closed or upstream unreachable (ConnectError)`；Cloudflare 错误页只给出名称。

### `switch_model` — 模型切换

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model` | string | ✅ | 模型 ID（如 `"grok-4-fast"`, `"grok-2-latest"`） |

- 名称会去掉首尾空白，并像 `web_search(model=...)` 一样对照 `/models` 校验：允许 `(high)` 这类思考后缀；空名、超过 200 个字符或不在列表中的名称会被拒绝并给出相近建议，不做任何修改。`/models` 不可达时照常切换，但附警告（未校验）。
- 选择保存到 `~/.config/grok-search/config.json`；该目录不可写时改存私有临时目录（重启后不保留，并给出警告）。
- 若 MCP 配置的 `env` 中设置了 `GROK_MODEL`，它优先：切换只对当前进程有效，重启后仍以 `GROK_MODEL` 为准。要永久更换模型，请修改 `GROK_MODEL`。

### `toggle_builtin_tools` — 工具路由控制

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `action` | string | ❌ | `"status"` | `"on"`（别名 `"block"`）禁用官方工具 / `"off"`（别名 `"allow"`）恢复官方工具 / `"status"` 查看状态；其他值（包括有歧义的 `enable`/`disable`）会被拒绝 |
| `project_dir` | string | ❌ | `""` | 项目根目录；不传时按下述顺序确定 |

修改 `<项目目录>/.claude/settings.json` 的 `permissions.deny`，一键禁用 Claude Code 官方的 WebSearch 和 WebFetch。项目目录依次取：`project_dir` 参数 → 环境变量 `CLAUDE_PROJECT_DIR` → 从服务进程当前目录向上找到的、含 `.git` 或 `.claude` 的最近目录（不会取到用户主目录或文件系统根目录）。都找不到时不写任何文件，并提示传入 `project_dir`；配置文件无法解析时也不会覆盖。

`.claude/settings.local.json` 与 `~/.claude/settings.json` 中的拒绝规则只读取、不修改，列在 `denied_elsewhere`，合并后的结果见 `effective`；执行 `off` 时如果这些文件仍屏蔽着工具会给出提示。指向项目之外的 `.claude` 目录或设置文件符号链接会被拒绝；写入采用原子替换；只读的 `settings.json` 不会被修改。（旧版未写入文档的 `disable` 实际会恢复官方工具，现在会被拒绝。）

### `plan_*` — 搜索规划（可选，默认不注册）

设置 `GROK_PLANNING_TOOLS=true` 后额外注册 6 个工具，按阶段记录复杂搜索的计划：`plan_intent` → `plan_complexity` → `plan_sub_query` → `plan_search_term` → `plan_tool_mapping` → `plan_execution`。它们只做记录，不发起搜索。输入会先校验（枚举值、`level` 1–3、数值范围、置信度 0–1），出错时返回 `{error, phase, invalid_fields}` 且不记录任何内容；`plan_intent`、`plan_complexity`、`plan_execution` 覆盖已有阶段时需要 `is_revision=true`。
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
A: 需要 OpenAI 兼容格式的 API 地址（支持 `/chat/completions` 和 `/models` 端点）。如使用官方 Grok，需通过兼容 OpenAI 格式的镜像站访问。首尾空白和末尾的 `/` 会被忽略：v1.12.0 之前，末尾带 `/` 时 `get_config_info` 显示连接成功，但每次 `web_search` 都返回 HTTP 404。
</details>

<details>
<summary>
Q: 报错 `proxy tunnel closed or upstream unreachable (ConnectError)` 是什么意思？
</summary>
A: 本地代理（`HTTP_PROXY`/`HTTPS_PROXY`）关闭了隧道，或者连不上网关。请检查代理是否在运行、代理规则是否放行网关域名，以及 `GROK_API_URL` 是否可达。
</details>

<details>
<summary>
Q: 工具失败时 MCP 结果的 `isError` 为什么是 `false`？
</summary>
A: 有意设计。失败以普通结果返回，模型才能读到结构化的错误字段和已经拿到的部分结果（如额外信源）。`web_search` 请看 `error`/`error_type` 与 `[搜索失败]` 前缀，`web_fetch`/`web_map` 请看首行的 `error=`。
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

### v1.12.0（未发布）

**web_search 与 Grok 上游**

- 修复：`GROK_API_URL` 末尾带 `/` 时每次 `web_search` 都返回 HTTP 404，而 `get_config_info` 显示连接成功；URL 与 Key 的首尾空白也会被忽略。
- 新增 `GROK_READ_TIMEOUT_S` 与 `GROK_SEARCH_TIMEOUT_S`：网关卡住时，一次搜索（及其并发名额）不再被占用约 12 分钟甚至无限期。
- 修复：超时与传输故障报告为 `timeout`/`network` 并附有用的说明，不再是 `unknown: ReadTimeout` 或空白的 `ConnectError`。
- 修复：CLIProxyAPI 账号池耗尽（503 `auth_unavailable`、429 `model_cooldown`、xAI `free-usage-exhausted`）会设置 `exhausted=true` 与 `retry_after_s`，第一次就熔断；给出的恢复时间在预算内时只等一次。网关的确定性凭据错误不再重试（`error_type: auth`）。
- 修复：503 及其他任何状态码的 Retry-After 都会被采用。
- 修复：Cloudflare 52x 错误按名称报告；522/524 归为 `timeout`；520/521/523/530 重试一次。
- 修复：SSE 解析不再丢弃含 U+0085/U+2028/U+2029 的帧，并按规范拼接多行 `data:` 事件；查询中的孤立 UTF-16 代理字符不再导致请求崩溃。
- 新增 `incomplete`/`finish_reason`/`native_finish_reason`/`usage` 字段；既没有 `finish_reason` 也没有 `[DONE]` 就关闭的流按 `truncated_stream` 重试。
- 修复熔断器：排队的调用拿到并发名额后再检查熔断；只有探测调用本身能释放半开探测名额。
- `sources_count`/`distinct_domains` 只统计 Grok 引用的来源，新增 `extra_sources_count`、`total_sources_count`；`get_sources` 新增 `cited_sources_count`/`extra_sources_count`，额外信源摘要限 500 字符。
- 没有原生引用时保留完整回答（以前可能被当成信源小节剥掉），正文中的引用标记、链接与 URL 都计为引用；括号平衡的 URL 不再被截断；回答解析为线性时间。
- 所有失败都以 `[搜索失败]` 开头；Grok 失败时仍列出已找到的额外信源，`model_check` 也照常返回；`extra_sources` 的 0–20 由参数校验保证；补位时多取余量；"Extra sources" 小节每条一行纯文本。
- OpenRouter 下按次指定的模型也会加 `:online`。

**核验层**

- DOI 与 arXiv 号按原样提取：带括号的 Lancet/Elsevier DOI、紧跟中文标点或文字的 DOI、URL 的 `?`/`#`/`.pdf` 尾巴、bioRxiv `vN.full`、句末句号、`.pdf` 链接、`arXiv.<id>` 与旧式编号。
- 新增 `lookup_failed`：`unresolved` 只保留登记处或网站明确的否定答复，超时、限流、5xx 改列入 `lookup_failed`。
- 不在 Crossref 的 DOI 经 doi.org 登记处与 DataCite 核对；Crossref 每进程并发 1（设置 `GROK_VERIFY_MAILTO` 后为 3），按 Retry-After 重试一次。
- arXiv：同一时间一个连接、上次结束 3 秒后再发，全进程 429/503 冷却，并发查询共用请求，读取超时 10 秒，DataCite 兜底，原因中写明状态码。
- `title_checks` 不再把作者-年份链接文字、期刊名、加粗标签或相邻论文的标题当作该编号的标题。
- 只核验 Grok 引用的来源；`GROK_VERIFY_IDS=false` 现在只关闭标识符查询，URL 检查由 `GROK_VERIFY_URLS` 单独控制。
- 新增 `GROK_VERIFY_FAKE_IP`/`GROK_VERIFY_DOH_URL`：Clash fake-ip 环境下 URL 检查也能工作。
- URL 检查最多读取解码后的 64 KB，从不读取重定向响应体（修复 gzip 炸弹/正则卡死导致整个服务器无响应的问题）；`GROK_VERIFY_MAILTO` 只发给登记处。

**web_fetch / web_map**

- Firecrawl 渲染的 404/403/5xx 错误页不再当作正文返回（`page_status=`），也不再替换较短的 Tavily 结果。
- Firecrawl 被重定向时首行给出 `final_url`/`title`；登录墙与反爬验证页（如知乎“安全验证”）标记 `warning=interstitial` 并附说明行。
- 带版本号的 arXiv 摘要链接返回所请求的版本；摘要页缓存一小时并写入核验缓存；arXiv API 缓慢或限流时在 `GROK_FETCH_ARXIV_BUDGET_S`（默认 8 秒）后放弃，并在 2 分钟内跳过。
- 非 http(s) URL 与本机/局域网 IP 字面量在本地拒绝（`error=invalid_url`），不调用付费后端。
- CJK 为主的页面默认每页 20,000 字符（`GROK_FETCH_MAX_CHARS_CJK`）。
- `web_map`：`output_schema=None`，不再重复 `structuredContent`；失败首行固定为 `[web_map] error=<类别> provider=tavily [status=…]`。

**配置与管理工具**

- `switch_model` 校验名称（空名、超长、不在 `/models` 中的名称被拒绝），并报告真实的模型来源（`GROK_MODEL_SOURCE`/`GROK_MODEL_NOTE`）。
- 配置与日志绝不写入项目工作目录；损坏的 `config.json` 会被忽略并提示；无法识别的环境变量值保持默认并列入 `config_warnings`。
- 日志按天分文件并保留 `GROK_LOG_RETENTION_DAYS` 个（默认 7）。
- Key 与 URL 脱敏更严格（最多显示四分之一，URL 凭据显示为 `***`，熔断提示中的 URL 也脱敏）；`get_config_info` 连接测试遇到传输错误重试一次，并列出新增的超时与核验设置。
- `toggle_builtin_tools`：`block`/`allow` 别名，拒绝有歧义的 `enable`/`disable`，报告其他设置文件中的拒绝规则，拒绝指向项目之外的符号链接，原子写入。
- `plan_*` 校验输入，覆盖已有阶段需要 `is_revision=true`。
- 服务器崩溃时输出 traceback 并以状态码 1 退出。

**安全**

- 错误响应体先脱敏再截断；`xai-` Key、Basic/裸 Authorization 值、`?key=` 与 URL 用户信息也会被脱敏。

**打包与文档**

- 依赖下限改为 `fastmcp>=2.11.0`（工具注册使用的 `meta` 参数自 2.11.0 起才有，更早的版本无法导入服务器）。
- 安装说明建议固定到标签或提交；补充工具失败以普通结果返回（`isError=false`）的说明；英文 README 与中文版重新对齐（补上 SSL `--native-tls` 安装方式与 Firecrawl 空内容重试说明）。

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
