"""Agent compatibility helpers for tool-using clients."""
import json
import hashlib
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager


DEFAULT_RESPONSE_STORE_PATH = "responses.db"
DEFAULT_RESPONSE_STORE_TTL_SEC = 86400
DEFAULT_RESPONSE_STORE_MAX_ROWS = 1000
DEFAULT_MAX_TOOL_OUTPUT_CHARS = 12000
DEFAULT_MAX_HISTORY_MESSAGES = 40
DEFAULT_MAX_HISTORY_CHARS = 60000

ACTION_RE = re.compile(
    r"("
    r"\b(create|generate|write|save|edit|modify|delete|remove|read|cat|list|ls|inspect|check|"
    r"run|execute|test|install|build|fix|patch|commit|push|open|download|fetch|search|"
    r"implement|add|update|upgrade|refactor|review|debug|diagnose|investigate|verify|"
    r"validate|repair|solve|resolve|setup|configure|rename|move|copy|deploy|publish|"
    r"release|launch|scaffold)\b"
    r"|帮我|看下|看看|创建|生成|写入|保存|修改|编辑|删除|读取|查看|列出|运行|执行|"
    r"测试|安装|构建|修复|提交|推送|打开|下载|搜索|检查|实现|新增|添加|更新|升级|"
    r"调整|移动|复制|重命名|重构|优化|分析|定位|排查|调试|验证|确认|解决|处理|报错|"
    r"触发|失效|失败|异常|故障|不生效|没生效|不起作用|运行一下|跑一下|修一下|改一下|"
    r"做一个|做个|制作|部署|上线|搭建|开发|编写|建一个|弄一个"
    r")",
    re.IGNORECASE,
)


