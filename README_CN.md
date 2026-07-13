# gemini-web2api

<p align="center">
  <img src="logo.png" width="200" alt="gemini-web2api logo">
</p>

[English](README.md)

将 Google Gemini 网页端转换为 OpenAI 兼容 API. 零成本, 跨平台, 单文件.

## 特性

- **可选密钥**: `api_keys` 为空时免密, 填入密钥后按 OpenAI Bearer Key 校验
- **OpenAI 兼容**: 直接替换 `/v1/chat/completions` 和 `/v1/models`
- **工具调用**: 完整的 Function Calling 支持 (OpenAI 格式)
- **多模型**: Flash, Flash Thinking (2万字+输出), Pro, Auto, Lite
- **思考深度**: 通过 `@think=N` 后缀调节 (0=最深, 4=最浅)
- **联网搜索**: 内置互联网访问 (Gemini 原生搜索能力)
- **跨平台**: 纯 Python, 仅一个可选依赖 (`httpx` 用于流式输出)
- **流式输出**: 基于 `httpx` 的 SSE Streaming 支持
- **Codex CLI**: Responses API (`/v1/responses`) 兼容 OpenAI Codex
- **Gemini CLI**: Google 原生 API (`/v1beta/models`) 兼容 Gemini CLI

## 快速开始

```bash
pip install httpx
python -m gemini_web2api
```

服务启动在 `http://localhost:8081/v1`.

## 客户端配置

### Cherry Studio / ChatBox / 任何 OpenAI 兼容客户端

| 字段 | 值 |
|------|-----|
| Base URL | `http://localhost:8081/v1` |
| API Key | `config.json` 中的任意 `api_keys`；未配置时随便填 |
| Model | `gemini-3.5-flash-thinking` |

### curl

```bash
curl http://localhost:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-key" \
  -d '{"model":"gemini-3.5-flash","messages":[{"role":"user","content":"你好!"}]}'
```

### OpenAI Python SDK

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8081/v1", api_key="sk-your-key")
resp = client.chat.completions.create(
    model="gemini-3.5-flash-thinking",
    messages=[{"role": "user", "content": "解释量子计算"}]
)
print(resp.choices[0].message.content)
```

### Gemini CLI

```bash
export GEMINI_API_KEY=none
export GOOGLE_GEMINI_BASE_URL=http://localhost:8081
gemini
```

支持 Google 原生 API 端点:
- `GET /v1beta/models` — 模型列表
- `POST /v1beta/models/{model}:generateContent` — 非流式生成
- `POST /v1beta/models/{model}:streamGenerateContent` — 流式生成 (SSE)

### Agent 客户端

Codex CLI、Claude Code、Copilot 这类编程 agent 需要流式工具调用协议，不只是普通聊天补全。本服务提供以下兼容端点:

| 客户端 | Base URL | API 形态 |
|------|----------|----------|
| Codex CLI | `http://localhost:8081/v1` | OpenAI Responses API (`/v1/responses`) |
| Claude Code | `http://localhost:8081` | Anthropic Messages API (`/v1/messages`) |
| Copilot / OpenAI 兼容 agent | `http://localhost:8081/v1` | Chat Completions (`/v1/chat/completions`) |

Codex 配置示例:

```toml
model_provider = "gemini-web2api"
model = "gemini-3.5-flash"

[model_providers.gemini-web2api]
name = "gemini-web2api"
base_url = "http://localhost:8081/v1"
wire_api = "responses"
env_key = "GEMINI_WEB2API_KEY"
requires_openai_auth = false
```

