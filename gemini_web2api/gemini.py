"""Gemini StreamGenerate protocol implementation with httpx streaming."""
import json
import time
import uuid
import re
import urllib.request
import urllib.parse
import ssl
import os
import hashlib

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

from .config import CONFIG

_ssl_ctx = None
_cookie_cache = {"str": "", "sapisid": None, "mtime": 0}
_httpx_client = None
TRUNCATION_ERROR_CODES = {1155}


def log(msg: str):
    if CONFIG["log_requests"]:
        import sys
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()


def _get_ssl_ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def _get_httpx_client():
    global _httpx_client
    if _httpx_client is None and HAS_HTTPX:
        proxy = CONFIG.get("proxy")
        transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
        _httpx_client = httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True)
    return _httpx_client


def load_cookie() -> tuple:
    """Load cookie from file with mtime-based caching."""
    cookie_file = CONFIG.get("cookie_file")
    if not cookie_file or not os.path.exists(cookie_file):
        return "", None
    try:
        mtime = os.path.getmtime(cookie_file)
        if mtime == _cookie_cache["mtime"] and _cookie_cache["str"]:
            return _cookie_cache["str"], _cookie_cache["sapisid"]
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
        _cookie_cache.update({"str": cookie_str, "sapisid": sapisid or None, "mtime": mtime})
        return cookie_str, sapisid if sapisid else None
    except Exception as e:
        log(f"Cookie load error: {e}")
        return _cookie_cache["str"], _cookie_cache["sapisid"]


def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


def _account_prefix() -> str:
    """Return the Gemini account path prefix for non-default Google accounts."""
    auth_user = CONFIG.get("auth_user")
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