def json_clone(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def conversation_hash(model: str, messages: list) -> str:
    payload = json.dumps(
        {"model": model or "", "messages": messages or []},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def tool_call_ids(messages: list) -> list:
    ids = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        tool_call_id = message.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            ids.append(tool_call_id)
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                call_id = call.get("id") or call.get("call_id")
                if isinstance(call_id, str) and call_id:
                    ids.append(call_id)
    return list(dict.fromkeys(ids))


def incremental_messages(messages: list, known_messages: list) -> list:
    """Return messages added after the exact transcript represented upstream."""
    messages = messages or []
    known_messages = known_messages or []
    if len(known_messages) > len(messages):
        return []
    for index, known in enumerate(known_messages):
        if messages[index] != known:
            return []
    return json_clone(messages[len(known_messages):])


def text_from_content(content) -> str:
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
                    parts.append(text_from_content(item.get("content", "")))
            elif item is not None:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    return str(content)


def latest_user_text(messages: list) -> str:
    for msg in reversed(messages or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            text = text_from_content(msg.get("content", ""))
            if text.strip():
                return text
    return ""


def any_user_action_text(messages: list) -> str:
    for msg in reversed(messages or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            text = text_from_content(msg.get("content", ""))
            if text.strip() and ACTION_RE.search(text):
                return text
    return ""


def latest_non_system_role(messages: list) -> str:
    for msg in reversed(messages or []):
        if isinstance(msg, dict) and msg.get("role") != "system":
            return msg.get("role", "")
    return ""


def sanitize_model_text(text: str) -> str:
    """Remove protocol fragments that Gemini may echo from tool-result prompts."""
    if not text:
        return text or ""
    cleaned = re.sub(r"(?ms)^\s*\[Tool result for [^\]]+\]:.*?(?:\n\s*\n|\Z)", "", text)
    cleaned = re.sub(
        r"(?ms)^\s*\[External tool execution result\].*?"
        r"\[/External tool execution result\]\s*",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"(?ms)^\s*\[External tool call accepted by the agent client\]\s*"
        r"\{.*?\}\s*",
        "",
        cleaned,
    )
    cleaned = re.sub(r"(?m)^\s*\[Assistant\]:\s*", "", cleaned)
    return cleaned.strip()


def truncate_text(text, max_chars: int = DEFAULT_MAX_TOOL_OUTPUT_CHARS) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head_len = max_chars // 2
    tail_len = max_chars - head_len
    omitted = len(text) - head_len - tail_len
    marker = f"\n\n[... truncated {omitted} characters ...]\n\n"
    return text[:head_len] + marker + text[-tail_len:]


def truncate_tool_output(output, max_chars: int = DEFAULT_MAX_TOOL_OUTPUT_CHARS) -> str:
    return truncate_text(output, max_chars)


def _truncate_message_content(message: dict, max_tool_chars: int) -> dict:
    msg = dict(message)
    if msg.get("role") == "tool":
        msg["content"] = truncate_tool_output(msg.get("content", ""), max_tool_chars)
    elif isinstance(msg.get("content"), list):
        content = []
        for item in msg["content"]:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                item = dict(item)
                item["content"] = truncate_tool_output(item.get("content", ""), max_tool_chars)
            content.append(item)
        msg["content"] = content
    return msg


def compact_messages(
    messages: list,
    max_messages: int = DEFAULT_MAX_HISTORY_MESSAGES,
    max_chars: int = DEFAULT_MAX_HISTORY_CHARS,
    max_tool_chars: int = DEFAULT_MAX_TOOL_OUTPUT_CHARS,
) -> list:
    """Deterministically trim stored history while preserving task and recent tool loop."""
    messages = [_truncate_message_content(m, max_tool_chars) for m in (messages or []) if isinstance(m, dict)]
    if not messages:
        return []

    system = [m for m in messages if m.get("role") == "system"][:1]
    non_system = [m for m in messages if m.get("role") != "system"]
    first_user = []
    for m in non_system:
        if m.get("role") == "user":
            first_user = [m]
            break

    protected_ids = {id(m) for m in system + first_user}
    recent = []
    for m in reversed(non_system):
        if id(m) not in protected_ids:
            recent.append(m)
        if len(recent) >= max_messages:
            break
    compacted = system + first_user + list(reversed(recent))

    encoded = json.dumps(compacted, ensure_ascii=False)
    if len(encoded) > max_chars and len(compacted) > len(system) + len(first_user):
        budget = max(1, max_messages // 2)
        recent = list(reversed(list(reversed(non_system))[:budget]))
        compacted = system + first_user + [{
            "role": "system",
            "content": "[Earlier conversation compacted. Keep following the original task and recent tool results.]",
        }] + recent
    return json_clone(compacted)


def should_retry_tool_call(messages: list, tools, tool_choice, text: str, tool_calls) -> bool:
    if not tools or tool_choice == "none" or tool_calls:
        return False
    if tool_choice == "required":
        return True
    if isinstance(tool_choice, dict):
        return True
    if latest_non_system_role(messages) == "tool":
        explicit_intent = re.search(
            r"\b(?:I\s+(?:should|will|need to|must)|let me)\b|(?:我(?:会|将|需要|必须)|让我)",
            text or "",
            re.IGNORECASE,
        )
        return bool(explicit_intent and ACTION_RE.search(text or ""))
    user_text = any_user_action_text(messages) or latest_user_text(messages)
    if not user_text or not ACTION_RE.search(user_text):
        return bool(text and ACTION_RE.search(text))
    return True


def allowed_tool_names(tools) -> list:
    names = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function", tool)
        name = fn.get("name") if isinstance(fn, dict) else None
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return list(dict.fromkeys(names))


def filter_tool_calls(tool_calls, tools) -> list:
    """Drop model-invented tool names before they reach an agent client."""
    allowed = set(allowed_tool_names(tools))
    if not allowed:
        return []
    return [
        call for call in (tool_calls or [])
        if isinstance(call, dict)
        and isinstance(call.get("function"), dict)
        and call["function"].get("name") in allowed
    ]


def fallback_tool_call(messages: list, tools, tool_choice=None) -> list:
    """Return a safe read-only shell call when Gemini refuses an obvious agent action.

    This is intentionally conservative: it only starts the tool loop with a
    workspace inspection command, leaving actual edits to the next model turn.
    """
    if not should_retry_tool_call(messages, tools, tool_choice, "", None):
        return None

    for tool in tools or []:
        fn = tool.get("function", tool) if isinstance(tool, dict) else {}
        name = fn.get("name") or tool.get("name", "") if isinstance(tool, dict) else ""
        if not isinstance(name, str) or not name.strip():
            continue
        lowered = name.lower()
        if not any(marker in lowered for marker in ("shell", "command", "terminal", "bash", "powershell")):
            continue

        parameters = fn.get("parameters") or tool.get("parameters", {}) if isinstance(tool, dict) else {}
        properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
        required = parameters.get("required", []) if isinstance(parameters, dict) else []
        arg_name = "command"
        if arg_name not in properties:
            if "cmd" in properties:
                arg_name = "cmd"
            else:
                candidates = [p for p in required if isinstance(p, str)]
                candidates += [p for p, spec in properties.items() if isinstance(spec, dict) and spec.get("type") == "string"]
                if candidates:
                    arg_name = candidates[0]

        return [{
            "id": f"call_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {
                "name": name.strip(),
                "arguments": json.dumps({arg_name: "pwd; ls"}, ensure_ascii=False),
            },
        }]

    return None


def build_tool_retry_prompt(prompt: str, tool_choice=None, tools=None) -> str:
    target = ""
    if isinstance(tool_choice, dict):
        fn_name = tool_choice.get("function", {}).get("name", "")
        if fn_name:
            target = f' Call only "{fn_name}".'
    elif allowed_tool_names(tools):
        target = " Call only one of these declared tools: " + ", ".join(
            f'"{name}"' for name in allowed_tool_names(tools)
        ) + "."
    return (
        f"{prompt}\n\n"
        "[System instruction]: Your previous answer did not call a tool even though a tool is required or needed. "
        "Return ONLY one valid tool_call block or raw JSON tool call object now. "
        "Do not explain, do not include markdown outside the tool call, and do not provide normal text."
        f"{target}"
    )


class ResponseStore:
    """SQLite-backed response/message store for Responses API previous_response_id."""

    def __init__(
        self,
        path: str = DEFAULT_RESPONSE_STORE_PATH,
        ttl_sec: int = DEFAULT_RESPONSE_STORE_TTL_SEC,
        max_rows: int = DEFAULT_RESPONSE_STORE_MAX_ROWS,
        max_history_messages: int = DEFAULT_MAX_HISTORY_MESSAGES,
        max_history_chars: int = DEFAULT_MAX_HISTORY_CHARS,
        max_tool_output_chars: int = DEFAULT_MAX_TOOL_OUTPUT_CHARS,
    ):
        self.path = path or DEFAULT_RESPONSE_STORE_PATH
        self.ttl_sec = int(ttl_sec or DEFAULT_RESPONSE_STORE_TTL_SEC)
        self.max_rows = int(max_rows or DEFAULT_RESPONSE_STORE_MAX_ROWS)
        self.max_history_messages = int(max_history_messages or DEFAULT_MAX_HISTORY_MESSAGES)
        self.max_history_chars = int(max_history_chars or DEFAULT_MAX_HISTORY_CHARS)
        self.max_tool_output_chars = int(max_tool_output_chars or DEFAULT_MAX_TOOL_OUTPUT_CHARS)
        self._lock = threading.Lock()
        self._ready = False
        self._memory_connection = None

    def _connect(self):
        if self.path == ":memory:":
            if self._memory_connection is None:
                self._memory_connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            return self._memory_connection
        if self.path != ":memory:":
            directory = os.path.dirname(os.path.abspath(self.path))
            if directory:
                os.makedirs(directory, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=30)
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA synchronous=NORMAL")
        return con

    @contextmanager
    def _connection(self):
        con = self._connect()
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            if self.path != ":memory:":
                con.close()

    def _init(self):
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            with self._connection() as con:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS responses (
                        id TEXT PRIMARY KEY,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        previous_response_id TEXT,
                        model TEXT,
                        response_json TEXT NOT NULL,
                        messages_json TEXT NOT NULL,
                        output_json TEXT NOT NULL
                    )
                    """
                )
                con.execute("CREATE INDEX IF NOT EXISTS idx_responses_updated ON responses(updated_at)")
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS upstream_sessions (
                        response_id TEXT PRIMARY KEY,
                        updated_at INTEGER NOT NULL,
                        model TEXT NOT NULL,
                        upstream_json TEXT NOT NULL
                    )
                    """
                )
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_sessions (
                        call_id TEXT PRIMARY KEY,
                        updated_at INTEGER NOT NULL,
                        model TEXT NOT NULL,
                        upstream_json TEXT NOT NULL,
                        messages_json TEXT NOT NULL
                    )
                    """
                )
                con.execute("CREATE INDEX IF NOT EXISTS idx_agent_sessions_updated ON agent_sessions(updated_at)")
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS conversation_sessions (
                        history_hash TEXT PRIMARY KEY,
                        updated_at INTEGER NOT NULL,
                        model TEXT NOT NULL,
                        upstream_json TEXT NOT NULL,
                        messages_json TEXT NOT NULL
                    )
                    """
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_conversation_sessions_updated "
                    "ON conversation_sessions(updated_at)"
                )
            self._ready = True

    def _prune(self, con):
        now = int(time.time())
        if self.ttl_sec > 0:
            con.execute("DELETE FROM responses WHERE updated_at < ?", (now - self.ttl_sec,))
            con.execute("DELETE FROM upstream_sessions WHERE updated_at < ?", (now - self.ttl_sec,))
            con.execute("DELETE FROM agent_sessions WHERE updated_at < ?", (now - self.ttl_sec,))
            con.execute("DELETE FROM conversation_sessions WHERE updated_at < ?", (now - self.ttl_sec,))
        if self.max_rows > 0:
            con.execute(
                """
                DELETE FROM responses
                WHERE id NOT IN (
                    SELECT id FROM responses ORDER BY updated_at DESC LIMIT ?
                )
                """,
                (self.max_rows,),
            )
            con.execute(
                """
                DELETE FROM upstream_sessions
                WHERE response_id NOT IN (
                    SELECT response_id FROM upstream_sessions ORDER BY updated_at DESC LIMIT ?
                )
                """,
                (self.max_rows,),
            )
            con.execute(
                """
                DELETE FROM agent_sessions
                WHERE call_id NOT IN (
                    SELECT call_id FROM agent_sessions ORDER BY updated_at DESC LIMIT ?
                )
                """,
                (self.max_rows * 4,),
            )
            con.execute(
                """
                DELETE FROM conversation_sessions
                WHERE history_hash NOT IN (
                    SELECT history_hash FROM conversation_sessions ORDER BY updated_at DESC LIMIT ?
                )
                """,
                (self.max_rows * 2,),
            )

    def save(self, response: dict, messages: list, output: list, previous_response_id: str = None):
        self._init()
        now = int(time.time())
        response = json_clone(response)
        response["created_at"] = response.get("created_at") or now
        if previous_response_id:
            response["previous_response_id"] = previous_response_id
        history = compact_messages(
            list(messages or []) + list(response_messages_from_output(output or [])),
            self.max_history_messages,
            self.max_history_chars,
            self.max_tool_output_chars,
        )
        with self._lock:
            with self._connection() as con:
                con.execute(
                    """
                    INSERT OR REPLACE INTO responses
                    (id, created_at, updated_at, previous_response_id, model, response_json, messages_json, output_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        response["id"],
                        int(response.get("created_at") or now),
                        now,
                        previous_response_id,
                        response.get("model", ""),
                        json.dumps(response, ensure_ascii=False),
                        json.dumps(history, ensure_ascii=False),
                        json.dumps(output or [], ensure_ascii=False),
                    ),
                )
                self._prune(con)

    def get_response(self, response_id: str):
        self._init()
        with self._connection() as con:
            row = con.execute("SELECT response_json FROM responses WHERE id = ?", (response_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def get_messages(self, response_id: str) -> list:
        self._init()
        with self._connection() as con:
            row = con.execute("SELECT messages_json FROM responses WHERE id = ?", (response_id,)).fetchone()
        return json.loads(row[0]) if row else []

    def save_upstream_session(self, response_id: str, model: str, upstream_state: dict):
        if not response_id or not upstream_state:
            return
        self._init()
        now = int(time.time())
        with self._lock:
            with self._connection() as con:
                con.execute(
                    "INSERT OR REPLACE INTO upstream_sessions "
                    "(response_id, updated_at, model, upstream_json) VALUES (?, ?, ?, ?)",
                    (response_id, now, model or "", json.dumps(upstream_state, ensure_ascii=False)),
                )
                self._prune(con)

    def get_upstream_session(self, response_id: str, model: str = None) -> dict:
        if not response_id:
            return {}
        self._init()
        with self._connection() as con:
            row = con.execute(
                "SELECT model, upstream_json FROM upstream_sessions WHERE response_id = ?",
                (response_id,),
            ).fetchone()
        if not row or (model and row[0] and row[0] != model):
            return {}
        return json.loads(row[1])

    def save_agent_session(
        self,
        model: str,
        upstream_state: dict,
        messages: list,
        call_ids: list = None,
    ):
        call_ids = list(dict.fromkeys(call_ids or tool_call_ids(messages)))
        if not call_ids or not upstream_state:
            return
        self._init()
        now = int(time.time())
        encoded_state = json.dumps(upstream_state, ensure_ascii=False)
        encoded_messages = json.dumps(messages or [], ensure_ascii=False)
        with self._lock:
            with self._connection() as con:
                con.executemany(
                    "INSERT OR REPLACE INTO agent_sessions "
                    "(call_id, updated_at, model, upstream_json, messages_json) VALUES (?, ?, ?, ?, ?)",
                    [(call_id, now, model or "", encoded_state, encoded_messages) for call_id in call_ids],
                )
                self._prune(con)

    def find_agent_session(self, model: str, messages: list) -> dict:
        call_ids = tool_call_ids(messages)
        if not call_ids:
            return {}
        self._init()
        placeholders = ",".join("?" for _ in call_ids)
        with self._connection() as con:
            row = con.execute(
                f"SELECT upstream_json, messages_json FROM agent_sessions "
                f"WHERE call_id IN ({placeholders}) AND model = ? ORDER BY updated_at DESC LIMIT 1",
                tuple(call_ids) + (model or "",),
            ).fetchone()
        if not row:
            return {}
        known_messages = json.loads(row[1])
        delta = incremental_messages(messages, known_messages)
        if not delta:
            return {}
        return {
            "upstream_state": json.loads(row[0]),
            "known_messages": known_messages,
            "delta_messages": delta,
        }

    def save_conversation_session(self, model: str, upstream_state: dict, messages: list):
        if not upstream_state or not messages:
            return
        known_messages = compact_messages(
            messages,
            self.max_history_messages,
            self.max_history_chars,
            self.max_tool_output_chars,
        )
        key = conversation_hash(model, known_messages)
        self._init()
        now = int(time.time())
        with self._lock:
            with self._connection() as con:
                con.execute(
                    "INSERT OR REPLACE INTO conversation_sessions "
                    "(history_hash, updated_at, model, upstream_json, messages_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        key,
                        now,
                        model or "",
                        json.dumps(upstream_state, ensure_ascii=False),
                        json.dumps(known_messages, ensure_ascii=False),
                    ),
                )
                self._prune(con)

    def find_conversation_session(self, model: str, messages: list) -> dict:
        messages = list(messages or [])
        if len(messages) < 2:
            return {}
        self._init()
        for prefix_len in range(len(messages) - 1, 0, -1):
            prefix = compact_messages(
                messages[:prefix_len],
                self.max_history_messages,
                self.max_history_chars,
                self.max_tool_output_chars,
            )
            key = conversation_hash(model, prefix)
            with self._lock:
                with self._connection() as con:
                    row = con.execute(
                        "SELECT upstream_json, messages_json FROM conversation_sessions "
                        "WHERE history_hash = ? AND model = ?",
                        (key, model or ""),
                    ).fetchone()
            if not row:
                continue
            known_messages = json.loads(row[1])
            delta = incremental_messages(messages, known_messages)
            if delta:
                return {
                    "upstream_state": json.loads(row[0]),
                    "known_messages": known_messages,
                    "delta_messages": delta,
                }
        return {}


def response_call_to_tool_call(item: dict, fallback_index: int = 0) -> dict:
    function = item.get("function", {}) if isinstance(item.get("function"), dict) else {}
    name = item.get("name") or function.get("name", "")
    arguments = item.get("arguments", function.get("arguments", "{}"))
    if arguments is None:
        arguments = "{}"
    elif not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    call_id = item.get("call_id") or item.get("id") or f"call_{fallback_index}"
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def response_content_to_message_parts(content) -> tuple:
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
                tool_calls.append(response_call_to_tool_call(part, index))
            elif part_type in ("image", "image_url", "input_image"):
                text_parts.append("[Image input not supported in this API. Please describe the image in text.]")
    elif isinstance(content, str):
        text_parts.append(content)
    elif content is not None:
        text_parts.append(str(content))
    return "\n".join(p for p in text_parts if p), tool_calls


def response_messages_from_output(output: list) -> list:
    messages = []
    for index, item in enumerate(output or []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            messages.append({"role": "assistant", "content": None, "tool_calls": [response_call_to_tool_call(item, index)]})
        elif item.get("type") == "message":
            text, tool_calls = response_content_to_message_parts(item.get("content", []))
            message = {"role": item.get("role", "assistant"), "content": text or None}
            if tool_calls:
                message["role"] = "assistant"
                message["tool_calls"] = tool_calls
            messages.append(message)
    return messages
