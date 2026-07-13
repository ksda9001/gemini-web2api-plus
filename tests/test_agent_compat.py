import asyncio
import importlib.util
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import gemini_web2api.gemini as gemini
from gemini_web2api.gemini import extract_response_text
import gemini_web2api.server as server
import gemini_web2api.webapi_backend as webapi_backend
from gemini_web2api.agent import ResponseStore, filter_tool_calls, sanitize_model_text
from gemini_web2api.config import CONFIG
from gemini_web2api.cookies import cookie_pairs_from_content
from gemini_web2api.tools import (
    agent_delta_to_prompt,
    google_contents_to_messages,
    messages_to_prompt,
    parse_tool_calls,
    strip_tool_call_protocol,
)
from gemini_web2api.webapi_backend import metadata_to_state, state_to_metadata


TOOLS = [{
    "type": "function",
    "name": "shell_command",
    "description": "Run a shell command",
    "parameters": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]

GOOGLE_TOOLS = [{
    "functionDeclarations": [{
        "name": "shell_command",
        "description": "Run a shell command",
        "parameters": TOOLS[0]["parameters"],
    }],
}]


def make_wrb_line(*texts):
    inner = [None] * 5
    inner[0] = "x" * 220
    inner[4] = [[None, list(texts)]]
    outer = [["wrb.fr", None, json.dumps(inner, ensure_ascii=False)]]
    return json.dumps(outer, ensure_ascii=False)


def load_single_file_module():
    module_path = Path(__file__).resolve().parents[1] / "gemini_web2api.py"
    spec = importlib.util.spec_from_file_location("gemini_web2api_single", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HttpHarness:
    def __init__(
        self,
        tmpdir,
        responses,
        reuse_upstream_sessions=True,
        reuse_upstream_agent_sessions=False,
    ):
        self.prompts = []
        self._responses = iter(responses)
        self._original_generate = server.generate
        self._original_generate_stream = server.generate_stream
        self._original_generate_with_state = server.generate_with_state
        server.generate = self._fake_generate
        server.generate_stream = self._fake_generate_stream
        server.generate_with_state = self._fake_generate_with_state
        server.RESPONSE_STORE = None
        CONFIG.update({
            "api_keys": [],
            "host": "127.0.0.1",
            "response_store_path": str(Path(tmpdir) / "responses.db"),
            "response_store_ttl_sec": 86400,
            "response_store_max_rows": 1000,
            "max_tool_output_chars": 80,
            "max_history_messages": 20,
            "max_history_chars": 10000,
            "max_google_prompt_chars": 18000,
            "max_google_agent_prompt_chars": 40000,
            "google_stream_auto_tools": False,
            "google_stream_auto_agent_tools": True,
            "reuse_upstream_sessions": reuse_upstream_sessions,
            "reuse_upstream_agent_sessions": reuse_upstream_agent_sessions,
            "agent_use_webapi": False,
            "upstream_session_backend": "gemini_webapi",
            "upstream_session_fallback_direct": True,
            "tool_retry_attempts": 1,
        })
        self.httpd = server.ThreadedServer(("127.0.0.1", 0), server.GeminiHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def _fake_generate(self, prompt, *args, **kwargs):
        self.prompts.append(prompt)
        return next(self._responses)

    def _fake_generate_stream(self, prompt, *args, **kwargs):
        self.prompts.append(prompt)
        yield next(self._responses)

    def _fake_generate_with_state(self, prompt, *args, **kwargs):
        self.prompts.append(prompt)
        index = len(self.prompts)
        response = next(self._responses)
        if isinstance(response, BaseException):
            raise response
        return response, {
            "conversation_id": "c_test",
            "response_id": f"r_{index}",
            "choice_id": f"rc_{index}",
        }, prompt

    def post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())

    def post_sse(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode()
        events = []
        for block in body.strip().split("\n\n"):
            event = None
            data = None
            for line in block.splitlines():
                if line.startswith("event: "):
                    event = line[len("event: "):]
                elif line.startswith("data: "):
                    raw = line[len("data: "):]
                    data = raw if raw == "[DONE]" else json.loads(raw)
            if event or data:
                events.append({"event": event, "data": data})
        return events

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
            return json.loads(resp.read().decode())

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.generate = self._original_generate
        server.generate_stream = self._original_generate_stream
        server.generate_with_state = self._original_generate_with_state
        server.RESPONSE_STORE = None


class SingleFileHttpHarness:
    def __init__(self, tmpdir, responses):
        self.module = load_single_file_module()
        self.prompts = []
        self._responses = iter(responses)
        self._original_call_gemini = self.module.GeminiHandler._call_gemini
        harness = self

        def fake_call(handler_self, prompt, model_id, think_mode, tools):
            harness.prompts.append(prompt)
            text = next(harness._responses)
            tool_calls = None
            if tools and text:
                text, tool_calls = harness.module.parse_tool_calls(text)
            return text or "", tool_calls

        self.module.GeminiHandler._call_gemini = fake_call
        self.module.CONFIG.update({
            "api_keys": [],
            "host": "127.0.0.1",
            "response_store_path": str(Path(tmpdir) / "single_responses.db"),
            "response_store_ttl_sec": 86400,
            "response_store_max_rows": 1000,
            "max_tool_output_chars": 80,
            "max_history_messages": 20,
            "max_history_chars": 10000,
            "max_google_prompt_chars": 18000,
            "google_stream_auto_tools": False,
            "tool_retry_attempts": 1,
        })
        self.httpd = server.ThreadedServer(("127.0.0.1", 0), self.module.GeminiHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.module.GeminiHandler._call_gemini = self._original_call_gemini


class AgentCompatTests(unittest.TestCase):
    def test_extract_response_text_merges_independent_segments(self):
        raw = "\n".join([
            make_wrb_line("第一段。\n"),
            make_wrb_line("第二段。\n"),
            make_wrb_line("第三段。"),
        ])
        self.assertEqual(extract_response_text(raw), "第一段。\n第二段。\n第三段。")

    def test_plain_chat_prompt_does_not_include_agent_or_tool_instructions(self):
        prompt, _ = messages_to_prompt([{"role": "user", "content": "普通聊天"}])
        self.assertEqual(prompt, "普通聊天")
        self.assertNotIn("Agent mode", prompt)
        self.assertNotIn("# Tool Use", prompt)

    def test_agent_prompt_uses_compact_tool_schema(self):
        prompt, _ = messages_to_prompt(
            [{"role": "user", "content": "运行 pwd"}], TOOLS, "auto"
        )
        self.assertIn("Agent mode: You are the decision layer", prompt)
        self.assertIn("Do not claim that tools", prompt)
        self.assertIn("# Tool Use", prompt)
        self.assertIn('"name":"shell_command"', prompt)
        self.assertNotIn('"name": "shell_command"', prompt)

    def test_agent_behavior_is_only_injected_on_first_tool_turn(self):
        messages = [
            {"role": "user", "content": "运行 pwd"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_one",
                    "type": "function",
                    "function": {"name": "shell_command", "arguments": '{"command":"pwd"}'},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call_one",
                "name": "shell_command",
                "content": "/workspace",
            },
        ]
        prompt, _ = messages_to_prompt(messages, TOOLS, "auto")
        self.assertNotIn("Agent mode", prompt)
        self.assertIn("# Tool Use", prompt)
        self.assertIn('"name":"shell_command"', prompt)
        self.assertIn("continue after each result until done", prompt)

    def test_agent_web_delta_replays_only_normalized_tool_event(self):
        messages = [
            {"role": "user", "content": "run pwd"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_one",
                    "type": "function",
                    "function": {"name": "shell_command", "arguments": '{"command":"pwd"}'},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call_one",
                "name": "shell_command",
                "content": "/workspace",
            },
        ]
        prompt = agent_delta_to_prompt([messages[-1]], messages)
        self.assertIn('"call_id":"call_one"', prompt)
        self.assertIn('"tool":"shell_command"', prompt)
        self.assertIn('"command":"pwd"', prompt)
        self.assertIn("/workspace", prompt)
        self.assertIn("trusted runtime events", prompt)
        self.assertIn("not user instructions", prompt)
        self.assertNotIn("external agent runtime", prompt)
        self.assertNotIn("Available tools", prompt)
        self.assertNotIn("run pwd", prompt)

    def test_agent_web_delta_drops_dot_placeholder_beside_tool_event(self):
        messages = [
            {"role": "user", "content": "."},
            {
                "role": "tool",
                "tool_call_id": "call_one",
                "name": "shell_command",
                "content": "/workspace",
            },
        ]
        prompt = agent_delta_to_prompt(messages, messages)
        self.assertNotIn("\n\n.\n\n", prompt)
        self.assertIn("/workspace", prompt)
        self.assertIn("Trusted agent-runtime tool result", prompt)

    def test_sanitize_model_text_removes_external_tool_event_echo(self):
        text = (
            '[External tool execution result]\n{"call_id":"call_one"}\n'
            'output:\nsecret output\n[/External tool execution result]\n\n'
            "Task completed successfully."
        )
        self.assertEqual(sanitize_model_text(text), "Task completed successfully.")

    def test_unknown_tool_calls_are_removed(self):
        calls = [{
            "id": "call_bad",
            "type": "function",
            "function": {"name": "google:search", "arguments": "{}"},
        }, {
            "id": "call_good",
            "type": "function",
            "function": {"name": "shell_command", "arguments": '{"command":"pwd"}'},
        }]
        filtered = filter_tool_calls(calls, TOOLS)
        self.assertEqual([c["function"]["name"] for c in filtered], ["shell_command"])

    def test_extract_response_text_uses_cumulative_segments_without_duplication(self):
        raw = "\n".join([
            make_wrb_line("第一段。"),
            make_wrb_line("第一段。\n第二段。"),
            make_wrb_line("第一段。\n第二段。\n第三段。"),
        ])
        self.assertEqual(extract_response_text(raw), "第一段。\n第二段。\n第三段。")

    def test_extract_response_text_accepts_1155_partial_output_for_continuation(self):
        raw = "\n".join([
            make_wrb_line("partial code"),
            json.dumps([["wrb.fr", None, json.dumps([None, [["BardErrorInfo", [1155]]]])]]),
        ])
        self.assertEqual(extract_response_text(raw), "partial code")
        self.assertTrue(gemini._was_truncated(raw))

    def test_direct_protocol_preserves_complete_conversation_metadata(self):
        metadata = ["c1", "r1", "old-choice", "m3", None, "m5", None, None, None, "ctx-token"]
        inner = [None] * 28
        inner[1] = metadata
        inner[4] = [["new-choice", ["answer"]]]
        inner[25] = "ctx-token-new"
        raw = json.dumps([["wrb.fr", None, json.dumps(inner)]])

        state = gemini._extract_conversation_state(raw)
        self.assertEqual(state["backend"], "direct")
        self.assertEqual(state["metadata"][0:3], ["c1", "r1", "new-choice"])
        self.assertEqual(state["metadata"][3], "m3")
        self.assertEqual(state["metadata"][9], "ctx-token-new")

        payload = urllib.parse.parse_qs(gemini._build_payload("continue", 1, 0, conversation=state))
        outer = json.loads(payload["f.req"][0])
        rebuilt = json.loads(outer[1])
        self.assertEqual(rebuilt[2], state["metadata"])

    def test_direct_protocol_marks_background_task_temporary(self):
        payload = urllib.parse.parse_qs(
            gemini._build_payload("make a title", 1, 0, temporary=True)
        )
        outer = json.loads(payload["f.req"][0])
        inner = json.loads(outer[1])
        self.assertEqual(inner[45], 1)

        normal_payload = urllib.parse.parse_qs(
            gemini._build_payload("ordinary chat", 1, 0)
        )
        normal_outer = json.loads(normal_payload["f.req"][0])
        normal_inner = json.loads(normal_outer[1])
        self.assertIsNone(normal_inner[45])

    def test_openwebui_metadata_templates_use_temporary_chat(self):
        prompts = [
            "### Task:\nGenerate a concise, 3-5 word title with an emoji summarizing the chat history.",
            "### Task:\nGenerate 1-3 broad tags categorizing the main themes of the chat history",
            "### Task:\nSuggest 3-5 relevant follow-up questions or prompts that the user might naturally ask next",
            "### Task:\nGenerate a detailed prompt for am image generation task based on the given language and context.",
        ]
        previous = CONFIG.get("temporary_background_tasks")
        try:
            CONFIG["temporary_background_tasks"] = True
            for prompt in prompts:
                self.assertTrue(server._is_background_metadata_prompt(prompt))
            self.assertFalse(server._is_background_metadata_prompt("记住123"))
            self.assertFalse(server._is_background_metadata_prompt("帮我生成三个标签"))
            CONFIG["temporary_background_tasks"] = False
            self.assertFalse(server._is_background_metadata_prompt(prompts[0]))
        finally:
            CONFIG["temporary_background_tasks"] = previous

    def test_webapi_metadata_round_trip_keeps_context(self):
        metadata = ["c1", "r2", "rc3", None, None, None, None, None, None, "context"]
        state = metadata_to_state(metadata)
        self.assertEqual(state["backend"], "gemini_webapi")
        self.assertEqual(state_to_metadata(state), metadata)

    def test_webapi_fresh_chats_do_not_share_metadata(self):
        class FakeChat:
            _ChatSession__metadata = ["shared", "rid", "rcid", None, None, None, None, None, None, ""]

            @property
            def metadata(self):
                return self._ChatSession__metadata

        class FakeClient:
            def start_chat(self, model):
                return FakeChat()

        first = webapi_backend._start_isolated_chat(FakeClient(), "flash")
        first._ChatSession__metadata[0] = "first-cid"
        second = webapi_backend._start_isolated_chat(FakeClient(), "flash")
        self.assertEqual(second.metadata[0], "")
        self.assertIsNot(first.metadata, second.metadata)

    def test_webapi_resumed_chat_owns_complete_metadata(self):
        class FakeChat:
            _ChatSession__metadata = []

            @property
            def metadata(self):
                return self._ChatSession__metadata

        class FakeClient:
            def start_chat(self, model):
                return FakeChat()

        state = metadata_to_state(
            ["cid", "rid", "rcid", None, None, None, None, None, None, "context"]
        )
        chat = webapi_backend._start_isolated_chat(FakeClient(), "flash", state)
        self.assertEqual(chat.metadata, state["metadata"])
        self.assertIsNot(chat.metadata, state["metadata"])

    def test_webapi_rejects_unauthenticated_session_by_default(self):
        class Status:
            name = "UNAUTHENTICATED"

        class FakeClient:
            account_status = Status()

        previous = {
            key: CONFIG.get(key)
            for key in ("require_authenticated_webapi", "webapi_allow_unverified_account")
        }
        try:
            CONFIG["require_authenticated_webapi"] = True
            CONFIG["webapi_allow_unverified_account"] = False
            with self.assertRaisesRegex(RuntimeError, "not authenticated"):
                webapi_backend._assert_authenticated(FakeClient())
            CONFIG["webapi_allow_unverified_account"] = True
            webapi_backend._assert_authenticated(FakeClient())
            CONFIG["require_authenticated_webapi"] = False
            webapi_backend._assert_authenticated(FakeClient())
        finally:
            CONFIG.update(previous)

    def test_webapi_accepts_available_account(self):
        class Status:
            name = "AVAILABLE"

        class FakeClient:
            account_status = Status()

        webapi_backend._assert_authenticated(FakeClient())

    def test_webapi_non_stream_generation_collects_stream_deltas(self):
        class Output:
            def __init__(self, delta):
                self.text_delta = delta

        class FakeChat:
            _ChatSession__metadata = ["cid", "rid", "rcid", None, None, None, None, None, None, ""]

            @property
            def metadata(self):
                return self._ChatSession__metadata

            async def send_message_stream(self, prompt, temporary=False):
                self.prompt = prompt
                self.temporary = temporary
                yield Output('{"name":"shell_')
                yield Output('command","arguments":{}}')
                self._ChatSession__metadata = [
                    "cid", "rid-next", "rcid-next", None, None,
                    None, None, None, None, "",
                ]

        class FakeClient:
            def __init__(self):
                self.chat = FakeChat()

            def start_chat(self, model):
                return self.chat

            def list_models(self):
                return []

        backend = object.__new__(webapi_backend.GeminiWebAPIBackend)
        fake_client = FakeClient()

        async def ensure_client():
            return fake_client

        backend._ensure_client = ensure_client
        text, state = asyncio.run(
            backend._generate("call the tool", "gemini-3.5-flash", temporary=True)
        )
        self.assertEqual(text, '{"name":"shell_command","arguments":{}}')
        self.assertEqual(state["conversation_id"], "cid")
        self.assertEqual(fake_client.chat.prompt, "call the tool")
        self.assertTrue(fake_client.chat.temporary)

    def test_webapi_dependency_imports_client_and_model_when_installed(self):
        try:
            import gemini_webapi
        except ImportError:
            self.skipTest("gemini-webapi is not installed in the source-only test environment")
        self.assertIs(webapi_backend.GeminiClient, gemini_webapi.GeminiClient)
        self.assertIsNotNone(webapi_backend.Model)

    def test_webapi_credentials_fingerprint_complete_cookie_jar(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            cookie_path = Path(tmpdir) / "cookie.txt"
            previous = CONFIG.get("cookie_file")
            try:
                cookie_path.write_text(
                    "__Secure-1PSID=one; SID=sid-one; SAPISID=sapi-one",
                    encoding="utf-8",
                )
                CONFIG["cookie_file"] = str(cookie_path)
                pairs, psid, psidts, first_fingerprint = webapi_backend.GeminiWebAPIBackend._credentials()
                self.assertEqual(psid, "one")
                self.assertEqual(psidts, "")
                self.assertEqual(pairs["SID"], "sid-one")
                cookie_path.write_text(
                    "__Secure-1PSID=one; SID=sid-two; SAPISID=sapi-one",
                    encoding="utf-8",
                )
                _, _, _, second_fingerprint = webapi_backend.GeminiWebAPIBackend._credentials()
                self.assertNotEqual(first_fingerprint, second_fingerprint)
            finally:
                CONFIG["cookie_file"] = previous

    def test_browser_cookie_export_omits_expired_and_non_gemini_cookies(self):
        exported = json.dumps([
            {"name": "SID", "value": "live", "domain": ".google.com", "expirationDate": 2000},
            {"name": "COMPASS", "value": "session", "domain": ".gemini.google.com"},
            {"name": "__Secure-1PSIDRTS", "value": "expired", "domain": ".google.com", "expirationDate": 999},
            {"name": "OTHER", "value": "ignore", "domain": ".example.com", "expirationDate": 3000},
            {"name": "ACCOUNT", "value": "ignore", "domain": "accounts.google.com", "hostOnly": True},
        ])
        pairs, next_expiry = cookie_pairs_from_content(exported, now=1000)
        self.assertEqual(pairs, {"SID": "live", "COMPASS": "session"})
        self.assertEqual(next_expiry, 2000)

    def test_direct_cookie_cache_rechecks_browser_export_at_expiry(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            cookie_path = Path(tmpdir) / "cookie.json"
            cookie_path.write_text(json.dumps([
                {"name": "SAPISID", "value": "live", "domain": ".google.com", "expirationDate": 3000},
                {"name": "__Secure-1PSIDRTS", "value": "short", "domain": ".google.com", "expirationDate": 1500},
            ]), encoding="utf-8")
            previous_path = CONFIG.get("cookie_file")
            previous_cache = dict(gemini._cookie_cache)
            CONFIG["cookie_file"] = str(cookie_path)
            gemini._cookie_cache = {"str": "", "sapisid": None, "mtime": 0, "expires_at": None}
            try:
                with patch.object(gemini.time, "time", side_effect=[1000, 1501]):
                    first, _ = gemini.load_cookie()
                    second, _ = gemini.load_cookie()
            finally:
                CONFIG["cookie_file"] = previous_path
                gemini._cookie_cache = previous_cache
        self.assertIn("__Secure-1PSIDRTS=short", first)
        self.assertNotIn("__Secure-1PSIDRTS=short", second)
        self.assertIn("SAPISID=live", second)

    def test_webapi_cache_resets_when_mounted_cookie_changes(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            cache_path = Path(tmpdir) / "cookies"
            cache_path.mkdir()
            cached = cache_path / ".cached_cookies_old.json"
            cached.write_text("old", encoding="utf-8")
            webapi_backend.GeminiWebAPIBackend._prepare_cookie_cache(str(cache_path), "first")
            self.assertFalse(cached.exists())
            fresh = cache_path / ".cached_cookies_fresh.json"
            fresh.write_text("fresh", encoding="utf-8")
            webapi_backend.GeminiWebAPIBackend._prepare_cookie_cache(str(cache_path), "first")
            self.assertTrue(fresh.exists())
            webapi_backend.GeminiWebAPIBackend._prepare_cookie_cache(str(cache_path), "second")
            self.assertFalse(fresh.exists())

    def test_webapi_source_fingerprint_ignores_active_cookie_expiry(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            cookie_path = Path(tmpdir) / "cookie.json"
            cookie_path.write_text(json.dumps([
                {"name": "__Secure-1PSID", "value": "login", "domain": ".google.com", "expirationDate": 3000},
                {"name": "__Secure-1PSIDRTS", "value": "short", "domain": ".google.com", "expirationDate": 1500},
            ]), encoding="utf-8")
            previous = CONFIG.get("cookie_file")
            CONFIG["cookie_file"] = str(cookie_path)
            try:
                source_before = webapi_backend.GeminiWebAPIBackend._source_cookie_fingerprint()
                with patch("gemini_web2api.cookies.time.time", return_value=1000):
                    active_before = webapi_backend.GeminiWebAPIBackend._credentials()[3]
                with patch("gemini_web2api.cookies.time.time", return_value=1600):
                    active_after = webapi_backend.GeminiWebAPIBackend._credentials()[3]
                source_after = webapi_backend.GeminiWebAPIBackend._source_cookie_fingerprint()
            finally:
                CONFIG["cookie_file"] = previous
        self.assertNotEqual(active_before, active_after)
        self.assertEqual(source_before, source_after)

    def test_webapi_serializes_concurrent_client_initialization(self):
        class Status:
            name = "AVAILABLE"

        class BrokenClient:
            def __init__(self):
                self.close_calls = 0

            async def close(self):
                self.close_calls += 1
                raise AttributeError("HTTP session was never created")

        class FakeClient:
            instances = []
            init_calls = 0

            def __init__(self, *args, **kwargs):
                self._running = False
                self.account_status = Status()
                self.cookies = {}
                FakeClient.instances.append(self)

            async def init(self, **kwargs):
                FakeClient.init_calls += 1
                await asyncio.sleep(0.01)
                self._running = True

            async def close(self):
                self._running = False

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            cookie_path = Path(tmpdir) / "cookie.txt"
            cookie_path.write_text("__Secure-1PSID=one; SID=sid-one", encoding="utf-8")
            previous_cookie_file = CONFIG.get("cookie_file")
            previous_client = webapi_backend.GeminiClient
            backend = object.__new__(webapi_backend.GeminiWebAPIBackend)
            broken = BrokenClient()
            backend._client = broken
            backend._cookie_fingerprint = "stale"
            backend._client_lock = asyncio.Lock()
            CONFIG["cookie_file"] = str(cookie_path)
            webapi_backend.GeminiClient = FakeClient
            try:
                async def initialize_twice():
                    return await asyncio.gather(
                        backend._ensure_client(), backend._ensure_client()
                    )

                first, second = asyncio.run(initialize_twice())
            finally:
                CONFIG["cookie_file"] = previous_cookie_file
                webapi_backend.GeminiClient = previous_client

        self.assertIs(first, second)
        self.assertEqual(FakeClient.init_calls, 1)
        self.assertEqual(len(FakeClient.instances), 1)
        self.assertEqual(broken.close_calls, 1)

    def test_webapi_non_stream_timeout_cancels_background_future(self):
        class TimedOutFuture:
            def __init__(self):
                self.cancelled = False

            def result(self, timeout):
                raise webapi_backend.FutureTimeoutError()

            def cancel(self):
                self.cancelled = True

        backend = object.__new__(webapi_backend.GeminiWebAPIBackend)
        future = TimedOutFuture()
        def fake_submit(coroutine):
            coroutine.close()
            return future
        backend._submit = fake_submit
        previous = CONFIG.get("webapi_request_timeout_sec")
        CONFIG["webapi_request_timeout_sec"] = 1
        try:
            with self.assertRaisesRegex(TimeoutError, "exceeded 1s"):
                backend.generate("hello", "gemini-3.5-flash")
        finally:
            CONFIG["webapi_request_timeout_sec"] = previous
        self.assertTrue(future.cancelled)

    def test_webapi_stream_idle_timeout_cancels_background_future(self):
        class PendingFuture:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

        stream = webapi_backend.SyncWebAPIStream(0.01)
        stream._future = PendingFuture()
        with self.assertRaisesRegex(TimeoutError, "idle"):
            next(stream)
        self.assertTrue(stream._future.cancelled)

    def test_generate_with_state_uses_webapi_session_backend(self):
        previous = {
            key: CONFIG.get(key)
            for key in ("reuse_upstream_sessions", "upstream_session_backend")
        }
        CONFIG.update({"reuse_upstream_sessions": True, "upstream_session_backend": "gemini_webapi"})
        state = metadata_to_state(["c1", "r1", "rc1", None, None, None, None, None, None, "ctx"])
        try:
            with patch(
                "gemini_web2api.webapi_backend.generate_with_state",
                return_value=("continued", state),
            ) as webapi_generate:
                text, returned_state, usage_prompt = gemini.generate_with_state(
                    "new tool result", 1, 0, conversation=state, model_name="gemini-3.5-flash"
                )
        finally:
            CONFIG.update(previous)
        self.assertEqual(text, "continued")
        self.assertEqual(returned_state, state)
        self.assertEqual(usage_prompt, "new tool result")
        webapi_generate.assert_called_once_with(
            "new tool result",
            "gemini-3.5-flash",
            state,
            temporary=False,
        )

    def test_agent_marked_state_skips_webapi_session_resume_by_default(self):
        previous = {
            key: CONFIG.get(key)
            for key in (
                "reuse_upstream_sessions",
                "upstream_session_backend",
                "reuse_upstream_agent_sessions",
                "retry_attempts",
            )
        }
        state = metadata_to_state(["c1", "r1", "rc1", None, None, None, None, None, None, "ctx"])
        state["backend"] = "gemini_webapi_agent"
        direct_state = {"backend": "direct", "conversation_id": "c2", "response_id": "r2", "choice_id": "rc2"}
        CONFIG.update({
            "reuse_upstream_sessions": True,
            "upstream_session_backend": "gemini_webapi",
            "reuse_upstream_agent_sessions": False,
            "retry_attempts": 1,
        })
        try:
            with patch(
                "gemini_web2api.webapi_backend.generate_with_state",
            ) as webapi_generate, patch.object(
                gemini,
                "_request_text",
                return_value=("continued", False, direct_state),
            ) as direct_request:
                text, returned_state, _ = gemini.generate_with_state(
                    "tool result",
                    1,
                    0,
                    conversation=state,
                    fallback_prompt="full tool history",
                    model_name="gemini-3.5-flash",
                )
        finally:
            CONFIG.update(previous)
        self.assertEqual(text, "continued")
        self.assertEqual(returned_state, direct_state)
        webapi_generate.assert_not_called()
        self.assertEqual(direct_request.call_args.args[0], "tool result")

    def test_agent_turn_honors_webapi_and_recovery_settings(self):
        handler = object.__new__(server.GeminiHandler)
        previous = {
            key: CONFIG.get(key)
            for key in (
                "agent_use_webapi",
                "agent_webapi_rebuild_on_failure",
                "agent_request_timeout_sec",
                "agent_retry_attempts",
            )
        }
        CONFIG.update({
            "agent_use_webapi": False,
            "agent_webapi_rebuild_on_failure": True,
            "agent_request_timeout_sec": 37,
            "agent_retry_attempts": 1,
        })
        try:
            with patch.object(
                server,
                "generate_with_state",
                return_value=("tool call", {}, "prompt"),
            ) as generate:
                text, state, usage = handler._generate_agent_turn(
                    "tool prompt",
                    "full prompt",
                    1,
                    0,
                    "gemini-3.5-flash",
                    [],
                    {},
                    None,
                    False,
                )
        finally:
            CONFIG.update(previous)
        self.assertEqual((text, state, usage), ("tool call", {}, "prompt"))
        self.assertFalse(generate.call_args.kwargs["allow_webapi"])
        self.assertTrue(generate.call_args.kwargs["agent_mode"])
        self.assertTrue(generate.call_args.kwargs["rebuild_webapi_on_failure"])
        self.assertEqual(generate.call_args.kwargs["request_timeout_sec"], 37)
        self.assertEqual(generate.call_args.kwargs["retry_attempts"], 1)

    def test_generate_with_state_marks_agent_web_session(self):
        previous = {
            key: CONFIG.get(key)
            for key in (
                "reuse_upstream_sessions",
                "reuse_upstream_agent_sessions",
                "upstream_session_backend",
            )
        }
        CONFIG.update({
            "reuse_upstream_sessions": True,
            "reuse_upstream_agent_sessions": True,
            "upstream_session_backend": "gemini_webapi",
        })
        state = metadata_to_state(
            ["c1", "r1", "rc1", None, None, None, None, None, None, "ctx"]
        )
        try:
            with patch(
                "gemini_web2api.webapi_backend.generate_with_state",
                return_value=("tool call", state),
            ) as webapi_generate:
                text, returned_state, usage_prompt = gemini.generate_with_state(
                    "full agent prompt",
                    1,
                    0,
                    model_name="gemini-3.5-flash",
                    agent_mode=True,
                    request_timeout_sec=17,
                )
        finally:
            CONFIG.update(previous)
        self.assertEqual(text, "tool call")
        self.assertEqual(usage_prompt, "full agent prompt")
        self.assertEqual(returned_state["backend"], "gemini_webapi_agent")
        webapi_generate.assert_called_once_with(
            "full agent prompt",
            "gemini-3.5-flash",
            None,
            temporary=False,
            timeout_sec=17,
        )

    def test_agent_web_resume_failure_rebuilds_fresh_web_session(self):
        previous = {
            key: CONFIG.get(key)
            for key in (
                "reuse_upstream_sessions",
                "reuse_upstream_agent_sessions",
                "upstream_session_backend",
                "upstream_session_fallback_direct",
            )
        }
        CONFIG.update({
            "reuse_upstream_sessions": True,
            "reuse_upstream_agent_sessions": True,
            "upstream_session_backend": "gemini_webapi",
            "upstream_session_fallback_direct": True,
        })
        old_state = metadata_to_state(
            ["old-c", "old-r", "old-rc", None, None, None, None, None, None, "ctx"]
        )
        old_state["backend"] = "gemini_webapi_agent"
        rebuilt_state = metadata_to_state(
            ["new-c", "new-r", "new-rc", None, None, None, None, None, None, "new"]
        )
        try:
            with patch(
                "gemini_web2api.webapi_backend.generate_with_state",
                side_effect=[RuntimeError("resume stalled"), ("continued", rebuilt_state)],
            ) as webapi_generate, patch.object(gemini, "_request_text") as direct_request:
                text, returned_state, usage_prompt = gemini.generate_with_state(
                    "tool result delta",
                    1,
                    0,
                    conversation=old_state,
                    fallback_prompt="full compacted agent history",
                    model_name="gemini-3.5-flash",
                    agent_mode=True,
                    rebuild_webapi_on_failure=True,
                    request_timeout_sec=23,
                )
        finally:
            CONFIG.update(previous)
        self.assertEqual(text, "continued")
        self.assertEqual(usage_prompt, "full compacted agent history")
        self.assertEqual(returned_state["backend"], "gemini_webapi_agent")
        self.assertEqual(returned_state["conversation_id"], "new-c")
        self.assertEqual(webapi_generate.call_count, 2)
        self.assertEqual(webapi_generate.call_args_list[0].args[0], "tool result delta")
        self.assertEqual(webapi_generate.call_args_list[0].args[2], old_state)
        self.assertEqual(webapi_generate.call_args_list[1].args[0], "full compacted agent history")
        self.assertIsNone(webapi_generate.call_args_list[1].args[2])
        direct_request.assert_not_called()

    def test_generate_with_state_honors_agent_timeout_and_retry_overrides(self):
        previous = CONFIG.get("retry_attempts")
        CONFIG["retry_attempts"] = 3
        try:
            with patch.object(
                gemini,
                "_request_text",
                side_effect=RuntimeError("upstream stalled"),
            ) as direct_request:
                with self.assertRaisesRegex(RuntimeError, "upstream stalled"):
                    gemini.generate_with_state(
                        "tool prompt",
                        1,
                        0,
                        allow_webapi=False,
                        request_timeout_sec=7,
                        retry_attempts=1,
                    )
        finally:
            CONFIG["retry_attempts"] = previous
        self.assertEqual(direct_request.call_count, 1)
        self.assertEqual(direct_request.call_args.kwargs["timeout_sec"], 7)

    def test_generate_with_state_forwards_temporary_to_webapi(self):
        previous = {
            key: CONFIG.get(key)
            for key in ("reuse_upstream_sessions", "upstream_session_backend")
        }
        CONFIG.update({"reuse_upstream_sessions": True, "upstream_session_backend": "gemini_webapi"})
        state = metadata_to_state(["", "", "", None, None, None, None, None, None, ""])
        try:
            with patch(
                "gemini_web2api.webapi_backend.generate_with_state",
                return_value=("title", state),
            ) as webapi_generate:
                text, _, _ = gemini.generate_with_state(
                    "title helper",
                    1,
                    0,
                    model_name="gemini-3.5-flash",
                    temporary=True,
                )
        finally:
            CONFIG.update(previous)
        self.assertEqual(text, "title")
        webapi_generate.assert_called_once_with(
            "title helper",
            "gemini-3.5-flash",
            None,
            temporary=True,
        )

    def test_webapi_failure_replays_full_prompt_through_direct_backend(self):
        previous = {
            key: CONFIG.get(key)
            for key in (
                "reuse_upstream_sessions",
                "upstream_session_backend",
                "upstream_session_fallback_direct",
                "retry_attempts",
            )
        }
        CONFIG.update({
            "reuse_upstream_sessions": True,
            "upstream_session_backend": "gemini_webapi",
            "upstream_session_fallback_direct": True,
            "retry_attempts": 1,
        })
        state = metadata_to_state(["c1", "r1", "rc1", None, None, None, None, None, None, "ctx"])
        direct_state = {"backend": "direct", "conversation_id": "c2", "response_id": "r2", "choice_id": "rc2"}
        try:
            with patch(
                "gemini_web2api.webapi_backend.generate_with_state",
                side_effect=RuntimeError("resume rejected"),
            ), patch.object(
                gemini,
                "_request_text",
                return_value=("rebuilt", False, direct_state),
            ) as direct_request:
                text, returned_state, usage_prompt = gemini.generate_with_state(
                    "delta", 1, 0, conversation=state, fallback_prompt="full history",
                    model_name="gemini-3.5-flash",
                )
        finally:
            CONFIG.update(previous)
        self.assertEqual(text, "rebuilt")
        self.assertEqual(returned_state, direct_state)
        self.assertEqual(usage_prompt, "full history")
        self.assertEqual(direct_request.call_args.args[0], "full history")
        self.assertIsNone(direct_request.call_args.args[5])

    def test_temporary_survives_webapi_fallback_to_direct(self):
        previous = {
            key: CONFIG.get(key)
            for key in (
                "reuse_upstream_sessions",
                "upstream_session_backend",
                "upstream_session_fallback_direct",
                "retry_attempts",
            )
        }
        CONFIG.update({
            "reuse_upstream_sessions": True,
            "upstream_session_backend": "gemini_webapi",
            "upstream_session_fallback_direct": True,
            "retry_attempts": 1,
        })
        try:
            with patch(
                "gemini_web2api.webapi_backend.generate_with_state",
                side_effect=RuntimeError("temporary backend failed"),
            ), patch.object(
                gemini,
                "_request_text",
                return_value=("temporary result", False, {}),
            ) as direct_request:
                text, _, _ = gemini.generate_with_state(
                    "metadata helper",
                    1,
                    0,
                    fallback_prompt="metadata helper",
                    model_name="gemini-3.5-flash",
                    temporary=True,
                )
        finally:
            CONFIG.update(previous)
        self.assertEqual(text, "temporary result")
        self.assertTrue(direct_request.call_args.args[6])

    def test_generate_continues_truncated_output_without_duplicate_overlap(self):
        responses = iter([
            ("```html\n<body>mars", True, {"conversation_id": "c1", "response_id": "r1", "choice_id": "rc1"}),
            ("mars</body>\n```", False, {"conversation_id": "c1", "response_id": "r2", "choice_id": "rc2"}),
        ])
        previous_attempts = CONFIG.get("continuation_attempts")
        CONFIG["continuation_attempts"] = 2
        try:
            with patch.object(gemini, "_request_text", side_effect=lambda *args, **kwargs: next(responses)):
                text = gemini.generate("build mars", 1, 0)
        finally:
            CONFIG["continuation_attempts"] = previous_attempts
        self.assertEqual(text, "```html\n<body>mars</body>\n```")

    def test_generate_retries_empty_upstream_response(self):
        responses = iter([
            ("", False, {}),
            ("complete", False, {"conversation_id": "c1", "response_id": "r1", "choice_id": "rc1"}),
        ])
        previous_attempts = CONFIG.get("retry_attempts")
        previous_delay = CONFIG.get("retry_delay_sec")
        CONFIG.update({"retry_attempts": 2, "retry_delay_sec": 0})
        try:
            with patch.object(gemini, "_request_text", side_effect=lambda *args, **kwargs: next(responses)):
                text = gemini.generate("hello", 1, 0)
        finally:
            CONFIG.update({"retry_attempts": previous_attempts, "retry_delay_sec": previous_delay})
        self.assertEqual(text, "complete")

    def test_single_file_extract_response_text_merges_segments(self):
        module = load_single_file_module()
        raw = "\n".join([
            make_wrb_line("Part A. "),
            make_wrb_line("Part B. "),
            make_wrb_line("Part C."),
        ])
        self.assertEqual(module.extract_response_text(raw), "Part A. Part B. Part C.")

    def test_single_file_entry_supports_codex_responses_tools(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = SingleFileHttpHarness(tmpdir, [
                "我会先检查项目。",
                '{"name":"shell_command","arguments":{"command":"Get-ChildItem"}}',
            ])
            try:
                response = harness.post("/v1/responses", {
                    "model": "gemini-3.5-flash",
                    "input": "帮我排查这个项目",
                    "tools": TOOLS,
                })
                self.assertEqual(response["output"][0]["type"], "function_call")
                self.assertEqual(response["output"][0]["name"], "shell_command")
                self.assertEqual(len(harness.prompts), 2)
            finally:
                harness.close()

    def test_single_file_entry_supports_claude_code_tools(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = SingleFileHttpHarness(tmpdir, [
                "我会先检查当前目录。",
                '{"name":"shell_command","arguments":{"command":"Get-ChildItem"}}',
            ])
            try:
                response = harness.post("/v1/messages", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "帮我看下当前目录"}],
                    "tools": [{
                        "name": "shell_command",
                        "description": "Run a shell command",
                        "input_schema": TOOLS[0]["parameters"],
                    }],
                })
                self.assertEqual(response["content"][0]["type"], "tool_use")
                self.assertEqual(response["content"][0]["name"], "shell_command")
                self.assertEqual(response["stop_reason"], "tool_use")
                self.assertEqual(len(harness.prompts), 2)
            finally:
                harness.close()

    def test_single_file_entry_supports_copilot_chat_tools(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = SingleFileHttpHarness(tmpdir, [
                "我会先检查当前目录。",
                '{"name":"shell_command","arguments":{"command":"Get-ChildItem"}}',
            ])
            try:
                response = harness.post("/v1/chat/completions", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "帮我看下当前目录"}],
                    "tools": TOOLS,
                })
                message = response["choices"][0]["message"]
                self.assertEqual(message["tool_calls"][0]["function"]["name"], "shell_command")
                self.assertEqual(response["choices"][0]["finish_reason"], "tool_calls")
                self.assertEqual(len(harness.prompts), 2)
            finally:
                harness.close()

    def test_response_store_persists_messages_output_and_response(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "responses.db"
            store = ResponseStore(str(db_path), max_tool_output_chars=80)
            response = {
                "id": "resp_one",
                "object": "response",
                "model": "gemini-3.5-flash",
                "output": [],
            }
            messages = [{"role": "user", "content": "创建 test.txt"}]
            output = [{
                "type": "function_call",
                "id": "call_1",
                "call_id": "call_1",
                "name": "shell_command",
                "arguments": '{"command":"New-Item test.txt"}',
                "status": "completed",
            }]

            store.save(response, messages, output)

            self.assertEqual(store.get_response("resp_one")["id"], "resp_one")
            saved_messages = store.get_messages("resp_one")
            self.assertEqual(saved_messages[0]["content"], "创建 test.txt")
            self.assertEqual(saved_messages[-1]["tool_calls"][0]["function"]["name"], "shell_command")

            with closing(sqlite3.connect(db_path)) as con:
                row = con.execute("SELECT output_json FROM responses WHERE id = ?", ("resp_one",)).fetchone()
            saved_output = json.loads(row[0])
            self.assertEqual(saved_output[0]["name"], "shell_command")

    def test_response_store_persists_upstream_and_agent_sessions(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            store = ResponseStore(str(Path(tmpdir) / "responses.db"))
            state = {"conversation_id": "c1", "response_id": "r1", "choice_id": "rc1"}
            store.save_upstream_session("resp_one", "gemini-3.5-flash", state)
            self.assertEqual(store.get_upstream_session("resp_one", "gemini-3.5-flash"), state)

            messages = [
                {"role": "user", "content": "run pwd"},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_one",
                    "type": "function",
                    "function": {"name": "shell_command", "arguments": '{"command":"pwd"}'},
                }]},
            ]
            store.save_agent_session("gemini-3.5-flash", state, messages)
            continued = messages + [{
                "role": "tool",
                "tool_call_id": "call_one",
                "name": "shell_command",
                "content": "/workspace",
            }]
            session = store.find_agent_session("gemini-3.5-flash", continued)
            self.assertEqual(session["upstream_state"], state)
            self.assertEqual(session["delta_messages"], [continued[-1]])

    def test_response_store_reuses_plain_conversation_by_history_prefix(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            store = ResponseStore(str(Path(tmpdir) / "responses.db"))
            state = metadata_to_state(
                ["c1", "r1", "rc1", None, None, None, None, None, None, "ctx"]
            )
            known = [
                {"role": "user", "content": "Remember code 7319."},
                {"role": "assistant", "content": "I will remember it."},
            ]
            store.save_conversation_session("gemini-3.5-flash", state, known)
            session = store.find_conversation_session(
                "gemini-3.5-flash",
                known + [{"role": "user", "content": "What was the code?"}],
            )
            self.assertEqual(session["upstream_state"], state)
            self.assertEqual(
                session["delta_messages"],
                [{"role": "user", "content": "What was the code?"}],
            )

    def test_response_store_links_previous_response_and_truncates_tool_output(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            store = ResponseStore(str(Path(tmpdir) / "responses.db"), max_tool_output_chars=40)
            store.save(
                {"id": "resp_first", "object": "response", "model": "gemini-3.5-flash", "output": []},
                [{"role": "user", "content": "创建 test.txt"}],
                [{
                    "type": "function_call",
                    "id": "call_1",
                    "call_id": "call_1",
                    "name": "shell_command",
                    "arguments": '{"command":"New-Item test.txt"}',
                    "status": "completed",
                }],
            )
            store.save(
                {"id": "resp_second", "object": "response", "model": "gemini-3.5-flash", "output": []},
                store.get_messages("resp_first") + [{
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "name": "shell_command",
                    "content": "A" * 120 + "TAIL",
                }],
                [{
                    "type": "function_call",
                    "id": "call_2",
                    "call_id": "call_2",
                    "name": "shell_command",
                    "arguments": '{"command":"Get-Content test.txt"}',
                    "status": "completed",
                }],
                previous_response_id="resp_first",
            )

            second = store.get_response("resp_second")
            self.assertEqual(second["previous_response_id"], "resp_first")
            messages = store.get_messages("resp_second")
            self.assertTrue(any(m.get("content") == "创建 test.txt" for m in messages))
            tool_message = next(m for m in messages if m.get("role") == "tool")
            self.assertIn("truncated", tool_message["content"])
            self.assertIn("TAIL", tool_message["content"])

    def test_response_store_prunes_by_ttl_and_max_rows(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "responses.db"
            store = ResponseStore(str(db_path), ttl_sec=1, max_rows=2)
            for idx in range(3):
                store.save(
                    {"id": f"resp_{idx}", "object": "response", "model": "gemini-3.5-flash", "output": []},
                    [{"role": "user", "content": f"message {idx}"}],
                    [{"type": "message", "id": f"msg_{idx}", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}],
                )
                time.sleep(0.01)

            with closing(sqlite3.connect(db_path)) as con:
                ids = [row[0] for row in con.execute("SELECT id FROM responses ORDER BY updated_at").fetchall()]
            self.assertEqual(ids, ["resp_1", "resp_2"])

            with closing(sqlite3.connect(db_path)) as con:
                con.execute("UPDATE responses SET updated_at = ?", (int(time.time()) - 10,))
                con.commit()
            store.save(
                {"id": "resp_new", "object": "response", "model": "gemini-3.5-flash", "output": []},
                [{"role": "user", "content": "new"}],
                [{"type": "message", "id": "msg_new", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}],
            )
            with closing(sqlite3.connect(db_path)) as con:
                ids = [row[0] for row in con.execute("SELECT id FROM responses ORDER BY updated_at").fetchall()]
            self.assertEqual(ids, ["resp_new"])

    def test_parse_tool_calls_suppresses_prose_around_call(self):
        text = (
            "I will update the file now.\n\n"
            "```tool_call\n"
            '{"name":"shell_command","arguments":{"command":"echo hi"}}\n'
            "```\n"
        )
        clean, calls = parse_tool_calls(text)
        self.assertEqual(clean, "")
        self.assertEqual(calls[0]["function"]["name"], "shell_command")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["command"], "echo hi")

    def test_parse_tool_calls_extracts_embedded_raw_json(self):
        text = 'Use the terminal: {"name":"shell_command","arguments":{"command":"pwd; ls"}}'
        clean, calls = parse_tool_calls(text)
        self.assertEqual(clean, "")
        self.assertEqual(calls[0]["function"]["name"], "shell_command")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["command"], "pwd; ls")

    def test_strip_tool_call_protocol_removes_truncated_tail(self):
        text = 'Done with the files.\n```tool_call\n{"name":"shell_command","arguments":{"command":'
        cleaned = strip_tool_call_protocol(text)
        self.assertEqual(cleaned, "Done with the files.")
        self.assertNotIn("tool_call", cleaned)
        self.assertNotIn("shell_command", cleaned)

    def test_parse_tool_calls_hides_truncated_raw_json_tool_call(self):
        text = 'Done.\n{"name":"shell_command","arguments":{"command":"unterminated'
        clean, calls = parse_tool_calls(text)
        self.assertEqual(clean, "Done.")
        self.assertEqual(calls, [])
        self.assertNotIn("shell_command", strip_tool_call_protocol(text))

    def test_responses_parses_prose_plus_tool_call_without_leaking_text(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                (
                    "I will modify the project now.\n"
                    "```tool_call\n"
                    '{"name":"shell_command","arguments":{"command":"echo hi"}}\n'
                    "```"
                ),
            ])
            try:
                response = harness.post("/v1/responses", {
                    "model": "gemini-3.5-flash",
                    "input": "modify the project",
                    "tools": TOOLS,
                })
                self.assertEqual(len(response["output"]), 1)
                self.assertEqual(response["output"][0]["type"], "function_call")
                self.assertEqual(response["output"][0]["name"], "shell_command")
                self.assertNotIn("I will modify", json.dumps(response, ensure_ascii=False))
                self.assertEqual(len(harness.prompts), 1)
            finally:
                harness.close()

    def test_chat_completions_parses_prose_plus_tool_call_for_copilot(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                (
                    "I will inspect the workspace.\n"
                    "```tool_call\n"
                    '{"name":"shell_command","arguments":{"command":"pwd; ls"}}\n'
                    "```"
                ),
            ])
            try:
                response = harness.post("/v1/chat/completions", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "inspect the workspace"}],
                    "tools": TOOLS,
                })
                message = response["choices"][0]["message"]
                self.assertIsNone(message["content"])
                self.assertEqual(message["tool_calls"][0]["function"]["name"], "shell_command")
                self.assertEqual(response["choices"][0]["finish_reason"], "tool_calls")
                self.assertNotIn("I will inspect", json.dumps(response, ensure_ascii=False))
                self.assertEqual(len(harness.prompts), 1)
            finally:
                harness.close()

    def test_anthropic_parses_prose_plus_tool_call_for_claude_code(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                (
                    "I will inspect the workspace.\n"
                    "```tool_call\n"
                    '{"name":"shell_command","arguments":{"command":"pwd; ls"}}\n'
                    "```"
                ),
            ])
            try:
                response = harness.post("/v1/messages", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "inspect the workspace"}],
                    "tools": [{
                        "name": "shell_command",
                        "description": "Run a shell command",
                        "input_schema": TOOLS[0]["parameters"],
                    }],
                })
                self.assertEqual(response["content"][0]["type"], "tool_use")
                self.assertEqual(response["content"][0]["name"], "shell_command")
                self.assertEqual(response["stop_reason"], "tool_use")
                self.assertNotIn("I will inspect", json.dumps(response, ensure_ascii=False))
                self.assertEqual(len(harness.prompts), 1)
            finally:
                harness.close()

    def test_responses_retries_when_model_describes_tool_action(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                "I will create the file for you.",
                '{"name":"shell_command","arguments":{"command":"New-Item -ItemType File -Force -Path test.txt"}}',
            ])
            try:
                response = harness.post("/v1/responses", {
                    "model": "gemini-3.5-flash",
                    "input": "创建 test.txt",
                    "tools": TOOLS,
                })
                self.assertEqual(response["output"][0]["type"], "function_call")
                self.assertEqual(response["output"][0]["name"], "shell_command")
                self.assertEqual(len(harness.prompts), 2)
                self.assertIn("previous answer", harness.prompts[1])
            finally:
                harness.close()

    def test_responses_retries_for_common_codex_chinese_request(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                "我先检查项目，然后解决这个问题。",
                '{"name":"shell_command","arguments":{"command":"Get-ChildItem"}}',
            ])
            try:
                response = harness.post("/v1/responses", {
                    "model": "gemini-3.5-flash",
                    "input": "帮我解决这个项目里 agent 不能触发的问题",
                    "tools": TOOLS,
                })
                self.assertEqual(response["output"][0]["type"], "function_call")
                self.assertEqual(response["output"][0]["name"], "shell_command")
                self.assertEqual(len(harness.prompts), 2)
                self.assertIn("previous answer", harness.prompts[1])
            finally:
                harness.close()

    def test_responses_retries_for_deployable_mars_prompt(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                "I will create an interactive draggable Mars app for Cloudflare Pages.",
                '{"name":"shell_command","arguments":{"command":"New-Item -ItemType Directory -Force outputs"}}',
            ])
            try:
                response = harness.post("/v1/responses", {
                    "model": "gemini-3.5-flash",
                    "input": "做一个可以部署在cf上的，可以拖动的运动的火星",
                    "tools": TOOLS,
                })
                self.assertEqual(response["output"][0]["type"], "function_call")
                self.assertEqual(response["output"][0]["name"], "shell_command")
                self.assertEqual(len(harness.prompts), 2)
                self.assertIn("previous answer", harness.prompts[1])
            finally:
                harness.close()

    def test_responses_falls_back_to_safe_shell_inspection_when_retry_stays_text(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                "I will inspect the workspace first.",
                "I still cannot call tools from here.",
            ])
            try:
                response = harness.post("/v1/responses", {
                    "model": "gemini-3.5-flash",
                    "input": "create a deployable Mars app",
                    "tools": TOOLS,
                })
                call = response["output"][0]
                self.assertEqual(call["type"], "function_call")
                self.assertEqual(call["name"], "shell_command")
                self.assertEqual(json.loads(call["arguments"])["command"], "pwd; ls")
                self.assertEqual(len(harness.prompts), 2)
            finally:
                harness.close()

    def test_responses_returns_safe_tool_when_upstream_errors(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [RuntimeError("upstream empty")])
            try:
                response = harness.post("/v1/responses", {
                    "model": "gemini-3.5-flash",
                    "input": "inspect the workspace",
                    "tools": TOOLS,
                    "tool_choice": "required",
                })
                self.assertEqual(response["output"][0]["type"], "function_call")
                self.assertEqual(response["output"][0]["name"], "shell_command")
            finally:
                harness.close()

    def test_chat_completions_returns_safe_tool_when_upstream_errors(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [RuntimeError("upstream empty")])
            try:
                response = harness.post("/v1/chat/completions", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "inspect the workspace"}],
                    "tools": TOOLS,
                    "tool_choice": "required",
                })
                call = response["choices"][0]["message"]["tool_calls"][0]
                self.assertEqual(call["function"]["name"], "shell_command")
                self.assertEqual(response["choices"][0]["finish_reason"], "tool_calls")
            finally:
                harness.close()

    def test_anthropic_returns_safe_tool_when_upstream_errors(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [RuntimeError("upstream empty")])
            try:
                response = harness.post("/v1/messages", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "inspect the workspace"}],
                    "tools": [{
                        "name": "shell_command",
                        "description": "Run a shell command",
                        "input_schema": TOOLS[0]["parameters"],
                    }],
                    "tool_choice": {"type": "any"},
                })
                self.assertEqual(response["content"][0]["type"], "tool_use")
                self.assertEqual(response["content"][0]["name"], "shell_command")
                self.assertEqual(response["stop_reason"], "tool_use")
            finally:
                harness.close()

    def test_responses_repairs_model_invented_tool_name(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                '{"name":"google:search","arguments":{"query":"pwd"}}',
                '{"name":"shell_command","arguments":{"command":"pwd"}}',
            ])
            try:
                response = harness.post("/v1/responses", {
                    "model": "gemini-3.5-flash",
                    "input": "run pwd",
                    "tools": TOOLS,
                    "tool_choice": "required",
                })
                self.assertEqual(response["output"][0]["type"], "function_call")
                self.assertEqual(response["output"][0]["name"], "shell_command")
                self.assertIn('"shell_command"', harness.prompts[1])
                self.assertNotIn('"google:search"', harness.prompts[1])
            finally:
                harness.close()

    def test_chat_completions_repairs_model_invented_tool_name(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                '{"name":"google:search","arguments":{"query":"pwd"}}',
                '{"name":"shell_command","arguments":{"command":"pwd"}}',
            ])
            try:
                response = harness.post("/v1/chat/completions", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "run pwd"}],
                    "tools": TOOLS,
                    "tool_choice": "required",
                })
                call = response["choices"][0]["message"]["tool_calls"][0]
                self.assertEqual(call["function"]["name"], "shell_command")
                self.assertEqual(response["choices"][0]["finish_reason"], "tool_calls")
            finally:
                harness.close()

    def test_anthropic_repairs_model_invented_tool_name(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                '{"name":"google:search","arguments":{"query":"pwd"}}',
                '{"name":"shell_command","arguments":{"command":"pwd"}}',
            ])
            try:
                response = harness.post("/v1/messages", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "run pwd"}],
                    "tools": [{
                        "name": "shell_command",
                        "description": "Run a shell command",
                        "input_schema": TOOLS[0]["parameters"],
                    }],
                    "tool_choice": {"type": "any"},
                })
                self.assertEqual(response["content"][0]["name"], "shell_command")
                self.assertEqual(response["stop_reason"], "tool_use")
            finally:
                harness.close()

    def test_anthropic_falls_back_to_safe_shell_inspection_when_retry_stays_text(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                "I will inspect the workspace first.",
                "I still cannot call tools from here.",
            ])
            try:
                response = harness.post("/v1/messages", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "inspect the workspace"}],
                    "tools": [{
                        "name": "shell_command",
                        "description": "Run a shell command",
                        "input_schema": TOOLS[0]["parameters"],
                    }],
                })
                block = response["content"][0]
                self.assertEqual(block["type"], "tool_use")
                self.assertEqual(block["name"], "shell_command")
                self.assertEqual(block["input"]["command"], "pwd; ls")
                self.assertEqual(response["stop_reason"], "tool_use")
            finally:
                harness.close()

    def test_chat_completions_falls_back_to_safe_shell_inspection_when_retry_stays_text(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                "I will inspect the workspace first.",
                "I still cannot call tools from here.",
            ])
            try:
                response = harness.post("/v1/chat/completions", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "inspect the workspace"}],
                    "tools": TOOLS,
                })
                message = response["choices"][0]["message"]
                tool_call = message["tool_calls"][0]
                self.assertEqual(tool_call["function"]["name"], "shell_command")
                self.assertEqual(json.loads(tool_call["function"]["arguments"])["command"], "pwd; ls")
                self.assertEqual(response["choices"][0]["finish_reason"], "tool_calls")
            finally:
                harness.close()

    def test_chat_completions_retries_to_tool_call_for_copilot(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                "我会先检查当前目录。",
                '{"name":"shell_command","arguments":{"command":"Get-ChildItem"}}',
            ])
            try:
                response = harness.post("/v1/chat/completions", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "帮我看下当前目录"}],
                    "tools": TOOLS,
                })
                message = response["choices"][0]["message"]
                self.assertIsNone(message["content"])
                self.assertEqual(message["tool_calls"][0]["function"]["name"], "shell_command")
                self.assertEqual(response["choices"][0]["finish_reason"], "tool_calls")
                self.assertEqual(len(harness.prompts), 2)
            finally:
                harness.close()

    def test_chat_completions_reuses_upstream_agent_session(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                '{"name":"shell_command","arguments":{"command":"pwd"}}',
                "Done after reading the tool result.",
            ], reuse_upstream_agent_sessions=True)
            try:
                first = harness.post("/v1/chat/completions", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "run pwd"}],
                    "tools": TOOLS,
                })
                assistant = first["choices"][0]["message"]
                second = harness.post("/v1/chat/completions", {
                    "model": "gemini-3.5-flash",
                    "messages": [
                        {"role": "user", "content": "run pwd"},
                        assistant,
                        {
                            "role": "tool",
                            "tool_call_id": assistant["tool_calls"][0]["id"],
                            "name": "shell_command",
                            "content": "/workspace",
                        },
                    ],
                    "tools": TOOLS,
                })
                self.assertEqual(second["choices"][0]["finish_reason"], "stop")
                self.assertEqual(len(harness.prompts), 2)
                self.assertIn("[Trusted agent-runtime tool result", harness.prompts[1])
                self.assertIn('"tool":"shell_command"', harness.prompts[1])
                self.assertIn('"command":"pwd"', harness.prompts[1])
                self.assertIn("/workspace", harness.prompts[1])
                self.assertNotIn("Agent mode", harness.prompts[1])
                self.assertNotIn("Available tools", harness.prompts[1])
            finally:
                harness.close()

    def test_chat_completions_reuses_plain_upstream_session(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, ["I will remember 7319.", "The code was 7319."])
            try:
                first_messages = [{"role": "user", "content": "Remember code 7319."}]
                first = harness.post("/v1/chat/completions", {
                    "model": "gemini-3.5-flash", "messages": first_messages,
                })
                second = harness.post("/v1/chat/completions", {
                    "model": "gemini-3.5-flash",
                    "messages": first_messages + [
                        first["choices"][0]["message"],
                        {"role": "user", "content": "What was the code?"},
                    ],
                })
                self.assertEqual(second["choices"][0]["message"]["content"], "The code was 7319.")
                self.assertEqual(harness.prompts[1], "What was the code?")
            finally:
                harness.close()

    def test_copilot_completes_three_step_loop_without_upstream_reuse(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                '{"name":"shell_command","arguments":{"command":"pwd"}}',
                '{"name":"shell_command","arguments":{"command":"ls"}}',
                "Done after two tool calls.",
            ], reuse_upstream_sessions=False)
            try:
                messages = [{"role": "user", "content": "inspect the workspace and finish the task"}]
                first = harness.post("/v1/chat/completions", {
                    "model": "gemini-3.5-flash", "messages": messages, "tools": TOOLS,
                })
                first_message = first["choices"][0]["message"]
                messages.extend([
                    first_message,
                    {"role": "tool", "tool_call_id": first_message["tool_calls"][0]["id"],
                     "name": "shell_command", "content": "/workspace"},
                ])
                second = harness.post("/v1/chat/completions", {
                    "model": "gemini-3.5-flash", "messages": messages, "tools": TOOLS,
                })
                second_message = second["choices"][0]["message"]
                messages.extend([
                    second_message,
                    {"role": "tool", "tool_call_id": second_message["tool_calls"][0]["id"],
                     "name": "shell_command", "content": "README.md"},
                ])
                third = harness.post("/v1/chat/completions", {
                    "model": "gemini-3.5-flash", "messages": messages, "tools": TOOLS,
                })
                self.assertEqual(first["choices"][0]["finish_reason"], "tool_calls")
                self.assertEqual(second["choices"][0]["finish_reason"], "tool_calls")
                self.assertEqual(third["choices"][0]["finish_reason"], "stop")
                self.assertEqual(sum("Agent mode" in prompt for prompt in harness.prompts), 1)
                self.assertTrue(all("Available tools" in prompt for prompt in harness.prompts))
            finally:
                harness.close()

    def test_chat_completions_accepts_legacy_functions_for_copilot(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                "I will inspect the workspace.",
                '{"name":"shell_command","arguments":{"command":"Get-ChildItem"}}',
            ])
            try:
                response = harness.post("/v1/chat/completions", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "inspect the workspace"}],
                    "functions": [{
                        "name": "shell_command",
                        "description": "Run a shell command",
                        "parameters": TOOLS[0]["parameters"],
                    }],
                    "function_call": "auto",
                })
                message = response["choices"][0]["message"]
                self.assertEqual(message["tool_calls"][0]["function"]["name"], "shell_command")
                self.assertEqual(response["choices"][0]["finish_reason"], "tool_calls")
                self.assertEqual(len(harness.prompts), 2)
            finally:
                harness.close()

    def test_chat_completions_streams_tool_call_for_copilot(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                "我会先检查当前目录。",
                '{"name":"shell_command","arguments":{"command":"Get-ChildItem"}}',
            ])
            try:
                events = harness.post_sse("/v1/chat/completions", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "帮我看下当前目录"}],
                    "tools": TOOLS,
                    "stream": True,
                })
                chunks = [e["data"] for e in events if isinstance(e["data"], dict)]
                self.assertEqual(chunks[0]["choices"][0]["finish_reason"], "tool_calls")
                tool_call = chunks[0]["choices"][0]["delta"]["tool_calls"][0]
                self.assertEqual(tool_call["function"]["name"], "shell_command")
                self.assertEqual(events[-1]["data"], "[DONE]")
            finally:
                harness.close()

    def test_responses_store_false_does_not_persist(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                '{"name":"shell_command","arguments":{"command":"echo hi"}}',
            ])
            try:
                response = harness.post("/v1/responses", {
                    "model": "gemini-3.5-flash",
                    "input": "运行 echo hi",
                    "tools": TOOLS,
                    "store": False,
                })
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    harness.get(f"/v1/responses/{response['id']}")
                self.assertEqual(cm.exception.code, 404)
                cm.exception.close()
            finally:
                harness.close()

    def test_google_api_retries_text_into_function_call(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                "我会先列出目录。",
                'function_call\n{"name":"shell_command","args":{"command":"Get-ChildItem"}}',
            ])
            try:
                response = harness.post("/v1beta/models/gemini-3.5-flash:generateContent", {
                    "contents": [{"role": "user", "parts": [{"text": "帮我看下当前目录"}]}],
                    "tools": GOOGLE_TOOLS,
                    "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
                })
                parts = response["candidates"][0]["content"]["parts"]
                self.assertEqual(parts[0]["functionCall"]["name"], "shell_command")
                self.assertEqual(parts[0]["functionCall"]["args"]["command"], "Get-ChildItem")
                self.assertEqual(len(harness.prompts), 2)
                self.assertIn("previous answer", harness.prompts[1])
            finally:
                harness.close()

    def test_google_stream_auto_tools_uses_text_stream_without_tool_prompt(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, ["完整回答"], reuse_upstream_sessions=False)
            try:
                events = harness.post_sse("/v1beta/models/gemini-3.5-flash:streamGenerateContent?alt=sse", {
                    "contents": [{"role": "user", "parts": [{"text": "普通聊天，不需要工具"}]}],
                    "tools": GOOGLE_TOOLS,
                    "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
                })
                data_events = [e["data"] for e in events if isinstance(e["data"], dict)]
                self.assertEqual(data_events[0]["candidates"][0]["content"]["parts"][0]["text"], "完整回答")
                self.assertEqual(data_events[-1]["candidates"][0]["finishReason"], "STOP")
                self.assertEqual(len(harness.prompts), 1)
                self.assertNotIn("# Tool Use", harness.prompts[0])
            finally:
                harness.close()

    def test_google_stream_keeps_tools_for_converted_claude_agent(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                'function_call\n{"name":"shell_command","args":{"command":"pwd"}}',
            ])
            try:
                events = harness.post_sse(
                    "/v1beta/models/gemini-3.5-flash:streamGenerateContent?alt=sse",
                    {
                        "systemInstruction": {"parts": [{"text": (
                            "x-anthropic-billing-header: enabled\n"
                            "You are a Claude agent working through the Claude Agent SDK."
                        )}]},
                        "contents": [{"role": "user", "parts": [{
                            "text": "创建一个可以部署到 Cloudflare 的火星页面",
                        }]}],
                        "tools": GOOGLE_TOOLS,
                        "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
                    },
                )
                data_events = [e["data"] for e in events if isinstance(e["data"], dict)]
                function_call = data_events[0]["candidates"][0]["content"]["parts"][0]["functionCall"]
                self.assertEqual(function_call["name"], "shell_command")
                self.assertEqual(function_call["args"]["command"], "pwd")
                self.assertEqual(len(harness.prompts), 1)
                self.assertIn("# Tool Use", harness.prompts[0])
            finally:
                harness.close()

    def test_google_agent_tools_default_on_for_legacy_null_config(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                'function_call\n{"name":"shell_command","args":{"command":"pwd"}}',
            ])
            previous = CONFIG.get("google_stream_auto_agent_tools")
            CONFIG["google_stream_auto_agent_tools"] = None
            try:
                response = harness.post("/v1beta/models/gemini-3.5-flash:generateContent", {
                    "systemInstruction": {"parts": [{"text": "You are Codex, an AI coding agent."}]},
                    "contents": [{"role": "user", "parts": [{"text": "检查当前项目"}]}],
                    "tools": GOOGLE_TOOLS,
                    "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
                })
                self.assertIn("functionCall", response["candidates"][0]["content"]["parts"][0])
            finally:
                CONFIG["google_stream_auto_agent_tools"] = previous
                harness.close()

    def test_google_stream_keeps_tools_for_converted_codex_and_copilot(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                'function_call\n{"name":"shell_command","args":{"command":"pwd"}}',
                'function_call\n{"name":"shell_command","args":{"command":"pwd"}}',
            ])
            try:
                for marker in ("You are Codex, an AI coding agent.", "You are GitHub Copilot coding agent."):
                    events = harness.post_sse(
                        "/v1beta/models/gemini-3.5-flash:streamGenerateContent?alt=sse",
                        {
                            "systemInstruction": {"parts": [{"text": marker}]},
                            "contents": [{"role": "user", "parts": [{
                                "text": "检查当前项目并执行必要操作",
                            }]}],
                            "tools": GOOGLE_TOOLS,
                            "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
                        },
                    )
                    data_events = [e["data"] for e in events if isinstance(e["data"], dict)]
                    function_call = data_events[0]["candidates"][0]["content"]["parts"][0]["functionCall"]
                    self.assertEqual(function_call["name"], "shell_command")
                self.assertEqual(len(harness.prompts), 2)
                self.assertTrue(all("# Tool Use" in prompt for prompt in harness.prompts))
            finally:
                harness.close()

    def test_google_function_response_reuses_stable_call_id(self):
        request = {
            "contents": [
                {"role": "model", "parts": [{"functionCall": {
                    "name": "shell_command", "args": {"command": "pwd"},
                }}]},
                {"role": "user", "parts": [{"functionResponse": {
                    "name": "shell_command", "response": {"output": "/workspace"},
                }}]},
            ],
        }
        messages = google_contents_to_messages(request)
        assistant = next(message for message in messages if message.get("role") == "assistant")
        tool = next(message for message in messages if message.get("role") == "tool")
        call_id = assistant["tool_calls"][0]["id"]
        self.assertTrue(call_id.startswith("call_g_"))
        self.assertEqual(tool["tool_call_id"], call_id)
        self.assertEqual(
            google_contents_to_messages(request)[0]["tool_calls"][0]["id"],
            call_id,
        )

    def test_google_stream_keeps_agent_title_request_tool_free(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, ["Mars page"], reuse_upstream_sessions=False)
            try:
                events = harness.post_sse(
                    "/v1beta/models/gemini-3.5-flash:streamGenerateContent?alt=sse",
                    {
                        "systemInstruction": {"parts": [{"text": (
                            "You are a Claude agent. You are coming up with a succinct title "
                            "for an agent chat session."
                        )}]},
                        "contents": [{"role": "user", "parts": [{"text": "创建火星网页"}]}],
                        "tools": GOOGLE_TOOLS,
                        "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
                    },
                )
                data_events = [e["data"] for e in events if isinstance(e["data"], dict)]
                self.assertEqual(
                    data_events[0]["candidates"][0]["content"]["parts"][0]["text"],
                    "Mars page",
                )
                self.assertNotIn("# Tool Use", harness.prompts[0])
            finally:
                harness.close()

    def test_google_agent_reuses_session_after_function_response(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                'function_call\n{"name":"shell_command","args":{"command":"pwd"}}',
                'function_call\n{"name":"shell_command","args":{"command":"ls"}}',
            ], reuse_upstream_agent_sessions=True)
            try:
                base_request = {
                    "systemInstruction": {"parts": [{"text": (
                        "x-anthropic-billing-header: enabled\n"
                        "You are a Claude agent working through the Claude Agent SDK."
                    )}]},
                    "tools": GOOGLE_TOOLS,
                    "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
                }
                first = harness.post("/v1beta/models/gemini-3.5-flash:generateContent", {
                    **base_request,
                    "contents": [{"role": "user", "parts": [{"text": "检查当前项目"}]}],
                })
                self.assertEqual(
                    first["candidates"][0]["content"]["parts"][0]["functionCall"]["name"],
                    "shell_command",
                )
                second = harness.post("/v1beta/models/gemini-3.5-flash:generateContent", {
                    **base_request,
                    "contents": [
                        {"role": "user", "parts": [{"text": "检查当前项目"}]},
                        {"role": "model", "parts": [{"functionCall": {
                            "name": "shell_command", "args": {"command": "pwd"},
                        }}]},
                        {"role": "user", "parts": [{"functionResponse": {
                            "name": "shell_command", "response": {"output": "/workspace"},
                        }}]},
                    ],
                })
                self.assertEqual(
                    second["candidates"][0]["content"]["parts"][0]["functionCall"]["args"]["command"],
                    "ls",
                )
                self.assertEqual(len(harness.prompts), 2)
                self.assertIn("Trusted agent-runtime tool result", harness.prompts[1])
                self.assertNotIn("Claude Agent SDK", harness.prompts[1])
                self.assertNotIn("Available tools", harness.prompts[1])
            finally:
                harness.close()

    def test_google_agent_falls_back_to_tool_instead_of_manual_code(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            manual_reply = "请在本地创建 index.html，然后手动复制下面的代码。"
            harness = HttpHarness(tmpdir, [manual_reply, manual_reply])
            try:
                response = harness.post("/v1beta/models/gemini-3.5-flash:generateContent", {
                    "systemInstruction": {"parts": [{"text": (
                        "x-anthropic-billing-header: enabled\n"
                        "You are a Claude agent working through the Claude Agent SDK."
                    )}]},
                    "contents": [{"role": "user", "parts": [{
                        "text": "创建一个可部署到 Cloudflare 的火星页面",
                    }]}],
                    "tools": GOOGLE_TOOLS,
                    "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
                })
                parts = response["candidates"][0]["content"]["parts"]
                self.assertEqual(parts[0]["functionCall"]["name"], "shell_command")
                self.assertEqual(parts[0]["functionCall"]["args"]["command"], "pwd; ls")
                self.assertEqual(len(harness.prompts), 2)
            finally:
                harness.close()

    def test_google_stream_reuses_same_upstream_conversation(self):
        class FakeWebStream:
            def __init__(self, text, state):
                self.text = text
                self._state = state
                self.state = None

            def __iter__(self):
                yield self.text
                self.state = self._state

        first_state = metadata_to_state(
            ["same-cid", "r1", "rc1", None, None, None, None, None, None, "ctx1"]
        )
        second_state = metadata_to_state(
            ["same-cid", "r2", "rc2", None, None, None, None, None, None, "ctx2"]
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [])
            try:
                with patch(
                    "gemini_web2api.webapi_backend.generate_stream_with_state",
                    side_effect=[
                        FakeWebStream("已记住123", first_state),
                        FakeWebStream("记得，是123", second_state),
                    ],
                ) as webapi_stream:
                    first = harness.post_sse(
                        "/v1beta/models/gemini-3.5-flash:streamGenerateContent?alt=sse",
                        {"contents": [{"role": "user", "parts": [{"text": "记住123"}]}]},
                    )
                    second = harness.post_sse(
                        "/v1beta/models/gemini-3.5-flash:streamGenerateContent?alt=sse",
                        {"contents": [
                            {"role": "user", "parts": [{"text": "记住123"}]},
                            {"role": "model", "parts": [{"text": "已记住123"}]},
                            {"role": "user", "parts": [{"text": "还记得什么？"}]},
                        ]},
                    )
                first_text = first[0]["data"]["candidates"][0]["content"]["parts"][0]["text"]
                second_text = second[0]["data"]["candidates"][0]["content"]["parts"][0]["text"]
                self.assertEqual(first_text, "已记住123")
                self.assertEqual(second_text, "记得，是123")
                self.assertEqual(webapi_stream.call_count, 2)
                self.assertEqual(webapi_stream.call_args_list[0].args[0], "记住123")
                self.assertIsNone(webapi_stream.call_args_list[0].args[2])
                self.assertEqual(webapi_stream.call_args_list[1].args[0], "还记得什么？")
                self.assertEqual(
                    webapi_stream.call_args_list[1].args[2]["conversation_id"],
                    "same-cid",
                )
            finally:
                harness.close()

    def test_google_api_trims_oversized_prompt_before_upstream(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, ["ok"])
            CONFIG["max_google_prompt_chars"] = 120
            try:
                response = harness.post("/v1beta/models/gemini-3.5-flash:generateContent", {
                    "contents": [
                        {"role": "user", "parts": [{"text": "old-" + ("x" * 500)}]},
                        {"role": "user", "parts": [{"text": "RECENT_QUESTION"}]},
                    ],
                })
                text = response["candidates"][0]["content"]["parts"][0]["text"]
                self.assertEqual(text, "ok")
                self.assertEqual(len(harness.prompts), 1)
                self.assertLess(len(harness.prompts[0]), 260)
                self.assertIn("Earlier Google native context omitted", harness.prompts[0])
                self.assertIn("RECENT_QUESTION", harness.prompts[0])
            finally:
                harness.close()

    def test_responses_streams_function_call_for_codex(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                "我会先检查项目。",
                '{"name":"shell_command","arguments":{"command":"Get-ChildItem"}}',
            ])
            try:
                events = harness.post_sse("/v1/responses", {
                    "model": "gemini-3.5-flash",
                    "input": "帮我排查这个项目",
                    "tools": TOOLS,
                    "stream": True,
                })
                event_names = [e["event"] for e in events]
                self.assertIn("response.function_call_arguments.done", event_names)
                done = next(e["data"] for e in events if e["event"] == "response.function_call_arguments.done")
                self.assertEqual(done["name"], "shell_command")
                self.assertIn("Get-ChildItem", done["arguments"])
            finally:
                harness.close()

    def test_responses_persists_history_get_and_truncates_tool_output(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                '{"name":"shell_command","arguments":{"command":"New-Item -ItemType File -Force -Path test.txt"}}',
                '{"name":"shell_command","arguments":{"command":"Get-Content test.txt"}}',
            ], reuse_upstream_agent_sessions=True)
            try:
                first = harness.post("/v1/responses", {
                    "model": "gemini-3.5-flash",
                    "instructions": "你是 Codex agent。",
                    "input": "创建 test.txt，然后读取确认",
                    "tools": TOOLS,
                })
                saved = harness.get(f"/v1/responses/{first['id']}")
                self.assertEqual(saved["id"], first["id"])
                self.assertEqual(saved["output"][0]["type"], "function_call")

                long_output = "A" * 200 + "TAIL"
                second = harness.post("/v1/responses", {
                    "model": "gemini-3.5-flash",
                    "previous_response_id": first["id"],
                    "input": [{
                        "type": "function_call_output",
                        "call_id": first["output"][0]["call_id"],
                        "output": long_output,
                    }],
                    "tools": TOOLS,
                })
                self.assertEqual(second["previous_response_id"], first["id"])
                self.assertEqual(second["output"][0]["type"], "function_call")
                self.assertNotIn("创建 test.txt", harness.prompts[1])
                self.assertIn("New-Item", harness.prompts[1])
                self.assertIn('"tool":"shell_command"', harness.prompts[1])
                self.assertNotIn("Agent mode", harness.prompts[1])
                self.assertNotIn("Available tools", harness.prompts[1])
                self.assertIn("truncated", harness.prompts[1])
                self.assertIn("TAIL", harness.prompts[1])
            finally:
                harness.close()

    def test_responses_allows_final_text_after_tool_output(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                '{"name":"shell_command","arguments":{"command":"pwd; ls"}}',
                "Done. I updated the files and verified the output.",
            ])
            try:
                first = harness.post("/v1/responses", {
                    "model": "gemini-3.5-flash",
                    "input": "inspect and fix the project",
                    "tools": TOOLS,
                })
                second = harness.post("/v1/responses", {
                    "model": "gemini-3.5-flash",
                    "previous_response_id": first["id"],
                    "input": [{
                        "type": "function_call_output",
                        "call_id": first["output"][0]["call_id"],
                        "output": "file list",
                    }],
                    "tools": TOOLS,
                })
                self.assertEqual(second["output"][0]["type"], "message")
                self.assertIn("updated the files", second["output"][0]["content"][0]["text"])
                self.assertEqual(len(harness.prompts), 2)
            finally:
                harness.close()

    def test_codex_completes_three_step_loop_without_upstream_reuse(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                '{"name":"shell_command","arguments":{"command":"pwd"}}',
                '{"name":"shell_command","arguments":{"command":"ls"}}',
                "Done after two tool calls.",
            ], reuse_upstream_sessions=False)
            try:
                first = harness.post("/v1/responses", {
                    "model": "gemini-3.5-flash", "input": "inspect the workspace and finish the task", "tools": TOOLS,
                })
                second = harness.post("/v1/responses", {
                    "model": "gemini-3.5-flash",
                    "previous_response_id": first["id"],
                    "input": [{"type": "function_call_output", "call_id": first["output"][0]["call_id"],
                               "output": "/workspace"}],
                    "tools": TOOLS,
                })
                third = harness.post("/v1/responses", {
                    "model": "gemini-3.5-flash",
                    "previous_response_id": second["id"],
                    "input": [{"type": "function_call_output", "call_id": second["output"][0]["call_id"],
                               "output": "README.md"}],
                    "tools": TOOLS,
                })
                self.assertEqual(first["output"][0]["type"], "function_call")
                self.assertEqual(second["output"][0]["type"], "function_call")
                self.assertEqual(third["output"][0]["type"], "message")
                self.assertEqual(sum("Agent mode" in prompt for prompt in harness.prompts), 1)
                self.assertTrue(all("Available tools" in prompt for prompt in harness.prompts))
            finally:
                harness.close()

    def test_responses_strips_echoed_tool_result_from_final_text(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                '{"name":"shell_command","arguments":{"command":"pwd; ls"}}',
                "[Tool result for call_abc]: Exit code: 0\nWall time: 1.2 seconds\nOutput:\nsecret output\n\nAll done. Files were updated.",
            ])
            try:
                first = harness.post("/v1/responses", {
                    "model": "gemini-3.5-flash",
                    "input": "inspect and fix the project",
                    "tools": TOOLS,
                })
                second = harness.post("/v1/responses", {
                    "model": "gemini-3.5-flash",
                    "previous_response_id": first["id"],
                    "input": [{
                        "type": "function_call_output",
                        "call_id": first["output"][0]["call_id"],
                        "output": "secret output",
                    }],
                    "tools": TOOLS,
                })
                text = second["output"][0]["content"][0]["text"]
                self.assertEqual(second["output"][0]["type"], "message")
                self.assertNotIn("[Tool result", text)
                self.assertNotIn("secret output", text)
                self.assertIn("All done", text)
            finally:
                harness.close()

    def test_anthropic_retries_text_into_tool_use_for_claude_code(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                "我会先检查当前目录。",
                '{"name":"shell_command","arguments":{"command":"Get-ChildItem"}}',
            ])
            try:
                response = harness.post("/v1/messages", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "帮我看下当前目录"}],
                    "tools": [{
                        "name": "shell_command",
                        "description": "Run a shell command",
                        "input_schema": TOOLS[0]["parameters"],
                    }],
                })
                self.assertEqual(response["content"][0]["type"], "tool_use")
                self.assertEqual(response["content"][0]["name"], "shell_command")
                self.assertEqual(response["stop_reason"], "tool_use")
                self.assertEqual(len(harness.prompts), 2)
            finally:
                harness.close()

    def test_anthropic_streams_tool_use_for_claude_code(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                "我会先检查当前目录。",
                '{"name":"shell_command","arguments":{"command":"Get-ChildItem"}}',
            ])
            try:
                events = harness.post_sse("/v1/messages", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "帮我看下当前目录"}],
                    "tools": [{
                        "name": "shell_command",
                        "description": "Run a shell command",
                        "input_schema": TOOLS[0]["parameters"],
                    }],
                    "stream": True,
                })
                event_names = [e["event"] for e in events]
                self.assertIn("content_block_start", event_names)
                block = next(e["data"]["content_block"] for e in events if e["event"] == "content_block_start")
                self.assertEqual(block["type"], "tool_use")
                self.assertEqual(block["name"], "shell_command")
                message_delta = next(e["data"] for e in events if e["event"] == "message_delta")
                self.assertEqual(message_delta["delta"]["stop_reason"], "tool_use")
            finally:
                harness.close()

    def test_anthropic_reuses_upstream_agent_session(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                '{"name":"shell_command","arguments":{"command":"pwd"}}',
                "Done after reading the tool result.",
            ], reuse_upstream_agent_sessions=True)
            try:
                first = harness.post("/v1/messages", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "run pwd"}],
                    "tools": [{
                        "name": "shell_command",
                        "description": "Run a shell command",
                        "input_schema": TOOLS[0]["parameters"],
                    }],
                })
                tool_use = first["content"][0]
                second = harness.post("/v1/messages", {
                    "model": "gemini-3.5-flash",
                    "messages": [
                        {"role": "user", "content": "run pwd"},
                        {"role": "assistant", "content": [tool_use]},
                        {"role": "user", "content": [{
                            "type": "tool_result",
                            "tool_use_id": tool_use["id"],
                            "content": "/workspace",
                        }]},
                    ],
                    "tools": [{
                        "name": "shell_command",
                        "description": "Run a shell command",
                        "input_schema": TOOLS[0]["parameters"],
                    }],
                })
                self.assertEqual(second["stop_reason"], "end_turn")
                self.assertEqual(len(harness.prompts), 2)
                self.assertIn("/workspace", harness.prompts[1])
                self.assertNotIn("Agent mode", harness.prompts[1])
                self.assertNotIn("Available tools", harness.prompts[1])
            finally:
                harness.close()

    def test_claude_code_completes_three_step_loop_without_upstream_reuse(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                '{"name":"shell_command","arguments":{"command":"pwd"}}',
                '{"name":"shell_command","arguments":{"command":"ls"}}',
                "Done after two tool calls.",
            ], reuse_upstream_sessions=False)
            tools = [{
                "name": "shell_command",
                "description": "Run a shell command",
                "input_schema": TOOLS[0]["parameters"],
            }]
            try:
                messages = [{"role": "user", "content": "inspect the workspace and finish the task"}]
                first = harness.post("/v1/messages", {
                    "model": "gemini-3.5-flash", "messages": messages, "tools": tools,
                })
                first_tool = first["content"][0]
                messages.extend([
                    {"role": "assistant", "content": [first_tool]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": first_tool["id"],
                                                    "content": "/workspace"}]},
                ])
                second = harness.post("/v1/messages", {
                    "model": "gemini-3.5-flash", "messages": messages, "tools": tools,
                })
                second_tool = second["content"][0]
                messages.extend([
                    {"role": "assistant", "content": [second_tool]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": second_tool["id"],
                                                    "content": "README.md"}]},
                ])
                third = harness.post("/v1/messages", {
                    "model": "gemini-3.5-flash", "messages": messages, "tools": tools,
                })
                self.assertEqual(first["stop_reason"], "tool_use")
                self.assertEqual(second["stop_reason"], "tool_use")
                self.assertEqual(third["stop_reason"], "end_turn")
                self.assertEqual(sum("Agent mode" in prompt for prompt in harness.prompts), 1)
                self.assertTrue(all("Available tools" in prompt for prompt in harness.prompts))
            finally:
                harness.close()

    def test_anthropic_preserves_thinking_and_retries_to_tool_use(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [
                "I should run a command.",
                '{"name":"shell_command","arguments":{"command":"Get-ChildItem"}}',
            ])
            try:
                response = harness.post("/v1/messages", {
                    "model": "gemini-3.5-flash",
                    "messages": [
                        {"role": "user", "content": "列出当前目录"},
                        {"role": "assistant", "content": [
                            {"type": "thinking", "thinking": "Need to inspect cwd."},
                            {"type": "tool_use", "id": "toolu_1", "name": "shell_command", "input": {"command": "pwd"}},
                        ]},
                        {"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "B" * 200 + "DONE"}
                        ]},
                    ],
                    "tools": [{
                        "name": "shell_command",
                        "description": "Run a shell command",
                        "input_schema": TOOLS[0]["parameters"],
                    }],
                })
                self.assertEqual(response["content"][0]["type"], "tool_use")
                self.assertEqual(response["stop_reason"], "tool_use")
                self.assertIn("Previous assistant thinking", harness.prompts[0])
                self.assertIn("truncated", harness.prompts[0])
                self.assertIn("previous answer", harness.prompts[1])
            finally:
                harness.close()


if __name__ == "__main__":
    unittest.main()
