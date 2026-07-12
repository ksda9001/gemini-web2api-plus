"""Synchronous adapter for HanaokaYuzu/gemini-webapi chat sessions."""
import asyncio
import hashlib
import json
import os
import queue
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeoutError

from .config import CONFIG

try:
    from gemini_webapi import GeminiClient
    from gemini_webapi.constants import Model
except ImportError:  # Keep source-only test environments usable.
    GeminiClient = None
    Model = None


MODEL_ALIASES = {
    "gemini-3.5-flash": "gemini-3-flash",
    "gemini-3.5-flash-thinking": "gemini-3-flash-thinking",
    "gemini-3.1-pro": "gemini-3-pro",
    "gemini-3.1-pro-enhanced": "gemini-3-pro-advanced",
    "gemini-auto": "unspecified",
    "gemini-3.5-flash-thinking-lite": "gemini-3-flash-thinking",
    "gemini-flash-lite": "gemini-3-flash",
}


def _request_timeout() -> int:
    return max(
        1,
        int(
            CONFIG.get(
                "webapi_request_timeout_sec",
                CONFIG.get("request_timeout_sec", 180),
            )
            or 180
        ),
    )


def _cookie_pairs() -> dict:
    path = CONFIG.get("cookie_file")
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        content = f.read().strip()
    if not content:
        return {}
    if content.startswith("{"):
        data = json.loads(content)
        if isinstance(data, dict) and isinstance(data.get("cookie"), str):
            content = data["cookie"]
        elif isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
    pairs = {}
    for part in content.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if name:
            pairs[name] = value
    return pairs


def state_to_metadata(state: dict = None) -> list:
    """Convert persisted state from either backend into Gemini's 10-item metadata."""
    state = state or {}
    metadata = state.get("metadata")
    if isinstance(metadata, list):
        result = list(metadata[:10])
        result.extend([None] * (10 - len(result)))
        if result[9] is None:
            result[9] = ""
        return result
    return [
        state.get("conversation_id", ""),
        state.get("response_id", ""),
        state.get("choice_id", ""),
        None, None, None, None, None, None,
        state.get("context", ""),
    ]


def metadata_to_state(metadata: list) -> dict:
    result = state_to_metadata({"metadata": metadata})
    return {
        "backend": "gemini_webapi",
        "metadata": result,
        "conversation_id": result[0] or "",
        "response_id": result[1] or "",
        "choice_id": result[2] or "",
        "context": result[9] or "",
    }


def _start_isolated_chat(client, model, state: dict = None):
    """Create a chat without inheriting gemini-webapi 2.0.0's shared metadata list."""
    chat = client.start_chat(model=model)
    # PyPI 2.0.0 assigns DEFAULT_METADATA directly instead of copying it. Using
    # the public setter would mutate that same global list, so replace the
    # private slot with an owned list for both fresh and resumed conversations.
    chat._ChatSession__metadata = state_to_metadata(state) if state else [
        "", "", "", None, None, None, None, None, None, ""
    ]
    return chat


def _account_status_name(client) -> str:
    status = getattr(client, "account_status", None)
    return str(getattr(status, "name", status or "UNKNOWN")).upper()


def _assert_authenticated(client):
    """Reject anonymous Gemini sessions when persistent Web history is expected."""
    if not CONFIG.get("require_authenticated_webapi", True):
        return
    status = _account_status_name(client)
    if status != "AVAILABLE":
        raise RuntimeError(
            "Gemini Web account is not authenticated "
            f"(account_status={status}); refresh the mounted browser cookies"
        )


