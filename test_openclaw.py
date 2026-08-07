import json
import os
import threading
import tempfile
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
            "choices": [{"message": {"content": "OpenClaw 回答"}}]
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
    def test_local_outbound_messages_are_cleaned(self):
        class _Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"success":true}'

        with mock.patch.object(app, "bot_token", return_value="token"), \
             mock.patch.object(app.urllib.request, "urlopen", return_value=_Response()) as urlopen:
            ok, _ = app.bot_send("wx-user", "**提醒** ✅", False)

        self.assertTrue(ok)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["data"]["content"], "提醒")

    def test_outbox_messages_are_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox = os.path.join(tmp, "outbox.jsonl")
            with mock.patch.object(app, "OUTBOX_FILE", outbox):
                app.outbox_push({"id": "reminder-1", "text": "**喝水** 🤖"})
            with open(outbox, "r", encoding="utf-8") as handle:
                item = json.loads(handle.readline())

        self.assertEqual(item["text"], "喝水")

    def test_outbox_push_waits_for_atomic_done_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox = os.path.join(tmp, "outbox.jsonl")
            with mock.patch.object(app, "OUTBOX_FILE", outbox):
                app.outbox_push({"id": "old", "text": "旧消息"})
                entered_replace = threading.Event()
                release_replace = threading.Event()
                real_replace = app.os.replace

                def delayed_replace(src, dst):
                    entered_replace.set()
                    release_replace.wait(timeout=2)
                    real_replace(src, dst)

                with mock.patch.object(app.os, "replace", side_effect=delayed_replace):
                    done = threading.Thread(target=app.outbox_done, args=({"old"},))
                    push = threading.Thread(
                        target=app.outbox_push,
                        args=({"id": "new", "text": "新消息"},),
                    )
                    done.start()
                    self.assertTrue(entered_replace.wait(timeout=1))
                    push.start()
                    time.sleep(0.05)
                    self.assertTrue(push.is_alive())
                    release_replace.set()
                    done.join(timeout=2)
                    push.join(timeout=2)

                self.assertFalse(done.is_alive())
                self.assertFalse(push.is_alive())
                self.assertEqual([item["id"] for item in app.outbox_pending()], ["new"])

    def test_wechat_reply_removes_markdown_decoration_and_emoji(self):
        clean = getattr(app, "clean_wechat_reply", None)
        self.assertTrue(callable(clean), "clean_wechat_reply 尚未实现")
        self.assertEqual(
            clean("**最新活动**\n- `免费领取`\n🤖 ✅"),
            "最新活动\n免费领取",
        )

    def test_wechat_reply_removes_internal_tool_trace(self):
        self.assertEqual(
            app.clean_wechat_reply(
                "我来查一下。\n"
                "to=exec code\n"
                '{"command":"date"}\n'
                "total_languages=1\n"
                "to=browser code\n"
                '{"action":"search","query":"柯洁"}\n'
                "最终没有查到可靠结果。"
            ),
            "我来查一下。\n最终没有查到可靠结果。",
        )

    def test_wechat_reply_removes_trailing_trace_links_and_tables(self):
        self.assertEqual(
            app.clean_wechat_reply(
                "最终回答\n"
                "to=exec code\n"
                "[官方文档](https://example.com)\n"
                "| 名称 | 值 |\n"
                "| --- | --- |\n"
                "| 状态 | 正常 |"
            ),
            "最终回答\n官方文档\n名称 值\n状态 正常",
        )

    def test_group_sender_name_is_cleaned_with_reply(self):
        self.assertEqual(
            app.wechat_outbound_text("**回答**", "😀**用户**", True),
            "@用户 回答",
        )

    def test_automation_route_exposes_only_reminder_and_daily_push(self):
        self.assertEqual(
            app.AUTOMATION_ACTIONS,
            {"set_reminder", "set_daily_push"},
        )
        self.assertNotIn("web_search", app.OPENCLAW_ROUTE_PROMPT)

    def test_legacy_web_search_and_ai_router_are_removed(self):
        for name in (
            "web_search",
            "web_search_ai",
            "AI_TOOLS",
            "ai_tool_call",
            "ROUTE_TOOLS",
            "ai_route",
        ):
            self.assertFalse(hasattr(app, name), name + " 仍然存在")

    def test_openclaw_route_parses_reminder_action(self):
        with mock.patch.object(
            app,
            "openclaw_chat",
            return_value='{"action":"set_reminder","time":"10分钟后","content":"喝水"}',
        ) as chat:
            tool, args = app.openclaw_route("提醒我10分钟后喝水", "wx-user")

        self.assertEqual(tool, "set_reminder")
        self.assertEqual(args, {"time": "10分钟后", "content": "喝水"})
        chat.assert_called_once_with(
            "提醒我10分钟后喝水",
            session_id="route:wx-user",
            system_prompt=app.OPENCLAW_ROUTE_PROMPT,
            sanitize=False,
        )

    def test_openclaw_route_keeps_action_json_before_user_text_cleaning(self):
        with mock.patch.object(
            app,
            "openclaw_chat",
            return_value='to=exec code\n{"action":"set_reminder","time":"10分钟后","content":"喝水"}',
        ):
            tool, args = app.openclaw_route("提醒我10分钟后喝水", "wx-user")

        self.assertEqual(tool, "set_reminder")
        self.assertEqual(args["content"], "喝水")

    def test_search_question_goes_directly_to_openclaw(self):
        question = "柯洁最近在干嘛"
        with mock.patch.object(app, "is_allowed", return_value=True), \
             mock.patch.object(app, "openclaw_route", side_effect=AssertionError("不应进入工具路由")), \
             mock.patch.object(app, "ai_answer", return_value="OpenClaw 回答") as answer:
            reply = app.smart_fallback(question, "wx-user", "用户", "", "", {"smart": True})

        self.assertEqual(reply, "OpenClaw 回答")
        answer.assert_called_once_with(question, session_id="wx-user")

    def test_reminder_is_classified_by_openclaw_then_dispatched(self):
        with mock.patch.object(app, "is_allowed", return_value=True), \
             mock.patch.object(
                 app,
                 "openclaw_route",
                 return_value=("set_reminder", {"time": "10分钟后", "content": "喝水"}),
             ) as route, \
             mock.patch.object(app, "dispatch_route", return_value="提醒已设置") as dispatch:
            reply = app.smart_fallback(
                "提醒我10分钟后喝水", "wx-user", "用户", "room-1", "群", {"smart": True}
            )

        self.assertEqual(reply, "提醒已设置")
        route.assert_called_once_with("提醒我10分钟后喝水", "wx-user")
        dispatch.assert_called_once_with(
            "set_reminder",
            {"time": "10分钟后", "content": "喝水"},
            "提醒我10分钟后喝水",
            "wx-user",
            "用户",
            "room-1",
            "群",
            rounds=1,
        )

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

    def test_openclaw_runtime_errors_return_safe_message(self):
        with mock.patch.object(app, "is_allowed", return_value=True), \
             mock.patch.object(app, "ai_answer", side_effect=RuntimeError("secret provider detail")):
            routed = app.dispatch_route(
                "chat_answer", {"question": "问题"}, "问题", "wx-user", "用户", "", ""
            )
            command = app.handle_command(
                "/ai 问题", "wx-user", "用户", "", "", app.load_config()
            )
        self.assertEqual(routed, app.AI_FAILURE_MSG)
        self.assertEqual(command, app.AI_FAILURE_MSG)
        self.assertNotIn("secret provider detail", routed + command)

    def test_public_config_does_not_expose_provider_tokens(self):
        public = getattr(app, "public_config", None)
        self.assertTrue(callable(public), "public_config 尚未实现")
        result = public({
            "auto_reply": True,
            "smart": True,
            "ai": {"base_url": "https://ai.example/v1", "api_key": "ai-secret", "model": "m"},
            "openclaw": {
                "enabled": True,
                "base_url": "http://127.0.0.1:18788/v1",
                "api_key": "claw-secret",
                "model": "openclaw:wxbot",
            },
        })
        self.assertEqual(result["auto_reply"], True)
        self.assertEqual(result["openclaw"], {"enabled": True, "configured": True})
        self.assertNotIn("api_key", json.dumps(result))

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
            reply = app.openclaw_chat("你好", session_id="wx-user-1", cfg=cfg, timeout=2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(reply, "OpenClaw 回答")
        self.assertEqual(_OpenClawHandler.request_auth, "Bearer gateway-token")
        self.assertEqual(_OpenClawHandler.request_data["model"], "openclaw:wxbot")
        self.assertEqual(_OpenClawHandler.request_data["user"], "wx-user-1")
        self.assertEqual(_OpenClawHandler.request_data["messages"][0]["role"], "system")
        self.assertIn("纯文本", _OpenClawHandler.request_data["messages"][0]["content"])
        self.assertNotIn("wxbot", _OpenClawHandler.request_data["messages"][0]["content"])
        self.assertEqual(
            _OpenClawHandler.request_data["messages"][-1],
            {"role": "user", "content": "你好"},
        )

    def test_ai_answer_does_not_fall_back_when_openclaw_is_not_configured(self):
        with mock.patch.object(app, "openclaw_config", return_value={}), \
             mock.patch.object(app, "ai_chat") as direct:
            with self.assertRaisesRegex(ValueError, "OpenClaw 未配置"):
                app.ai_answer("问题", session_id="wx-user-2")
        direct.assert_not_called()

    def test_legacy_search_command_is_answered_by_openclaw(self):
        with mock.patch.object(app, "ai_answer", return_value="OpenClaw 回答") as answer:
            reply = app.handle_command(
                "/搜索 柯洁最近在干嘛", "wx-user", "用户", "", "", {"smart": True}
            )

        self.assertEqual(reply, "OpenClaw 回答")
        answer.assert_called_once_with("柯洁最近在干嘛", session_id="wx-user")


if __name__ == "__main__":
    unittest.main()
