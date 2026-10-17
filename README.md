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
                  ├─ web_fetch   ───► 直连下载（文本/代码文件）→ Tavily Extract → Firecrawl Scrape（内容抓取，自动降级）
                  └─ web_map     ───► Tavily Map（站点映射）
```

### 功能特性

- **双引擎**：Grok 搜索 + Tavily 抓取/映射，互补协作
- **Firecrawl 托底**：Tavily 提取失败时自动降级到 Firecrawl Scrape，支持空内容自动重试
- **OpenAI 兼容接口**，支持任意 Grok 镜像站
- **自动时间注入**（每次搜索注入本地日期、时间与 ASCII 时区：UTC 偏移如 `UTC+08:00`，能确定时附 IANA 名称）
- **只读工具注解**：`web_search`/`web_fetch`/`web_map`/`get_sources`/`get_config_info` 声明 `readOnlyHint`，Claude Code 会并行执行同一轮里的多次调用，而不是逐个排队
- **并发上限 + 按模型熔断**：`GROK_MAX_CONCURRENCY` 限制同时在途的 Grok 请求；连续 429 或账号池耗尽时按模型熔断（同一模型的不同思考档位，如 `(high)`/`(low)`/`(8192)`，共用一套熔断状态）并返回结构化错误，冷却后自动探测恢复。CLIProxyAPI / xAI 的账号池耗尽响应（503 `auth_unavailable`、429 `model_cooldown`、`free-usage-exhausted` 等）第一次就会熔断；排队等并发名额的调用拿到名额后会再检查一次熔断状态
- **失败必有回声**：上游流内错误帧、429、空响应、超时、网络故障和中途断开的流都会变成 `error`/`error_type`/`retry_after_s` 字段，不再静默返回空正文；被上游截断或过滤的回答标记 `incomplete`。工具失败按设计以普通结果返回（MCP `isError=false`），请以 `error` 字段或首行判断
- **搜索时长有上限**：`GROK_READ_TIMEOUT_S`（单次读取）与 `GROK_SEARCH_TIMEOUT_S`（整次调用，含排队、重试与等待）保证网关卡住时搜索也会结束并返回 `timeout`
- **两种上游接口**：`GROK_API_MODE=chat`（默认，`/chat/completions`）或 `responses`（`/responses`，即 Responses API）。CLIProxyAPI 的 chat 接口要等整个回答生成完才发出第一个字节，Responses 接口则边生成边推送事件（实测 2–3 秒内即有响应），长时间的多智能体搜索不再受读取超时和 Cloudflare 100 秒限制的影响；xAI 还会在用量里给出本次花费（`usage.cost_usd`）与搜索次数，结果里附上 Grok 用过的搜索词（`search_queries`）
- **按次换模型**：`web_search(model=...)` 支持 CPA 风格的思考后缀（如 `grok-4.7(high)`、`grok-4.7(8192)`），按基础名称对照 `/models` 校验、请求时保留完整名称；模型列表缓存有有效期，获取失败不缓存；TTS/语音、图像、嵌入等非对话模型按名称在本地拒绝，相近建议只列对话模型（同系列优先）
- **额外信源双引擎分配**：`extra_sources` 在 Tavily 与 Firecrawl 之间对半分配，与 Grok 已引用的链接及引擎之间去重，一方失败由另一方补位，过程写入 `extra_sources_report`；指定 `platform` 时引擎只在该平台的域名内搜索（Tavily `include_domains`、Firecrawl `site:`），平台外结果被丢弃
- **核验层（不是事实核验）**：回答里的 arXiv 号、DOI 经 arXiv API / Crossref / doi.org 登记处 / DataCite 确认存在（包括 arXiv 自己的 `10.48550/arXiv.<id>` DOI、Zenodo 以及万方/ISTIC 等其他登记机构的 DOI），Grok 引用的 URL 确认可访问，回答中写出的论文标题与登记标题自动比对（`title_checks`）；**不检查来源是否支持对应论断**（`claim_support: not_checked`）。`unresolved` 表示登记处或网站明确答复不存在（应视为可疑）；`lookup_failed` 表示没拿到答复（限流、5xx、超时、人机验证或登录墙页面，如 Reddit “Prove your humanity”），不能当作反对引用的证据；没检查的条目连同原因列在 `skipped`。只核验 Grok 引用的来源，不核验 `extra_sources` 的结果；URL 请求前先确认目标地址都是公网地址，Clash/mihomo fake-ip 环境下先经 DNS-over-HTTPS 查出真实地址（局限见 `web_search` 一节）。默认精简输出（`GROK_VERIFY_DETAIL=compact`）：只列需要关注的条目，计数全部保留
- **原样抓取文本文件、新鲜内容与快照分页**：raw.githubusercontent.com、gist、GitHub/GitLab/Codeberg/Bitbucket 文件页和代码/配置/文本扩展名的 URL 由本机直连下载、逐字节原样返回（`source=direct`；只访问公网地址，最大 5 MB，不发送任何 API Key）；Firecrawl 默认要求现抓（`maxAge=0`），首行标出后端缓存状态（`cache=`）；普通网页 Tavily 结果过短或是人机验证页时回退 Firecrawl；arXiv 摘要页走 arXiv API（带版本号的链接返回该版本），不可用时改用 DataCite 记录；超长页面首屏返回 `snapshot_id`，后续页按 `offset` + `snapshot_id` 固定读取同一份内容，不会错页；CJK 为主的页面默认每页 20,000 字符；页面本身返回 HTTP ≥ 400 时算作失败（`page_status=`），Firecrawl 被重定向时首行给出 `final_url=`/`title=`，登录墙与反爬验证页标记 `warning=interstitial`；非 http(s) 链接和本机/局域网 IP 直接在本地拒绝（`error=invalid_url`），不调用付费后端；失败时逐个列出各后端的错误类别、HTTP 状态和脱敏详情
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

> **固定版本（推荐）**：`@main` 是分支，uvx 每次启动都会到 GitHub 解析它：总是运行主分支的最新代码，修复来得最快；代价是每次启动多花几秒，GitHub 不可达时服务器无法启动，而且未经你审阅的改动（包括不兼容的改动）会在下次启动时直接生效。日常使用建议固定到发布标签或提交，把上面的 `git+https://github.com/Cody-Xue/GrokSearch@main` 换成 `git+https://github.com/Cody-Xue/GrokSearch@v1.13.0`（以仓库实际存在的标签为准，打标签之前可用 `@<提交 SHA>`）：每次运行的都是同一份代码，可复现，缓存后启动不再依赖 GitHub；代价是升级要手动改版本号。两种写法都可以，下文示例沿用 `@main`。

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
| `GROK_MODEL` | ❌ | `grok-4-fast` | 默认模型（设置后优先于 `~/.config/grok-search/config.json` 与 `switch_model`；空值视为未设置）。费用提示：多智能体/高推理档位（如 `grok-4.20-multi-agent-0309(high)`）每次搜索实测消耗 50 万–160 万 prompt tokens；日常查询建议用轻量模型，如 `grok-4.7`（本版实测每次约 1.9 万–13 万），设为 `GROK_MODEL` 或按次传 `web_search(model=...)` |
| `TAVILY_API_KEY` | ❌ | - | Tavily API 密钥（用于 web_fetch / web_map） |
| `TAVILY_API_URL` | ❌ | `https://api.tavily.com` | Tavily API 地址 |
| `TAVILY_ENABLED` | ❌ | `true` | 是否启用 Tavily；`false` 时 `web_fetch`、`web_map`、`extra_sources` 都不会调用 Tavily（即使配置了 key） |
| `FIRECRAWL_API_KEY` | ❌ | - | Firecrawl API 密钥（Tavily 失败时托底） |
| `FIRECRAWL_API_URL` | ❌ | `https://api.firecrawl.dev/v2` | Firecrawl API 地址 |
| `GROK_DEBUG` | ❌ | `false` | 调试模式（日志会记录完整的提示词与回答）。进度消息（如 `Begin Fetch`、`Fetch Finished`）只在开启时写入日志并推送给客户端；警告与错误始终写入并推送；客户端通知失败（如连接已关闭）不会让工具失败 |
| `GROK_LOG_LEVEL` | ❌ | `INFO` | 日志级别 |
| `GROK_LOG_DIR` | ❌ | `logs` | 日志目录；相对路径位于 `~/.config/grok-search` 下，不可用时改用私有临时目录（绝不写到工作目录）。每天一个文件 `grok_search_YYYYMMDD.log`，运行中的服务器过午夜自动换文件 |
| `GROK_LOG_RETENTION_DAYS` | ❌ | `7` | 保留的每日日志文件个数（含当天），更旧的在启动和换日时删除 |
| `GROK_RETRY_MAX_ATTEMPTS` | ❌ | `3` | 最大重试次数 |
| `GROK_RETRY_MULTIPLIER` | ❌ | `1` | 重试退避乘数 |
| `GROK_RETRY_MAX_WAIT` | ❌ | `10` | 重试最大等待秒数 |
| `GROK_RETRY_BUDGET_S` | ❌ | `45` | 单次调用重试总等待预算（秒），上游 Retry-After 超出预算时立即放弃；`0` 关闭。只计重试之间的等待，整次调用由 `GROK_SEARCH_TIMEOUT_S` 限定 |
| `GROK_API_MODE` | ❌ | `chat` | 上游接口：`chat` 调用 `/chat/completions`；`responses` 调用 `/responses`（Responses API：流式事件、`store: false`，xAI 与 CLIProxyAPI 支持，其他网关不一定支持）。`(high)` 等思考后缀在两种接口下都由网关转换为推理强度；无法识别的值按 `chat` 处理并列入 `config_warnings` |
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
| `GROK_VERIFY_DOH_URL` | ❌ | `https://cloudflare-dns.com/dns-query` | `GROK_VERIFY_FAKE_IP=doh` 使用的 DoH JSON 接口（Cloudflare/Google JSON 格式，如 `https://dns.google/resolve`），经配置的代理访问；`web_fetch` 的直连下载也使用这两项 |
| `GROK_VERIFY_DETAIL` | ❌ | `compact` | 核验块的详细程度：`compact` 只列需要关注的内容（全部 `unresolved` 与 `lookup_failed`、`match` 以外的 `title_checks`、`urls_unusual`、前 10 条 `skipped` 及 `skipped_omitted`），`checked`/`summary` 保留全部计数，scope 为一句简述；`full` 另列每条已解析的 arXiv/DOI 记录、每个可访问 URL（状态与标题）、一致的标题比对和完整的 scope 说明（即 v1.12.0 的输出）。无法识别的值按 `compact` 处理并列入 `config_warnings` |
| `GROK_PLANNING_TOOLS` | ❌ | `false` | 是否注册 `plan_*` 六个搜索规划工具 |
| `GROK_FETCH_MIN_CHARS` | ❌ | `2000` | 普通网页的 Tavily 抓取结果低于此长度时回退 Firecrawl。不用于直连下载、代码文件回退和 arXiv 摘要页回退（这些内容本来就可能很短） |
| `GROK_FETCH_MAX_CHARS` | ❌ | `40000` | `web_fetch` 单次默认返回字符数 |
| `GROK_FETCH_MAX_CHARS_CJK` | ❌ | `20000` | 页面中 CJK 字符超过 30% 时 `web_fetch` 的默认每页字符数（不超过 `GROK_FETCH_MAX_CHARS`），避免单次结果超过 Claude Code 约 25k token 的 MCP 输出上限 |
| `GROK_FETCH_ARXIV_BUDGET_S` | ❌ | `8` | `web_fetch` 走 arXiv API 的总时限（秒，含 3 秒闸门与一次重试）；限流、超时、网络或 5xx 失败后 2 分钟内跳过 arXiv API（`attempts=arxiv-api:cooldown`）。arXiv API 失败时先用 DataCite 的记录生成摘要页（免费），再依次 Firecrawl、Tavily |
| `GROK_FETCH_SNAPSHOT_TTL_S` | ❌ | `900` | `web_fetch` 分页快照的空闲有效期（秒），过期后需从 `offset=0` 重新抓取 |
| `GROK_FETCH_DIRECT` | ❌ | `true` | 文本/代码文件（raw、gist、文件页、代码/配置/文本扩展名）由本机直接从源站下载并原样返回。源站会看到你的 IP；设为 `false` 时这些文件改由 Firecrawl（优先）和 Tavily 读取 raw 地址，损坏的副本会被标出 |
| `GROK_FETCH_MAX_AGE_S` | ❌ | `0` | Firecrawl 抓取时接受的最长缓存时间（秒，作为 `maxAge` 发送）。`0` 总是现抓；不设时 Firecrawl 自身默认最多返回 2 天前的缓存。缓存命中与现抓同样消耗 1 credit（[文档](https://docs.firecrawl.dev/features/fast-scraping)） |
| `GROK_MAP_REGEX_TIMEOUT_S` | ❌ | `2` | `web_map` 在本地按 `select_domains` 匹配主机名的时限（秒，0.5–60，含等待空闲匹配进程的时间）。匹配在独立子进程中进行，到时即结束该进程，结果不做本地过滤并附 `scope_note` |
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
  - `error_type` 取值：`bad_request`（查询为空或只含空白/不可见/控制字符：在本地拒绝，正文以 `[搜索失败] 查询为空…` 开头，不调用 Grok、`/models` 或额外信源引擎，不产生费用）、`config`（`GROK_API_URL`/`GROK_API_KEY` 缺失或无效）、`proxy_config`（本地代理环境错误）、`invalid_model`、`rate_limit`（`exhausted: true` 表示该模型的账号池被封，等待 `retry_after_s` 或换模型）、`circuit_open`、`timeout`（`GROK_SEARCH_TIMEOUT_S` 内没拿到完整回答、读取超时，或 Cloudflare 522/524）、`network`（连接、代理或 TLS 故障，消息不会为空）、`truncated_stream`（流在既没有 `finish_reason` 也没有 `[DONE]` 时关闭，会重试）、`empty_response`、`auth`（网关没有该模型可用的凭据，如 CPA `auth_not_found`，不重试）、`http_<状态码>`、`unknown`，以及上游流内透传的错误码（如 `invalid_request_error`、`model_not_found`、`server_error`、`upstream_error`）。
  - `rate_limit` + `exhausted: true` 也涵盖 CLIProxyAPI 的 503 `auth_unavailable`/“no auth available”、429 `model_cooldown`/“cooling down” 和 xAI 的 `free-usage-exhausted`，第一次就会熔断。`retry_after_s` 取自任何状态码的 Retry-After 头，或 CPA 错误帧里的 `reset_seconds`。
  - 账号池耗尽且给出的恢复时间在 `GROK_RETRY_BUDGET_S` 与总时限之内时，只等一次再试；否则立即失败并给出 `retry_after_s`，不盲目重试。确定性错误（`retryable: false`、`authentication_error`、`auth_not_found`）不重试。
  - Cloudflare：520/521/523/530 重试一次；522/524 按 `timeout` 报告，524 不会重跑（这次搜索已经跑了约 100 秒，可缩小问题或换更轻的模型）。Cloudflare 错误页只给出名称，不引用 HTML。
  - 熔断器：排队等 `GROK_MAX_CONCURRENCY` 名额的调用拿到名额后会再检查一次熔断状态；半开探测名额只能由探测调用自己释放。
- **核验层 `verification`**：检查回答里的 arXiv 号 / DOI 和 Grok 引用的 URL（`extra_sources` 的结果不检查），只看是否存在、能否访问，以及回答里写在标识符旁边的论文标题与登记标题是否一致（`title_checks`：`match` / `partial` / `possible_mismatch` / `not_stated`）。**不检查来源是否支持对应论断**，`claim_support` 固定为 `not_checked`。
  - arXiv 号包括旧式编号（如 `hep-th/9711200`）和 `10.48550/arXiv.<id>` 形式的 DOI；DOI 先查 Crossref，Crossref 返回 404 时由 doi.org 登记处判断是否存在、由 DataCite 提供标题。
  - `unresolved`（视为可疑）：登记处答复不存在、页面返回 404/410 或其他 4xx、域名不存在（NXDOMAIN）。401/403/451 附注“拒绝自动访问，页面可能仍存在”。
  - `lookup_failed`（结果未知，不能当作反对引用的证据）：429、5xx、超时、网络错误、登记处冷却、人机验证页、核验总时限到期；原因里写明 HTTP 状态。`summary` 中也有 `lookup_failed` 计数。返回 2xx/3xx 的人机验证页和登录墙（Reddit 的 JS 验证 “Prove your humanity”、PyPI 的 Fastly “Client Challenge”、百度“安全验证”、跳转到 `/login`）同样列入 `lookup_failed`，原因为 `bot check / interstitial (page may exist)`，不计入可访问的 URL；判定规则与 `web_fetch` 共用。
  - 10.5555 前缀的 DOI（Crossref 的测试前缀，ACM DL 用它标识未注册的条目）查不到时仍列入 `unresolved`，原因附提示：请按标题核对论文。
  - `skipped`：未检查的条目及原因（超出数量上限、URL 检查已关闭、标识符检查已关闭（`GROK_VERIFY_IDS=false`）、非公网地址、非 http 链接、arxiv.org/doi.org 链接里没有可识别的编号、超过 400 字符或含 `.`/`..` 路径段的 DOI 形文本；回答里写的 DOI 模板如 `10.48550/arXiv.<id>`、`10.1145/3XXXXXX.3XXXXXX` 排在最后，最多列 20 条）。
  - `title_checks` 只使用为该标识符写的标题：离它最近的明确标题，不会取到另一篇论文的标题；标识符位于行首时上一行也算，标识符写成标签后的 Markdown 链接时也一样（如 `- arXiv: [1706.03762](https://arxiv.org/abs/1706.03762)`）；作者-年份文字、期刊名、以冒号结尾的标签和不足 3 个词的片段不算标题。
  - **精简输出（默认）**：`GROK_VERIFY_DETAIL=compact` 时 `verification` 带 `detail: "compact"`，`scope` 换成一句 112 字符的简述（`claim_support` 仍为 `not_checked`；各列表的含义见本节），只列需要关注的内容：全部 `unresolved` 与 `lookup_failed`、`match` 以外的 `title_checks`（`partial`/`possible_mismatch`/`not_stated`）、`urls_unusual`（最终状态不是 200 的可访问 URL，如 202/203 的人机验证页；只在有时出现）、前 10 条 `skipped`（其余条数见 `skipped_omitted`），以及 `timed_out`（如有）。`checked` 与 `summary` 保留全部计数（`urls_reachable`、`identifiers_found`、`title_match` 等）。精简模式下没有 `arxiv`、`doi`、`urls` 三个键（不是空列表）。`GROK_VERIFY_DETAIL=full` 恢复 v1.12.0 的完整输出。3 次实测搜索中，核验块合计从 18,756 字符降到 7,352 字符（-61%），整个 `web_search` 结果从 29,021 降到 17,617 字符（-39%）；大量引用拒绝脚本访问的站点（如 Reddit）时 `lookup_failed` 不截断，核验块仍可能较大。
- **URL 检查会访问哪些地址**：请求前及每次重定向前先用系统解析器解析域名（含 hosts 文件与局域网名称），只要有一个地址不是公网地址就不访问并记入 `skipped`。回环、内网、链路本地、CGNAT（100.64.0.0/10）、`127.1`/`2130706433`/`0x7f.0.0.1` 这类数字写法，以及 IPv4 映射、6to4、NAT64 中内嵌的非公网地址都算在内。Clash/mihomo fake-ip 模式下本地解析只得到 198.18.0.0/15：`GROK_VERIFY_FAKE_IP=doh`（默认）时经配置的代理用 DNS-over-HTTPS 查出真实地址，同样要求全部是公网地址，再经代理访问；`skip`、`allow` 见环境变量表。重定向逐跳跟随，从不读取重定向的响应体；正文最多读取解码后的 64 KB（gzip/deflate 限量解压，其他编码不解码）。经代理得到的 502/503/504 记为 `lookup_failed`（来自代理或网站）。本地解析不了的域名记为 `unresolved`（`dns lookup failed`）。局限：检查时的解析与实际连接是两次独立解析，无法防御 DNS 重绑定；经 HTTP/SOCKS 代理请求时由代理决定真实目标（代理可能使用不同的、例如分流的 DNS）；DoH 模式会让 DoH 服务商看到被引用的域名。登记处（arXiv / Crossref / doi.org / DataCite）与 DoH 查询访问固定域名，不经过此检查。
- **模型 `model`**：可带 CPA 思考后缀，如 `grok-4.7(high)`、`grok-4.7(8192)`、`grok-4.7(auto)`。与默认模型完全相同（含后缀）或基础名称相同时直接放行；其他模型按基础名称对照 `/models` 校验，请求时保留完整名称。列表获取失败时不拦截（`model_check.status = unverified`）。OpenRouter 地址下按次指定的模型同样会自动加 `:online`，也可以显式写 `model:online`。
  - 非对话模型按名称识别（名称中含 tts、voice、audio、speech、whisper、transcribe、image、imagine、video、embed/embedding、moderation、realtime、rerank 等独立片段），在本地以 `invalid_model` 拒绝，`model_check.reason` 为 `not_chat_model`，不会发出搜索请求（最多读取 `/models` 列表以给出建议；`/models` 不可用时同样拒绝）。例：`grok-tts` → “'grok-tts' is not a chat/search model (audio/speech model, name contains 'tts')”。
  - 相近建议只列对话模型，同系列优先，再按名称相似度排序，例如 `grok-4.8` → `grok-4.7`。无效结果的 `model_check.reason` 为 `bad_name`、`unsupported_suffix`、`not_listed` 或 `not_chat_model`。
  - 配置的默认模型（`GROK_MODEL` 或配置文件）不会被拦截，只在 `get_config_info` 的 `config_warnings` 中提示；名称恰好含上述片段的真实对话模型可以这样使用。
  - **费用**：多智能体/高推理档位（如默认的 `grok-4.20-multi-agent-0309(high)`）每次搜索实测消耗 50 万–160 万 prompt tokens（`platform=GitHub` 时约 159 万）；`grok-4.7` 在本版的实测中每次约 1.9 万–13 万。日常查询建议用轻量模型：设为 `GROK_MODEL`，或按次传 `web_search(model="grok-4.7")`。`usage.prompt_tokens` 显示每次的实际用量；上游报告花费时（xAI，两种接口都可能有，Responses 模式实测都有）`usage.cost_usd` 直接给出美元金额：本版实测默认模型每次约 0.5–0.7 美元，`grok-4.7` 约 0.02–0.05 美元。
- **额外信源 `extra_sources`**（0–20，超出范围由参数校验拒绝）：两个引擎都可用时 Tavily 分 ⌈n/2⌉、Firecrawl 分 ⌊n/2⌋，每个引擎多取 2 条备用；与 Grok 已引用的链接及引擎之间去重（忽略协议、`www.`、片段、末尾斜杠和跟踪参数），按轮流顺序选取；某个引擎失败或结果不足时，由另一个引擎补一次（补取数量为缺口 + 2 条余量 + 已遇到的重复条数，最多 20）。结果附在正文末尾的 "Extra sources" 小节，每条一行纯文本（标题、链接、引擎、最多 300 字符的摘要）；过程见 `extra_sources_report`。
  - **按平台搜索**：指定 `platform` 且平台名已知（GitHub、GitLab、Reddit、X/Twitter、Stack Overflow、Hacker News、YouTube、arXiv、Wikipedia、知乎、B站、微博、CSDN、掘金、V2EX、Hugging Face、PyPI、npm、Medium、小红书、豆瓣、微信公众号等约 40 个），或写的是主机名（如 `docs.python.org`）时，引擎只在该平台的主域名内搜索：Tavily 用 `include_domains`，Firecrawl 在查询后加 `site:`（[文档](https://docs.firecrawl.dev/api-reference/endpoint/search)）；结果再按主机名后缀过滤（`githubhelp.com` 不算 GitHub），平台外结果被丢弃并计入 `off_platform_skipped`，补位时一并多取，因此可能少于请求数。短链/内容域名（githubusercontent.com、redd.it、youtu.be、b23.tv、weibo.cn、hf.co）只由本地过滤接受，不发给引擎（实测 Firecrawl 在 `site:github.com OR site:githubusercontent.com` 下会漏出平台外结果）。多个平台用逗号分隔（如 `"GitHub, Reddit"`）；平台名旁的其他词（如 `GitHub issues`）会追加到引擎查询。未知平台不过滤，只把平台文字追加到引擎查询。`extra_sources_report.platform` 记录 `requested`、`applied`（`domain_filter` / `query_hint` / `none`）、`domains`、`also_accepted`、`engines`、`off_platform_skipped`、`query_hint`、`unresolved`。实测 `platform="GitHub"`、`extra_sources=6`：6 条全部在 github.com。
- **“没找到”的含义**：回答中的“本次检索未找到”只代表这次搜索没找到；需要确定结论时，直接到对应平台核实（例如用 `web_fetch` 打开仓库的 issue 搜索页）。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | string | ✅ | - | 搜索查询语句 |
| `platform` | string | ❌ | `""` | 聚焦平台（如 `"Twitter"`, `"GitHub, Reddit"`）；Grok 收到 "focus on" 提示，已知平台或主机名还会限定 `extra_sources` 的域名（见上文）；只含空白时视为未指定 |
| `model` | string | ❌ | `""` | 按次指定 Grok 模型 ID，可带思考后缀；非对话模型在本地拒绝 |
| `extra_sources` | int | ❌ | `0` | 额外补充信源数量（0–20，Tavily/Firecrawl，0 关闭；计入 `extra_sources_count`，不计入 `sources_count`） |
| `instructions` | string | ❌ | `""` | 按次追加给搜索模型的要求 |
| `verify` | bool | ❌ | `true` | 是否附加核验层；标识符查询受 `GROK_VERIFY_IDS` 控制，URL 检查受 `GROK_VERIFY_URLS` 控制，两者都关闭时不核验；详细程度由 `GROK_VERIFY_DETAIL` 控制（默认 `compact`） |

每次搜索自动注入本地日期、时间与时区上下文，以提升时效性搜索的准确度。时区写成 ASCII 的 UTC 偏移（如 `UTC+08:00`）；能从 `TZ`、`/etc/localtime`、`/etc/timezone` 或时区对象确定 IANA 名称且当前偏移一致时附上（如 `UTC+08:00 (Asia/Shanghai)`）。不再使用本地化的时区名：中文 Windows 上以前会变成 `Timezone: ??????`，名称无法解码时整次搜索还会失败。

返回值（结构化字典）：
- `session_id`: 本次查询的会话 ID
- `content`: Grok 回答正文。没有原生引用时保留全文，只剥离信源标题下的纯链接列表；有原生引用时保留原文，只合并重复标记。回答被截断时紧接正文附一行 `[回答不完整] …` 说明，"Extra sources" 小节在其后
- `sources_count` / `distinct_domains`: Grok 引用的信源数量 / 不同站点数（没有原生引用时，按正文中的引用标记、Markdown 链接和 URL 统计）
- `extra_sources_count` / `total_sources_count`: 额外信源数量 / 两者合计
- `incomplete` / `finish_reason` / `native_finish_reason`: 仅在上游提前结束生成时出现（`finish_reason` 为 `length` 或 `content_filter`，或 `native_finish_reason` 不是 `stop`），表示回答不完整
- `usage`: 上游报告用量时给出 `{prompt_tokens, completion_tokens, reasoning_tokens}`，上游提供时还有 `cached_tokens`、`cost_usd`（本次计费，美元）与 `web_search_calls` / `x_search_calls`（Grok 自己发起的搜索次数）（多智能体模型每次可达 50 万–160 万 prompt tokens，见上文“费用”）
- `search_queries`: Responses 模式下 Grok 实际使用的搜索词（最多 10 个），可用来判断“本次检索未找到”是否搜对了词
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

获取网页内容（Markdown）或原样获取文本文件。按 URL 类型选择后端：

- **文本/代码文件**：raw.githubusercontent.com、gist.githubusercontent.com、GitHub/GitLab/Codeberg/Bitbucket 的文件页（如 `github.com/<o>/<r>/blob/<ref>/<path>`，改读对应的 raw 地址）、`/raw/` 链接，以及以代码/配置/文本扩展名结尾的 URL（`.py` `.h` `.c` `.rs` `.go` `.js` `.ts` `.toml` `.yaml` `.json` `.md` `.txt` `.csv` `.xml` `.sql` `.lock` 等）由本机直接下载（不会重复抓取），逐字节原样返回（`source=direct`，首行带 `content_type=` 与 `status=`，文件页改写为 raw 时带 `final_url=`）。只接受 `text/*`（HTML 除外）与 JSON/XML/YAML/TOML/JS 类型。直连下载在你的机器上运行：请求前及每次重定向前都经过与核验层相同的地址检查（只访问公网地址；fake-ip 环境按 `GROK_VERIFY_FAKE_IP`/`GROK_VERIFY_DOH_URL` 处理），解压后最多 5 MB，超时 30 秒，不发送任何 API Key。源站会看到你的 IP，`GROK_FETCH_DIRECT=false` 可关闭。直连失败或关闭时，Firecrawl（优先）和 Tavily 读取 raw 地址；明显损坏的代码（换行被合并、缩进丢失、`_` 被转义为 `\_`、`#include <…>` 的目标被删）标记 `warning=degraded_code degraded=<原因>`，不应引用。返回 HTML 的地址按普通网页抓取。只靠扩展名判断的 URL（如 `bundlephobia.com/package/chart.js`）无法直连（代理、下载失败、`GROK_FETCH_DIRECT=false`）时按网页读取，先用 Tavily：短于 `GROK_FETCH_MIN_CHARS` 的结果会再试 Firecrawl（多一次调用），由其内容类型决定它是文件（此时优先采用 Firecrawl 原样返回的 text/plain 副本，Tavily 的副本也会检查是否损坏）还是网页；更长且 Markdown 链接很少的结果按文件采用，并检查是否损坏。GitHub raw CDN 对分支地址缓存约 5 分钟（`cache-control: max-age=300`），刚推送后可能仍是旧内容；固定到提交的地址总是准确的。
- **arXiv 摘要页**：走 arXiv API，带版本号的链接（如 `/abs/1706.03762v1`）返回该版本，front matter 中有 `version: v1`（未指定版本时为 `vN (latest)`）。arXiv API 不可用时，用 DataCite 对 `10.48550/arXiv.<id>` 的记录生成摘要页（`source=datacite`，免费：标题、全部作者、摘要、分类、备注、首末提交日期；只有最新版本，指定了其他版本时跳过），再依次 Firecrawl、Tavily，不适用长度门槛，不会重复抓取。
- **其他网页**：Tavily Extract；结果缺失、短于 `GROK_FETCH_MIN_CHARS` 或是人机验证页时再用 Firecrawl Scrape。

**新鲜度**：Firecrawl 按 `GROK_FETCH_MAX_AGE_S` 发送 `maxAge`（默认 `0`：总是现抓；不设时 Firecrawl 默认最多返回 2 天前的缓存，见 [文档](https://docs.firecrawl.dev/features/fast-scraping)）。首行的 `cache=` 是后端的缓存状态：`hit`/`miss` 是 Firecrawl 报告的 `cacheState`（命中时带 `cached_at=`），`bypassed` 表示 `maxAge=0` 现抓，`possible` 表示 Tavily（按 URL 缓存，Extract API 没有新鲜度参数，见 [文档](https://docs.tavily.com/documentation/api-reference/endpoint/extract)，近期改动的页面可能是旧内容）。`cached=true` 另指本服务的分页快照。

云端后端访问不到你的机器或局域网，因此调用前先在本地拒绝这类 URL：没有 http(s) 协议、没有主机名、`localhost`/`*.localhost`，以及回环/内网/链路本地/CGNAT 的 IP 字面量。结果为 `[web_fetch] error=invalid_url reason=<malformed|missing_scheme|unsupported_scheme|missing_host|local_address>`，不调用任何后端；此检查不解析域名。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `url` | string | ✅ | - | 完整的公网 http(s) URL |
| `max_chars` | int | ❌ | `0` | 本次最多返回的字符数，`0` 使用 `GROK_FETCH_MAX_CHARS`，页面 CJK 字符超过 30% 时使用 `GROK_FETCH_MAX_CHARS_CJK` |
| `offset` | int | ❌ | `0` | 从第几个字符开始读；大于 0 时必须同时传 `snapshot_id` |
| `snapshot_id` | string | ❌ | `""` | 首屏返回的快照 ID，用于读取后续页 |

首行是元数据：`[web_fetch] source=… total_chars=… offset=… returned=… truncated=… [next_offset=… snapshot_id=… expires_in_s=…] [cached=true] [content_type=… status=…] [cache=hit|miss|bypassed|possible cached_at=…] [attempts=…] [warning=interstitial|degraded_code|size_limit] [degraded=…] [final_url=… title="…"]`。首行之后、空行之前可能还有说明行：`注意:` 行解释警告；某个后端失败但另一个后端给出了结果时，失败的后端各有一行 `- <后端>: <类别> …`。页面超过一次能返回的长度时，服务端保存整页快照；后续用相同 `url` 加 `offset=next_offset` 与 `snapshot_id` 翻页，每一页都切自同一份内容。不带 `snapshot_id` 的 `offset>0` 会被拒绝；快照过期（默认空闲 900 秒）后返回 `error=snapshot_expired`，需从 `offset=0` 重新抓取。

Firecrawl 被重定向时首行出现 `final_url` 与 `title`（`title` 总在最后，且是 JSON 引号字符串）。`warning=interstitial` 加一行 `注意:` 表示内容是登录墙或反爬验证页（如知乎“安全验证”、Cloudflare “Just a moment...”、Reddit “Prove your humanity”），不是所请求的页面。判定规则与核验层共用：标题整体以及 ` - `/` | ` 两侧分别匹配，并检查正文中的验证标记；讨论 CAPTCHA 的普通文章不受影响。Tavily 的结果同样会被检查（Tavily 返回验证页时改试 Firecrawl），验证页不会替换真实的抽取结果。

失败时首行为 `[web_fetch] error=…`，随后逐行列出每个后端的错误类别（`auth`、`quota`、`rate_limit`、`timeout`、`proxy_config`、`network`、`upstream_error`、`bad_request`、`unsupported_site`、`blocked`、`not_text`、`extract_failed`、`empty_content`、`http_error`、`disabled`、`not_configured` 等）、HTTP 状态和脱敏后的详情。`unsupported_site` 表示服务商不支持或不允许抓取该站点（如 Firecrawl 对 Reddit 的 403 “we do not support this site”，Tavily 的 “URL is not supported”），不是 Key 的问题；`auth` 只用于 401 或提到 API Key/令牌/凭据的 403，没有说明的 403 为 `http_error`；`blocked` 表示直连下载的目标是或解析到非公网地址；`not_text` 表示目标不是纯文本资源（已改按网页抓取）。页面本身返回 HTTP ≥ 400（404、403、5xx…）时该后端算作失败，详情为 `target page answered HTTP <状态码>`，类别为 `extract_failed`（429 为 `rate_limit`，5xx 为 `upstream_error`），首行加 `page_status=<状态码>`。

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
| `allow_external` | bool | ❌ | `true` | 是否包含站外链接（与 Tavily 默认一致）。`false` 只保留起始 URL 所在主机的链接（`www.<主机>` 视为同一主机）；`app.`、`status.` 等其他子域名在本地过滤掉，因为 Tavily 把同站子域名算作站内。同时给出 `select_domains` 时改按其正则过滤 |
| `select_paths` | string[] | ❌ | - | 只保留路径匹配这些正则的 URL，如 `["/docs/.*"]` |
| `select_domains` | string[] | ❌ | - | 只在匹配这些正则的域名/子域名内遍历（Python 正则语法；去重后最多 20 个，每个最长 300 字符）；`allow_external=false` 时结果也在本地按主机名过滤（`re.search`），`allow_external=true` 时只影响遍历 |
| `exclude_paths` | string[] | ❌ | - | 跳过路径匹配这些正则的 URL |
| `exclude_domains` | string[] | ❌ | - | 跳过匹配这些正则的域名/子域名 |

`TAVILY_ENABLED=false` 时 `web_map` 不可用。结果较少或返回很快不一定是故障，可用上述参数与 `max_depth` 调整范围。

成功时输出 JSON `{base_url, results, response_time, scope, filtered_out}`：`scope` 为 `exact_host`（`allow_external=false`）、`select_domains`（`allow_external=false` 且给了 `select_domains`）或 `unfiltered`（`allow_external=true`，Tavily 的列表原样返回），`filtered_out` 是在本地去掉的 URL 数。`select_domains` 的正则来自调用方，因此在独立的 Python 子进程中匹配（`-I -S`，不带任何环境变量，拿不到 API Key 和代理设置；同一时刻最多 2 个，等待空闲进程的时间计入时限；子进程不依赖服务器也会结束：Linux/macOS 上到时自行结束并限制 CPU 时间，Linux 上另限 512 MB 内存，且服务器进程一结束（包括被强制结束）就随之结束；Windows 上子进程放在 Job Object 中，内存限 512 MB，服务器进程结束时一并结束），超过 `GROK_MAP_REGEX_TIMEOUT_S`（默认 2 秒）即结束该进程：回溯或占内存很久的正则（`^(\w+\.?)*example\.org$`、`(?:\b){50000000}x`）不会阻塞服务器和其他工具。匹配超时或失败时原样返回 Tavily 的列表（`scope: unfiltered`），并附 `scope_note: "local regex filter timed out; results not filtered (…)"` 或 `"local regex filter failed; …"`，不算错误。只有 `allow_external=false` 且给了 `select_domains` 时才启动子进程；按起始主机过滤是普通的字符串比较。`select_paths`、`exclude_paths`、`exclude_domains` 只由 Tavily 处理。Tavily 的 `limit` 在本地过滤之前生效，所以 `allow_external=false` 时返回的 URL 可能少于 `limit`。Tavily 的参数语义见 [Map 文档](https://docs.tavily.com/documentation/api-reference/endpoint/map)。实测 `https://www.firecrawl.dev`、`allow_external=false`：去掉 8 个其他子域名的链接，只剩 `firecrawl.dev`/`www.firecrawl.dev`。

失败时首行为 `[web_map] error=<类别> provider=tavily [status=<HTTP 状态>]`（类别同 `web_fetch`，包括 `unsupported_site` 与 `http_error`），随后是中文说明和一行脱敏详情；成功时输出 JSON（见上），且不在 `structuredContent` 中重复一份。`file://`、`localhost` 和本机/局域网 IP 在本地拒绝（`[web_map] error=invalid_url reason=…`）；`docs.example.com` 这样的裸域名仍交给 Tavily。`select_domains` 超过 20 个、某个超过 300 字符或不是有效的 Python 正则时，在调用 Tavily 之前拒绝：`[web_map] error=bad_request reason=too_many_patterns|pattern_too_long|invalid_regex`。

### `get_config_info` — 配置诊断

无需参数。显示所有配置状态（含 Tavily/Firecrawl 实际是否启用）、测试 Grok API 连接、返回响应时间和可用模型列表（API Key 自动脱敏），并刷新 `web_search(model=...)` 校验用的模型列表缓存。本地代理环境错误（如 `NO_PROXY` 中的 `[::1]`）会单独标为“本地代理环境错误”。

- `GROK_MODEL_SOURCE` 为 `env` | `switch_model` | `config` | `default`，`GROK_MODEL_NOTE` 说明 `switch_model` 的选择能保持多久。
- 还会列出 `GROK_CONFIG_FILE`、`GROK_LOG_RETENTION_DAYS`、`GROK_RETRY_MULTIPLIER`、`GROK_RETRY_MAX_WAIT`、`GROK_API_MODE`、`GROK_READ_TIMEOUT_S`、`GROK_SEARCH_TIMEOUT_S`、各项 `GROK_VERIFY_*`（含 `GROK_VERIFY_FAKE_IP`、`GROK_VERIFY_DOH_URL`、`GROK_VERIFY_DETAIL`）、`GROK_FETCH_*`（含 `GROK_FETCH_DIRECT`、`GROK_FETCH_MAX_AGE_S`）与 `GROK_MAP_REGEX_TIMEOUT_S`；`config_warnings` 列出无法识别的环境变量值以及配置文件、日志目录的问题。
- API Key 最多显示四分之一（不足 24 个字符时最多显示末 4 位）；URL 中的凭据（用户信息、查询参数值）显示为 `***`。
- `connection_test` 中的 `available_models` 不变，另有 `chat_models`（`web_search`/`switch_model` 接受的模型）与 `non_chat_models`（按名称识别的 TTS/语音/音频、图像/视频生成、嵌入、审核、实时、重排模型，总是被拒绝）。`GROK_MODEL` 本身像非对话模型时，`config_warnings` 会给出提示。
- 连接测试遇到传输错误（连接重置或拒绝、代理隧道关闭）时重试一次（`retried_after`/`attempts`）；空错误信息会写成 `proxy tunnel closed or upstream unreachable (ConnectError)`；Cloudflare 错误页只给出名称。

### `switch_model` — 模型切换

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model` | string | ✅ | 模型 ID（如 `"grok-4-fast"`, `"grok-2-latest"`） |

- 名称会去掉首尾空白，并像 `web_search(model=...)` 一样对照 `/models` 校验：允许 `(high)` 这类思考后缀；空名、超过 200 个字符、不在列表中的名称以及非对话模型（如 `grok-tts`，即使 `/models` 不可达）会被拒绝并给出相近的对话模型建议，不做任何修改。`/models` 不可达时其他名称照常切换，但附警告（未校验）。
- 切换到当前已在使用的模型不做任何修改：返回 `already_active: true`，不写 `config.json`，`GROK_MODEL_SOURCE` 保持不变（例如仍为 `env`）。切回 `GROK_MODEL` 的值时来源恢复为 `env`，不再有 `GROK_MODEL_NOTE` 和警告。
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
A: Grok（`GROK_API_URL` + `GROK_API_KEY`）为必填，提供核心搜索能力。Tavily 和 Firecrawl 均为可选：配置 Tavily 后 `web_fetch` 对普通网页优先使用 Tavily Extract，失败时降级到 Firecrawl Scrape；文本/代码文件由本机直连下载，不需要这两个服务（直连失败时才用它们）；两者均未配置时其他页面的 `web_fetch` 返回配置错误提示。`web_map` 依赖 Tavily。设置 `TAVILY_ENABLED=false` 可以在保留 key 的情况下停用 Tavily：此时 `web_fetch` 只用 Firecrawl，`web_map` 不可用，`extra_sources` 只走 Firecrawl。
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
Q: 为什么一次搜索用了几十万甚至上百万 prompt tokens？
</summary>
A: 这是上游的计费方式，不是本服务放大了提示词：多智能体/高推理档位（如 `grok-4.20-multi-agent-0309(high)`）会在内部发起许多次搜索，每一轮都把累计的上下文重新计为 prompt tokens，实测每次搜索 50 万–160 万。日常查询请换用轻量模型（如 `grok-4.7`，本版实测每次约 1.9 万–13 万）：设为 `GROK_MODEL`，或按次传 `web_search(model="grok-4.7")`。`usage.prompt_tokens` 显示每次的实际用量。空白查询现在在本地拒绝，不再计费。
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

### v1.13.0（2026-10-17）

- 新增 `GROK_API_MODE=chat|responses`（默认 `chat`，行为不变）。`responses` 改用 `/responses`（Responses API）：请求体为 `input` 加 `store: false`，按事件流解析正文增量、引用注解（按文本段换算偏移）和 `response.completed` / `incomplete` / `failed`；`incomplete` 映射到 `finish_reason`（`max_output_tokens` → `length`），`failed` 与错误事件按原有规则分类（号池耗尽、限流、确定性错误）；没有结束事件就断开的流按 `truncated_stream` 重试。重试、熔断、总时限与引用位置校验与 chat 模式相同。
- 实测（CLIProxyAPI）：Responses 接口 2–3 秒内返回响应头并持续推送搜索进度与正文，chat 接口则要等回答生成完；`(high)` 后缀在两种接口下都转换为 `reasoning.effort: high`。
- `usage` 增加 `cached_tokens`、`cost_usd`（xAI 的 `cost_in_usd_ticks`，1 美元 = 10¹⁰ ticks）与 `web_search_calls` / `x_search_calls`；Responses 模式的结果增加 `search_queries`。
- 版本号 1.13.0；安装说明中的固定版本改为 `@v1.13.0`。

### v1.12.1（2026-10-17）

**web_fetch**

- 修复：源代码与文本文件不再被改坏。raw/gist 地址、GitHub/GitLab/Codeberg/Bitbucket 文件页（改读 raw）、`/raw/` 链接和代码/配置/文本扩展名的 URL 由本机直连下载，逐字节原样返回（`source=direct`，首行带 `content_type=`/`status=`）。以前它们经 Tavily 抽取：Tavily 会把 text/plain 当 HTML 转换，`#include <assert.h>` 变成 `#include  // assert()`，`_` 变成 `\_`，换行或缩进丢失，`<…>` 之间的大段代码被当作标签删掉，首行没有任何提示。实测 Python.h（raw 与 blob 页）、v1.12.0 的 verify.py（82,163 字符）和 pyproject.toml 都与 curl 结果逐字节一致。直连失败或关闭（新设置 `GROK_FETCH_DIRECT=false`）时由 Firecrawl 优先读取 raw 地址，损坏的副本标记 `warning=degraded_code degraded=<原因>`。直连下载只访问公网地址（与核验层同一道检查），最大 5 MB，不发送 API Key。
- 修复：分支 raw 地址不再返回旧缓存（报告中 main 分支的 pyproject.toml 三次都返回 1.11.0）。直连下载从源站读取；Firecrawl 请求带 `maxAge`（新设置 `GROK_FETCH_MAX_AGE_S`，默认 `0` = 现抓，以前沿用 Firecrawl 默认的 2 天缓存），首行给出 `cache=hit|miss|bypassed` 与 `cached_at=`；Tavily 没有新鲜度参数，其结果标 `cache=possible`。
- 修复：小文件不再抓两次（`tavily:short(792),firecrawl:ok`）。文本文件走直连（不会重复抓取），不适用 `GROK_FETCH_MIN_CHARS`；只靠扩展名判断、无法直连的 URL 按网页读取：提取服务的短结果会再试 Firecrawl（多一次调用，与 v1.12.0 对短网页的处理一致；bundlephobia.com/package/chart.js：Tavily 只有 521 字符的残片，Firecrawl 有 3,424 字符的页面），由其内容类型决定是否为文件；arXiv API 不可用时摘要页由 DataCite 的记录生成（免费，`source=datacite`），之后才是 Firecrawl、Tavily，且不再按长度门槛重复抓取。
- 修复：Firecrawl 的 403 “we do not support this site”（如 Reddit）报告为新类别 `unsupported_site`，不再是暗示 Key 有误的 `auth`。只有 401 或提到 API Key/令牌/凭据的 403 才算 `auth`，没有说明的 403 为 `http_error`；Tavily 的 “URL is not supported” 也从 `bad_request` 改为 `unsupported_site`。某个后端失败而另一个给出结果时，首行下方逐行说明失败原因。
- 人机验证检测改为 `web_fetch` 与核验层共用的检测器：整个标题以及 ` - `/` | ` 两侧分别匹配（可识别 “Reddit - Prove your humanity”），新增 prove your humanity、verify you are human、are you a robot、Client Challenge、安全验证/验证码/人机验证等措辞，并检查正文中的验证标记；Tavily 的结果也会检查，验证页会触发 Firecrawl 重试。讨论 CAPTCHA 的普通文章不受影响（保留正文长度与验证文字的保护）。

**核验层**

- 修复：返回 2xx/3xx 的人机验证页与登录墙（Reddit、PyPI、百度、跳转到 `/login`）列入 `lookup_failed`，原因 `bot check / interstitial (page may exist)`，不再计入可访问的 URL。实测：一次 `platform=Reddit` 的搜索引用的 Reddit 帖子全部进入 `lookup_failed`。
- 核验块默认精简（新设置 `GROK_VERIFY_DETAIL=compact`）：scope 说明从 587 字符缩到 112 字符，只列需要关注的条目（全部 `unresolved`/`lookup_failed`、`match` 以外的标题比对、`urls_unusual`、前 10 条 `skipped`），计数全部保留在 `summary`。3 次实测搜索的核验块合计 18,756 → 7,352 字符（-61%），整个 `web_search` 结果 29,021 → 17,617 字符（-39%）。`GROK_VERIFY_DETAIL=full` 恢复 v1.12.0 的完整输出。
- 修复：Grok 常用的 “- Title: X” 加 “- arXiv: [id](url)” 写法下，标题比对能读到上一行的标题（以前是 `not_stated`，错误标题也看不出来）。
- 查不到的 10.5555 前缀 DOI（Crossref 测试前缀，ACM DL 用于未注册条目）附提示：请按标题核对论文。

**web_search**

- 修复：时间上下文的时区改为 ASCII 的 UTC 偏移（如 `UTC+08:00`），能确定且偏移一致时附 IANA 名称（如 `(Asia/Shanghai)`）。修复中文 Windows 上的 `Timezone: ??????`，以及时区名无法解码时整次搜索因 UnicodeEncodeError 失败的问题。实测 `grok-4.7` 复述为 “2026-10-17, UTC-07:00 (America/Los_Angeles)”。
- 修复：空白查询（含全角空格、零宽字符、控制字符）在本地以 `bad_request` 拒绝，不再发给 Grok、`/models` 和额外信源引擎（报告中一次空白查询消耗了 500,905 prompt tokens）；只含空白的 `platform` 视为未指定。
- 修复：`extra_sources` 遵循 `platform`：已知平台映射到域名（Tavily `include_domains`、Firecrawl `site:`），平台外结果在本地丢弃，过程写入 `extra_sources_report.platform`；未知平台把平台文字加入引擎查询。实测 `platform="GitHub"`、`extra_sources=6`：6 条全部在 github.com（报告中 6 条有 3 条不在 GitHub）。
- 修复：非对话模型（如 `grok-tts`、图像、嵌入模型）在本地以 `invalid_model` 拒绝（`model_check.reason: not_chat_model`），不再到上游才返回 400；相近建议只列对话模型，同系列优先（`grok-4.8` → `grok-4.7`，以前会建议 `grok-tts`）。
- 文档：多智能体/高推理模型每次搜索消耗 50 万–160 万 prompt tokens，日常查询建议用 `grok-4.7` 等轻量模型。

**web_map、管理工具与日志**

- 修复：`allow_external=false` 只保留起始 URL 所在主机的链接，`app.`、`status.` 等其他子域名在本地过滤；与 `select_domains` 同用时按其正则过滤。新增输出字段 `scope`、`filtered_out`。正则在独立子进程中匹配（不继承环境变量，同一时刻最多 2 个），超过 `GROK_MAP_REGEX_TIMEOUT_S`（新设置，默认 2 秒，含等待时间；Linux/macOS 上子进程另有自己的闹钟和 CPU 时间上限，Linux 上另限内存并随服务器结束；Windows 上由 Job Object 限内存并随服务器结束）即结束，病态正则无法阻塞服务器（实测 `web_map` 运行 `^(\w+\.?)*example\.org$` 期间 `get_config_info` 在毫秒级返回）；超时或失败时返回未过滤的列表并附 `scope_note`。最多 20 个、每个最长 300 字符，无效正则在本地以 `bad_request` 拒绝。
- 修复：`switch_model` 切换到当前模型时不做任何修改（`already_active: true`），`GROK_MODEL_SOURCE` 保持 `env`；切回 `GROK_MODEL` 的值时来源恢复为 `env`。
- 修复：`log_info` 的进度消息只在 `GROK_DEBUG` 开启时推送给客户端；所有客户端通知都有异常保护，连接已关闭时工具照常返回。
- `get_config_info` 新增 `chat_models`/`non_chat_models`，并列出 `GROK_VERIFY_DETAIL`、`GROK_FETCH_DIRECT`、`GROK_FETCH_MAX_AGE_S`、`GROK_MAP_REGEX_TIMEOUT_S`。
- 工具元数据版本：`web_search` 2.4.0、`web_fetch` 1.7.0、`web_map` 1.6.0、`get_config_info` 1.7.0、`switch_model` 1.6.0。

**打包与文档**

- 版本号 1.12.1；安装说明中的固定版本改为 `@v1.12.1`；v1.12.0 的变更记录补上发布日期。

### v1.12.0（2026-10-16）

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