class GeminiWebAPIBackend:
    """Run the async Gemini web client on one persistent background event loop."""

    def __init__(self):
        if GeminiClient is None:
            raise RuntimeError("gemini-webapi is not installed")
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._client = None
        self._cookie_fingerprint = ""

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coroutine) -> Future:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    @staticmethod
    def _credentials() -> tuple:
        pairs = _cookie_pairs()
        secure_1psid = pairs.get("__Secure-1PSID", "")
        secure_1psidts = pairs.get("__Secure-1PSIDTS", "")
        fingerprint = hashlib.sha256(
            json.dumps(pairs, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return pairs, secure_1psid, secure_1psidts, fingerprint

    async def _ensure_client(self):
        pairs, secure_1psid, secure_1psidts, fingerprint = self._credentials()
        if not secure_1psid:
            raise RuntimeError("cookie file does not contain __Secure-1PSID")
        if (
            self._client is not None
            and getattr(self._client, "_running", False)
            and fingerprint == self._cookie_fingerprint
        ):
            _assert_authenticated(self._client)
            return self._client

        if self._client is not None:
            await self._client.close()

        cache_path = CONFIG.get("cookie_cache_path")
        if cache_path:
            os.environ["GEMINI_COOKIE_PATH"] = str(cache_path)
        self._client = GeminiClient(
            secure_1psid,
            secure_1psidts or None,
            proxy=CONFIG.get("proxy"),
        )
        # Preserve the complete browser session. Some accounts can authenticate
        # with 1PSID alone, while others still require companion Google cookies.
        self._client.cookies = pairs
        timeout = int(CONFIG.get("request_timeout_sec", 180) or 180)
        await self._client.init(
            timeout=timeout,
            auto_close=False,
            auto_refresh=bool(CONFIG.get("cookie_auto_refresh", True)),
            refresh_interval=max(60, int(CONFIG.get("cookie_refresh_interval_sec", 600) or 600)),
            watchdog_timeout=min(timeout, int(CONFIG.get("webapi_watchdog_sec", 120) or 120)),
            verbose=bool(CONFIG.get("log_requests", False)),
        )
        try:
            _assert_authenticated(self._client)
        except Exception:
            await self._client.close()
            self._client = None
            self._cookie_fingerprint = ""
            raise
        self._cookie_fingerprint = fingerprint
        return self._client

    @staticmethod
    def _model(client, requested_name: str):
        target = MODEL_ALIASES.get(requested_name, requested_name)
        if target == "unspecified":
            return Model.UNSPECIFIED
        for model in client.list_models() or []:
            if not getattr(model, "is_available", True):
                continue
            if target in (getattr(model, "model_name", ""), getattr(model, "display_name", "")):
                return model
        return target

    async def _generate(
        self,
        prompt: str,
        model_name: str,
        state: dict = None,
        temporary: bool = False,
    ):
        client = await self._ensure_client()
        chat = _start_isolated_chat(client, self._model(client, model_name), state)
        output = await chat.send_message(prompt, temporary=temporary)
        return output.text, metadata_to_state(chat.metadata)

    async def _stream(
        self,
        result,
        prompt: str,
        model_name: str,
        state: dict = None,
        temporary: bool = False,
    ):
        try:
            client = await self._ensure_client()
            chat = _start_isolated_chat(client, self._model(client, model_name), state)
            async for output in chat.send_message_stream(prompt, temporary=temporary):
                if output.text_delta:
                    result._queue.put(("delta", output.text_delta))
            result.state = metadata_to_state(chat.metadata)
            result._queue.put(("done", None))
        except BaseException as exc:
            result._queue.put(("error", exc))

    def generate(
        self,
        prompt: str,
        model_name: str,
        state: dict = None,
        temporary: bool = False,
    ) -> tuple:
        timeout = _request_timeout()
        future = self._submit(self._generate(prompt, model_name, state, temporary))
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            future.cancel()
            raise TimeoutError(f"Gemini webapi request exceeded {timeout}s")

    def generate_stream(
        self,
        prompt: str,
        model_name: str,
        state: dict = None,
        temporary: bool = False,
    ):
        result = SyncWebAPIStream(_request_timeout())
        result._future = self._submit(
            self._stream(result, prompt, model_name, state, temporary)
        )
        return result


class SyncWebAPIStream:
    def __init__(self, timeout: int):
        self._queue = queue.Queue()
        self._future = None
        self._timeout = timeout
        self.state = None

    def __iter__(self):
        return self

    def __next__(self):
        try:
            kind, value = self._queue.get(timeout=self._timeout)
        except queue.Empty:
            if self._future:
                self._future.cancel()
            raise TimeoutError(f"Gemini webapi stream was idle for {self._timeout}s")
        if kind == "delta":
            return value
        if kind == "error":
            raise value
        raise StopIteration


_BACKEND = None
_BACKEND_LOCK = threading.Lock()


def backend() -> GeminiWebAPIBackend:
    global _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is None:
            _BACKEND = GeminiWebAPIBackend()
        return _BACKEND


def generate_with_state(
    prompt: str,
    model_name: str,
    state: dict = None,
    temporary: bool = False,
) -> tuple:
    return backend().generate(prompt, model_name, state, temporary)


def generate_stream_with_state(
    prompt: str,
    model_name: str,
    state: dict = None,
    temporary: bool = False,
):
    return backend().generate_stream(prompt, model_name, state, temporary)
