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
from .cookies import cookie_header, load_cookie_pairs

_ssl_ctx = None
_cookie_cache = {"str": "", "sapisid": None, "mtime": 0, "expires_at": None}
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
    """Load only browser-valid Gemini cookies with expiry-aware caching."""
    cookie_file = CONFIG.get("cookie_file")
    if not cookie_file or not os.path.exists(cookie_file):
        return "", None
    try:
        now = time.time()
        mtime = os.path.getmtime(cookie_file)
        expires_at = _cookie_cache.get("expires_at")
        if (
            mtime == _cookie_cache["mtime"]
            and _cookie_cache["str"]
            and (expires_at is None or now < expires_at)
        ):
            return _cookie_cache["str"], _cookie_cache["sapisid"]
        pairs, next_expiry = load_cookie_pairs(cookie_file, now)
        cookie_str = cookie_header(pairs)
        sapisid = pairs.get("SAPISID", "")
        _cookie_cache.update({
            "str": cookie_str,
            "sapisid": sapisid or None,
            "mtime": mtime,
            "expires_at": next_expiry,
        })
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
    temporary: bool = False,
) -> str:
    inner = [None] * 102
    if file_refs:
        refs = [[None, None, ref] for ref in file_refs]
        inner[0] = [prompt, 0, None, refs, None, None, 0]
    else:
        inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    conversation = conversation or {}
    metadata = conversation.get("metadata")
    if isinstance(metadata, list):
        inner[2] = list(metadata[:10])
        inner[2].extend([None] * (10 - len(inner[2])))
        if inner[2][9] is None:
            inner[2][9] = ""
    else:
        inner[2] = [
            conversation.get("conversation_id", ""),
            conversation.get("response_id", ""),
            conversation.get("choice_id", ""),
            None, None, None, None, None, None,
            conversation.get("context", ""),
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
    if temporary:
        # Gemini Web's temporary-chat flag: generate normally without writing
        # the helper request to the account's visible chat history.
        inner[45] = 1
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
                metadata = list(ids[:10])
                metadata.extend([None] * (10 - len(metadata)))
                metadata[2] = choice_id
                context = inner[25] if len(inner) > 25 and isinstance(inner[25], str) else ""
                if context:
                    metadata[9] = context
                elif metadata[9] is None:
                    metadata[9] = ""
                return {
                    "backend": "direct",
                    "metadata": metadata,
                    "conversation_id": ids[0],
                    "response_id": ids[1],
                    "choice_id": choice_id,
                    "context": metadata[9],
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
    temporary: bool = False,
    timeout_sec: int = None,
) -> tuple:
    body = _build_payload(
        prompt, model_id, think_mode, file_refs, extra_fields, conversation, temporary
    ).encode()
    url = _get_url()
    headers = _build_headers()
    ctx = _get_ssl_ctx()
    proxy = CONFIG.get("proxy")
    timeout_sec = max(1, int(timeout_sec or CONFIG["request_timeout_sec"]))
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=ctx),
        )
        resp = opener.open(req, timeout=timeout_sec)
    else:
        resp = urllib.request.urlopen(req, context=ctx, timeout=timeout_sec)
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
    model_name: str = None,
    temporary: bool = False,
    allow_webapi: bool = True,
    agent_mode: bool = False,
    rebuild_webapi_on_failure: bool = False,
    request_timeout_sec: int = None,
    retry_attempts: int = None,
) -> tuple:
    """Generate text and return Gemini Web conversation state for the next turn."""
    session_backend = CONFIG.get("upstream_session_backend", "direct")
    conversation_backend = (conversation or {}).get("backend")
    valid_webapi_state = (
        not conversation
        or conversation_backend in (None, "gemini_webapi")
        or (
            CONFIG.get("reuse_upstream_agent_sessions", False)
            and conversation_backend == "gemini_webapi_agent"
        )
    )
    can_use_webapi = (
        allow_webapi
        and CONFIG.get("reuse_upstream_sessions", False)
        and session_backend == "gemini_webapi"
        and not file_refs
        and valid_webapi_state
    )
    if can_use_webapi:
        from .webapi_backend import generate_with_state as webapi_generate

        selected_name = model_name or CONFIG.get("default_model", "gemini-3.5-flash")
        webapi_kwargs = {"temporary": temporary}
        if request_timeout_sec is not None:
            webapi_kwargs["timeout_sec"] = request_timeout_sec
        try:
            text, state = webapi_generate(
                prompt,
                selected_name,
                conversation,
                **webapi_kwargs,
            )
            if not text:
                raise RuntimeError("Gemini webapi backend returned an empty response")
            if agent_mode:
                state = dict(state or {})
                state["backend"] = "gemini_webapi_agent"
            return text, state, prompt
        except Exception as e:
            if agent_mode and conversation and fallback_prompt and rebuild_webapi_on_failure:
                log(f"Gemini agent Web resume failed; rebuilding Web conversation: {e}")
                try:
                    text, state = webapi_generate(
                        fallback_prompt,
                        selected_name,
                        None,
                        **webapi_kwargs,
                    )
                    if not text:
                        raise RuntimeError("rebuilt Gemini Web conversation returned an empty response")
                    state = dict(state or {})
                    state["backend"] = "gemini_webapi_agent"
                    return text, state, fallback_prompt
                except Exception as rebuild_error:
                    log(f"Gemini agent Web rebuild failed; using direct backend: {rebuild_error}")
            if not CONFIG.get("upstream_session_fallback_direct", True):
                raise
            log(f"Gemini webapi session failed; rebuilding through direct backend: {e}")
            prompt = fallback_prompt or prompt
            conversation = None

    last_err = None
    active_conversation = conversation
    active_prompt = prompt
    request_timeout_sec = max(1, int(request_timeout_sec or CONFIG["request_timeout_sec"]))
    retry_attempts = max(1, int(retry_attempts or CONFIG["retry_attempts"]))
    for attempt in range(retry_attempts):
        try:
            text, truncated, state = _request_text(
                active_prompt,
                model_id,
                think_mode,
                file_refs,
                extra_fields,
                active_conversation,
                temporary,
                timeout_sec=request_timeout_sec,
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
            if attempt < retry_attempts - 1:
                log(f"Retry {attempt+1}/{retry_attempts}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
    else:
        raise last_err

    for continuation_index in range(int(CONFIG.get("continuation_attempts", 2) or 0)):
        if not truncated:
            break
        log(f"Upstream output truncated; requesting continuation {continuation_index + 1}")
        continuation_prompt = _continuation_prompt(prompt, text)
        continuation = ""
        for attempt in range(retry_attempts):
            try:
                continuation, truncated, continuation_state = _request_text(
                    continuation_prompt,
                    model_id,
                    think_mode,
                    file_refs,
                    extra_fields,
                    state,
                    temporary,
                    timeout_sec=request_timeout_sec,
                )
                if not continuation:
                    raise RuntimeError("Gemini continuation returned an empty response")
                break
            except Exception as e:
                last_err = e
                if attempt < retry_attempts - 1:
                    log(f"Continuation retry {attempt+1}/{retry_attempts}: {e}")
                    time.sleep(CONFIG["retry_delay_sec"])
        if not continuation:
            raise last_err
        text = _append_continuation(text, continuation)
        if continuation_state:
            state = continuation_state
    if truncated:
        log("Warning: continuation limit reached while upstream still reports truncation")
    return text, state, active_prompt


def generate(
    prompt: str,
    model_id: int,
    think_mode: int,
    file_refs: list = None,
    extra_fields: dict = None,
    temporary: bool = False,
) -> str:
    """Non-streaming generation with retry."""
    text, _, _ = generate_with_state(
        prompt,
        model_id,
        think_mode,
        file_refs,
        extra_fields,
        temporary=temporary,
    )
    return text


def generate_stream(
    prompt: str,
    model_id: int,
    think_mode: int,
    file_refs: list = None,
    extra_fields: dict = None,
    temporary: bool = False,
):
    """Streaming generation via httpx with retry on connection failure."""
    if not HAS_HTTPX:
        text = generate(prompt, model_id, think_mode, file_refs, extra_fields, temporary)
        if text:
            yield text
        return

    body = _build_payload(
        prompt, model_id, think_mode, file_refs, extra_fields, temporary=temporary
    )
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
                    temporary,
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
