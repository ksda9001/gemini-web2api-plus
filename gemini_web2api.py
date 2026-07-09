#!/usr/bin/env python3
"""
gemini-web2api - Gemini Web to OpenAI API proxy.

Converts Google Gemini's web interface into an OpenAI-compatible API server.
Zero authentication required. Works on any platform (Windows/macOS/Linux).

Usage:
    pip install httpx
    python gemini_web2api.py [--port 8081] [--config config.json]

Client configuration (Cherry Studio, ChatBox, etc.):
    Base URL: http://localhost:8081/v1
    API Key: (anything or empty)

How it works:
    Sends requests directly to Gemini's public StreamGenerate endpoint.
    The backend does not verify authentication for basic text generation.
    Model selection via MODE_CATEGORY field [79] in the request payload.
    This is NOT a user-tier spoofing attack - the endpoint simply doesn't
    require auth for anonymous access.
"""
import json
import urllib.request
import urllib.parse
import time
import ssl
import sys
import uuid
import re
import os
import hashlib
import argparse
import base64
import threading
import sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

__version__ = "1.1.0"

# ─── Configuration ───────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "port": 8081,
    "host": "0.0.0.0",
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
    "gemini_bl": "boq_assistant-bard-web-server_20260525.09_p0",
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.5-flash",
    "log_requests": True,
    "cookie_file": None,
    "proxy": None,
    "api_keys": [],
    "response_store_path": "responses.db",
    "response_store_ttl_sec": 86400,
    "response_store_max_rows": 1000,
    "max_tool_output_chars": 12000,
    "max_history_messages": 40,
    "max_history_chars": 60000,
    "tool_retry_attempts": 1,
}

CONFIG = dict(DEFAULT_CONFIG)

RESPONSE_HISTORY = {}
RESPONSE_HISTORY_LOCK = threading.Lock()
RESPONSE_HISTORY_MAX = 100
RESPONSE_STORE = None

# ─── Models ──────────────────────────────────────────────────────────────────
# Mapping from JS source: MODE_CATEGORY enum (028-6eb337387583.js)
#   1=FAST, 2=THINKING, 3=PRO, 4=AUTO, 5=FAST_DYNAMIC_THINKING, 6=FLASH_LITE

MODELS = {
    "gemini-3.5-flash": {
        "mode": 1, "think": 4,
        "desc": "Fast general-purpose model",
    },
    "gemini-3.5-flash-thinking": {
        "mode": 2, "think": 0,
        "desc": "Deep thinking mode, longest output (~20k chars)",
    },
    "gemini-3.1-pro": {
        "mode": 3, "think": 4,
        "desc": "Pro model (requires cookie for real routing)",
    },
    "gemini-auto": {
        "mode": 4, "think": 4,
        "desc": "Auto model selection",
    },
    "gemini-3.5-flash-thinking-lite": {
        "mode": 5, "think": 0,
        "desc": "Dynamic thinking with adaptive depth",
    },
    "gemini-flash-lite": {
        "mode": 6, "think": 4,
        "desc": "Lightweight fast model",
    },
}

# ─── Utilities ───────────────────────────────────────────────────────────────

def log(msg: str):
    if CONFIG["log_requests"]:
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()


def load_cookie() -> tuple:
    """Load cookie from file. Returns (cookie_str, sapisid)."""
    cookie_file = CONFIG.get("cookie_file")
    if not cookie_file:
        return "", None
    if not os.path.exists(cookie_file):
        return "", None
    try:
        with open(cookie_file, "r") as f:
            content = f.read().strip()
        if content.startswith("{"):
            data = json.loads(content)
            cookie_str = data.get("cookie", "")
            sapisid = data.get("sapisid", "")
        else:
            cookie_str = content
            pairs = dict(p.split("=", 1) for p in cookie_str.split("; ") if "=" in p)
            sapisid = pairs.get("SAPISID", "")
        return cookie_str, sapisid if sapisid else None
    except Exception as e:
        log(f"Cookie load error: {e}")
        return "", None


def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


def account_prefix() -> str:
    """Return the Gemini account path prefix for non-default Google accounts."""
    auth_user = CONFIG.get("auth_user")
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


# ─── Gemini Protocol ─────────────────────────────────────────────────────────

def gemini_stream_generate(prompt: str, model_id: int, think_mode: int) -> str:
    """Send prompt to Gemini StreamGenerate with retry."""
    inner = [None] * 80
    inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    inner[41] = [2]
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id

    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if CONFIG.get("xsrf_token"):
        params["at"] = CONFIG["xsrf_token"]
    body = urllib.parse.urlencode(params).encode()
    reqid = int(time.time()) % 1000000
    prefix = account_prefix()
    url = (
        f"https://gemini.google.com{prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if prefix:
        headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])

    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)

    last_err = None
    for attempt in range(CONFIG["retry_attempts"]):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            ctx = ssl.create_default_context()
            proxy = CONFIG.get("proxy")
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx)
                )
                resp = opener.open(req, timeout=CONFIG["request_timeout_sec"])
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=CONFIG["request_timeout_sec"])
            return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
    raise last_err