def _build_headers() -> dict:
    account_prefix = _account_prefix()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{account_prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if account_prefix:
        headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    return headers


def _build_payload(
    prompt: str,
    model_id: int,
    think_mode: int,
    file_refs: list = None,
    extra_fields: dict = None,
    conversation: dict = None,
) -> str:
    inner = [None] * 102
    if file_refs:
        refs = [[None, None, ref] for ref in file_refs]
        inner[0] = [prompt, 0, None, refs, None, None, 0]
    else:
        inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    conversation = conversation or {}
    inner[2] = [
        conversation.get("conversation_id", ""),
        conversation.get("response_id", ""),
        conversation.get("choice_id", ""),
        None, None, None, None, None, None, "",
    ]
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
    if extra_fields:
        for k, v in extra_fields.items():
            inner[k] = v
    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if CONFIG.get("xsrf_token"):
        params["at"] = CONFIG["xsrf_token"]
    return urllib.parse.urlencode(params)


def _get_url() -> str:
    reqid = int(time.time()) % 1000000
    account_prefix = _account_prefix()
    return (
        f"https://gemini.google.com{account_prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )


def clean_text(text: str) -> str:
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    text = re.sub(r'http://googleusercontent\.com/card_content/\d+\n?', '', text)
    return text.strip()


def _bard_error_codes(raw: str) -> list:
    """Extract BardErrorInfo codes from both plain and JSON-encoded frames."""
    if not raw or "BardErrorInfo" not in raw:
        return []
    return [int(code) for code in re.findall(r"BardErrorInfo[^0-9]{0,40}\[(\d+)\]", raw)]


def _raise_for_bard_error(raw: str):
    errors = [code for code in _bard_error_codes(raw) if code not in TRUNCATION_ERROR_CODES]
    if errors:
        raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{errors[0]}]")


def _was_truncated(raw: str) -> bool:
    return any(code in TRUNCATION_ERROR_CODES for code in _bard_error_codes(raw))


def _extract_conversation_state(raw: str) -> dict:
    for line in raw.split("\n"):
        if '"wrb.fr"' not in line:
            continue
        try:
            outer = json.loads(line)
            inner_str = outer[0][2]
            inner = json.loads(inner_str) if inner_str else None
            ids = inner[1] if isinstance(inner, list) and len(inner) > 4 else None
            candidates = inner[4] if isinstance(inner, list) and len(inner) > 4 else None
            choice_id = candidates[0][0] if candidates and isinstance(candidates[0], list) else None
            if (
                isinstance(ids, list)
                and len(ids) >= 2
                and all(isinstance(value, str) and value for value in ids[:2])
                and isinstance(choice_id, str)
                and choice_id
            ):
                return {
                    "conversation_id": ids[0],
                    "response_id": ids[1],
                    "choice_id": choice_id,
                }
        except (json.JSONDecodeError, IndexError, TypeError):
            continue
    return {}


def _extract_texts_from_line(line: str) -> list:
    """Parse a single wrb.fr line and return list of text strings found."""
    if '"wrb.fr"' not in line or len(line) < 200:
        return []
    try:
        arr = json.loads(line)
        inner_str = arr[0][2]
        if not inner_str or len(inner_str) < 50:
            return []
        inner = json.loads(inner_str)
        if not (isinstance(inner, list) and len(inner) > 4 and inner[4]):
            return []
        texts = []
        for part in inner[4]:
            if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                for t in part[1]:
                    if isinstance(t, str) and t:
                        texts.append(t)
        return texts
    except (json.JSONDecodeError, IndexError, TypeError):
        return []


def _merge_text_segments(texts: list) -> str:
    """Merge Gemini text segments that may be cumulative or independent chunks."""
    merged = ""
    for text in texts or []:
        if not isinstance(text, str) or not text:
            continue
        if not merged:
            merged = text
        elif text == merged:
            continue
        elif text.startswith(merged):
            merged = text
        elif merged.endswith(text):
            continue
        else:
            merged += text
    return merged


def extract_response_text(raw: str) -> str:
    """Parse full response to get final text."""
    _raise_for_bard_error(raw)
    texts = []
    for line in raw.split("\n"):
        texts.extend(_extract_texts_from_line(line))
    return clean_text(_merge_text_segments(texts))


def _continuation_prompt(original_prompt: str, partial_text: str) -> str:
    context_chars = int(CONFIG.get("continuation_context_chars", 16000) or 16000)
    original_tail = original_prompt[-6000:]
    partial_tail = partial_text[-context_chars:]
    return (
        "The previous response was cut off by the upstream output limit. Continue exactly from the final "
        "character of the partial response. Return only the missing continuation: do not restart, summarize, "
        "repeat existing text, or add a new opening Markdown fence. Finish every incomplete code block and "
        "the original task.\n\n"
        f"Original request (tail):\n{original_tail}\n\n"
        f"Partial response (tail):\n{partial_tail}"
    )


def _append_continuation(partial: str, continuation: str) -> str:
    continuation = (continuation or "").lstrip()
    if partial.count("```") % 2 and continuation.startswith("```"):
        continuation = re.sub(r"^```[^\n]*\n?", "", continuation, count=1)
    max_overlap = min(len(partial), len(continuation), 4000)
    for overlap in range(max_overlap, 3, -1):
        if partial.endswith(continuation[:overlap]):
            continuation = continuation[overlap:]
            break
    return partial + continuation


def _request_text(
    prompt: str,
    model_id: int,
    think_mode: int,
    file_refs=None,
    extra_fields=None,
    conversation: dict = None,
) -> tuple:
    body = _build_payload(
        prompt, model_id, think_mode, file_refs, extra_fields, conversation
    ).encode()
    url = _get_url()
    headers = _build_headers()
    ctx = _get_ssl_ctx()
    proxy = CONFIG.get("proxy")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=ctx),
        )
        resp = opener.open(req, timeout=CONFIG["request_timeout_sec"])
    else:
        resp = urllib.request.urlopen(req, context=ctx, timeout=CONFIG["request_timeout_sec"])
    raw = resp.read().decode("utf-8", errors="replace")
    return extract_response_text(raw), _was_truncated(raw), _extract_conversation_state(raw)


