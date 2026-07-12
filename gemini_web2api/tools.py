"""Tool calling and multimodal message parsing."""
import json
import re
import uuid
import base64
import io

MAX_IMAGE_B64_SIZE = 50000  # ~37KB raw image

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


def _compress_b64_if_needed(b64: str) -> str:
    """Compress image if base64 is too large for text embedding."""
    if len(b64) <= MAX_IMAGE_B64_SIZE:
        return b64
    try:
        from PIL import Image
        img_data = base64.b64decode(b64)
        img = Image.open(io.BytesIO(img_data))
        # Resize to max 256px on longest side
        max_dim = 256
        ratio = min(max_dim / img.width, max_dim / img.height)
        if ratio < 1:
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
        # Convert to JPEG with quality reduction
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=60)
        compressed = base64.b64encode(buf.getvalue()).decode()
        return compressed
    except Exception:
        # If PIL not available, truncate (model will get partial data)
        return b64[:MAX_IMAGE_B64_SIZE]


def _build_tool_choice_instruction(tool_choice, tool_defs: list) -> str:
    """Build tool_choice constraint instruction.

    tool_choice values:
      - "none": do not call any tool
      - "auto": decide whether to call tools (default)
      - "required": must call at least one tool
      - {"type": "function", "function": {"name": "xxx"}}: must call specific tool
    """
    if tool_choice == "none":
        return "\n\nIMPORTANT: Do NOT call any tools. Respond with text only."
    if tool_choice == "required":
        return "\n\nIMPORTANT: You MUST call at least one tool. Do not respond with text only."
    if isinstance(tool_choice, dict):
        fn_name = tool_choice.get("function", {}).get("name", "")
        if fn_name:
            return f'\n\nIMPORTANT: You MUST call the tool "{fn_name}". Do not call other tools.'
    return ""


def openai_tools_from_request(req: dict):
    """Return OpenAI-style tools, accepting both tools and legacy functions."""
    if req.get("tools"):
        return req.get("tools")
    functions = req.get("functions")
    if not functions:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            },
        }
        for fn in functions
    ]


def openai_tool_choice_from_request(req: dict):
    """Return OpenAI-style tool_choice, accepting legacy function_call."""
    if "tool_choice" in req:
        return req.get("tool_choice", "auto")
    function_call = req.get("function_call")
    if function_call is None:
        return "auto"
    if function_call in ("auto", "none"):
        return function_call
    if isinstance(function_call, dict) and function_call.get("name"):
        return {"type": "function", "function": {"name": function_call["name"]}}
    return "auto"


def messages_to_prompt(
    messages: list,
    tools: list = None,
    tool_choice=None,
    include_agent_instruction: bool = None,
) -> tuple:
    """Convert OpenAI messages to (prompt_str, images_list).

    Returns (prompt, images) where images is a list of (bytes, mime_type) tuples.
    """
    parts = []
    images = []

    agent_tools = bool(tools) and tool_choice != "none"
    if include_agent_instruction is None:
        include_agent_instruction = agent_tools
    if include_agent_instruction:
        parts.append(f"[System instruction]: {AGENT_BEHAVIOR_INSTRUCTION}")

    if agent_tools:
        tool_defs = []
        for tool in tools:
            fn = tool.get("function", tool) if tool.get("type") == "function" else tool
            tool_defs.append({
                "name": fn.get("name", tool.get("name", "")),
                "description": fn.get("description", tool.get("description", "")),
                "parameters": fn.get("parameters", tool.get("parameters", {})),
            })
        if tool_defs:
            constraint = _build_tool_choice_instruction(tool_choice, tool_defs)
            parts.append(
                "# Tool Use\n\n"
                "You can call the following tools. Call format:\n"
                '```tool_call\n{"name": "func_name", "arguments": {...}}\n```\n'
                'If code fences are unavailable, output ONLY this raw JSON object: {"name": "func_name", "arguments": {...}}\n'
                "When calling tools, output ONLY the tool_call block(s) or raw JSON tool call object(s).\n\n"
                f"Available tools:\n{json.dumps(tool_defs, ensure_ascii=False, separators=(',', ':'))}"
                f"{constraint}"
            )

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, list):
            text_parts = []
            for c in content:
                if c.get("type") in ("text", "input_text"):
                    text_parts.append(c.get("text", ""))
                elif c.get("type") == "image_url":
                    text_parts.append("[Note: Image input not supported in this API. Please describe the image in text.]")
                elif c.get("type") == "image":
                    text_parts.append("[Note: Image input not supported in this API. Please describe the image in text.]")
            content = " ".join(text_parts)

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

    prompt = "\n\n".join(p for p in parts if p)
    return prompt, images


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


def _decode_json_values(text: str) -> list:
    values = []
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            break
        values.append(value)
        index = end
    return values


