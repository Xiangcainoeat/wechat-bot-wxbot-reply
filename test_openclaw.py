import json
import os
import threading
import tempfile
import time
import unittest
import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from unittest import mock
import urllib.error
import urllib.request

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
    def _patch_openclaw_paths(self, stack, *, registry=None, index=None, transcript_dir=None):
        """Keep session fixtures isolated regardless of the production constant spelling."""
        aliases = {
            "registry": ("OPENCLAW_SESSIONS_FILE", "OPENCLAW_SESSION_FILE"),
            "index": (
                "OPENCLAW_INDEX_FILE",
                "OPENCLAW_SESSION_INDEX_FILE",
                "OPENCLAW_SESSIONS_INDEX_FILE",
            ),
            "transcript_dir": (
                "OPENCLAW_TRANSCRIPT_DIR",
                "OPENCLAW_TRANSCRIPTS_DIR",
                "OPENCLAW_SESSION_DIR",
            ),
        }
        values = {
            "registry": registry,
            "index": index,
            "transcript_dir": transcript_dir,
        }
        for kind, value in values.items():
            if value is None:
                continue
            for name in aliases[kind]:
                stack.enter_context(mock.patch.object(app, name, value, create=True))

    @staticmethod
    def _write_json(path, value):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)

    @staticmethod
    def _write_jsonl(path, records):
        with open(path, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _public_api_request(self, method, path, body=None):
        """Issue an authenticated request against the embedded admin handler."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        server.is_public = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:{}{}".format(server.server_port, path)
        data = None
        headers = {"Cookie": "wxbot_admin=test-session"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            try:
                with urllib.request.urlopen(request, timeout=3) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def _require_callable(self, name):
        fn = getattr(app, name, None)
        self.assertTrue(callable(fn), "{} 尚未实现".format(name))
        return fn

    def _session_fixture(self, tmp):
        user_id = "wxid_" + ("A" * 72)
        old_key = "agent:wxbot:openai-user:" + user_id + ":session:old"
        route_key = "agent:wxbot:openai-user:route:" + user_id
        foreign_key = "agent:other:openai-user:" + user_id
        old_sid = "transcript-old"
        index = os.path.join(tmp, "sessions.json")
        transcript_dir = os.path.join(tmp, "transcripts")
        os.makedirs(transcript_dir)
        self._write_json(index, {
            old_key: {
                "sessionId": old_sid,
                "updatedAt": 1723036800000,
                "contextTokens": 12400,
                "contextWindow": 128000,
            },
            route_key: {
                "sessionId": "route-transcript",
                "updatedAt": 1723036801000,
            },
            foreign_key: {
                "sessionId": "foreign-transcript",
                "updatedAt": 1723036802000,
            },
        })
        transcript = os.path.join(transcript_dir, old_sid + ".jsonl")
        self._write_jsonl(transcript, [
            {
                "type": "message",
                "timestamp": "2026-08-07T10:00:00Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "你好"}]},
            },
            {
                "type": "message",
                "timestamp": "2026-08-07T10:00:01Z",
                "message": {"role": "assistant", "content": "你好，我是 OpenClaw。"},
            },
            {
                "type": "compaction",
                "timestamp": "2026-08-07T10:01:00Z",
                "message": {"summary": "早期对话已压缩", "tokensBefore": 120000, "tokensAfter": 5000},
            },
        ])
        return {
            "user_id": user_id,
            "old_key": old_key,
            "old_sid": old_sid,
            "index": index,
            "transcript_dir": transcript_dir,
            "transcript": transcript,
        }

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

    def test_active_session_defaults_to_wechat_id(self):
        active_key = self._require_callable("openclaw_active_key")
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            registry = os.path.join(tmp, "openclaw_sessions.json")
            self._patch_openclaw_paths(stack, registry=registry)
            user_id = "wx-user-default"
            self.assertEqual(active_key(user_id), user_id)
            self.assertEqual(active_key(user_id), user_id)

    def test_new_session_rotates_key_without_removing_previous_session(self):
        active_key = self._require_callable("openclaw_active_key")
        start_new = self._require_callable("openclaw_start_new_session")
        parse_index = self._require_callable("openclaw_parse_sessions_index")
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            fixture = self._session_fixture(tmp)
            registry = os.path.join(tmp, "openclaw_sessions.json")
            self._patch_openclaw_paths(
                stack,
                registry=registry,
                index=fixture["index"],
                transcript_dir=fixture["transcript_dir"],
            )
            old_key = active_key(fixture["user_id"])
            new_key = start_new(fixture["user_id"])
            self.assertNotEqual(new_key, old_key)
            self.assertTrue(new_key.startswith(fixture["user_id"] + ":session:"))
            self.assertEqual(active_key(fixture["user_id"]), new_key)

            sessions = parse_index(
                index_path=fixture["index"], transcript_dir=fixture["transcript_dir"]
            )
            serialized = json.dumps(sessions, ensure_ascii=False)
            self.assertIn(fixture["old_sid"], serialized)

    def test_sessions_index_excludes_foreign_and_route_sessions(self):
        parse_index = self._require_callable("openclaw_parse_sessions_index")
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._session_fixture(tmp)
            sessions = parse_index(
                index_path=fixture["index"], transcript_dir=fixture["transcript_dir"]
            )
        items = sessions.get("sessions", sessions) if isinstance(sessions, dict) else sessions
        self.assertIsInstance(items, list)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].get("session_id"), fixture["old_sid"])

    def test_transcript_parser_returns_user_assistant_and_compaction_records(self):
        parse_transcript = self._require_callable("openclaw_parse_transcript")
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._session_fixture(tmp)
            records = parse_transcript(fixture["transcript"])
        self.assertIsInstance(records, list)
        self.assertEqual(records[0]["role"], "user")
        self.assertEqual(records[0]["content"], "你好")
        self.assertEqual(records[1]["role"], "assistant")
        self.assertEqual(records[1]["content"], "你好，我是 OpenClaw。")
        self.assertEqual(records[2]["type"], "compaction")
        self.assertEqual(records[2]["tokens_before"], 120000)

    def test_context_usage_format_includes_used_limit_and_percent(self):
        format_usage = self._require_callable("format_context_usage")
        self.assertEqual(
            format_usage(12400, 128000),
            "（上下文 12.4k / 128k，9.7%）",
        )
        self.assertEqual(format_usage(None, None), "")

    def test_ai_answer_uses_active_session_key(self):
        with mock.patch.object(
            app,
            "openclaw_config",
            return_value={"enabled": True, "base_url": "http://gateway", "api_key": "token"},
        ), mock.patch.object(app, "openclaw_active_key", return_value="wx-user:session:new", create=True) as active, \
             mock.patch.object(app, "openclaw_chat", return_value="回答") as chat:
            reply = app.ai_answer("问题", session_id="wx-user")

        self.assertEqual(reply, "回答")
        active.assert_called_once_with("wx-user")
        self.assertEqual(chat.call_args.kwargs["session_id"], "wx-user:session:new")

    def test_compact_command_dispatches_to_current_openclaw_session(self):
        compact = self._require_callable("openclaw_compact_session")
        with mock.patch.object(app, "openclaw_active_key", return_value="wx-user"), \
             mock.patch.object(app, "openclaw_chat", return_value="已压缩当前上下文。") as chat:
            self.assertEqual(compact("wx-user"), "已压缩当前上下文。")
        chat.assert_called_once_with("/compact", session_id="wx-user", sanitize=False)

    def test_compact_and_new_session_commands_are_handled(self):
        with mock.patch.object(
            app, "openclaw_compact_session", return_value="已压缩当前上下文。", create=True
        ) as compact, mock.patch.object(
            app, "openclaw_start_new_session", return_value="wx-user:session:new", create=True
        ) as start_new:
            compact_reply = app.handle_command(
                "/compact", "wx-user", "用户", "", "", app.load_config()
            )
            new_reply = app.handle_command(
                "开启新的会话", "wx-user", "用户", "", "", app.load_config()
            )

        self.assertTrue(compact_reply)
        self.assertTrue(new_reply)
        compact.assert_called_once_with("wx-user")
        start_new.assert_called_once_with("wx-user")

    def test_openclaw_session_api_hides_long_ids_and_lists_only_wxbot_sessions(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack, \
             mock.patch.object(app, "valid_session", return_value=True):
            fixture = self._session_fixture(tmp)
            registry = os.path.join(tmp, "openclaw_sessions.json")
            self._patch_openclaw_paths(
                stack,
                registry=registry,
                index=fixture["index"],
                transcript_dir=fixture["transcript_dir"],
            )
            status, payload = self._public_api_request("GET", "/api/openclaw/sessions")

        self.assertEqual(status, 200)
        self.assertTrue(payload.get("success"))
        self.assertIsInstance(payload.get("sessions"), list)
        self.assertEqual(len(payload["sessions"]), 1)
        self.assertIn("session_ref", payload["sessions"][0])
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(fixture["user_id"], serialized)
        self.assertNotIn("agent:wxbot:openai-user:", serialized)
        self.assertNotIn(fixture["transcript_dir"], serialized)

    def test_openclaw_session_detail_api_returns_transcript_messages_and_compactions(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack, \
             mock.patch.object(app, "valid_session", return_value=True):
            fixture = self._session_fixture(tmp)
            self._patch_openclaw_paths(
                stack,
                index=fixture["index"],
                transcript_dir=fixture["transcript_dir"],
            )
            list_status, listing = self._public_api_request("GET", "/api/openclaw/sessions")
            self.assertEqual(list_status, 200)
            session_ref = listing["sessions"][0]["session_ref"]
            status, payload = self._public_api_request(
                "GET", "/api/openclaw/sessions/{}".format(session_ref)
            )

        self.assertEqual(status, 200)
        self.assertTrue(payload.get("success"))
        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertEqual(payload["messages"][1]["role"], "assistant")
        self.assertEqual(len(payload["compactions"]), 1)
        self.assertNotIn(fixture["user_id"], json.dumps(payload, ensure_ascii=False))

    def test_openclaw_session_post_dispatches_new_action_by_short_reference(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack, \
             mock.patch.object(app, "valid_session", return_value=True), \
             mock.patch.object(
                 app,
                 "openclaw_start_new_session",
                 return_value="wx-user:session:new",
                 create=True,
             ) as start_new:
            fixture = self._session_fixture(tmp)
            self._patch_openclaw_paths(
                stack,
                index=fixture["index"],
                transcript_dir=fixture["transcript_dir"],
            )
            status, payload = self._public_api_request(
                "POST",
                "/api/openclaw/sessions",
                {"action": "new", "session_ref": "session-old"},
            )

        self.assertEqual(status, 200)
        self.assertTrue(payload.get("success"))
        start_new.assert_called_once()
        self.assertNotIn(fixture["user_id"], json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
