import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from unittest import mock

import app


class _OpenClawHandler(BaseHTTPRequestHandler):
    request_data = None
    request_auth = None

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        type(self).request_data = json.loads(self.rfile.read(size).decode("utf-8"))
        type(self).request_auth = self.headers.get("Authorization")
        body = json.dumps({
            "choices": [{"message": {"content": "龙虾回答"}}]
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


class _PrivatePageHandler(BaseHTTPRequestHandler):
    requests = 0

    def do_GET(self):
        type(self).requests += 1
        body = ("<html><body>PRIVATE " + ("内部资料" * 80) + "</body></html>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


class _RedirectHandler(BaseHTTPRequestHandler):
    target = ""

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", type(self).target)
        self.end_headers()

    def log_message(self, _format, *_args):
        pass


class _LargePageHandler(BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        try:
            self.wfile.write(type(self).body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, _format, *_args):
        pass


class _ConcurrentOpenClawHandler(BaseHTTPRequestHandler):
    active = 0
    max_active = 0
    lock = threading.Lock()

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(size)
        with type(self).lock:
            type(self).active += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
        time.sleep(0.15)
        with type(self).lock:
            type(self).active -= 1
        body = b'{"choices":[{"message":{"content":"ok"}}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


class OpenClawTests(unittest.TestCase):
    def test_same_session_requests_are_serialized(self):
        _ConcurrentOpenClawHandler.active = 0
        _ConcurrentOpenClawHandler.max_active = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ConcurrentOpenClawHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cfg = {
            "base_url": "http://127.0.0.1:{}/v1".format(server.server_port),
            "api_key": "gateway-token",
            "model": "openclaw:wxbot",
        }
        errors = []

        def call():
            try:
                app.openclaw_chat("问题", session_id="same-user", cfg=cfg, timeout=2)
            except Exception as e:
                errors.append(e)

        workers = [threading.Thread(target=call) for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertFalse(errors)
        self.assertEqual(_ConcurrentOpenClawHandler.max_active, 1)

    def test_page_fetch_rejects_untrusted_public_domains(self):
        public_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with mock.patch.object(app.socket, "getaddrinfo", return_value=public_dns):
            with self.assertRaises(ValueError):
                app._validate_fetch_url("https://attacker.example/article")

    def test_openclaw_runtime_errors_return_safe_message(self):
        with mock.patch.object(app, "is_allowed", return_value=True), \
             mock.patch.object(app, "ai_answer", side_effect=RuntimeError("secret provider detail")):
            routed = app.dispatch_route(
                "chat_answer", {"question": "问题"}, "问题", "wx-user", "用户", "", ""
            )
            command = app.handle_command(
                "/ai 问题", "wx-user", "用户", "", "", app.load_config()
            )
        self.assertEqual(routed, "🤖 " + app.AI_FAILURE_MSG)
        self.assertEqual(command, "⚠️ " + app.AI_FAILURE_MSG)
        self.assertNotIn("secret provider detail", routed + command)

    def test_public_config_does_not_expose_provider_tokens(self):
        public = getattr(app, "public_config", None)
        self.assertTrue(callable(public), "public_config 尚未实现")
        result = public({
            "auto_reply": True,
            "smart": True,
            "ai": {"base_url": "https://ai.example/v1", "api_key": "ai-secret", "model": "m"},
            "openclaw": {"enabled": True, "base_url": "http://127.0.0.1:18788/v1", "api_key": "claw-secret", "model": "openclaw:wxbot"},
        })
        self.assertEqual(result["auto_reply"], True)
        self.assertEqual(result["openclaw"], {"enabled": True, "configured": True})
        self.assertNotIn("api_key", json.dumps(result))

    def test_page_fetch_rejects_private_network_targets(self):
        _PrivatePageHandler.requests = 0
        server = HTTPServer(("127.0.0.1", 0), _PrivatePageHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            text = app._fetch_page_text(
                "http://127.0.0.1:{}/secret".format(server.server_port),
                timeout=2,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(text, "")
        self.assertEqual(_PrivatePageHandler.requests, 0)

    def test_page_fetch_revalidates_redirect_targets(self):
        _PrivatePageHandler.requests = 0
        target = HTTPServer(("127.0.0.1", 0), _PrivatePageHandler)
        redirect = HTTPServer(("127.0.0.1", 0), _RedirectHandler)
        _RedirectHandler.target = "http://127.0.0.1:{}/secret".format(target.server_port)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        target_thread.start()
        redirect_thread.start()
        original_validate = app._validate_fetch_url
        calls = []

        def validate(url):
            calls.append(url)
            if len(calls) == 1:
                return None
            return original_validate(url)

        try:
            with mock.patch.object(app, "_validate_fetch_url", side_effect=validate):
                app._fetch_page_text(
                    "http://127.0.0.1:{}/redirect".format(redirect.server_port),
                    timeout=2,
                )
        finally:
            redirect.shutdown()
            target.shutdown()
            redirect.server_close()
            target.server_close()
            redirect_thread.join(timeout=2)
            target_thread.join(timeout=2)

        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(_PrivatePageHandler.requests, 0)

    def test_page_fetch_rejects_oversized_responses(self):
        limit = getattr(app, "MAX_PAGE_BYTES", 1000000)
        _LargePageHandler.body = b"x" * (limit + 1)
        server = HTTPServer(("127.0.0.1", 0), _LargePageHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(app, "_validate_fetch_url", return_value=None):
                text = app._fetch_page_text(
                    "http://127.0.0.1:{}/large".format(server.server_port),
                    timeout=2,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(text, "")

    def test_openclaw_chat_sends_stable_user_and_returns_content(self):
        server = HTTPServer(("127.0.0.1", 0), _OpenClawHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cfg = {
            "base_url": "http://127.0.0.1:{}/v1".format(server.server_port),
            "api_key": "gateway-token",
            "model": "openclaw:wxbot",
        }
        try:
            chat = getattr(app, "openclaw_chat", None)
            self.assertTrue(callable(chat), "openclaw_chat 尚未实现")
            reply = chat("你好", session_id="wx-user-1", cfg=cfg, timeout=2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(reply, "龙虾回答")
        self.assertEqual(_OpenClawHandler.request_auth, "Bearer gateway-token")
        self.assertEqual(_OpenClawHandler.request_data["model"], "openclaw:wxbot")
        self.assertEqual(_OpenClawHandler.request_data["user"], "wx-user-1")
        self.assertEqual(
            _OpenClawHandler.request_data["messages"],
            [{"role": "user", "content": "你好"}],
        )

    def test_ai_answer_falls_back_when_openclaw_is_not_configured(self):
        answer = getattr(app, "ai_answer", None)
        self.assertTrue(callable(answer), "ai_answer 尚未实现")
        with mock.patch.object(app, "openclaw_config", return_value={}), \
             mock.patch.object(app, "ai_chat", return_value="原 AI 回答") as direct:
            self.assertEqual(answer("问题", session_id="wx-user-2"), "原 AI 回答")
        direct.assert_called_once_with("问题")


if __name__ == "__main__":
    unittest.main()