def _tool_calls_from_json_text(payload_text: str) -> list:
    payload_text = (payload_text or "").strip()
    if not payload_text:
        return []
    try:
        values = [json.loads(payload_text)]
    except json.JSONDecodeError:
        values = _decode_json_values(payload_text)

    tool_calls = []
    for value in values:
        tool_calls.extend(_normalize_tool_call_payload(value))
    return tool_calls


def _find_tool_json_spans(text: str) -> list:
    spans = []
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        if text[index] not in "{[":
            index += 1
            continue
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index += 1
            continue
        tool_calls = _normalize_tool_call_payload(value)
        if tool_calls:
            spans.append((index, end, tool_calls))
            index = end
        else:
            index += 1
    return spans


def _remove_spans(text: str, spans: list) -> str:
    if not spans:
        return text
    parts = []
    last_end = 0
    for start, end in sorted((max(0, s), min(len(text), e)) for s, e in spans if e > s):
        if start < last_end:
            last_end = max(last_end, end)
            continue
        parts.append(text[last_end:start])
        last_end = end
    parts.append(text[last_end:])
    return "".join(parts)


def _strip_truncated_raw_tool_json(text: str) -> str:
    for match in re.finditer(r"(?ms)(?:^|\n)[ \t]*(?=[{\[])", text):
        tail = text[match.start():]
        head = tail[:2000]
        looks_like_tool = (
            re.search(r'"tool_calls"\s*:', head)
            or (
                re.search(r'"(?:name|tool|tool_name)"\s*:', head)
                and re.search(r'"(?:arguments|args|input)"\s*:', head)
            )
            or (
                re.search(r'"function"\s*:\s*\{', head)
                and re.search(r'"name"\s*:', head)
            )
        )
        if looks_like_tool:
            return text[:match.start()].strip()
    return text


_TOOL_PROTOCOL_FENCE_RE = re.compile(
    r"```(?P<label>tool_call|function_call|json)?[ \t]*\r?\n(?P<body>.*?)\r?\n```",
    re.DOTALL | re.IGNORECASE,
)


def strip_tool_call_protocol(text: str) -> str:
    """Remove tool-call protocol fragments from text that may be shown to users."""
    if not text:
        return text or ""

    spans = []
    for match in _TOOL_PROTOCOL_FENCE_RE.finditer(text):
        label = (match.group("label") or "").lower()
        calls = _tool_calls_from_json_text(match.group("body"))
        if label in ("tool_call", "function_call") or calls:
            spans.append((match.start(), match.end()))
    cleaned = _remove_spans(text, spans)

    # Hide a truncated protocol tail; retry/fallback can recover a structured call.
    cleaned = re.sub(
        r"(?is)```(?:tool_call|function_call)[ \t]*(?:\r?\n|$).*",
        "",
        cleaned,
    )

    prefix_spans = []
    for match in re.finditer(r"(?im)(?:^|\n)[ \t]*(?:tool_call|function_call)[ \t]*\r?\n", cleaned):
        tail = cleaned[match.end():]
        stripped_tail = tail.lstrip()
        leading = len(tail) - len(stripped_tail)
        raw_spans = _find_tool_json_spans(stripped_tail)
        if raw_spans and raw_spans[0][0] == 0:
            prefix_spans.append((match.start(), match.end() + leading + raw_spans[0][1]))
    cleaned = _remove_spans(cleaned, prefix_spans)

    raw_json_spans = [(start, end) for start, end, _ in _find_tool_json_spans(cleaned)]
    cleaned = _remove_spans(cleaned, raw_json_spans)
    cleaned = _strip_truncated_raw_tool_json(cleaned)
    return cleaned.strip()


def _json_from_single_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r'```(?:json|tool_call|function_call)?\s*\n(.*?)\n```', stripped, re.DOTALL)
    return match.group(1).strip() if match else stripped


def parse_tool_calls(text: str) -> tuple:
    """Extract tool_call blocks or raw JSON calls. Returns (clean_text, tool_calls_list)."""
    text = text or ""
    tool_calls = []
    protocol_spans = []
    for match in _TOOL_PROTOCOL_FENCE_RE.finditer(text):
        label = (match.group("label") or "").lower()
        block_calls = _tool_calls_from_json_text(match.group("body"))
        if label in ("tool_call", "function_call") or block_calls:
            protocol_spans.append((match.start(), match.end()))
        tool_calls.extend(block_calls)

    if not tool_calls:
        clean = _remove_spans(text, protocol_spans).strip()
        raw_text = _json_from_single_fence(clean)
        tool_calls.extend(_tool_calls_from_json_text(raw_text))

    if not tool_calls:
        clean = _remove_spans(text, protocol_spans)
        for _, _, raw_calls in _find_tool_json_spans(clean):
            tool_calls.extend(raw_calls)

    if tool_calls:
        return "", tool_calls
    return strip_tool_call_protocol(text), tool_calls


