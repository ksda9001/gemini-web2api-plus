"""HTTP server: OpenAI-compatible API endpoints."""
import json
import time
import uuid
import re
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

from .config import CONFIG
from .models import MODELS, resolve_model
from .gemini import generate, generate_stream, log
from .tools import messages_to_prompt, parse_tool_calls, google_contents_to_prompt, parse_google_function_calls
from .multimodal import upload_image, fetch_image_bytes
from . import __version__

RESPONSE_HISTORY = {}
RESPONSE_HISTORY_LOCK = threading.Lock()
RESPONSE_HISTORY_MAX = 100


def _usage(prompt: str, text: str) -> dict:
    p = len(prompt) // 4
    c = len(text or "") // 4
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _upload_images(images: list) -> list:
    """Upload images and return list of file references. Returns None if no images."""
    if not images:
        return None
    file_refs = []
    for item in images:
        try:
            if isinstance(item, tuple) and len(item) == 2:
                data, mime = item
                if isinstance(data, str):
                    data = fetch_image_bytes(data)
                    mime = mime or "image/png"
                if data:
                    ref = upload_image(data, "image.png", mime or "image/png")
                    file_refs.append(ref)
        except Exception as e:
            log(f"Image upload failed: {e}")
    return file_refs if file_refs else None


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

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _write_sse(self, event: str, data: dict):
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()

    def _parse_body(self, body: bytes) -> dict:
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return None

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
            if self.path == "/v1/models":
                self.send_json({"object": "list", "data": [
                    {"id": n, "object": "model", "created": 1700000000,
                     "owned_by": "google", "description": c["desc"]}
                    for n, c in MODELS.items()
                ]})
            elif self.path.startswith("/v1beta/models"):
                self.send_json({"models": [
                    {"name": f"models/{n}", "displayName": n, "description": c["desc"],
                     "supportedGenerationMethods": ["generateContent", "streamGenerateContent"]}
                    for n, c in MODELS.items()
                ]})
            elif self.path == "/":
                self.send_json({"status": "ok", "version": __version__, "models": list(MODELS.keys())})
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        try:
            if self.path.startswith("/v1/") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            if self.path == "/v1/chat/completions":
                self._handle_chat(body)
            elif self.path == "/v1/responses":
                self._handle_responses(body)
            elif self.path == "/v1/messages":
                self._handle_anthropic_messages(body)
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

    # ─── /v1/chat/completions ─────────────────────────────────────────────────

    def _handle_chat(self, body: bytes):
        req = self._parse_body(body)
        if req is None:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        model_name, model_id, think_mode, err, extra_fields = resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        tools = req.get("tools")
        tool_choice = req.get("tool_choice", "auto")
        prompt, images = messages_to_prompt(req.get("messages", []), tools, tool_choice)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty prompt"}}, 400)
            return

        stream = req.get("stream", False)
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        if stream and (not tools or tool_choice == "none"):
            try:
                self._start_sse()
                for delta in generate_stream(prompt, model_id, think_mode, _upload_images(images), extra_fields):
                    chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                             "model": model_name, "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                end = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                       "model": model_name, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                self.wfile.write(f"data: {json.dumps(end)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        try:
            text = generate(prompt, model_id, think_mode, _upload_images(images), extra_fields)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        tool_calls = None
        if tools and text and tool_choice != "none":
            text, tool_calls = parse_tool_calls(text)
        msg = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        finish = "tool_calls" if tool_calls else "stop"

        if stream:
            self._start_sse()
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
                "usage": {"prompt_tokens": len(prompt)//4, "completion_tokens": len(text or "")//4,
                          "total_tokens": (len(prompt)+len(text or ""))//4},
            })

    # ─── /v1/responses (Codex CLI) ───────────────────────────────────────────

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
        if not response_id:
            return []
        with RESPONSE_HISTORY_LOCK:
            history = RESPONSE_HISTORY.get(response_id, [])
            return json.loads(json.dumps(history, ensure_ascii=False))

    def _store_response_history(self, response_id: str, messages: list, output: list):
        history = list(messages) + self._responses_output_to_messages(output)
        with RESPONSE_HISTORY_LOCK:
            RESPONSE_HISTORY[response_id] = json.loads(json.dumps(history, ensure_ascii=False))
            while len(RESPONSE_HISTORY) > RESPONSE_HISTORY_MAX:
                RESPONSE_HISTORY.pop(next(iter(RESPONSE_HISTORY)))

    def _handle_responses(self, body: bytes):
        req = self._parse_body(body)
        if req is None:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        model_name, model_id, think_mode, err, extra_fields = resolve_model(
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

        if tools:
            tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}}
                     if t.get("type") == "function" and "function" not in t else t for t in tools]

        tool_choice = req.get("tool_choice", "auto")
        prompt, images = messages_to_prompt(messages, tools, tool_choice)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty input"}}, 400)
            return

        try:
            text = generate(prompt, model_id, think_mode, _upload_images(images), extra_fields)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        tool_calls = None
        if tools and text and tool_choice != "none":
            text, tool_calls = parse_tool_calls(text)

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

        self._store_response_history(rid, messages, output)

        if req.get("stream"):
            self._start_sse()
            ev = {"type": "response.created", "response": {"id": rid, "object": "response", "status": "in_progress", "model": model_name, "output": []}}
            self._write_sse("response.created", ev)
            for output_index, item in enumerate(output):
                self._write_response_stream_item(output_index, item)
            resp_obj = {"id": rid, "object": "response", "status": "completed", "model": model_name, "output": output,
                        "usage": self._response_usage(prompt, text)}
            self._write_sse("response.completed", {"type": "response.completed", "response": resp_obj})
            self.wfile.flush()
        else:
            self.send_json({"id": rid, "object": "response", "created_at": int(time.time()), "status": "completed",
                            "model": model_name, "output": output,
                            "usage": self._response_usage(prompt, text)})

    # ─── /v1beta/models (Google Gemini CLI) ──────────────────────────────────

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

    def _handle_anthropic_messages(self, body: bytes):
        req = self._parse_body(body)
        if req is None:
            self.send_json({"type": "error", "error": {"type": "invalid_request_error", "message": "invalid JSON"}}, 400)
            return
        model_name, model_id, think_mode, err, extra_fields = resolve_model(req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"type": "error", "error": {"type": "invalid_request_error", "message": err}}, 400)
            return

        tools = self._anthropic_tools_to_openai(req.get("tools", []))
        tool_choice = self._anthropic_tool_choice_to_openai(req.get("tool_choice", "auto"))
        prompt, images = messages_to_prompt(self._anthropic_to_openai_messages(req), tools, tool_choice)
        if not prompt.strip():
            self.send_json({"type": "error", "error": {"type": "invalid_request_error", "message": "empty input"}}, 400)
            return

        try:
            text = generate(prompt, model_id, think_mode, _upload_images(images), extra_fields)
        except Exception as e:
            self.send_json({"type": "error", "error": {"type": "api_error", "message": f"upstream error: {e}"}}, 502)
            return

        tool_calls = None
        if tools and text and tool_choice != "none":
            text, tool_calls = parse_tool_calls(text)
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

    def _handle_google_generate(self, body: bytes, stream: bool):
        req = self._parse_body(body)
        if req is None:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        m = re.match(r'/v1beta/models/([^:?]+)', self.path)
        model_name = m.group(1) if m else CONFIG["default_model"]
        model_name, model_id, think_mode, err, extra_fields = resolve_model(model_name)
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        tool_config = req.get("toolConfig", {})
        fc_mode = tool_config.get("functionCallingConfig", {}).get("mode", "AUTO")
        has_tools = bool(req.get("tools")) and fc_mode != "NONE"
        prompt, images = google_contents_to_prompt(req)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty content"}}, 400)
            return

        file_refs = _upload_images(images)
        log(f"Google API: model={model_name} stream={stream} tools={has_tools} prompt_len={len(prompt)}")

        if stream and not has_tools:
            try:
                self._start_sse()
                full_text = ""
                for delta in generate_stream(prompt, model_id, think_mode, file_refs, extra_fields):
                    if not delta:
                        continue
                    full_text += delta
                    chunk_obj = {
                        "candidates": [{"content": {"parts": [{"text": delta}], "role": "model"}, "index": 0}],
                        "modelVersion": model_name,
                    }
                    self.wfile.write(f"data: {json.dumps(chunk_obj, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                final_chunk = {
                    "candidates": [{"finishReason": "STOP", "index": 0}],
                    "usageMetadata": {
                        "promptTokenCount": len(prompt) // 4,
                        "candidatesTokenCount": len(full_text) // 4,
                        "totalTokenCount": (len(prompt) + len(full_text)) // 4,
                    },
                    "modelVersion": model_name,
                }
                self.wfile.write(f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        try:
            text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        if not text:
            log("Warning: empty response from Gemini")

        response_parts = []
        if has_tools and text:
            clean_text, function_calls = parse_google_function_calls(text)
            if function_calls:
                if clean_text:
                    response_parts.append({"text": clean_text})
                for fc in function_calls:
                    response_parts.append({"functionCall": {"name": fc["name"], "args": fc["args"]}})
            else:
                response_parts.append({"text": text})
        else:
            response_parts.append({"text": text or "I apologize, but I was unable to generate a response. Please try again."})

        candidate = {
            "content": {"parts": response_parts, "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
        usage = {
            "promptTokenCount": len(prompt) // 4,
            "candidatesTokenCount": len(text or "") // 4,
            "totalTokenCount": (len(prompt) + len(text or "")) // 4,
        }
        response_obj = {
            "candidates": [candidate],
            "usageMetadata": usage,
            "modelVersion": model_name,
        }

        if stream:
            self._start_sse()
            self.wfile.write(f"data: {json.dumps(response_obj, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
        else:
            self.send_json(response_obj)


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