def generate_with_state(
    prompt: str,
    model_id: int,
    think_mode: int,
    file_refs: list = None,
    extra_fields: dict = None,
    conversation: dict = None,
    fallback_prompt: str = None,
) -> tuple:
    """Generate text and return Gemini Web conversation state for the next turn."""
    last_err = None
    active_conversation = conversation
    active_prompt = prompt
    for attempt in range(CONFIG["retry_attempts"]):
        try:
            text, truncated, state = _request_text(
                active_prompt,
                model_id,
                think_mode,
                file_refs,
                extra_fields,
                active_conversation,
            )
            if not text:
                raise RuntimeError("Gemini upstream returned an empty response")
            break
        except Exception as e:
            last_err = e
            if active_conversation and fallback_prompt:
                log(f"Gemini conversation resume failed; rebuilding context: {e}")
                active_conversation = None
                active_prompt = fallback_prompt
                continue
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
    else:
        raise last_err

    for continuation_index in range(int(CONFIG.get("continuation_attempts", 2) or 0)):
        if not truncated:
            break
        log(f"Upstream output truncated; requesting continuation {continuation_index + 1}")
        continuation_prompt = _continuation_prompt(prompt, text)
        continuation = ""
        for attempt in range(CONFIG["retry_attempts"]):
            try:
                continuation, truncated, continuation_state = _request_text(
                    continuation_prompt,
                    model_id,
                    think_mode,
                    file_refs,
                    extra_fields,
                    state,
                )
                if not continuation:
                    raise RuntimeError("Gemini continuation returned an empty response")
                break
            except Exception as e:
                last_err = e
                if attempt < CONFIG["retry_attempts"] - 1:
                    log(f"Continuation retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                    time.sleep(CONFIG["retry_delay_sec"])
        if not continuation:
            raise last_err
        text = _append_continuation(text, continuation)
        if continuation_state:
            state = continuation_state
    if truncated:
        log("Warning: continuation limit reached while upstream still reports truncation")
    return text, state


def generate(
    prompt: str,
    model_id: int,
    think_mode: int,
    file_refs: list = None,
    extra_fields: dict = None,
) -> str:
    """Non-streaming generation with retry."""
    text, _ = generate_with_state(prompt, model_id, think_mode, file_refs, extra_fields)
    return text


def generate_stream(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None):
    """Streaming generation via httpx with retry on connection failure."""
    if not HAS_HTTPX:
        text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
        if text:
            yield text
        return

    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields)
    url = _get_url()
    headers = _build_headers()
    client = _get_httpx_client()

    last_err = None
    for attempt in range(CONFIG["retry_attempts"]):
        try:
            emitted_text = ""
            truncated = False
            with client.stream("POST", url, content=body, headers=headers) as resp:
                buf = ""
                for chunk in resp.iter_text():
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        _raise_for_bard_error(line)
                        truncated = truncated or _was_truncated(line)
                        for t in _extract_texts_from_line(line):
                            next_text = _merge_text_segments([emitted_text, t])
                            if len(next_text) <= len(emitted_text):
                                continue
                            delta = clean_text(next_text[len(emitted_text):])
                            if delta:
                                yield delta
                            emitted_text = next_text
            if not emitted_text:
                raise RuntimeError("Gemini upstream returned an empty response")
            if truncated:
                log("Upstream stream truncated; requesting continuation")
                continuation = generate(
                    _continuation_prompt(prompt, emitted_text),
                    model_id,
                    think_mode,
                    file_refs,
                    extra_fields,
                )
                completed = _append_continuation(emitted_text, continuation)
                delta = completed[len(emitted_text):]
                if delta:
                    yield delta
            return
        except Exception as e:
            last_err = e
            if emitted_text:
                raise
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Stream retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
    raise last_err
