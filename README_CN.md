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
python gemini_web2api.py
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
- 使用 SQLite 保存 Responses 历史, 支持 `previous_response_id` 和 `GET /v1/responses/{id}`
- 对超长工具输出和旧历史做确定性截断/压缩
- 在 prompt 上下文中保留 Anthropic `thinking` / `redacted_thinking` 信息

### 聊天与 Agent 工具调用

Google 原生流式接口 (`/v1beta/models/{model}:streamGenerateContent`) 默认将 `google_stream_auto_tools` 设为 `false`. Open WebUI/NewAPI 这类聊天集成有时会在普通聊天里也发送 `tools` 和 `functionCallingConfig.mode=AUTO`. 如果把这些工具 schema 注入 Gemini Web prompt, prompt 会明显膨胀, 容易触发空回复或截断, 所以默认会把这个特定的 stream AUTO 场景当作普通流式聊天处理.

这不会关闭 agent 能力. Codex 走 `/v1/responses`, Claude Code 走 `/v1/messages`, Copilot/OpenAI 兼容 agent 走 `/v1/chat/completions`; 这些端点仍保留工具调用、修复重试、SQLite 的 `previous_response_id` 状态和多步执行能力. 非流式 Google 原生 `generateContent` 也保留 function calling. 只有在你明确需要 Google 原生流式 AUTO 工具调用, 并且能接受更高的 prompt 膨胀/截断风险时, 才建议把 `google_stream_auto_tools` 改成 `true`.

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
python gemini_web2api.py --cookie-file cookie.txt
```

### 如何获取 Cookie

1. 打开 Chrome, 访问 [gemini.google.com](https://gemini.google.com) 并登录 **Gemini Advanced** 付费账号
2. 打开开发者工具 (F12) → Application → Cookies → `https://gemini.google.com`
3. 复制以下 cookie 值: `SID`, `HSID`, `SSID`, `APISID`, `SAPISID`, `__Secure-1PSID`
4. 创建 `cookie.txt`, 格式如下:

```
SID=你的SID值; HSID=你的HSID值; SSID=你的SSID值; APISID=你的APISID值; SAPISID=你的SAPISID值; __Secure-1PSID=你的1PSID值
```

或使用 JSON 格式:
```json
{"cookie": "SID=xxx; HSID=xxx; SSID=xxx; APISID=xxx; SAPISID=xxx; __Secure-1PSID=xxx", "sapisid": "你的SAPISID值"}
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
  "tool_retry_attempts": 1
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
- `tool_retry_attempts`: 模型应该调用工具却返回文本时的修复重试次数

流式接口不会再把空上游响应作为正常的 `STOP` 返回。空响应会按 `retry_attempts` 自动重试；检测到 1155 截断时会自动续写并去除重叠片段。SSE 心跳只是注释帧，不会显示在聊天正文，也不会改变 Codex、Claude Code、Copilot 的工具调用协议。

## Docker 部署

```bash
cp config.example.json config.json
docker build -t gemini-web2api .
docker run -d --name gemini-web2api -p 8081:8081 -v ./config.json:/app/config.json gemini-web2api
```

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
python gemini_web2api.py --proxy http://127.0.0.1:7890
```

**方式 2: config.json**
```json
{"proxy": "http://127.0.0.1:7890"}
```

**方式 3: 环境变量** (自动检测)
```bash
set HTTPS_PROXY=http://127.0.0.1:7890
python gemini_web2api.py
```

支持 Clash, V2Ray, Shadowsocks 等任何 HTTP 代理.

## 已知限制

- **不支持图片/多模态输入**: Gemini 的图片上传需要专有的 WIZ streaming RPC 协议 (ProcessFile), 无法在标准 HTTP 代理中实现. 发送图片会被忽略并返回提示.
- **Pro/Ultra 非真实路由**: 无付费订阅 cookie 时, `gemini-3.1-pro` 实际路由到 Flash 模型. "Pro" 只是 UI 偏好标签.
- **单轮对话**: 每次请求是独立对话, 多轮上下文通过在 prompt 中包含历史消息模拟.
- **频率限制**: Google 可能限制高频请求, server 会自动重试但持续高负载可能被封.

## 系统要求

- Python 3.8+
- `httpx` (`pip install httpx`) — 用于流式请求
- 需要能访问 `gemini.google.com` (部分地区需代理)

## 工作原理

逆向 Google Gemini 网页端的 StreamGenerate 协议, 将 OpenAI API 格式与 Gemini 内部 protobuf-like 格式互转. 模型选择通过请求 payload 的 `[79]` 字段控制, 映射自 Gemini 前端 JS 源码中的 `MODE_CATEGORY` 枚举.

## 致谢

- [linux.do](https://linux.do) 社区
- 开源 API 代理生态

## License

MIT