# ─── Google Native API helpers ─────────────────────────────────────────────────


def build_tool_prompt(tool_defs: list) -> str:
    """Build natural tool-use prompt for Gemini Web that avoids prompt-injection detection."""
    tool_spec = json.dumps(tool_defs, indent=2, ensure_ascii=False)
    return (
        "# Tool Use\n\n"
        "You can call the following tools to help accomplish tasks. "
        "These tools connect to the user's local environment and will execute when called.\n\n"
        "Call format (use this exact format):\n"
        "```function_call\n"
        '{"name": "<tool_name>", "args": {<arguments>}}\n'
        "```\n\n"
        "When calling tools:\n"
        "- Output ONLY the function_call block(s), nothing else\n"
        "- You may call multiple tools with multiple blocks\n"
        "- After receiving a [Tool result for ...], use that data to answer the user\n\n"
        f"Available tools:\n{tool_spec}"
    )


def _google_tool_choice_instruction(req: dict) -> str:
    """Extract tool_choice constraint from Google API toolConfig."""
    tool_config = req.get("toolConfig", {})
    fc_config = tool_config.get("functionCallingConfig", {})
    mode = fc_config.get("mode", "AUTO")
    allowed = fc_config.get("allowedFunctionNames", [])

    if mode == "NONE":
        return "\n\nIMPORTANT: Do NOT call any tools. Respond with text only."
    if mode == "ANY":
        if allowed:
            names = ", ".join(f'"{n}"' for n in allowed)
            return f"\n\nIMPORTANT: You MUST call one of these tools: {names}. Do not respond with text only."
        return "\n\nIMPORTANT: You MUST call at least one tool. Do not respond with text only."
    return ""


def google_contents_to_prompt(req: dict) -> tuple:
    """Convert Google API contents/tools/systemInstruction to (prompt_str, images_list).

    Returns (prompt, images) where images is a list of (bytes, mime_type) tuples.
    """
    parts = []
    images = []

    tool_config = req.get("toolConfig", {})
    fc_mode = tool_config.get("functionCallingConfig", {}).get("mode", "AUTO")

    tools = req.get("tools")
    tool_defs = []
    if tools and fc_mode != "NONE":
        for tool_group in tools:
            for fn in tool_group.get("functionDeclarations", []):
                td = {"name": fn.get("name", ""), "description": fn.get("description", "")}
                params = fn.get("parameters") or fn.get("parametersJsonSchema")
                if params:
                    td["parameters"] = params
                tool_defs.append(td)

    sys_inst = req.get("systemInstruction")
    if sys_inst:
        sys_parts = sys_inst.get("parts", [])
        sys_text = " ".join(p.get("text", "") for p in sys_parts if p.get("text"))
        if sys_text:
            if tool_defs:
                constraint = _google_tool_choice_instruction(req)
                parts.append(sys_text + "\n\n" + build_tool_prompt(tool_defs) + constraint)
            else:
                parts.append(sys_text)
    elif tool_defs:
        constraint = _google_tool_choice_instruction(req)
        parts.append(build_tool_prompt(tool_defs) + constraint)

    for content in req.get("contents", []):
        role = content.get("role", "user")
        msg_parts = []
        for p in content.get("parts", []):
            if p.get("text"):
                msg_parts.append(p["text"])
            elif p.get("inlineData"):
                data = p["inlineData"]
                mime = data.get("mimeType", "image/png")
                images.append((base64.b64decode(data["data"]), mime))
            elif p.get("functionCall"):
                fc = p["functionCall"]
                msg_parts.append(
                    f'```function_call\n{json.dumps({"name": fc["name"], "args": fc.get("args", {})}, ensure_ascii=False)}\n```'
                )
            elif p.get("functionResponse"):
                fr = p["functionResponse"]
                msg_parts.append(
                    f'[Tool result for {fr.get("name", "")}]: {json.dumps(fr.get("response", {}), ensure_ascii=False)}'
                )
        text = "\n".join(msg_parts)
        if role == "model":
            parts.append(f"[Assistant]: {text}")
        else:
            parts.append(text)

    return "\n\n".join(p for p in parts if p), images


