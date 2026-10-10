import http.client
import importlib.machinery
import importlib.util
import io
import json
import socket
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "xgroxy"
loader = importlib.machinery.SourceFileLoader("xgroxy_module", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
xgroxy = importlib.util.module_from_spec(spec)
loader.exec_module(xgroxy)


def auth_entry(key="access-token"):
    return {
        "key": key,
        "refresh_token": "refresh-token",
        "expires_at": "2999-01-01T00:00:00.000Z",
        "email": "test@example.com",
    }


class AuthTests(unittest.TestCase):
    def test_shared_auth_file_is_selected_and_updated_without_data_loss(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "auth.json"
            other = {"key": "other-token", "custom": True}
            original = {
                "another-client": other,
                xgroxy.CLIENT_ID_SCOPE_KEY: auth_entry(),
            }
            path.write_text(json.dumps(original), encoding="utf-8")

            entry = xgroxy.read_auth(str(path))
            self.assertEqual(entry["key"], "access-token")
            entry["key"] = "new-token"
            xgroxy.write_auth(entry, str(path))

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["another-client"], other)
            self.assertEqual(saved[xgroxy.CLIENT_ID_SCOPE_KEY]["key"], "new-token")
            self.assertNotIn("_auth_path", saved[xgroxy.CLIENT_ID_SCOPE_KEY])
            self.assertIn("_auth_path", entry, "write_auth must not mutate its caller")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_refresh_reuses_token_rotated_by_another_request(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "auth.json"
            stale = auth_entry("old-token")
            stale["_auth_path"] = str(path)
            current = auth_entry("new-token")
            xgroxy.write_auth(current, str(path))

            with mock.patch.object(xgroxy, "_http_form_post") as post:
                result = xgroxy.refresh_token(stale, force=True)

            self.assertEqual(result["key"], "new-token")
            post.assert_not_called()


class TransformationTests(unittest.TestCase):
    def test_configured_default_model_is_forwarded_and_messages_pass_through(self):
        result = xgroxy._forward_fields({
            "messages": [{"role": "assistant", "content": "ok", "reasoning_content": "chain"}],
            "ignored": "value",
        }, "configured-model")
        self.assertEqual(result["model"], "configured-model")
        # reasoning_content replay is preserved: xAI accepts it (2026-10).
        self.assertEqual(result["messages"][0]["reasoning_content"], "chain")
        self.assertNotIn("ignored", result)

    def test_stream_chunk_drops_empty_tool_calls_array(self):
        # xAI attaches "tool_calls": [] to reasoning deltas; forwarding it
        # fragments reasoning streams in OpenAI-compatible clients and leads
        # to "reasoning part ... not found" errors downstream.
        encoded = xgroxy._rewrite_sse_chunk({
            "id": "chunk-1",
            "model": "model-1",
            "choices": [
                {"index": 0, "delta": {"reasoning_content": "thinking", "tool_calls": []},
                 "finish_reason": None},
            ],
        }, False)
        payload = json.loads(encoded.decode()[len("data: "):].strip())
        self.assertNotIn("tool_calls", payload["choices"][0]["delta"])
        self.assertEqual(payload["choices"][0]["delta"]["reasoning_content"], "thinking")

    def test_stream_chunk_keeps_populated_tool_calls(self):
        encoded = xgroxy._rewrite_sse_chunk({
            "id": "chunk-2",
            "model": "model-1",
            "choices": [
                {"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1",
                 "function": {"name": "read_file", "arguments": "{}"}}]},
                 "finish_reason": None},
            ],
        }, False)
        payload = json.loads(encoded.decode()[len("data: "):].strip())
        delta = payload["choices"][0]["delta"]
        self.assertEqual(len(delta["tool_calls"]), 1)
        self.assertEqual(delta["tool_calls"][0]["function"]["name"], "read_file")

    def test_stream_chunk_drops_null_reasoning_field(self):
        encoded = xgroxy._rewrite_sse_chunk({
            "id": "chunk-3",
            "model": "model-1",
            "choices": [
                {"index": 0, "delta": {"reasoning_content": None, "content": "hi"},
                 "finish_reason": None},
            ],
        }, False)
        payload = json.loads(encoded.decode()[len("data: "):].strip())
        self.assertNotIn("reasoning_content", payload["choices"][0]["delta"])
        self.assertEqual(payload["choices"][0]["delta"]["content"], "hi")

    def test_common_openai_and_xai_options_are_forwarded(self):
        body = {
            "messages": [],
            "parallel_tool_calls": False,
            "n": 2,
            "logprobs": True,
            "top_logprobs": 3,
            "service_tier": "default",
            "reasoning_effort": "high",
        }
        result = xgroxy._forward_fields(body)
        for field in body:
            self.assertEqual(result[field], body[field])

    def test_stream_chunk_preserves_role_and_all_choices(self):
        encoded = xgroxy._rewrite_sse_chunk({
            "id": "chunk-1",
            "model": "model-1",
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": "a"},
                 "finish_reason": None},
                {"index": 1, "delta": {"content": "b"}, "finish_reason": "stop"},
            ],
        }, False)
        payload = json.loads(encoded.decode()[len("data: "):].strip())
        self.assertEqual(payload["choices"][0]["delta"]["role"], "assistant")
        self.assertEqual(payload["choices"][1]["index"], 1)

    def test_usage_only_stream_chunk_is_not_dropped(self):
        encoded = xgroxy._rewrite_sse_chunk({
            "id": "chunk-2", "choices": [], "usage": {"total_tokens": 7},
        }, True)
        payload = json.loads(encoded.decode()[len("data: "):].strip())
        self.assertEqual(payload["choices"], [])
        self.assertEqual(payload["usage"]["total_tokens"], 7)

    def test_stream_relay_accepts_data_without_space_and_finishes(self):
        handler = object.__new__(xgroxy._Handler)
        handler.wfile = io.BytesIO()
        response = iter([
            b'data:{"choices":[{"delta":{"content":"hi"},"index":0}]}\n',
            b"\n",
            b"data: [DONE]\n",
        ])
        handler._relay_stream(response, False)
        output = handler.wfile.getvalue()
        self.assertIn(b'"content": "hi"', output)
        self.assertTrue(output.endswith(b"data: [DONE]\n\n"))