def gemini_stream_generate_iter(prompt: str, model_id: int, think_mode: int):
    """Send prompt and yield incremental text deltas using httpx streaming."""
    inner = [None] * 80
    inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    inner[41] = [2]
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id

    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if CONFIG.get("xsrf_token"):
        params["at"] = CONFIG["xsrf_token"]
    body = urllib.parse.urlencode(params)
    reqid = int(time.time()) % 1000000
    prefix = account_prefix()
    url = (
        f"https://gemini.google.com{prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if prefix:
        headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)

    proxy = CONFIG.get("proxy")

    if not HAS_HTTPX:
        # Fallback: non-streaming with urllib
        raw = gemini_stream_generate(prompt, model_id, think_mode)
        text = extract_response_text(raw)
        if text:
            yield text
        return

    prev_text = ""
    transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
    with httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True) as client:
        with client.stream("POST", url, content=body, headers=headers) as resp:
            buf = ""
            for chunk in resp.iter_text():
                buf += chunk
                if "BardErrorInfo" in buf:
                    import re as _re
                    m = _re.search(r'BardErrorInfo\s*\[(\d+)\]', buf)
                    if m:
                        raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{m.group(1)}]")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if '"wrb.fr"' not in line or len(line) < 200:
                        continue
                    try:
                        arr = json.loads(line)
                        inner_str = arr[0][2]
                        if not inner_str or len(inner_str) < 50:
                            continue
                        inner2 = json.loads(inner_str)
                        if isinstance(inner2, list) and len(inner2) > 4 and inner2[4]:
                            for part in inner2[4]:
                                if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                                    for t in part[1]:
                                        if isinstance(t, str) and len(t) > len(prev_text):
                                            delta = t[len(prev_text):]
                                            delta = clean_gemini_text(delta)
                                            if delta:
                                                yield delta
                                            prev_text = t
                    except (json.JSONDecodeError, IndexError, TypeError):
                        pass


def clean_gemini_text(text: str) -> str:
    """Remove internal code execution artifacts."""
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    return text.strip()


def extract_response_text(raw: str) -> str:
    """Parse StreamGenerate response to extract final text."""
    import re as _re
    bard_err = _re.search(r'BardErrorInfo\s*\[(\d+)\]', raw)
    if bard_err:
        raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{bard_err.group(1)}]")
    texts = []
    for line in raw.split("\n"):
        if '"wrb.fr"' not in line or len(line) < 200:
            continue
        try:
            arr = json.loads(line)
            inner_str = arr[0][2]
            if not inner_str or len(inner_str) < 50:
                continue
            inner = json.loads(inner_str)
            if isinstance(inner, list) and len(inner) > 4 and inner[4]:
                for part in inner[4]:
                    if isinstance(part, list) and len(part) > 1 and part[1]:
                        if isinstance(part[1], list):
                            for t in part[1]:
                                if isinstance(t, str) and len(t) > 0:
                                    texts.append(t)
        except (json.JSONDecodeError, IndexError, TypeError):
            pass
    text = ""
    for t in reversed(texts):
        if t.strip():
            text = t
            break
    return clean_gemini_text(text)


# ─── OpenAI Format Helpers ───────────────────────────────────────────────────

AGENT_BEHAVIOR_INSTRUCTION = (
    "Agent behavior:\n"
    "- If the user asks to create, edit, read, delete, list, move, or inspect files; "
    "run commands; install dependencies; execute tests; open URLs; or otherwise act on the local environment, "
    "call the appropriate tool instead of describing what you would do.\n"
    "- When the user asks to generate or create a file, write the file through tools. "
    "Do not merely print the file contents in a normal text answer unless the user explicitly asks to only see the contents.\n"
    "- Work step by step. After receiving a tool result, decide whether another tool call is needed and continue "
    "until the user's task is complete.\n"
    "- Do not finish with a text answer while required file edits, commands, tests, or inspections remain undone.\n"
    "- Use the same natural language as the user's latest request for final answers and user-facing status text. "
    "Keep tool names, file paths, commands, and JSON arguments in their required syntax.\n"
)


def _build_tool_choice_instruction(tool_choice, tool_defs: list) -> str:
    if tool_choice == "none":
        return "\n\nIMPORTANT: Do NOT call any tools. Respond with text only."
    if tool_choice == "required":
        return "\n\nIMPORTANT: You MUST call at least one tool. Do not respond with text only."
    if isinstance(tool_choice, dict):
        fn_name = tool_choice.get("function", {}).get("name", "")
        if fn_name:
            return f'\n\nIMPORTANT: You MUST call the tool "{fn_name}". Do not call other tools.'
    return ""


def messages_to_prompt(messages: list, tools: list = None, tool_choice=None) -> str:
    """Convert OpenAI messages to prompt string."""
    parts = []
    parts.append(f"[System instruction]: {AGENT_BEHAVIOR_INSTRUCTION}")

    if tools and tool_choice != "none":
        tool_defs = []
        for tool in tools:
            fn = tool.get("function", tool) if tool.get("type") == "function" else tool
            tool_defs.append({
                "name": fn.get("name", tool.get("name", "")),
                "description": fn.get("description", tool.get("description", "")),
                "parameters": fn.get("parameters", tool.get("parameters", {})),
            })
        if tool_defs:
            parts.append(
                "[System instruction]: You have access to tools. "
                "To call a tool, respond with:\n"
                '```tool_call\n{"name": "func_name", "arguments": {...}}\n```\n'
                'If code fences are unavailable, output ONLY this raw JSON object: {"name": "func_name", "arguments": {...}}\n'
                "Only use tool_call blocks or raw JSON tool call objects when needed.\n\n"
                f"Available tools:\n{json.dumps(tool_defs, indent=2)}"
                f"{_build_tool_choice_instruction(tool_choice, tool_defs)}"
            )
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content
                if c.get("type") in ("text", "input_text")
            )
        if role == "system":
            parts.append(f"[System instruction]: {content}")
        elif role == "assistant":
            if msg.get("tool_calls"):
                tc_strs = []
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    tc_strs.append(
                        f'```tool_call\n{{"name": "{fn.get("name")}", '
                        f'"arguments": {fn.get("arguments", "{}")}}}\n```'
                    )
                parts.append(f"[Assistant]: {content or ''}\n" + "\n".join(tc_strs))
            else:
                parts.append(f"[Assistant]: {content}")
        elif role == "tool":
            parts.append(f"[Tool result for {msg.get('name', '')}]: {content}")
        else:
            parts.append(content if content else "")
    return "\n\n".join(p for p in parts if p)


