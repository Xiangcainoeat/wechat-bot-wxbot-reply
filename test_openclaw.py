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


class _HtmlModelsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<!doctype html><title>OpenClaw Control</title>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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

    def test_wechat_identity_is_shared_between_private_and_group_transport_ids(self):
        identity_user_id = self._require_callable("identity_user_id")
        with tempfile.TemporaryDirectory() as tmp:
            identity_file = os.path.join(tmp, "identities.json")
            payload = {
                "name": "Z",
                "alias": "",
                "gender": 1,
                "province": "海南",
                "city": "三亚",
                "signature": "顺顺利利",
                "avatar": "http://bot/res?media=%2Fcgi-bin%2Fwebwxgeticon%3Fseq%3D781301151%26username%3D%40old",
            }
            with mock.patch.object(app, "IDENTITY_FILE", identity_file):
                first = identity_user_id(dict(payload, id="@old-private"), "@old-private")
                second = identity_user_id(dict(payload, id="@new-group"), "@new-group")

        self.assertEqual(first, second)
        self.assertNotEqual(first, "@new-group")

    def test_same_name_without_stable_profile_is_not_merged(self):
        identity_user_id = self._require_callable("identity_user_id")
        with tempfile.TemporaryDirectory() as tmp:
            identity_file = os.path.join(tmp, "identities.json")
            with mock.patch.object(app, "IDENTITY_FILE", identity_file):
                first = identity_user_id({"id": "@first", "name": "同名用户"}, "@first")
                second = identity_user_id({"id": "@second", "name": "同名用户"}, "@second")

        self.assertNotEqual(first, second)

    def test_bot_send_resolves_identity_to_current_transport_id(self):
        identity_user_id = self._require_callable("identity_user_id")
        class _Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"success":true}'

        with tempfile.TemporaryDirectory() as tmp:
            identity_file = os.path.join(tmp, "identities.json")
            payload = {
                "name": "Z", "province": "海南", "city": "三亚",
                "signature": "顺顺利利", "gender": 1,
                "avatar": "http://bot/res?media=%2Fcgi-bin%2Fwebwxgeticon%3Fseq%3D781301151%26username%3D%40old",
            }
            with mock.patch.object(app, "IDENTITY_FILE", identity_file), \
                 mock.patch.object(app, "bot_token", return_value="token"), \
                 mock.patch.object(app.urllib.request, "urlopen", return_value=_Response()) as urlopen:
                identity = identity_user_id(dict(payload, id="@stable"), "@stable")
                identity_user_id(dict(payload, id="@current"), "@current")
                ok, _ = app.bot_send(identity, "提醒", False)

        self.assertTrue(ok)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["to"], {"id": "@current"})

    def test_receive_pipeline_uses_identity_id_instead_of_transport_id(self):
        source = {
            "room": {"id": "@@room", "payload": {"topic": "测试群"}},
            "from": {"payload": {"id": "@temporary", "name": "Z"}},
            "to": {"payload": {"name": "kindle"}},
        }
        fields = {
            "type": (None, b"text"),
            "content": (None, "普通消息".encode("utf-8")),
            "source": (None, json.dumps(source, ensure_ascii=False).encode("utf-8")),
            "isMentioned": (None, b"0"),
            "isMsgFromSelf": (None, b"0"),
            "isSystemEvent": (None, b"0"),
        }

        class _Receiver:
            def _json(self, value):
                self.result = value

        receiver = _Receiver()
        with mock.patch.object(app, "identity_user_id", return_value="stable-user", create=True) as identify, \
             mock.patch.object(app, "record_user") as record_user, \
             mock.patch.object(app, "load_config", return_value={"auto_reply": False}), \
             mock.patch.object(app, "save_record") as save_record:
            app.Handler._on_receive(receiver, fields)

        identify.assert_called_once_with(source["from"]["payload"], "@temporary")
        record_user.assert_called_once_with("stable-user", "Z")
        self.assertEqual(save_record.call_args.args[0]["fromId"], "stable-user")

    def test_historical_messages_use_the_stable_identity_reference(self):
        identity_user_id = self._require_callable("identity_user_id")
        with tempfile.TemporaryDirectory() as tmp:
            identity_file = os.path.join(tmp, "identities.json")
            payload = {
                "name": "Z", "gender": 1, "province": "海南", "city": "三亚",
                "signature": "顺顺利利",
            }
            with mock.patch.object(app, "IDENTITY_FILE", identity_file):
                stable = identity_user_id(dict(payload, id="@old"), "@old")
                identity_user_id(dict(payload, id="@new"), "@new")
                public = app._public_message_record({"fromId": "@new", "content": "你好"})

        self.assertEqual(public["user_ref"], app._short_ref(stable, "u"))

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
        self.assertNotIn("ai", result)
        self.assertNotIn("api_key", json.dumps(result))

    def test_admin_ai_tab_is_openclaw_configuration_only(self):
        page = app.ADMIN_PAGE
        self.assertIn("OpenClaw Gateway 配置", page)
        self.assertIn("id=\"claw-base\"", page)
        self.assertIn("/api/openclaw/config", page)
        self.assertNotIn("id=\"ai-base\"", page)
        self.assertNotIn("loadAI()", page)

    def test_admin_session_action_uses_valid_javascript_quoted_arguments(self):
        page = app.ADMIN_PAGE
        self.assertIn("viewOpenClawSession(\\'", page)
        self.assertIn("openClawSessionAction(\\'compact\\',\\'", page)
        self.assertNotIn("viewOpenClawSession(''+", page)
        self.assertNotIn("openClawSessionAction(''compact'',", page)

    def test_admin_updates_openclaw_configuration_and_removes_legacy_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            self._write_json(config_path, {
                "ai": {"base_url": "https://legacy.example/v1", "api_key": "legacy", "model": "old"},
                "openclaw": {"enabled": True, "base_url": "http://old-gateway/v1", "api_key": "old-token", "model": "openclaw:wxbot"},
            })
            with mock.patch.object(app, "CONFIG_FILE", config_path), \
                 mock.patch.object(app, "valid_session", return_value=True):
                get_status, current = self._public_api_request("GET", "/api/openclaw/config")
                post_status, _ = self._public_api_request("POST", "/api/openclaw/config", {
                    "enabled": True,
                    "base_url": "http://new-gateway/v1",
                    "api_key": "new-token",
                    "model": "openclaw:wxbot",
                    "session_index": "/sessions/index.json",
                    "transcript_dir": "/sessions",
                })
                saved = app.load_config()

        self.assertEqual(get_status, 200)
        self.assertNotIn("old-token", json.dumps(current))
        self.assertEqual(post_status, 200)
        self.assertEqual(saved["openclaw"]["base_url"], "http://new-gateway/v1")
        self.assertEqual(saved["openclaw"]["api_key"], "new-token")
        self.assertNotIn("ai", saved)

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

    def test_wechat_login_proxy_extracts_qr_and_uses_same_origin_assets(self):
        extract = self._require_callable("extract_wechat_login_qr")
        html = (
            '<script src="/static/qrcode.min.js"></script>'
            '<script>qrcode.makeCode("https://login.weixin.qq.com/l/test-code");'
            'new EventSource("/sse");</script>'
        )
        self.assertEqual(extract(html), "https://login.weixin.qq.com/l/test-code")
        page = getattr(app, "WECHAT_LOGIN_PAGE", "")
        self.assertIn("/wechat-login/qrcode.js", page)
        self.assertIn("/api/wechat-login/qrcode", page)
        self.assertNotIn('new EventSource("/sse")', page)

    def test_admin_status_and_page_do_not_expose_long_ids(self):
        long_user_id = "wxid_" + "B" * 72
        long_room_id = "room_" + "C" * 72
        old_last = app.STATS.get("last")
        try:
            app.STATS["last"] = {
                "time": "2026-08-07 15:00:00",
                "from": "用户",
                "fromId": long_user_id,
                "room": "群聊",
                "roomId": long_room_id,
                "content": "你好",
                "reply": "**回答**",
            }
            with mock.patch.object(app, "valid_session", return_value=True), \
                 mock.patch.object(app, "bot_status", return_value={"reachable": True, "logged_in": False, "raw": "unHealthy"}), \
                 mock.patch.object(app, "bot_token", return_value="login-secret"):
                status, payload = self._public_api_request("GET", "/api/status")
        finally:
            app.STATS["last"] = old_last

        self.assertEqual(status, 200)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(long_user_id, serialized)
        self.assertNotIn(long_room_id, serialized)
        self.assertNotIn("login-secret", serialized)
        self.assertEqual(payload["login_url"], "/wechat-login")
        page = app.ADMIN_PAGE
        self.assertNotIn("显示完整ID", page)
        self.assertNotIn("toggleIds", page)
        self.assertIn("tab-sessions", page)
        self.assertIn("/api/openclaw/sessions", page)

    def test_public_message_record_hides_transport_id_used_as_sender_name(self):
        long_user_id = "wxid_" + "D" * 72
        public = app._public_message_record({
            "time": "2026-08-07 15:00:00",
            "from": long_user_id,
            "fromId": long_user_id,
            "room": "私聊",
            "roomId": "",
            "content": "你好",
            "reply": "回答",
        })
        serialized = json.dumps(public, ensure_ascii=False)
        self.assertNotIn(long_user_id, serialized)
        self.assertTrue(public["from"].startswith("u-"))
        self.assertTrue(public["user_ref"].startswith("u-"))

    def test_public_log_line_redacts_registered_transport_ids(self):
        redact = self._require_callable("public_log_line")
        long_user_id = "wxid_" + "E" * 72
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            app, "IDENTITY_FILE", os.path.join(tmp, "identities.json")
        ):
            app.identity_user_id({"id": long_user_id, "name": "用户"}, long_user_id)
            line = redact('{"fromId":"' + long_user_id + '"}')
        self.assertNotIn(long_user_id, line)
        self.assertIn("u-", line)

    def test_public_log_line_redacts_unregistered_room_ids(self):
        redact = self._require_callable("public_log_line")
        long_room_id = "@@" + "F" * 72
        line = redact('{"roomId":"' + long_room_id + '"}')
        self.assertNotIn(long_room_id, line)
        self.assertIn("r-", line)

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

    def test_context_status_ignores_zero_gateway_usage_and_reads_session_index(self):
        context_status = self._require_callable("_openclaw_context_status")
        with tempfile.TemporaryDirectory() as tmp:
            index = os.path.join(tmp, "sessions.json")
            self._write_json(index, {
                app._OPENCLAW_INDEX_PREFIX + "wx-user": {
                    "totalTokens": 5800,
                    "contextTokens": 128000,
                },
            })
            with mock.patch.object(
                app, "openclaw_config", return_value={"session_index": index}
            ), mock.patch.dict(
                app._OPENCLAW_LAST_USAGE,
                {"wx-user": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                }},
                clear=True,
            ):
                self.assertEqual(
                    context_status("wx-user"),
                    "（上下文 5.8k / 128k，4.5%）",
                )

    def test_context_status_sums_input_and_cache_read_tokens(self):
        context_status = self._require_callable("_openclaw_context_status")
        with mock.patch.dict(
            app._OPENCLAW_LAST_USAGE,
            {"wx-user": {
                "input": 3088,
                "cacheRead": 1792,
                "output": 1952,
                "totalTokens": 6832,
            }},
            clear=True,
        ):
            self.assertEqual(
                context_status("wx-user"),
                "（上下文 4.9k / 128k，3.8%；缓存命中 1792 / 未命中 3088）",
            )

    def test_sessions_index_context_uses_input_plus_cache_read_from_transcript(self):
        parse_index = self._require_callable("openclaw_parse_sessions_index")
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._session_fixture(tmp)
            # 覆盖 fixture 的最后一条助手回复，带 input+cacheRead 的真实 usage。
            with open(fixture["transcript"], "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "type": "message",
                    "timestamp": "2026-08-07T11:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": "最新回复",
                        "usage": {
                            "input": 3088,
                            "cacheRead": 1792,
                            "output": 1952,
                            "totalTokens": 6832,
                        },
                    },
                }) + "\n")
            sessions = parse_index(
                index_path=fixture["index"], transcript_dir=fixture["transcript_dir"]
            )
            items = sessions.get("sessions", sessions) if isinstance(sessions, dict) else sessions
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].get("context_used"), 3088 + 1792)

    def test_openclaw_fetch_models_falls_back_to_native_config(self):
        fetch_models = self._require_callable("openclaw_fetch_models")
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "openclaw.json")
            self._write_json(config_path, {
                "models": {
                    "providers": {
                        "wxbot": {"models": [{"id": "gpt-5.5"}]},
                        "bailian": {"models": [{"id": "qwen3.5-plus"}]},
                    },
                },
            })
            server = HTTPServer(("127.0.0.1", 0), _HtmlModelsHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with mock.patch.object(
                    app,
                    "openclaw_config",
                    return_value={
                        "base_url": "http://127.0.0.1:{}/v1".format(server.server_port),
                        "api_key": "gateway-token",
                    },
                ), mock.patch.object(app, "OPENCLAW_CONFIG_FILE", config_path, create=True):
                    models = fetch_models(timeout=2)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual(models, ["wxbot/gpt-5.5", "bailian/qwen3.5-plus"])

    def test_wechat_system_prompt_does_not_claim_a_model(self):
        prompt = app.WECHAT_SYSTEM_PROMPT
        self.assertIn("我是 OpenClaw 智能体", prompt)
        self.assertNotIn("当前使用", prompt)
        self.assertNotIn("{model}", prompt)
        self.assertNotIn("gpt", prompt.lower())

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

    def test_looks_evasive_reply_detects_evasive_patterns(self):
        detect = self._require_callable("_looks_evasive_reply")
        self.assertTrue(detect("我没看到官方提前官宣，以游戏内活动中心为准。"))
        self.assertTrue(detect("我现在没法实时确认，你可以自己去查。"))
        self.assertTrue(detect("暂未官宣，建议你关注官方微博和公众号。"))
        self.assertTrue(detect("你把活动名、截图，或者公告链接发我一下，我直接帮你看。"))
        self.assertTrue(detect("我查一下王者荣耀明天的最新活动信息。"))
        self.assertTrue(detect("需要查一下最新公告才能确认，稍等。"))
        self.assertTrue(detect("<tool_calls>\n<invoke name=\"session_status\">\n<parameter name=\"action\" value=\"status\"/>\n</invoke>\n</tool_calls>"))

    def test_clean_wechat_reply_strips_tool_call_markup(self):
        clean = self._require_callable("clean_wechat_reply")
        raw = ("<tool_calls>\n"
               "<invoke name=\"browser\">\n"
               "<parameter name=\"action\" value=\"open\"/>\n"
               "<parameter name=\"url\" value=\"https://example.com\"/>\n"
               "</invoke>\n"
               "</tool_calls>\n根据搜索到的资料，明天活动是夏日农友节。")
        result = clean(raw)
        self.assertNotIn("<tool_calls>", result)
        self.assertNotIn("<invoke", result)
        self.assertNotIn("<parameter", result)
        self.assertIn("夏日农友节", result)
        inline = clean("<tool_calls>\n<invoke name=\"exec\">\n"
                       "<parameter name=\"cmd\" string=\"true\">curl -s x | grep y</parameter>\n"
                       "</invoke>\n</tool_calls>\n答案")
        self.assertEqual(inline.strip(), "答案")

    def test_ai_answer_falls_back_when_raw_reply_is_tool_call(self):
        ai_answer = self._require_callable("ai_answer")
        with mock.patch.object(
            app, "openclaw_config",
            return_value={"enabled": True, "base_url": "http://127.0.0.1:18788/v1",
                          "api_key": "k", "model": "openclaw:wxbot"},
        ), mock.patch.object(
            app, "openclaw_active_key", return_value="wx-user",
        ), mock.patch.object(
            app, "local_web_search",
            return_value="1. 夏日农友节\n   8月8日开启",
        ), mock.patch.object(
            app, "openclaw_chat", side_effect=[
                "<tool_calls>\n<invoke name=\"exec\">\n"
                "<parameter name=\"cmd\" string=\"true\">curl x</parameter>\n"
                "</invoke>\n</tool_calls>",
                "明天是夏日农友节开启日，可领暴击夺宝券。",
            ],
        ) as chat:
            result = ai_answer("王者明天活动")

        self.assertIn("夏日农友节", result)
        self.assertNotIn("<tool_calls>", result)
        self.assertNotIn("<invoke", result)
        self.assertEqual(chat.call_count, 2)

    def test_looks_evasive_reply_ignores_normal_answers(self):
        detect = self._require_callable("_looks_evasive_reply")
        self.assertFalse(detect("明天 8月8日 有无双祈愿活动，8月8日开启。"))
        self.assertFalse(detect("今天上海晴到多云，28 度。"))

    def test_ai_answer_composes_answer_from_local_search_when_evasive(self):
        ai_answer = self._require_callable("ai_answer")
        with mock.patch.object(
            app, "openclaw_config",
            return_value={"enabled": True, "base_url": "http://127.0.0.1:18788/v1",
                          "api_key": "k", "model": "wxbot/gpt-5.5"},
        ), mock.patch.object(
            app, "openclaw_active_key", return_value="wx-user",
        ), mock.patch.object(
            app, "local_web_search",
            return_value="1. 无双祈愿活动 8月8日开启\n   妲己九尾天狐返场",
        ) as local_search, mock.patch.object(
            app, "openclaw_chat", side_effect=[
                "我没看到相关公告，你可以自己去查。",
                "明天 8月8日 有无双祈愿活动，8月8日开启。",
            ],
        ) as chat:
            result = ai_answer("王者明天抽奖活动")

        self.assertIn("无双祈愿", result)
        self.assertNotIn("我没看到", result)
        self.assertEqual(chat.call_count, 2)
        self.assertNotEqual(
            chat.call_args_list[1].kwargs.get("system_prompt"),
            app.WECHAT_SYSTEM_PROMPT,
        )
        self.assertEqual(
            chat.call_args_list[1].kwargs.get("system_prompt"),
            app.OPENCLAW_COMPOSE_SYSTEM_PROMPT,
        )
        local_search.assert_called_once()

    def test_usage_breakdown_handles_deepseek_and_openclaw_formats(self):
        breakdown = self._require_callable("_usage_breakdown")
        deepseek = {
            "prompt_tokens": 265,
            "prompt_tokens_details": {"cached_tokens": 256},
            "prompt_cache_hit_tokens": 256,
            "prompt_cache_miss_tokens": 9,
        }
        self.assertEqual(breakdown(deepseek), (265, 256, 9))
        self.assertEqual(breakdown({"input": 3088, "cacheRead": 1792}), (4880, 1792, 3088))
        self.assertEqual(breakdown({"prompt_tokens": 100}), (100, 0, 100))
        self.assertIsNone(breakdown({}))

    def test_openclaw_context_status_includes_cache_breakdown(self):
        status = self._require_callable("_openclaw_context_status")
        with mock.patch.object(app, "openclaw_config", return_value={}):
            with app.OPENCLAW_USAGE_LOCK:
                app._OPENCLAW_LAST_USAGE["wx-user"] = {
                    "prompt_tokens": 265,
                    "prompt_tokens_details": {"cached_tokens": 256},
                    "prompt_cache_hit_tokens": 256,
                    "prompt_cache_miss_tokens": 9,
                }
            try:
                result = status("wx-user")
            finally:
                with app.OPENCLAW_USAGE_LOCK:
                    app._OPENCLAW_LAST_USAGE.pop("wx-user", None)
        self.assertIn("上下文 265 / 128k", result)
        self.assertIn("缓存命中 256 / 未命中 9", result)

    def test_ai_answer_falls_back_to_local_search_when_retry_evasive(self):
        ai_answer = self._require_callable("ai_answer")
        with mock.patch.object(
            app, "openclaw_config",
            return_value={"enabled": True, "base_url": "http://127.0.0.1:18788/v1",
                          "api_key": "k", "model": "wxbot/gpt-5.5"},
        ), mock.patch.object(
            app, "openclaw_active_key", return_value="wx-user",
        ), mock.patch.object(
            app, "local_web_search",
            return_value="1. 夏日农友节内容一览\n   8月8日来玩王者荣耀",
        ) as local_search, mock.patch.object(
            app, "openclaw_chat", return_value="我没看到官方公告，建议你自己去查。",
        ) as chat:
            result = ai_answer("王者明天抽奖活动")

        self.assertIn("夏日农友节", result)
        self.assertIn("本地搜索", result)
        self.assertEqual(chat.call_count, 2)
        local_search.assert_called_once()

    def test_ai_answer_falls_back_to_local_search_when_gateway_raises(self):
        ai_answer = self._require_callable("ai_answer")
        with mock.patch.object(
            app, "openclaw_config",
            return_value={"enabled": True, "base_url": "http://127.0.0.1:18788/v1",
                          "api_key": "k", "model": "wxbot/gpt-5.5"},
        ), mock.patch.object(
            app, "openclaw_active_key", return_value="wx-user",
        ), mock.patch.object(
            app, "local_web_search",
            return_value="1. 夏日农友节内容一览\n   8月8日来玩王者荣耀",
        ) as local_search, mock.patch.object(
            app, "openclaw_chat", side_effect=TimeoutError("timed out"),
        ) as chat:
            result = ai_answer("王者明天抽奖活动")

        self.assertIn("夏日农友节", result)
        self.assertIn("本地搜索", result)
        chat.assert_called_once()
        local_search.assert_called_once()

    def test_local_web_search_uses_sogou_then_bing_fallback(self):
        local_web_search = self._require_callable("local_web_search")
        with mock.patch.object(app, "_sogou_results", return_value=[]), \
             mock.patch.object(
                 app, "_bing_results",
                 return_value=[("Bing 标题", "Bing 摘要", "https://example.com/x")],
             ):
            text = local_web_search("@kindle 王者 夏日农友节")
        self.assertIn("Bing 标题", text)
        self.assertIn("Bing 摘要", text)

    def test_local_web_search_formats_sogou_results(self):
        local_web_search = self._require_callable("local_web_search")
        with mock.patch.object(
            app, "_sogou_results",
            return_value=[("夏日农友节内容一览", "8月8日来玩王者荣耀", "https://sogou.com/link?url=x")],
        ), mock.patch.object(app, "_bing_results", return_value=[]):
            text = local_web_search("王者 夏日农友节")
        self.assertIn("夏日农友节内容一览", text)
        self.assertIn("8月8日来玩王者荣耀", text)
        self.assertTrue(text.startswith("1 夏日农友节内容一览"))

    def test_local_web_search_numbering_ignores_snippet_lines(self):
        local_web_search = self._require_callable("local_web_search")
        with mock.patch.object(
            app, "_sogou_results",
            return_value=[
                ("标题一", "摘要一", "https://sogou.com/link?url=1"),
                ("标题二", "摘要二", "https://sogou.com/link?url=2"),
                ("标题三", "", "https://sogou.com/link?url=3"),
            ],
        ), mock.patch.object(app, "_bing_results", return_value=[]):
            text = local_web_search("王者 夏日农友节")
        self.assertIn("\n2 标题二", text)
        self.assertIn("\n3 标题三", text)
        self.assertNotIn("\n4 ", text)


if __name__ == "__main__":
    unittest.main()
