import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import gemini_web2api.server as server
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

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
            return json.loads(resp.read().decode())

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.generate = self._original_generate
        server.RESPONSE_STORE = None


class AgentCompatTests(unittest.TestCase):
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