def _tool_arguments_to_json(arguments) -> str:
    if arguments is None:
        return "{}"
    if isinstance(arguments, str):
        return arguments.strip() or "{}"
    return json.dumps(arguments, ensure_ascii=False)


def _normalize_tool_call(data: dict):
    if not isinstance(data, dict):
        return None

    name = None
    arguments = {}

    function = data.get("function")
    if isinstance(function, dict):
        name = function.get("name") or data.get("name")
        arguments = function.get("arguments", data.get("arguments", {}))
    elif data.get("type") == "tool_use":
        name = data.get("name")
        arguments = data.get("input", data.get("arguments", {}))
    else:
        name = data.get("name") or data.get("tool") or data.get("tool_name")
        if "arguments" in data:
            arguments = data.get("arguments")
        elif "args" in data:
            arguments = data.get("args")
        else:
            return None

    if not isinstance(name, str) or not name.strip():
        return None

    call_id = data.get("id") or data.get("call_id") or f"call_{uuid.uuid4().hex[:8]}"
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name.strip(),
            "arguments": _tool_arguments_to_json(arguments),
        },
    }


def _normalize_tool_call_payload(payload) -> list:
    if isinstance(payload, dict) and isinstance(payload.get("tool_calls"), list):
        payload = payload["tool_calls"]

    items = payload if isinstance(payload, list) else [payload]
    tool_calls = []
    for item in items:
        tool_call = _normalize_tool_call(item)
        if tool_call:
            tool_calls.append(tool_call)
    return tool_calls


def _json_from_single_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r'```(?:json|tool_call)?\s*\n(.*?)\n```', stripped, re.DOTALL)
    return match.group(1).strip() if match else stripped


def parse_tool_calls(text: str) -> tuple:
    """Extract tool_call blocks or raw JSON calls. Returns (clean_text, tool_calls_list)."""
    tool_calls = []
    pattern = r'```tool_call\s*\n(.*?)\n```'
    clean_parts = []
    last_end = 0
    for m in re.finditer(pattern, text, re.DOTALL):
        clean_parts.append(text[last_end:m.start()])
        last_end = m.end()
        try:
            data = json.loads(m.group(1).strip())
            tool_calls.extend(_normalize_tool_call_payload(data))
        except json.JSONDecodeError:
            pass
    clean_parts.append(text[last_end:])
    clean = "".join(clean_parts).strip()
    if not tool_calls and clean:
        try:
            data = json.loads(_json_from_single_fence(clean))
            raw_tool_calls = _normalize_tool_call_payload(data)
            if raw_tool_calls:
                return "", raw_tool_calls
        except json.JSONDecodeError:
            pass
    return clean, tool_calls


ACTION_RE = re.compile(
    r"(\b(create|generate|write|save|edit|modify|delete|remove|read|cat|list|ls|inspect|check|"
    r"run|execute|test|install|build|fix|patch|commit|push|open|download|fetch|search)\b"
    r"|创建|生成|写入|保存|修改|编辑|删除|读取|查看|列出|运行|执行|测试|安装|构建|修复|提交|推送|打开|下载|搜索|检查)",
    re.IGNORECASE,
)