Claude Code 环境变量示例:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8081
export ANTHROPIC_AUTH_TOKEN=sk-your-key
export ANTHROPIC_MODEL=gemini-3.5-flash
```

Agent 兼容能力包括:
- 当模型只描述动作而没有调用工具时, 自动进行一次工具调用修复重试
- 当模型生成客户端未声明的工具名时, 拒绝该调用并自动修复
- 使用 SQLite 保存 Responses 历史, 支持 `previous_response_id` 和 `GET /v1/responses/{id}`
- 保存 Gemini Web 完整 10 项 conversation metadata，并通过 SQLite 在普通聊天和 Agent 请求间复用
- 完整 Agent 行为指令只在工具链首轮发送；无状态后续轮使用紧凑 JSON 工具 schema
- 对超长工具输出和旧历史做确定性截断/压缩
- 在 prompt 上下文中保留 Anthropic `thinking` / `redacted_thinking` 信息
- 已测试 Codex Responses、Claude Messages、Copilot/OpenAI Chat Completions 的多步工具循环

### 聊天与 Agent 工具调用

Google 原生流式接口 (`/v1beta/models/{model}:streamGenerateContent`) 默认将 `google_stream_auto_tools` 设为 `false`. Open WebUI/NewAPI 这类聊天集成有时会在普通聊天里也发送 `tools` 和 `functionCallingConfig.mode=AUTO`. 如果把这些工具 schema 注入 Gemini Web prompt, prompt 会明显膨胀, 容易触发空回复或截断, 所以默认会把这个特定的 stream AUTO 场景当作普通流式聊天处理.

没有工具的 OpenAI/Responses/Anthropic 请求不会注入 Agent behavior 指令。Codex 走 `/v1/responses`, Claude Code 走 `/v1/messages`, Copilot/OpenAI 兼容 agent 走 `/v1/chat/completions`；这些客户端发送工具时仍保留完整 agent 能力。Agent 工具链中，完整 Agent 行为指令只会在第一次工具调用前注入；请求历史已经包含工具调用或工具结果时，后续轮不再重复该指令。

客户端通常会在每个 HTTP 请求里携带 `tools`，这是 Codex/Claude/Copilot 模型协议的正常行为。启用 `reuse_upstream_sessions` 后，服务使用 `gemini-webapi` 获取当前网页 token、动态模型 header 和完整 conversation metadata；SQLite 按消息历史前缀或工具 call ID 找回会话，后续轮只向同一个 Gemini 对话发送新增消息/工具结果。OpenAI Chat Completions、Codex Responses、Claude Messages，以及 Open WebUI 使用的 Google 原生 `/v1beta` 普通聊天都会复用这条链。若认证、metadata 或上游协议拒绝续接，服务自动改用压缩后的完整历史和旧 direct 后端重试。实际执行工具、继续循环直到最终回复的仍是接入的 agent 客户端。

## 可用模型

| 模型 | 说明 | 输出量 |
|------|------|--------|
| `gemini-3.5-flash` | 快速通用 | ~1.2万字 |
| `gemini-3.5-flash-thinking` | 深度思考, 最长输出 | **~2万字** |
| `gemini-3.5-flash-thinking-lite` | 自适应思考深度 | ~1.5万字 |
| `gemini-3.1-pro` | Pro (需 cookie 才能真正路由) | ~1.2万字 |
| `gemini-auto` | 自动选择模型 | 不定 |
| `gemini-flash-lite` | 轻量快速 | ~1万字 |

### 思考深度

在模型名后追加 `@think=N`:

```
gemini-3.5-flash-thinking@think=0   # 最深 (默认)
gemini-3.5-flash-thinking@think=2   # 中等
gemini-3.5-flash-thinking@think=4   # 最浅
```

## 可选: Cookie 配置 (Pro 模型)

匿名访问对所有模型有效, 但 `gemini-3.1-pro` 在无认证时会路由到 Flash. 要获得真正的 Pro 路由, 需要 **Gemini Advanced (付费订阅)** 账号的 cookie:

```bash
python -m gemini_web2api --cookie-file cookie.txt
```

### 如何获取 Cookie

1. 打开 Chrome, 访问 [gemini.google.com](https://gemini.google.com) 并登录 **Gemini Advanced** 付费账号
2. 打开开发者工具 (F12) → Application → Cookies → `https://gemini.google.com`
3. 至少复制同一浏览器会话的 `__Secure-1PSID` 和 `__Secure-1PSIDTS`；完整导出时也可保留 `SID`, `HSID`, `SSID`, `APISID`, `SAPISID`
4. 创建 `cookie.txt`, 格式如下:

