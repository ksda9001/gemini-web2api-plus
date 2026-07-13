# gemini-web2api

<p align="center">
  <img src="logo.png" width="200" alt="gemini-web2api logo">
</p>

[中文文档](README_CN.md)

Convert Google Gemini's web interface into an OpenAI-compatible API. Zero cost, cross-platform, single file.

## Features

- **Optional API Keys**: no auth when `api_keys` is empty, OpenAI-style Bearer auth when configured
- **OpenAI Compatible**: Drop-in replacement for `/v1/chat/completions` and `/v1/models`
- **Tool Calling**: Full function calling support (OpenAI format)
- **Multiple Models**: Flash, Flash Thinking (20k+ char output), Pro, Auto, Lite
- **Thinking Depth**: Adjustable via `@think=N` suffix (0=deepest, 4=shallowest)
- **Web Search**: Built-in internet access (Gemini's native search)
- **Cross-Platform**: Pure Python, single optional dependency (`httpx` for streaming)
- **Streaming**: SSE streaming support via `httpx`
- **Codex CLI**: Responses API (`/v1/responses`) for OpenAI Codex integration
- **Gemini CLI**: Google native API (`/v1beta/models`) for Gemini CLI compatibility

## Quick Start

```bash
pip install httpx
python -m gemini_web2api
```

Server starts at `http://localhost:8081/v1`.

## Client Configuration

### Cherry Studio / ChatBox / any OpenAI client

| Field | Value |
|-------|-------|
| Base URL | `http://localhost:8081/v1` |
| API Key | any `api_keys` value from `config.json`; anything if not configured |
| Model | `gemini-3.5-flash-thinking` |

### curl

#### bash / macOS / Linux

```bash
curl http://localhost:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-key" \
  -d '{"model":"gemini-3.5-flash","messages":[{"role":"user","content":"Hello!"}]}'
```

#### PowerShell (Windows)

```powershell
curl.exe --% http://127.0.0.1:8081/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer sk-your-key" -d "{\"model\":\"gemini-3.5-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello!\"}]}"
```

> Note: On Windows PowerShell, use `curl.exe` and `--%` so PowerShell does not reinterpret JSON quoting or curl options.

### OpenAI Python SDK

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8081/v1", api_key="sk-your-key")
resp = client.chat.completions.create(
    model="gemini-3.5-flash-thinking",
    messages=[{"role": "user", "content": "Explain quantum computing"}]
)
print(resp.choices[0].message.content)
```

### Gemini CLI

```bash
export GEMINI_API_KEY=none
export GOOGLE_GEMINI_BASE_URL=http://localhost:8081
gemini
```

Supports Google native API endpoints:
- `GET /v1beta/models` — list models
- `POST /v1beta/models/{model}:generateContent` — non-streaming
- `POST /v1beta/models/{model}:streamGenerateContent` — streaming (SSE)

### Agent Clients

Codex CLI, Claude Code, and Copilot-style coding agents require streaming tool-use protocols, not just plain chat completions. This server exposes the compatible endpoints below:

| Client | Base URL | API surface |
|--------|----------|-------------|
| Codex CLI | `http://localhost:8081/v1` | OpenAI Responses API (`/v1/responses`) |
| Claude Code | `http://localhost:8081` | Anthropic Messages API (`/v1/messages`) |
| Copilot / OpenAI-compatible agents | `http://localhost:8081/v1` | Chat Completions (`/v1/chat/completions`) |

Example Codex provider:

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

Example Claude Code environment:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8081
export ANTHROPIC_AUTH_TOKEN=sk-your-key
export ANTHROPIC_MODEL=gemini-3.5-flash
```

Agent compatibility includes:
- automatic tool-call repair retry when the model describes an action instead of calling a tool
- rejection and repair of model-invented tool names that were not declared by the client
- SQLite-backed Responses history for `previous_response_id` and `GET /v1/responses/{id}`
- complete 10-field Gemini Web conversation metadata persisted for plain chat and agent requests
- the full Agent behavior instruction only on the first tool turn, plus compact JSON tool schemas on stateless follow-up turns
- deterministic truncation/compaction of long tool outputs and old history
- Anthropic `thinking` / `redacted_thinking` preservation in prompt context
- tested multi-step loops for Codex Responses, Claude Messages, and Copilot/OpenAI Chat Completions

### Chat vs Agent Tool Use

Google native streaming requests (`/v1beta/models/{model}:streamGenerateContent`) default `google_stream_auto_tools` to `false`. Many chat UIs, including Open WebUI/NewAPI-style integrations, may send `tools` plus `functionCallingConfig.mode=AUTO` even for ordinary chat. Injecting those tool schemas into the Gemini Web prompt can make the prompt very large and cause empty or truncated replies, so the default treats that specific stream AUTO case as plain streaming chat.

Tool-free OpenAI, Responses, and Anthropic requests do not receive the Agent behavior instruction. Codex uses `/v1/responses`, Claude Code uses `/v1/messages`, and Copilot/OpenAI-compatible agents use `/v1/chat/completions`; requests that actually provide tools keep complete agent behavior. On an agent request, the full Agent behavior instruction is injected only before the first tool call. Follow-up requests whose history already contains a tool call/result omit that instruction.

Clients normally include `tools` in every HTTP request as part of Codex, Claude, and Copilot model protocols. With `reuse_upstream_sessions` enabled, the server uses `gemini-webapi` for live page tokens, dynamic model headers, cookie rotation, and complete conversation metadata. SQLite restores sessions by message-history prefix or tool call ID, so follow-up turns send only new messages/tool results. This applies to OpenAI Chat Completions, Codex Responses, Claude Messages, and Google-native `/v1beta` plain chats used by Open WebUI. If authentication or metadata continuation fails, the request automatically falls back to compressed full-history replay through the legacy direct backend. The connected agent client still executes each tool and drives the loop until the model returns a final answer.

## Available Models

| Model | Description | Output |
|-------|-------------|--------|
| `gemini-3.5-flash` | Fast general-purpose | ~12k chars |
| `gemini-3.5-flash-thinking` | Deep thinking, longest output | **~20k chars** |
| `gemini-3.5-flash-thinking-lite` | Adaptive thinking depth | ~15k chars |
| `gemini-3.1-pro` | Pro (needs cookie for real routing) | ~12k chars |
| `gemini-auto` | Auto model selection | varies |
| `gemini-flash-lite` | Lightweight fast | ~10k chars |

### Thinking Depth

Append `@think=N` to any model name:

```
gemini-3.5-flash-thinking@think=0   # deepest (default)
gemini-3.5-flash-thinking@think=2   # medium
gemini-3.5-flash-thinking@think=4   # shallowest
```

## Optional: Cookie for Pro

Anonymous access works for all models, but `gemini-3.1-pro` routes to Flash without authentication. To get real Pro routing, you need a **Gemini Advanced (paid subscription)** account cookie:

```bash
python -m gemini_web2api --cookie-file cookie.txt
```

### How to get cookies

1. Open Chrome, go to [gemini.google.com](https://gemini.google.com) and sign in with a **Gemini Advanced** Google account
2. Open DevTools (F12) → Application → Cookies → `https://gemini.google.com`
3. Copy `__Secure-1PSID` and `__Secure-1PSIDTS` from the same browser session; a full export may also retain `SID`, `HSID`, `SSID`, `APISID`, and `SAPISID`
4. Create `cookie.txt` in this format:

```
SID=your_sid_value; HSID=your_hsid_value; SSID=your_ssid_value; APISID=your_apisid_value; SAPISID=your_sapisid_value; __Secure-1PSID=your_1psid_value; __Secure-1PSIDTS=your_1psidts_value
```

Or use the JSON format:
```json
{"cookie": "SID=xxx; HSID=xxx; SSID=xxx; APISID=xxx; SAPISID=xxx; __Secure-1PSID=xxx; __Secure-1PSIDTS=xxx", "sapisid": "your_sapisid_value"}
```

**Alternative (browser extension)**: Use any "Export Cookies" extension to export cookies for `gemini.google.com` in Netscape format, then convert to the single-line format above.

### Authenticated account path and XSRF token

If the signed-in Gemini page URL contains an account index, such as:

```
https://gemini.google.com/u/1/app/...
```

set `auth_user` to that index. Authenticated web requests may also require the page XSRF token. In the rendered Gemini page source, this token is exposed as `SNlM0e`; pass it as `xsrf_token` in `config.json`. The server sends it as the `at` form field.

Example:

```json
{
  "cookie_file": "/app/cookie.txt",
  "auth_user": "1",
  "xsrf_token": "AOOh0P...",
  "gemini_bl": "boq_assistant-bard-web-server_YYYYMMDD.xx_p0"
}
```

If authenticated requests return HTTP 400 with an `xsrf` error, refresh Gemini Web, update `xsrf_token`, and make sure `auth_user` matches the `/u/<index>/` part of the browser URL.

Pro routing requires **Gemini Advanced** (paid subscription). A free Google account cookie will authenticate but silently fall back to Flash.

## Configuration

Create `config.json` in the same directory:

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

When `api_keys` is `[]`, authentication is disabled. When one or more keys are set, `/v1/*` endpoints require `Authorization: Bearer <key>` or `x-api-key: <key>`.

Agent-related config:
- `response_store_path`: SQLite file for Responses API state; mount it as a volume in Docker if you want history to survive container recreation
- `response_store_ttl_sec`: history retention window
- `max_tool_output_chars`: head/tail truncation limit for shell/tool outputs stored in context
- `max_history_messages` / `max_history_chars`: deterministic context compaction limits
- `max_google_prompt_chars`: hard cap for Google native prompt text sent upstream; older context is trimmed first to reduce empty/truncated responses
- `google_stream_auto_tools`: keep `false` to prioritize stable Open WebUI/NewAPI-style streaming chat; set `true` only to enable Google native streaming AUTO function calling
- `continuation_attempts`: maximum automatic continuation turns when Gemini Web reports its output-limit marker (`BardErrorInfo 1155`)
- `sse_heartbeat_sec`: SSE comment heartbeat interval while waiting for Gemini's first output or an agent tool decision, keeping NewAPI, Open WebUI, and reverse proxies from treating active work as a dead connection
- `reuse_upstream_sessions`: enable Gemini Web continuation with complete metadata for plain chats and Agent tool loops across Chat Completions, Claude Messages, Codex Responses, and Google-native `/v1beta`. It defaults to `false` for anonymous deployments; enable it after mounting cookies from one browser session
- `upstream_session_backend`: `gemini_webapi` uses the external Gemini Web session library with dynamic page tokens/model headers and cookie refresh; `direct` means this project's legacy direct request to Gemini Web's internal `StreamGenerate` endpoint, not an official or stateless model API. Both backends can carry Gemini Web conversation metadata/CIDs
- `upstream_session_fallback_direct`: replay full history through the direct backend if the primary backend cannot initialize or resume
- `reuse_upstream_agent_sessions`: persist the Agent call ID to Gemini Web metadata mapping in SQLite. The full Agent behavior prompt and tool schema are sent when the Web conversation is created; later turns send only normalized new tool events and user follow-ups
- `agent_use_webapi`: use the authenticated Gemini Web conversation as the primary Agent backend. Tool calls still execute in Codex, Claude Code, or Copilot; their results are encoded as incremental external-tool events in the same Gemini conversation
- `agent_webapi_rebuild_on_failure`: if a saved Gemini CID cannot resume, replay the compacted full Agent history into one fresh Gemini Web conversation and replace the SQLite mapping before falling back to the direct backend
- `agent_request_timeout_sec` / `agent_retry_attempts`: limits used only by Agent turns for both Web and direct requests. The defaults (`75` seconds, `1` attempt) avoid spending several minutes on a stalled request before recovery; ordinary chat retains its own timeout and retry settings
- `cookie_cache_path`: private persistent directory for rotated Google cookies; mount it as a volume and never commit it
- `cookie_auto_refresh` / `cookie_refresh_interval_sec`: rotate and persist `__Secure-1PSIDTS` in the background
- `webapi_watchdog_sec`: no-progress timeout for a stalled Gemini Web stream
- `webapi_request_timeout_sec`: total wait for non-stream requests and idle wait between streaming deltas; expiration cancels the background task and allows the configured direct fallback
- `tool_retry_attempts`: repair retries when the model should call a tool but returns text
- `temporary_background_tasks`: recognize Open WebUI's default title, tags, follow-up, and image-prompt helper requests and send them as Gemini temporary chats, so only the real conversation appears in Gemini Web history
- `require_authenticated_webapi`: require Gemini account status `AVAILABLE` before using persistent upstream sessions; expired cookies are reported and routed through the configured direct fallback instead of silently creating anonymous conversations

Agent Web continuation is incremental: the initial turn sends the behavior instruction, tool schema, and task once. A successful tool call saves its Gemini conversation metadata under the client call ID in SQLite. Later turns resume that CID with only the new normalized tool call/result event and any new user text. If the external `gemini-webapi` adapter rejects the account session, the fallback still targets Gemini Web's `StreamGenerate` endpoint and preserves CID continuation when Google returns usable metadata.

Streaming endpoints no longer report an empty upstream response as a successful `STOP`. Empty responses are retried according to `retry_attempts`; an explicit 1155 truncation is continued automatically with overlapping text removed. SSE heartbeats are comment frames, so they do not appear in chat content or alter the Codex, Claude Code, or Copilot tool protocols.

## Docker

```bash
cp config.example.json config.json
docker build -t gemini-web2api .
docker run -d --name gemini-web2api -p 8081:8081 \
  -v ./config.json:/app/config.json \
  -v gemini-web2api-data:/app/data \
  gemini-web2api
```

Use the equivalent named volume with Podman. Without a persistent `/app/data` mount, SQLite history can be lost when the container is removed and recreated.

Or use Docker Compose:

```bash
cp config.example.json config.json
docker compose up -d
```

To mount a cookie file:

```bash
docker run -d --name gemini-web2api -p 8081:8081 -v ./config.json:/app/config.json -v ./cookie.txt:/app/cookie.txt gemini-web2api
```

Set `"cookie_file": "/app/cookie.txt"` in `config.json`.

> **Note**: If you get empty responses (`content: null`) with Docker's default bridge network, switch to host networking: `docker run --network host ...` or add `network_mode: host` in your compose file. This is caused by Gemini's upstream rejecting requests from certain Docker NAT IP ranges.

## Proxy

If you cannot access `gemini.google.com` directly (connection timeout), configure a proxy:

**Method 1: CLI argument**
```bash
python -m gemini_web2api --proxy http://127.0.0.1:7890
```

**Method 2: config.json**
```json
{"proxy": "http://127.0.0.1:7890"}
```

**Method 3: Environment variable** (auto-detected)
```bash
export HTTPS_PROXY=http://127.0.0.1:7890
python -m gemini_web2api
```

Works with Clash, V2Ray, Shadowsocks, or any HTTP proxy.

## Tool Calling

```python
resp = client.chat.completions.create(
    model="gemini-3.5-flash",
    messages=[{"role": "user", "content": "What's the weather in Tokyo?"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
        }
    }]
)
```

## Limitations

- **No image/multimodal input**: Gemini's image upload requires a proprietary streaming RPC protocol (WIZ/ProcessFile) that cannot be replicated in a standard HTTP proxy. Image inputs in messages will be ignored with a note.
- **Not real Pro/Ultra**: Without a paid subscription cookie, `gemini-3.1-pro` routes to the same Flash model. The "Pro" label is a UI preference, not a backend model switch.
- **Unofficial upstream protocol**: Google web protocol, model-header, or risk-control changes can still break continuation. Full-history replay is a fallback, not an official API stability guarantee.
- **Rate limits**: Google may throttle high-frequency requests. The server retries automatically but sustained heavy use may be blocked.

## Requirements

- Python 3.10+
- `gemini-webapi` for dynamic authentication, model discovery, cookie rotation, and chat metadata
- `httpx` for streaming on the legacy direct fallback
- Network access to `gemini.google.com` (proxy/VPN may be needed in some regions)

## How It Works

This tool converts OpenAI, Anthropic, and Gemini requests into Gemini Web conversations. Its primary session backend reuses `HanaokaYuzu/Gemini-API` for dynamic page tokens, model discovery, cookie rotation, and `ChatSession.metadata`; SQLite links that metadata to client-visible history. The older `[79]` mode payload remains only as a direct fallback.

The adapter gives every API conversation its own metadata list and explicitly restores the intended CID. This also works around the shared `DEFAULT_METADATA` list in the published `gemini-webapi 2.0.0` wheel, preventing unrelated API conversations from being appended to one Gemini Web history item.

## Acknowledgments

- Inspired by the open-source API proxy ecosystem
- [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) for the dynamic Gemini Web session client
- [Nativu5/Gemini-FastAPI](https://github.com/Nativu5/Gemini-FastAPI) for persistent history-prefix session matching design

## License

MIT

---

## 致谢

本项目的开发 agent 能力由 [GenericAgent](https://github.com/lsdefine/GenericAgent) 提供。

### 🚩 友情链接

[![GenericAgent](https://img.shields.io/badge/Agent_Framework-GenericAgent-orange?style=for-the-badge&logo=github)](https://github.com/lsdefine/GenericAgent)
[![LinuxDo](https://img.shields.io/badge/社区-LinuxDo-blue?style=for-the-badge)](https://linux.do/)
