import importlib.util
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

import gemini_web2api.server as server
from gemini_web2api.agent import ResponseStore
from gemini_web2api.config import CONFIG


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


def load_single_file_module():
    module_path = Path(__file__).resolve().parents[1] / "gemini_web2api.py"
    spec = importlib.util.spec_from_file_location("gemini_web2api_single", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HttpHarness:
    def __init__(self, tmpdir, responses):
        self.prompts = []
        self._responses = iter(responses)
        self._original_generate = server.generate
        server.generate = self._fake_generate
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
            "tool_retry_attempts": 1,
        })
        self.httpd = server.ThreadedServer(("127.0.0.1", 0), server.GeminiHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def _fake_generate(self, prompt, *args, **kwargs):
        self.prompts.append(prompt)
        return next(self._responses)

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
            ])
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
                self.assertIn("创建 test.txt", harness.prompts[1])
                self.assertIn("New-Item", harness.prompts[1])
                self.assertIn("truncated", harness.prompts[1])
                self.assertIn("TAIL", harness.prompts[1])
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