```
SID=你的SID值; HSID=你的HSID值; SSID=你的SSID值; APISID=你的APISID值; SAPISID=你的SAPISID值; __Secure-1PSID=你的1PSID值; __Secure-1PSIDTS=你的1PSIDTS值
```

或使用 JSON 格式:
```json
{"cookie": "SID=xxx; HSID=xxx; SSID=xxx; APISID=xxx; SAPISID=xxx; __Secure-1PSID=xxx; __Secure-1PSIDTS=xxx", "sapisid": "你的SAPISID值"}
```

**替代方案 (浏览器扩展)**: 使用任意 "Export Cookies" 扩展导出 `gemini.google.com` 的 cookie, 然后转换为上述单行格式.

### 登录账号路径与 XSRF Token

如果已登录的 Gemini 页面 URL 带账号序号, 例如:

```
https://gemini.google.com/u/1/app/...
```

请把 `auth_user` 设置为该序号。登录态的 Gemini Web 请求还可能需要页面里的 XSRF token。该 token 在渲染后的 Gemini 页面源码中名为 `SNlM0e`; 在 `config.json` 中填入 `xsrf_token` 后, 服务会把它作为 `at` 表单字段提交。

示例:

```json
{
  "cookie_file": "/app/cookie.txt",
  "auth_user": "1",
  "xsrf_token": "AOOh0P...",
  "gemini_bl": "boq_assistant-bard-web-server_YYYYMMDD.xx_p0"
}
```

如果登录态请求返回 HTTP 400 且错误中包含 `xsrf`, 请刷新 Gemini Web 后更新 `xsrf_token`, 并确认 `auth_user` 与浏览器 URL 中的 `/u/<序号>/` 一致.

Pro 路由需要 **Gemini Advanced** (付费订阅). 免费 Google 账号的 cookie 可以登录认证, 但会静默回退到 Flash.

## 配置文件

在同目录创建 `config.json`:

```json
{
  "port": 8081,
  "host": "0.0.0.0",
  "retry_attempts": 3,
  "retry_delay_sec": 2,
  "request_timeout_sec": 180,
  "gemini_bl": "boq_assistant-bard-web-server_20260525.09_p0",
  "auth_user": null,
  "xsrf_token": null,
  "api_keys": ["sk-your-key"],
  "cookie_file": null,
  "proxy": null,
  "log_requests": true,
  "response_store_path": "responses.db",
  "response_store_ttl_sec": 86400,
  "response_store_max_rows": 1000,
  "max_tool_output_chars": 12000,
  "max_history_messages": 40,
  "max_history_chars": 60000,
  "max_google_prompt_chars": 18000,
  "google_stream_auto_tools": false,
  "continuation_attempts": 2,
  "sse_heartbeat_sec": 10,
  "reuse_upstream_sessions": false,
  "upstream_session_backend": "gemini_webapi",
  "upstream_session_fallback_direct": true,
  "reuse_upstream_agent_sessions": true,
  "agent_use_webapi": true,
  "agent_webapi_rebuild_on_failure": true,
  "agent_request_timeout_sec": 75,
  "agent_retry_attempts": 1,
  "cookie_cache_path": "/app/data/gemini_cookies",
  "cookie_auto_refresh": true,
  "cookie_refresh_interval_sec": 600,
  "webapi_watchdog_sec": 120,
  "webapi_request_timeout_sec": 180,
  "tool_retry_attempts": 1,
  "temporary_background_tasks": true,
  "require_authenticated_webapi": true
}
```

`api_keys` 为空数组 `[]` 时不校验密钥；填入一个或多个密钥后, `/v1/*` 接口需要 `Authorization: Bearer <key>` 或 `x-api-key: <key>`.