def _json_clone(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def _text_from_content(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") in ("text", "input_text", "output_text"):
                    parts.append(item.get("text", ""))
                elif item.get("type") == "tool_result":
                    parts.append(_text_from_content(item.get("content", "")))
            elif item is not None:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    return str(content)


def _truncate_text(text, max_chars: int = 12000) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head_len = max_chars // 2
    tail_len = max_chars - head_len
    omitted = len(text) - head_len - tail_len
    return text[:head_len] + f"\n\n[... truncated {omitted} characters ...]\n\n" + text[-tail_len:]


def _compact_messages(messages: list, max_messages: int = 40, max_chars: int = 60000, max_tool_chars: int = 12000) -> list:
    compacted = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        msg = dict(msg)
        if msg.get("role") == "tool":
            msg["content"] = _truncate_text(msg.get("content", ""), max_tool_chars)
        compacted.append(msg)
    system = [m for m in compacted if m.get("role") == "system"][:1]
    non_system = [m for m in compacted if m.get("role") != "system"]
    first_user = []
    for msg in non_system:
        if msg.get("role") == "user":
            first_user = [msg]
            break
    protected_ids = {id(m) for m in system + first_user}
    recent = []
    for msg in reversed(non_system):
        if id(msg) not in protected_ids:
            recent.append(msg)
        if len(recent) >= max_messages:
            break
    result = system + first_user + list(reversed(recent))
    if len(json.dumps(result, ensure_ascii=False)) > max_chars:
        result = system + first_user + [{"role": "system", "content": "[Earlier conversation compacted.]"}] + list(reversed(recent[:max(1, max_messages // 2)]))
    return _json_clone(result)


def _latest_user_text(messages: list) -> str:
    for msg in reversed(messages or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            text = _text_from_content(msg.get("content", ""))
            if text.strip():
                return text
    return ""


def _any_user_action_text(messages: list) -> str:
    for msg in reversed(messages or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            text = _text_from_content(msg.get("content", ""))
            if text.strip() and ACTION_RE.search(text):
                return text
    return ""


def _should_retry_tool_call(messages: list, tools, tool_choice, text: str, tool_calls) -> bool:
    if not tools or tool_choice == "none" or tool_calls:
        return False
    if tool_choice == "required" or isinstance(tool_choice, dict):
        return True
    user_text = _any_user_action_text(messages) or _latest_user_text(messages)
    return bool(user_text and ACTION_RE.search(user_text))


def _build_tool_retry_prompt(prompt: str, tool_choice=None) -> str:
    target = ""
    if isinstance(tool_choice, dict):
        fn_name = tool_choice.get("function", {}).get("name", "")
        if fn_name:
            target = f' Call only "{fn_name}".'
    return (
        f"{prompt}\n\n"
        "[System instruction]: Your previous answer described the action instead of calling a tool. "
        "Return ONLY one valid tool_call block or raw JSON tool call object now."
        f"{target}"
    )


def _response_store_path() -> str:
    return CONFIG.get("response_store_path") or "responses.db"


def _response_store_init():
    path = _response_store_path()
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(
        "CREATE TABLE IF NOT EXISTS responses ("
        "id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
        "previous_response_id TEXT, model TEXT, response_json TEXT NOT NULL, messages_json TEXT NOT NULL)"
    )
    return con


def _response_store_save(response: dict, messages: list, previous_response_id: str = None):
    now = int(time.time())
    response = dict(response)
    response.setdefault("created_at", now)
    if previous_response_id:
        response["previous_response_id"] = previous_response_id
    history = _compact_messages(
        messages + _responses_output_to_messages_static(response.get("output", [])),
        CONFIG.get("max_history_messages", 40),
        CONFIG.get("max_history_chars", 60000),
        CONFIG.get("max_tool_output_chars", 12000),
    )
    with RESPONSE_HISTORY_LOCK:
        with _response_store_init() as con:
            con.execute(
                "INSERT OR REPLACE INTO responses "
                "(id, created_at, updated_at, previous_response_id, model, response_json, messages_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    response["id"],
                    int(response.get("created_at") or now),
                    now,
                    previous_response_id,
                    response.get("model", ""),
                    json.dumps(response, ensure_ascii=False),
                    json.dumps(history, ensure_ascii=False),
                ),
            )
            ttl = int(CONFIG.get("response_store_ttl_sec", 86400) or 0)
            if ttl > 0:
                con.execute("DELETE FROM responses WHERE updated_at < ?", (now - ttl,))


def _response_store_get_response(response_id: str):
    with RESPONSE_HISTORY_LOCK:
        with _response_store_init() as con:
            row = con.execute("SELECT response_json FROM responses WHERE id = ?", (response_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _response_store_get_messages(response_id: str) -> list:
    if not response_id:
        return []
    with RESPONSE_HISTORY_LOCK:
        with _response_store_init() as con:
            row = con.execute("SELECT messages_json FROM responses WHERE id = ?", (response_id,)).fetchone()
    return json.loads(row[0]) if row else []


def _response_call_to_tool_call_static(item: dict, fallback_index: int = 0) -> dict:
    function = item.get("function", {}) if isinstance(item.get("function"), dict) else {}
    name = item.get("name") or function.get("name", "")
    arguments = item.get("arguments", function.get("arguments", "{}"))
    if arguments is None:
        arguments = "{}"
    elif not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    call_id = item.get("call_id") or item.get("id") or f"call_{fallback_index}"
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _response_content_to_message_parts_static(content) -> tuple:
    text_parts = []
    tool_calls = []
    if isinstance(content, list):
        for index, part in enumerate(content):
            if not isinstance(part, dict):
                if part is not None:
                    text_parts.append(str(part))
                continue
            part_type = part.get("type")
            if part_type in ("input_text", "output_text", "text"):
                text_parts.append(part.get("text", ""))
            elif part_type == "function_call":
                tool_calls.append(_response_call_to_tool_call_static(part, index))
    elif isinstance(content, str):
        text_parts.append(content)
    elif content is not None:
        text_parts.append(str(content))
    return "\n".join(p for p in text_parts if p), tool_calls


def _responses_output_to_messages_static(output: list) -> list:
    messages = []
    for index, item in enumerate(output or []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            messages.append({"role": "assistant", "content": None, "tool_calls": [_response_call_to_tool_call_static(item, index)]})
        elif item.get("type") == "message":
            text, tool_calls = _response_content_to_message_parts_static(item.get("content", []))
            message = {"role": item.get("role", "assistant"), "content": text or None}
            if tool_calls:
                message["role"] = "assistant"
                message["tool_calls"] = tool_calls
            messages.append(message)
    return messages


# ─── HTTP Handler ────────────────────────────────────────────────────────────

class GeminiHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log(fmt % args)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_sse(self, event: str, data: dict):
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()

    def _authorized(self):
        keys = CONFIG.get("api_keys") or []
        if not keys:
            return True
        auth = self.headers.get("Authorization", "")
        key = auth[7:] if auth.startswith("Bearer ") else self.headers.get("x-api-key", "")
        return key in keys

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        try:
            if self.path.startswith("/v1/") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            if self.path.startswith("/v1/responses/"):
                response_id = self.path.rsplit("/", 1)[-1].split("?", 1)[0]
                response = _response_store_get_response(response_id)
                if response is None:
                    self.send_json({"error": {"message": "response not found"}}, 404)
                else:
                    self.send_json(response)
            elif self.path == "/v1/models":
                self.send_json({"object": "list", "data": [
                    {"id": n, "object": "model", "created": 1700000000,
                     "owned_by": "google", "description": c["desc"]}
                    for n, c in MODELS.items()
                ]})
            elif self.path.startswith("/v1beta/models"):
                self._handle_google_models_list()
            elif self.path == "/":
                self.send_json({"status": "ok", "version": __version__,
                                "models": list(MODELS.keys())})
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"GET error: {e}")

    def do_POST(self):
        try:
            if self.path.startswith("/v1/") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            if self.path == "/v1/chat/completions":
                self.handle_chat(body)
            elif self.path == "/v1/responses":
                self.handle_responses(body)
            elif self.path == "/v1/messages":
                self.handle_anthropic_messages(body)
            elif ":generateContent" in self.path:
                self._handle_google_generate(body, stream=False)
            elif ":streamGenerateContent" in self.path:
                self._handle_google_generate(body, stream=True)
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"POST error: {e}")
            try:
                self.send_json({"error": {"message": str(e)}}, 500)
            except:
                pass

    def _resolve_model(self, model_name):
        think_override = None
        if "@think=" in model_name:
            model_name, think_str = model_name.rsplit("@think=", 1)
            think_override = int(think_str)
        cfg = MODELS.get(model_name)
        if not cfg:
            return None, None, None, f"Unknown model: {model_name}"
        return model_name, cfg["mode"], (think_override if think_override is not None else cfg["think"]), None

    def _call_gemini(self, prompt, model_id, think_mode, tools):
        raw = gemini_stream_generate(prompt, model_id, think_mode)
        text = extract_response_text(raw)
        tool_calls = None
        if tools and text:
            text, tool_calls = parse_tool_calls(text)
        return text or "", tool_calls

    def handle_chat(self, body: bytes):
        req = json.loads(body)
        model_name, model_id, think_mode, err = self._resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        tools = req.get("tools")
        tool_choice = req.get("tool_choice", "auto")
        chat_messages = _compact_messages(
            req.get("messages", []),
            CONFIG.get("max_history_messages", 40),
            CONFIG.get("max_history_chars", 60000),
            CONFIG.get("max_tool_output_chars", 12000),
        )
        prompt = messages_to_prompt(chat_messages, tools, tool_choice)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty prompt"}}, 400)
            return

        stream = req.get("stream", False)
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        if stream and (not tools or tool_choice == "none"):
            # True streaming: forward chunks as they arrive
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                for delta_text in gemini_stream_generate_iter(prompt, model_id, think_mode):
                    chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                             "model": model_name, "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                # Final chunk
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                         "model": model_name, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log(f"Stream error: {e}")
            return

        # Non-streaming (or tool calling which needs full response)
        try:
            text, tool_calls = self._call_gemini(prompt, model_id, think_mode, tools if tool_choice != "none" else None)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return
        if _should_retry_tool_call(chat_messages, tools, tool_choice, text, tool_calls):
            for _ in range(int(CONFIG.get("tool_retry_attempts", 1) or 0)):
                retry_text, retry_calls = self._call_gemini(_build_tool_retry_prompt(prompt, tool_choice), model_id, think_mode, tools)
                if retry_calls:
                    text, tool_calls = retry_text, retry_calls
                    break

        msg = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        finish = "tool_calls" if tool_calls else "stop"

        if stream:
            # Stream mode with tools: send as a single standards-shaped chunk.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if tool_calls:
                delta = {"role": "assistant", "content": None, "tool_calls": []}
                for index, tc in enumerate(tool_calls):
                    delta["tool_calls"].append({
                        "index": index,
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": tc["function"]["arguments"],
                        },
                    })
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                         "model": model_name, "choices": [{"index": 0, "delta": delta, "finish_reason": "tool_calls"}]}
            else:
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                         "model": model_name, "choices": [{"index": 0, "delta": {"role": "assistant", "content": text or ""}, "finish_reason": "stop"}]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self.send_json({
                "id": cid, "object": "chat.completion", "created": int(time.time()),
                "model": model_name,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": {"prompt_tokens": len(prompt)//4, "completion_tokens": len(text)//4,
                          "total_tokens": (len(prompt)+len(text))//4},
            })

    def _response_usage(self, prompt: str, text: str) -> dict:
        input_tokens = len(prompt) // 4
        output_tokens = len(text or "") // 4
        return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}

    def _write_response_stream_item(self, output_index: int, item: dict):
        self._write_sse("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": output_index,
            "item": {**item, "status": "in_progress", **({"content": []} if item["type"] == "message" else {})},
        })

        if item["type"] == "function_call":
            arguments = item.get("arguments", "")
            if arguments:
                self._write_sse("response.function_call_arguments.delta", {
                    "type": "response.function_call_arguments.delta",
                    "item_id": item["id"],
                    "output_index": output_index,
                    "delta": arguments,
                })
            self._write_sse("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "item_id": item["id"],
                "output_index": output_index,
                "call_id": item["call_id"],
                "name": item["name"],
                "arguments": arguments,
            })
        elif item["type"] == "message":
            for content_index, part in enumerate(item["content"]):
                if part.get("type") != "output_text":
                    continue
                text = part.get("text", "")
                self._write_sse("response.content_part.added", {
                    "type": "response.content_part.added",
                    "item_id": item["id"],
                    "output_index": output_index,
                    "content_index": content_index,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                })
                if text:
                    self._write_sse("response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "item_id": item["id"],
                        "output_index": output_index,
                        "content_index": content_index,
                        "delta": text,
                    })
                self._write_sse("response.output_text.done", {
                    "type": "response.output_text.done",
                    "item_id": item["id"],
                    "output_index": output_index,
                    "content_index": content_index,
                    "text": text,
                })
                self._write_sse("response.content_part.done", {
                    "type": "response.content_part.done",
                    "item_id": item["id"],
                    "output_index": output_index,
                    "content_index": content_index,
                    "part": part,
                })

        self._write_sse("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": output_index,
            "item": item,
        })

    def _response_call_to_tool_call(self, item: dict, fallback_index: int = 0) -> dict:
        function = item.get("function", {}) if isinstance(item.get("function"), dict) else {}
        name = item.get("name") or function.get("name", "")
        arguments = item.get("arguments", function.get("arguments", "{}"))
        if arguments is None:
            arguments = "{}"
        elif not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        call_id = item.get("call_id") or item.get("id") or f"call_{fallback_index}"
        return {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }

    def _response_content_to_message_parts(self, content) -> tuple:
        text_parts = []
        tool_calls = []
        if isinstance(content, list):
            for index, part in enumerate(content):
                if not isinstance(part, dict):
                    if part is not None:
                        text_parts.append(str(part))
                    continue
                part_type = part.get("type")
                if part_type in ("input_text", "output_text", "text"):
                    text_parts.append(part.get("text", ""))
                elif part_type == "function_call":
                    tool_calls.append(self._response_call_to_tool_call(part, index))
                elif part_type in ("image", "image_url", "input_image"):
                    text_parts.append("[Image input not supported in this API. Please describe the image in text.]")
        elif isinstance(content, str):
            text_parts.append(content)
        elif content is not None:
            text_parts.append(str(content))
        return "\n".join(p for p in text_parts if p), tool_calls

    def _responses_input_to_messages(self, input_items) -> list:
        messages = []
        if isinstance(input_items, str):
            return [{"role": "user", "content": input_items}]
        if not isinstance(input_items, list):
            return messages

        for index, item in enumerate(input_items):
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue

            item_type = item.get("type")
            if item_type == "function_call":
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [self._response_call_to_tool_call(item, index)],
                })
            elif item_type == "function_call_output":
                output = item.get("output", "")
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False)
                output = _truncate_text(output, CONFIG.get("max_tool_output_chars", 12000))
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id", ""),
                    "name": item.get("name", "") or item.get("call_id", ""),
                    "content": output,
                })
            elif item_type == "reasoning":
                continue
            else:
                role = item.get("role", "user")
                text, tool_calls = self._response_content_to_message_parts(item.get("content", ""))
                message = {"role": role, "content": text or None}
                if tool_calls:
                    message["role"] = "assistant"
                    message["tool_calls"] = tool_calls
                messages.append(message)
        return messages

    def _responses_output_to_messages(self, output: list) -> list:
        messages = []
        for index, item in enumerate(output or []):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [self._response_call_to_tool_call(item, index)],
                })
            elif item.get("type") == "message":
                text, tool_calls = self._response_content_to_message_parts(item.get("content", []))
                message = {"role": item.get("role", "assistant"), "content": text or None}
                if tool_calls:
                    message["role"] = "assistant"
                    message["tool_calls"] = tool_calls
                messages.append(message)
        return messages

    def _load_response_history(self, response_id: str) -> list:
        return _response_store_get_messages(response_id) if response_id else []

    def _store_response_history(self, response: dict, messages: list, previous_response_id: str = None):
        _response_store_save(response, messages, previous_response_id)

    def handle_responses(self, body: bytes):
        """OpenAI Responses API for Codex CLI compatibility."""
        req = json.loads(body)
        model_name, model_id, think_mode, err = self._resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        input_items = req.get("input", [])
        tools = req.get("tools")
        previous_response_id = req.get("previous_response_id")
        previous_messages = self._load_response_history(previous_response_id)
        current_messages = self._responses_input_to_messages(input_items)

        if previous_messages:
            messages = previous_messages + current_messages
        else:
            messages = []
            if req.get("instructions"):
                messages.append({"role": "system", "content": req["instructions"]})
            messages.extend(current_messages)

        if previous_messages and req.get("instructions"):
            first = messages[0] if messages else {}
            if first.get("role") != "system" or first.get("content") != req["instructions"]:
                messages.insert(0, {"role": "system", "content": req["instructions"]})

        messages = _compact_messages(
            messages,
            CONFIG.get("max_history_messages", 40),
            CONFIG.get("max_history_chars", 60000),
            CONFIG.get("max_tool_output_chars", 12000),
        )

        if tools:
            tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}}
                     if t.get("type") == "function" and "function" not in t else t for t in tools]

        tool_choice = req.get("tool_choice", "auto")
        prompt = messages_to_prompt(messages, tools, tool_choice)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty input"}}, 400)
            return

        try:
            text, tool_calls = self._call_gemini(prompt, model_id, think_mode, tools if tool_choice != "none" else None)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return
        if _should_retry_tool_call(messages, tools, tool_choice, text, tool_calls):
            for _ in range(int(CONFIG.get("tool_retry_attempts", 1) or 0)):
                retry_text, retry_calls = self._call_gemini(_build_tool_retry_prompt(prompt, tool_choice), model_id, think_mode, tools)
                if retry_calls:
                    text, tool_calls = retry_text, retry_calls
                    break

        rid = f"resp_{uuid.uuid4().hex[:16]}"
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        output = []
        if tool_calls:
            for tc in tool_calls:
                output.append({"type": "function_call", "id": tc["id"], "call_id": tc["id"],
                               "name": tc["function"]["name"], "arguments": tc["function"]["arguments"], "status": "completed"})
        if text or not tool_calls:
            output.append({"type": "message", "id": mid, "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": text or "", "annotations": []}]})

        resp_obj = {"id": rid, "object": "response", "created_at": int(time.time()), "status": "completed",
                    "model": model_name, "output": output,
                    "usage": self._response_usage(prompt, text)}
        if previous_response_id:
            resp_obj["previous_response_id"] = previous_response_id
        if req.get("store", True) is not False:
            self._store_response_history(resp_obj, messages, previous_response_id)

        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            ev = {"type": "response.created", "response": {"id": rid, "object": "response", "status": "in_progress", "model": model_name, "output": []}}
            self._write_sse("response.created", ev)
            for output_index, item in enumerate(output):
                self._write_response_stream_item(output_index, item)
            self._write_sse("response.completed", {"type": "response.completed", "response": resp_obj})
            self.wfile.flush()
        else:
            self.send_json(resp_obj)


    # ─── Google Native API (Gemini CLI compatible) ────────────────────────────

    def _anthropic_content_to_text(self, content):
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return "" if content is None else str(content)
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "tool_result":
                result = block.get("content", "")
                if not isinstance(result, str):
                    result = json.dumps(result, ensure_ascii=False)
                result = _truncate_text(result, CONFIG.get("max_tool_output_chars", 12000))
                parts.append(f"[Tool result for {block.get('tool_use_id', '')}]: {result}")
        return "\n".join(p for p in parts if p)

    def _anthropic_to_openai_messages(self, req: dict) -> list:
        messages = []
        system = req.get("system")
        if isinstance(system, str) and system:
            messages.append({"role": "system", "content": system})
        elif isinstance(system, list):
            text = self._anthropic_content_to_text(system)
            if text:
                messages.append({"role": "system", "content": text})

        for msg in req.get("messages", []):
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "assistant" and isinstance(content, list):
                text_parts = []
                tool_calls = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") in ("thinking", "redacted_thinking"):
                        thinking = block.get("thinking") or block.get("data") or ""
                        if thinking:
                            text_parts.append(f"[Previous assistant thinking preserved but hidden from user]: {thinking}")
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                            },
                        })
                converted = {"role": "assistant", "content": "\n".join(text_parts) or None}
                if tool_calls:
                    converted["tool_calls"] = tool_calls
                messages.append(converted)
            else:
                messages.append({"role": role, "content": self._anthropic_content_to_text(content)})
        return messages

    def _anthropic_tools_to_openai(self, tools: list) -> list:
        converted = []
        for tool in tools or []:
            converted.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            })
        return converted

    def _anthropic_tool_choice_to_openai(self, tool_choice):
        if not isinstance(tool_choice, dict):
            return tool_choice or "auto"
        choice_type = tool_choice.get("type")
        if choice_type == "none":
            return "none"
        if choice_type in ("any", "auto"):
            return "required" if choice_type == "any" else "auto"
        if choice_type == "tool" and tool_choice.get("name"):
            return {"type": "function", "function": {"name": tool_choice["name"]}}
        return "auto"

    def _write_anthropic_stream_text(self, message_id: str, model_name: str, text: str, usage: dict):
        self._write_sse("message_start", {"type": "message_start", "message": {
            "id": message_id, "type": "message", "role": "assistant", "model": model_name,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": usage["prompt_tokens"], "output_tokens": 0},
        }})
        self._write_sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        if text:
            self._write_sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})
        self._write_sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        self._write_sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": usage["completion_tokens"]}})
        self._write_sse("message_stop", {"type": "message_stop"})

    def _write_anthropic_stream_tool(self, message_id: str, model_name: str, tool_calls: list, usage: dict):
        self._write_sse("message_start", {"type": "message_start", "message": {
            "id": message_id, "type": "message", "role": "assistant", "model": model_name,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": usage["prompt_tokens"], "output_tokens": 0},
        }})
        for index, tc in enumerate(tool_calls):
            fn = tc["function"]
            self._write_sse("content_block_start", {"type": "content_block_start", "index": index, "content_block": {
                "type": "tool_use", "id": tc["id"], "name": fn["name"], "input": {},
            }})
            self._write_sse("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {
                "type": "input_json_delta", "partial_json": fn.get("arguments", "{}"),
            }})
            self._write_sse("content_block_stop", {"type": "content_block_stop", "index": index})
        self._write_sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": usage["completion_tokens"]}})
        self._write_sse("message_stop", {"type": "message_stop"})

    def _safe_json_object(self, text: str) -> dict:
        try:
            value = json.loads(text or "{}")
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}

    def handle_anthropic_messages(self, body: bytes):
        req = self._parse_body(body)
        if req is None:
            self.send_json({"type": "error", "error": {"type": "invalid_request_error", "message": "invalid JSON"}}, 400)
            return
        model_name, model_id, think_mode, err = self._resolve_model(req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"type": "error", "error": {"type": "invalid_request_error", "message": err}}, 400)
            return

        tools = self._anthropic_tools_to_openai(req.get("tools", []))
        tool_choice = self._anthropic_tool_choice_to_openai(req.get("tool_choice", "auto"))
        anthropic_messages = self._anthropic_to_openai_messages(req)
        prompt = messages_to_prompt(anthropic_messages, tools, tool_choice)
        if not prompt.strip():
            self.send_json({"type": "error", "error": {"type": "invalid_request_error", "message": "empty input"}}, 400)
            return

        try:
            text, tool_calls = self._call_gemini(prompt, model_id, think_mode, tools if tool_choice != "none" else None)
        except Exception as e:
            self.send_json({"type": "error", "error": {"type": "api_error", "message": f"upstream error: {e}"}}, 502)
            return
        if _should_retry_tool_call(anthropic_messages, tools, tool_choice, text, tool_calls):
            for _ in range(int(CONFIG.get("tool_retry_attempts", 1) or 0)):
                retry_text, retry_calls = self._call_gemini(_build_tool_retry_prompt(prompt, tool_choice), model_id, think_mode, tools)
                if retry_calls:
                    text, tool_calls = retry_text, retry_calls
                    break

        usage = _usage(prompt, text or "")
        message_id = f"msg_{uuid.uuid4().hex[:16]}"
        if tool_calls:
            content = [{"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"], "input": self._safe_json_object(tc["function"].get("arguments"))} for tc in tool_calls]
            stop_reason = "tool_use"
        else:
            content = [{"type": "text", "text": text or ""}]
            stop_reason = "end_turn"

        if req.get("stream"):
            self._start_sse()
            if tool_calls:
                self._write_anthropic_stream_tool(message_id, model_name, tool_calls, usage)
            else:
                self._write_anthropic_stream_text(message_id, model_name, text or "", usage)
            return

        self.send_json({
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": model_name,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"]},
        })


    def _parse_google_model_from_path(self):
        """Extract model name from /v1beta/models/{model}:method path."""
        m = re.match(r'/v1beta/models/([^:?]+)', self.path)
        if m:
            return m.group(1)
        return None

    def _handle_google_models_list(self):
        """GET /v1beta/models — Google AI format model list."""
        models = []
        for name, cfg in MODELS.items():
            models.append({
                "name": f"models/{name}",
                "displayName": name,
                "description": cfg["desc"],
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
            })
        self.send_json({"models": models})

    def _google_contents_to_prompt(self, req: dict) -> str:
        """Convert Google API contents format to prompt string."""
        parts = []
        sys_inst = req.get("systemInstruction")
        if sys_inst:
            sys_parts = sys_inst.get("parts", [])
            sys_text = " ".join(p.get("text", "") for p in sys_parts if p.get("text"))
            if sys_text:
                parts.append(f"[System instruction]: {sys_text}")

        for content in req.get("contents", []):
            role = content.get("role", "user")
            text_parts = []
            for p in content.get("parts", []):
                if p.get("text"):
                    text_parts.append(p["text"])
            text = " ".join(text_parts)
            if role == "model":
                parts.append(f"[Assistant]: {text}")
            else:
                parts.append(text)
        return "\n\n".join(p for p in parts if p)

    def _handle_google_generate(self, body: bytes, stream: bool):
        """Handle Google native generateContent / streamGenerateContent."""
        req = json.loads(body)
        model_name = self._parse_google_model_from_path()
        if not model_name:
            self.send_json({"error": {"message": "model not specified in path"}}, 400)
            return

        model_name, model_id, think_mode, err = self._resolve_model(model_name)
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        prompt = self._google_contents_to_prompt(req)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty content"}}, 400)
            return

        try:
            text, _ = self._call_gemini(prompt, model_id, think_mode, None)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        candidate = {
            "content": {"parts": [{"text": text or ""}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
        usage = {
            "promptTokenCount": len(prompt) // 4,
            "candidatesTokenCount": len(text) // 4,
            "totalTokenCount": (len(prompt) + len(text)) // 4,
        }
        response_obj = {
            "candidates": [candidate],
            "usageMetadata": usage,
            "modelVersion": model_name,
        }

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(f"data: {json.dumps(response_obj)}\n\n".encode())
            self.wfile.flush()
        else:
            self.send_json(response_obj)


# ─── Main ────────────────────────────────────────────────────────────────────

def load_config(path: str):
    if path and os.path.exists(path):
        with open(path) as f:
            CONFIG.update(json.load(f))
        log(f"Config loaded: {path}")


def main():
    parser = argparse.ArgumentParser(description="Gemini Web to OpenAI API")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--cookie-file", type=str, default=None, help="Path to cookie file")
    parser.add_argument("--proxy", type=str, default=None, help="HTTP proxy, e.g. http://127.0.0.1:7890")
    parser.add_argument("--version", action="version", version=f"gemini-web2api {__version__}")
    args = parser.parse_args()

    config_path = args.config or os.environ.get("GEMINI_WEB2API_CONFIG")
    if not config_path:
        for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
            if os.path.exists(p):
                config_path = p
                break
    load_config(config_path)

    if args.port:
        CONFIG["port"] = args.port
    if args.cookie_file:
        CONFIG["cookie_file"] = args.cookie_file
    if args.proxy:
        CONFIG["proxy"] = args.proxy

    class ThreadedServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    port = CONFIG["port"]
    server = ThreadedServer((CONFIG["host"], port), GeminiHandler)
    print(f"gemini-web2api v{__version__}")
    print(f"  Listening: http://0.0.0.0:{port}")
    print(f"  Base URL:  http://localhost:{port}/v1")
    print(f"  Models:    {', '.join(MODELS.keys())}")
    print(f"  Cookie:    {'yes (' + CONFIG['cookie_file'] + ')' if CONFIG.get('cookie_file') else 'none (anonymous)'}")
    print(f"  Proxy:     {CONFIG.get('proxy') or 'none (uses system env HTTP_PROXY/HTTPS_PROXY)'}")
    print(f"  Retry:     {CONFIG['retry_attempts']}x / {CONFIG['retry_delay_sec']}s")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