def _google_call_from_payload(payload) -> list:
    if isinstance(payload, dict) and isinstance(payload.get("tool_calls"), list):
        payload = payload["tool_calls"]

    items = payload if isinstance(payload, list) else [payload]
    calls = []
    for item in items:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if isinstance(function, dict):
            name = function.get("name") or item.get("name")
            args = function.get("arguments", item.get("arguments", item.get("args", {})))
        elif item.get("type") == "tool_use":
            name = item.get("name")
            args = item.get("input", item.get("arguments", item.get("args", {})))
        else:
            name = item.get("name") or item.get("tool") or item.get("tool_name")
            args = item.get("args", item.get("arguments", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"value": args}
        if isinstance(name, str) and name.strip():
            calls.append({"name": name.strip(), "args": args if isinstance(args, dict) else {}})
    return calls


def parse_google_function_calls(text: str) -> tuple:
    """Extract function_call blocks from model output.

    Handles 3 formats:
    1. ```function_call\\n{...}\\n``` (standard)
    2. function_call\\n{...} (without backticks)
    3. Raw JSON with "name" + "args" keys

    Returns (clean_text, [{"name": ..., "args": ...}])
    """
    text = text or ""
    function_calls = []
    clean = text

    fence_pattern = r'```(?:function_call|tool_call|json)?\s*\n(.*?)\n```'
    for match in re.finditer(fence_pattern, text, re.DOTALL):
        payload_text = match.group(1).strip()
        try:
            function_calls.extend(_google_call_from_payload(json.loads(payload_text)))
        except json.JSONDecodeError:
            pass
    clean = re.sub(fence_pattern, '', clean, flags=re.DOTALL).strip()

    if not function_calls:
        prefix_pattern = r'(?:^|\n)(?:function_call|tool_call)\s*\n'
        prefix_match = re.search(prefix_pattern, clean)
        if prefix_match:
            payload_text = clean[prefix_match.end():].strip()
            for value in _decode_json_values(payload_text):
                function_calls.extend(_google_call_from_payload(value))
            if function_calls:
                clean = clean[:prefix_match.start()].strip()

    if not function_calls:
        stripped = clean.strip()
        if stripped.startswith(("{", "[")):
            for value in _decode_json_values(stripped):
                function_calls.extend(_google_call_from_payload(value))
            if function_calls:
                clean = ""
    return clean, function_calls


def google_tools_to_openai(req: dict) -> list:
    """Convert Google native functionDeclarations to OpenAI-style tools."""
    converted = []
    tool_config = req.get("toolConfig", {})
    fc_mode = tool_config.get("functionCallingConfig", {}).get("mode", "AUTO")
    if fc_mode == "NONE":
        return converted
    for tool_group in req.get("tools") or []:
        for fn in tool_group.get("functionDeclarations", []):
            converted.append({
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters") or fn.get("parametersJsonSchema") or {},
                },
            })
    return converted


def google_tool_choice_to_openai(req: dict):
    """Convert Google functionCallingConfig to OpenAI-style tool_choice."""
    fc_config = req.get("toolConfig", {}).get("functionCallingConfig", {})
    mode = fc_config.get("mode", "AUTO")
    allowed = fc_config.get("allowedFunctionNames", [])
    if mode == "NONE":
        return "none"
    if mode == "ANY":
        if len(allowed) == 1:
            return {"type": "function", "function": {"name": allowed[0]}}
        return "required"
    return "auto"


def google_contents_to_messages(req: dict) -> list:
    """Convert Google native contents/systemInstruction to OpenAI-style messages for retry heuristics."""
    messages = []
    sys_inst = req.get("systemInstruction")
    if sys_inst:
        sys_parts = sys_inst.get("parts", [])
        sys_text = " ".join(p.get("text", "") for p in sys_parts if p.get("text"))
        if sys_text:
            messages.append({"role": "system", "content": sys_text})

    for content in req.get("contents", []):
        role = "assistant" if content.get("role") == "model" else "user"
        text_parts = []
        for part in content.get("parts", []):
            if part.get("text"):
                text_parts.append(part["text"])
            elif part.get("functionCall"):
                fc = part["functionCall"]
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {
                            "name": fc.get("name", ""),
                            "arguments": json.dumps(fc.get("args", {}), ensure_ascii=False),
                        },
                    }],
                })
            elif part.get("functionResponse"):
                fr = part["functionResponse"]
                messages.append({
                    "role": "tool",
                    "name": fr.get("name", ""),
                    "content": json.dumps(fr.get("response", {}), ensure_ascii=False),
                })
        if text_parts:
            messages.append({"role": role, "content": "\n".join(text_parts)})
    return messages


def build_google_tool_retry_prompt(prompt: str, req: dict) -> str:
    """Build a retry prompt that asks Gemini Web for a Google-compatible function_call."""
    choice = google_tool_choice_to_openai(req)
    target = ""
    if isinstance(choice, dict):
        fn_name = choice.get("function", {}).get("name", "")
        if fn_name:
            target = f' Call only "{fn_name}".'
    return (
        f"{prompt}\n\n"
        "[System instruction]: Your previous answer did not call a function even though a function call is required or needed. "
        "Return ONLY one valid function_call block or raw JSON function call object now. "
        "Use this exact shape: ```function_call\n{\"name\":\"func_name\",\"args\":{}}\n``` "
        "Do not explain, do not include markdown outside the function call, and do not provide normal text."
        f"{target}"
    )