class ServiceTests(unittest.TestCase):
    def test_systemd_unit_escapes_special_arguments(self):
        unit = xgroxy._build_unit_text(
            Path('/tmp/x groxy%/x"groxy'),
            "127.0.0.1",
            8788,
            'key with $ and "quotes"',
            "model%name",
            "/tmp/auth file.json",
        )
        exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        self.assertIn("x groxy%%", exec_line)
        self.assertIn("$$", exec_line)
        self.assertIn('\\"quotes\\"', exec_line)
        self.assertNotIn("Environment=", unit)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not installed")
    def test_generated_systemd_unit_passes_systemd_validation(self):
        unit = xgroxy._build_unit_text(
            SCRIPT, "127.0.0.1", 8788, None, "grok-4.6", "/tmp/auth.json")
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "xgroxy.service"
            path.write_text(unit, encoding="utf-8")
            result = subprocess.run(
                ["systemd-analyze", "verify", str(path)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.auth_path = Path(self.tmp.name) / "auth.json"
        xgroxy.write_auth(auth_entry(), str(self.auth_path))

        class Handler(xgroxy._Handler):
            api_key = None
            auth_file_path = str(self.auth_path)
            default_model = "configured-model"

        self.handler_class = Handler
        self.server = xgroxy._ThreadedServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), data

    def test_non_object_json_returns_400_instead_of_crashing_handler(self):
        status, headers, data = self.request(
            "POST", "/v1/chat/completions", "[]", {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertEqual(int(headers["Content-Length"]), len(data))
        self.assertIn(b"JSON object", data)

    def test_missing_messages_returns_clear_400(self):
        status, _, data = self.request(
            "POST", "/v1/chat/completions", "{}", {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertIn(b"messages", data)

    def test_query_string_routes_and_nonstream_uses_configured_model(self):
        captured = {}

        def upstream(body, token):
            captured.update(body)
            return 200, {"choices": []}, {}

        with mock.patch.object(xgroxy, "_upstream_request", side_effect=upstream):
            status, _, _ = self.request(
                "POST", "/v1/chat/completions?trace=1",
                json.dumps({"messages": [{"role": "user", "content": "hi"}]}),
                {"Content-Type": "application/json"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(captured["model"], "configured-model")

    def test_stream_connection_failure_is_a_502_not_a_false_200(self):
        with mock.patch.object(
            self.handler_class,
            "_open_upstream_stream",
            side_effect=urllib.error.URLError("offline"),
        ):
            status, _, data = self.request(
                "POST", "/v1/chat/completions",
                json.dumps({"messages": [], "stream": True}),
                {"Content-Type": "application/json"},
            )
        self.assertEqual(status, 502)
        self.assertIn(b"offline", data)

    def test_successful_stream_is_framed_by_connection_close(self):
        upstream = io.BytesIO(
            b'data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        with mock.patch.object(
            self.handler_class, "_open_upstream_stream", return_value=upstream,
        ):
            status, headers, data = self.request(
                "POST", "/v1/chat/completions",
                json.dumps({"messages": [], "stream": True}),
                {"Content-Type": "application/json"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Connection"], "close")
        self.assertTrue(data.endswith(b"data: [DONE]\n\n"))

    def test_health_requires_configured_api_key(self):
        self.handler_class.api_key = "secret"
        status, _, _ = self.request("GET", "/health")
        self.assertEqual(status, 401)
        status, _, data = self.request(
            "GET", "/health", headers={"Authorization": "Bearer secret"})
        self.assertEqual(status, 200)
        self.assertIn(b'test@example.com', data)

    def test_models_reports_real_context_limits(self):
        # Agents that read /v1/models must see the true window, not guess.
        self.handler_class.default_model = "grok-4.6"
        status, _, data = self.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        entry = json.loads(data)["data"][0]
        self.assertEqual(entry["id"], "grok-4.6")
        self.assertEqual(entry["context_window"], 500000)
        self.assertEqual(entry["max_output_tokens"], 500000)
        self.assertEqual(entry["limit"],
                         {"context": 500000, "output": 500000})

    def test_models_omits_limits_for_unknown_model(self):
        self.handler_class.default_model = "some-future-model"
        status, _, data = self.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        entry = json.loads(data)["data"][0]
        self.assertEqual(entry["id"], "some-future-model")
        self.assertNotIn("limit", entry)


class ModelLimitTests(unittest.TestCase):
    def test_grok46_advertises_the_published_500k_window(self):
        # xAI docs + models.dev both list 500,000 context for grok-4.6.
        # If this ever fails, someone changed the table without checking
        # https://docs.x.ai/developers/models — clients silently fall back to
        # a 128k-style default when limits are wrong or missing.
        self.assertEqual(xgroxy.MODEL_LIMITS["grok-4.6"],
                         {"context": 500000, "output": 500000})
        self.assertEqual(xgroxy.MODEL_LIMITS["grok-4.5"],
                         {"context": 500000, "output": 500000})
        for name, limits in xgroxy.MODEL_LIMITS.items():
            self.assertGreater(limits["context"], 0, name)
            self.assertGreater(limits["output"], 0, name)


class ProcessTests(unittest.TestCase):
    def test_sigterm_stops_server_without_deadlock(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            auth_path = Path(tmp_dir) / "auth.json"
            xgroxy.write_auth(auth_entry(), str(auth_path))
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]

            process = subprocess.Popen(
                [sys.executable, str(SCRIPT), "serve", "--port", str(port),
                 "--auth-file", str(auth_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                first_line = process.stdout.readline()
                self.assertIn("xgroxy", first_line)
                process.terminate()
                process.communicate(timeout=3)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            self.assertEqual(process.returncode, 0)


if __name__ == "__main__":
    unittest.main()