Agent 相关配置:
- `response_store_path`: Responses API 状态的 SQLite 文件；Docker 部署时建议挂载为 volume, 让历史在容器重建后仍保留
- `response_store_ttl_sec`: 历史保留时间
- `max_tool_output_chars`: shell/tool 输出进入上下文前的首尾截断长度
- `max_history_messages` / `max_history_chars`: 历史上下文压缩上限
- `max_google_prompt_chars`: Google 原生接口发往上游的 prompt 字符上限；超长时优先裁掉更早的上下文, 降低空回复/截断概率
- `google_stream_auto_tools`: 保持 `false` 可优先保证 Open WebUI/NewAPI 这类流式聊天稳定；只有需要 Google 原生流式 AUTO 工具调用时才设为 `true`
- `continuation_attempts`: Gemini Web 明确返回输出上限标记 (`BardErrorInfo 1155`) 时，自动从断点续写的最大轮数
- `sse_heartbeat_sec`: 等待 Gemini 首段或 agent 工具决策期间发送 SSE 注释心跳的间隔，避免 NewAPI、Open WebUI 或反向代理把仍在工作的请求当成断连
- `reuse_upstream_sessions`: 启用 Gemini Web 上游会话复用；保存完整 metadata，适用于普通聊天和 Chat Completions、Claude Messages、Codex Responses、Google 原生 `/v1beta` 的 Agent 工具循环。匿名部署默认保持 `false`；配置同一浏览器会话的 Cookie 后再启用
- `upstream_session_backend`: `gemini_webapi` 使用外部 Gemini 网页会话库，支持动态页面 token、模型 header 和 Cookie 刷新；`direct` 指项目自身直接请求 Gemini Web 内部 `StreamGenerate` 端点的旧实现，不是官方 API，也不是无状态模型接口。两种后端都能携带 Gemini 网页会话 metadata/CID
- `upstream_session_fallback_direct`: 新后端初始化或续接失败时，自动用完整历史回退到 direct 后端
- `reuse_upstream_agent_sessions`: 在 SQLite 中保存 Agent `call_id` 到 Gemini Web metadata 的映射。完整 Agent 行为提示和工具定义只在创建网页会话时发送；后续只发送规范化的新增工具事件和用户追问
- `agent_use_webapi`: 以登录后的 Gemini 网页会话作为 Agent 主后端。工具仍由 Codex、Claude Code 或 Copilot 执行，结果会作为增量外部工具事件续接到同一个 Gemini 对话
- `agent_webapi_rebuild_on_failure`: 已保存的 Gemini CID 无法续接时，先把压缩后的完整 Agent 历史发送到一个新网页会话并替换 SQLite 映射，再考虑回退 direct 后端
- `agent_request_timeout_sec` / `agent_retry_attempts`: 仅用于 Agent 的网页和 direct 单轮限制。默认值（`75` 秒、`1` 次）避免卡住时等待数分钟；普通聊天继续使用自己的超时和重试设置
- `cookie_cache_path`: 自动轮换后的 Google Cookie 私有缓存目录；必须放在持久化 volume 中，不能提交 Git
- `cookie_auto_refresh` / `cookie_refresh_interval_sec`: 后台轮换 `__Secure-1PSIDTS` 并保存，避免长期运行后认证过期
- `webapi_watchdog_sec`: Gemini 流长时间无数据时判定停滞并恢复的阈值
- `webapi_request_timeout_sec`: 新上游非流式请求的总等待上限，以及流式请求相邻输出之间的闲置上限；超时会取消后台任务并按配置回退 direct
- `tool_retry_attempts`: 模型应该调用工具却返回文本时的修复重试次数
- `temporary_background_tasks`: 识别 Open WebUI 默认的标题、标签、后续问题和图片提示词后台请求，并使用 Gemini 临时聊天发送；这些辅助请求仍会正常返回结果，但不会出现在 Gemini 网页历史中，只有真实对话会保留
- `require_authenticated_webapi`: 使用持久化上游会话前要求 Gemini 账号状态为 `AVAILABLE`；Cookie 过期时会明确记录并走已配置的 direct 回退，不再静默创建无法出现在账号历史中的匿名会话

Agent 网页续接采用增量方式：首轮只发送一次行为提示、工具定义和任务，并在工具调用后把 Gemini 会话 metadata 按客户端 `call_id` 保存到 SQLite。后续轮恢复同一个 CID，只发送规范化后的新增工具调用/结果事件和新增用户文本。如果外部 `gemini-webapi` 适配器拒绝当前账号会话，兜底仍请求 Gemini Web 的 `StreamGenerate` 端点；只要 Google 返回可用 metadata，就继续复用 CID。

流式接口不会再把空上游响应作为正常的 `STOP` 返回。空响应会按 `retry_attempts` 自动重试；检测到 1155 截断时会自动续写并去除重叠片段。SSE 心跳只是注释帧，不会显示在聊天正文，也不会改变 Codex、Claude Code、Copilot 的工具调用协议。

## Docker 部署

```bash
cp config.example.json config.json
docker build -t gemini-web2api .
docker run -d --name gemini-web2api -p 8081:8081 \
  -v ./config.json:/app/config.json \
  -v gemini-web2api-data:/app/data \
  gemini-web2api
```

Podman 使用同样的命名 volume 即可。如果没有持久化挂载 `/app/data`，删除并重建容器时 SQLite 历史可能丢失。

或使用 Docker Compose:

```bash
cp config.example.json config.json
docker compose up -d
```

如需挂载 Cookie 文件:

```bash
docker run -d --name gemini-web2api -p 8081:8081 -v ./config.json:/app/config.json -v ./cookie.txt:/app/cookie.txt gemini-web2api
```

此时 `config.json` 中设置 `"cookie_file": "/app/cookie.txt"`.

> **注意**: 如果 Docker 默认 bridge 网络下出现空回复 (`content: null`), 请切换到 host 网络: `docker run --network host ...` 或在 compose 文件中添加 `network_mode: host`. 这是 Gemini 上游拒绝来自 Docker NAT IP 段的请求导致的.

## 代理配置

如果无法直接访问 `gemini.google.com` (连接超时), 需要配置代理:

**方式 1: 命令行参数**
```bash
python -m gemini_web2api --proxy http://127.0.0.1:7890
```

**方式 2: config.json**
```json
{"proxy": "http://127.0.0.1:7890"}
```

**方式 3: 环境变量** (自动检测)
```bash
set HTTPS_PROXY=http://127.0.0.1:7890
python -m gemini_web2api
```

支持 Clash, V2Ray, Shadowsocks 等任何 HTTP 代理.

## 已知限制

- **不支持图片/多模态输入**: Gemini 的图片上传需要专有的 WIZ streaming RPC 协议 (ProcessFile), 无法在标准 HTTP 代理中实现. 发送图片会被忽略并返回提示.
- **Pro/Ultra 非真实路由**: 无付费订阅 cookie 时, `gemini-3.1-pro` 实际路由到 Flash 模型. "Pro" 只是 UI 偏好标签.
- **上游协议仍是非官方实现**: Google 改动网页协议、模型 header 或风控规则后仍可能失效；服务会回退到完整历史重放，但不等同于官方 Gemini API 的稳定性.
- **频率限制**: Google 可能限制高频请求, server 会自动重试但持续高负载可能被封.

## 系统要求

- Python 3.10+
- `gemini-webapi` — 动态认证、模型发现、Cookie 轮换和会话 metadata
- `httpx` — direct 回退路径的流式请求
- 需要能访问 `gemini.google.com` (部分地区需代理)

## 工作原理

服务把 OpenAI/Anthropic/Gemini 格式转成 Gemini Web 请求。主会话后端复用 `HanaokaYuzu/Gemini-API` 的动态网页 token、模型发现、Cookie 轮换和 `ChatSession.metadata`；SQLite 保存 metadata 与客户端消息历史的关联。旧的 `[79]` 模式请求仅作为 direct 回退。

适配层会为每个 API 对话创建独立的 metadata 列表，并显式恢复目标 CID。这也规避了 `gemini-webapi 2.0.0` 发布包中 `DEFAULT_METADATA` 被不同 `ChatSession` 共享的问题，防止互不相关的 API 对话串入同一条 Gemini 网页历史。

## 致谢

- [linux.do](https://linux.do) 社区
- [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) — Gemini Web 动态认证和会话客户端
- [Nativu5/Gemini-FastAPI](https://github.com/Nativu5/Gemini-FastAPI) — 消息历史前缀匹配与持久会话设计参考
- 开源 API 代理生态

## License

MIT
