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
import urllib.parse
import urllib.request

import app


# OpenClaw 网关直连时模型可能返回的 DSML 全角反斜杠标签（U+FF5C）前缀
BS = "\uff5c\uff5c"


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


class _ModelsJsonHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"object": "list", "data": [
            {"id": "deepseek-v4-flash", "object": "model"},
            {"id": "deepseek-v4-pro", "object": "model"},
        ]}).encode("utf-8")
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
        # 生产 config.json 里可能配了 openclaw.session_index，会覆盖上面 patch 的
        # 常量导致测试读到真实会话；把 CONFIG_FILE 指向临时目录里不存在的文件，
        # 让 load_config() 走默认值（无 session_index），保证 fixture 生效。
        if registry or index or transcript_dir:
            base = os.path.dirname(registry or index or transcript_dir or ".")
            stack.enter_context(mock.patch.object(
                app, "CONFIG_FILE", os.path.join(base, "config.json"), create=True
            ))

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

    def test_group_reply_is_not_prefixed_with_sender_mention(self):
        self.assertEqual(
            app.wechat_outbound_text("**回答**", "😀**用户**", True),
            "回答",
        )

    def test_automation_route_exposes_only_reminder_and_daily_push(self):
        self.assertEqual(
            app.AUTOMATION_ACTIONS,
            {"set_reminder", "set_daily_push"},
        )
        self.assertNotIn("web_search", app.OPENCLAW_ROUTE_PROMPT)

    def test_legacy_web_search_and_ai_router_are_removed(self):
        for name in (
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
            session_id="group:room-1",
        )

    def test_ai_session_key_isolates_private_and_group_scenarios(self):
        key = self._require_callable("ai_session_key")
        self.assertEqual(key("wx-user", ""), "wx-user")
        self.assertEqual(key("wx-user", "room-1"), "group:room-1")
        # 同一群里所有成员共享同一个会话键；不同群、私聊互不相同
        self.assertEqual(key("wx-user", "room-1"), key("wx-user2", "room-1"))
        self.assertNotEqual(key("wx-user", "room-1"), key("wx-user", "room-2"))
        self.assertNotEqual(key("wx-user", "room-1"), key("wx-user", ""))

    def test_smart_fallback_uses_shared_group_session_in_room(self):
        with mock.patch.object(app, "is_allowed", return_value=True), \
             mock.patch.object(app, "openclaw_route", side_effect=AssertionError("不应路由")), \
             mock.patch.object(app, "ai_answer", return_value="群回答") as answer:
            reply = app.smart_fallback("明天活动", "wx-user", "用户", "room-1", "群", {"smart": True})
        self.assertEqual(reply, "群回答")
        answer.assert_called_once_with("明天活动", session_id="group:room-1")

    def test_handle_command_compact_uses_shared_group_session(self):
        with mock.patch.object(app, "openclaw_compact_session",
                               return_value="已压缩。", create=True) as compact:
            reply = app.handle_command("/compact", "wx-user", "用户", "room-1", "群",
                                       app.load_config())
        self.assertEqual(reply, "已压缩。")
        compact.assert_called_once_with("group:room-1")

    def test_dispatch_route_chat_answer_uses_group_session_key(self):
        with mock.patch.object(app, "is_allowed", return_value=True), \
             mock.patch.object(app, "ai_answer", return_value="回答") as answer:
            reply = app.dispatch_route(
                "chat_answer", {"question": "问题"}, "问题", "wx-user", "用户", "room-1", "群"
            )
        self.assertEqual(reply, "回答")
        answer.assert_called_once_with("问题", session_id="group:room-1")

    def test_agent_switch_preference_is_isolated_per_scenario(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs = os.path.join(tmp, "agent_prefs.json")
            with mock.patch.object(app, "AGENT_PREFS_FILE", prefs), \
                 mock.patch.object(app, "load_config", return_value={"default_agent": "openclaw"}):
                app.handle_agent_switch("codex", "wx-user")
                app.handle_agent_switch("codex", "group:room-1")
                app.handle_agent_switch("默认", "group:room-1")
                self.assertEqual(app.active_agent("wx-user"), "codex")
                self.assertEqual(app.active_agent("group:room-1"), "openclaw")

    def test_openclaw_session_api_lists_group_sessions(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack, \
             mock.patch.object(app, "valid_session", return_value=True):
            fixture = self._session_fixture(tmp)
            room_id = "@@room_" + ("C" * 40)
            group_key = "agent:wxbot:openai-user:group:" + room_id + ":session:grp"
            group_sid = "transcript-group"
            with open(fixture["index"], "r", encoding="utf-8") as f:
                index_data = json.load(f)
            index_data[group_key] = {"sessionId": group_sid, "updatedAt": 1723036803000}
            with open(fixture["index"], "w", encoding="utf-8") as f:
                json.dump(index_data, f, ensure_ascii=False)
            transcript = os.path.join(fixture["transcript_dir"], group_sid + ".jsonl")
            self._write_jsonl(transcript, [
                {"type": "message", "timestamp": "2026-08-07T10:00:00Z",
                 "message": {"role": "user", "content": [{"type": "text", "text": "群问题"}]}},
            ])
            app._recent.append({"roomId": room_id, "room": "测试群",
                                "from": "用户", "fromId": "wx-user"})
            try:
                self._patch_openclaw_paths(
                    stack,
                    registry=os.path.join(tmp, "openclaw_sessions.json"),
                    index=fixture["index"],
                    transcript_dir=fixture["transcript_dir"],
                )
                status, payload = self._public_api_request("GET", "/api/openclaw/sessions")
            finally:
                app._recent.clear()

        self.assertEqual(status, 200)
        group_items = [s for s in payload["sessions"] if s.get("is_group")]
        self.assertEqual(len(group_items), 1)
        self.assertEqual(group_items[0]["user_name"], "测试群")
        self.assertNotIn(room_id, json.dumps(payload, ensure_ascii=False))

    def test_parse_mention_request_detects_group_mention_intent(self):
        parse = self._require_callable("_parse_mention_request")
        self.assertEqual(parse("艾特张三说你好", "room-1"), ("张三", "你好"))
        self.assertEqual(parse("帮我在群里艾特张三说你好", "room-1"), ("张三", "你好"))
        self.assertEqual(parse("@张三 说 明天开会", "room-1"), ("张三", "明天开会"))
        self.assertEqual(parse("at王五发消息：晚上聚餐", "room-1"), ("王五", "晚上聚餐"))
        self.assertIsNone(parse("你好", "room-1"))
        self.assertIsNone(parse("艾特张三说你好", ""))  # 私聊不识别
        self.assertIsNone(parse("今天@张三 一起吃饭吗", "room-1"))  # 只是提及，不是指令

    def test_parse_mention_request_supports_bare_and_prefixed_forms(self):
        parse = self._require_callable("_parse_mention_request")
        # 群里指挥转达：以 @名字 开头，不再追问是哪个群
        self.assertEqual(parse("@张三 你好", "room-1"), ("张三", "你好"))
        self.assertEqual(parse("@张三", "room-1"), ("张三", ""))
        self.assertEqual(parse("帮我在群里艾特张三", "room-1"), ("张三", ""))
        self.assertEqual(parse("帮我艾特张三：晚上聚餐", "room-1"), ("张三", "晚上聚餐"))
        self.assertEqual(parse("艾特王五发消息", "room-1"), ("王五", ""))
        # 元问题（问艾特功能怎么用）不算指令
        self.assertIsNone(parse("艾特功能怎么用", "room-1"))
        self.assertIsNone(parse("介绍一下艾特功能", "room-1"))
        self.assertIsNone(parse("你好@张三 一起吃饭吗", "room-1"))

    def test_group_memory_learns_aliases_and_resolves_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = os.path.join(tmp, "group_memory.json")
            with mock.patch.object(app, "GROUP_MEMORY_FILE", memory):
                app.remember_sender_in_room("room-1", "测试群", "member-a", "王小明")
                app.remember_sender_in_room("room-1", "测试群", "member-b", "老李")
                # 精确匹配
                self.assertEqual(app.resolve_member_name("room-1", "王小明"), ("member-a", "王小明"))
                # 外号/别名：把输入称呼记为别名后可直接命中
                app.remember_alias_for_member("room-1", "member-a", "小明")
                self.assertEqual(app.resolve_member_name("room-1", "小明"), ("member-a", "小明"))
                # 模糊纠错（错别字/多字少字）
                resolved = app.resolve_member_name("room-1", "王小鸣")
                self.assertIsNotNone(resolved)
                self.assertEqual(resolved[0], "member-a")
                # 未知称呼解析不到
                self.assertIsNone(app.resolve_member_name("room-1", "张三"))
                # 长期记忆落盘
                with open(memory, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data["rooms"]["room-1"]["room_name"], "测试群")
                self.assertEqual(data["rooms"]["room-1"]["members"]["member-a"]["names"], ["王小明", "小明"])

    def test_do_mention_uses_canonical_name_and_learns_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = os.path.join(tmp, "group_memory.json")
            with mock.patch.object(app, "GROUP_MEMORY_FILE", memory), \
                 mock.patch.object(app, "is_allowed", return_value=True), \
                 mock.patch.object(app, "bot_send",
                                   return_value=(True, '{"success":true}')) as send:
                app.remember_sender_in_room("room-1", "测试群", "member-a", "王小明")
                reply = app.do_mention("小明", "你好", "wx-user", "用户", "room-1", "测试群")
                self.assertIn("已替你在群里艾特 王小明", reply)
                send.assert_called_once_with("测试群", "@王小明 你好", is_room=True)
                # 输入称呼被记入长期记忆
                self.assertEqual(app.resolve_member_name("room-1", "小明"), ("member-a", "小明"))

    def test_do_mention_sends_group_message_and_rejects_private(self):
        with mock.patch.object(app, "is_allowed", return_value=True), \
             mock.patch.object(app, "bot_send",
                               return_value=(True, '{"success":true}')) as send:
            reply = app.do_mention("张三", "你好", "wx-user", "用户", "room-1", "测试群")
            private_reply = app.do_mention("张三", "你好", "wx-user", "用户", "", "")
            empty_reply = app.do_mention("张三", "", "wx-user", "用户", "room-1", "测试群")
        self.assertIn("已替你在群里艾特 张三", reply)
        self.assertIn("只能在群里使用", private_reply)
        self.assertIn("请带上要发送的内容", empty_reply)
        send.assert_called_once_with("测试群", "@张三 你好", is_room=True)

    def test_do_mention_requires_permission(self):
        with mock.patch.object(app, "is_allowed", return_value=False), \
             mock.patch.object(app, "bot_send", side_effect=AssertionError("不应发送")):
            reply = app.do_mention("张三", "你好", "wx-user", "用户", "room-1", "测试群")
        self.assertEqual(reply, app.AI_NO_PERMISSION_MSG)

    def test_stats_toggle_natural_language_and_persistence(self):
        parse = self._require_callable("_parse_stats_toggle_request")
        self.assertIs(parse("隐藏上下文统计"), False)
        self.assertIs(parse("关闭上下文统计"), False)
        self.assertIs(parse("不要显示token统计"), False)
        self.assertIs(parse("显示上下文统计"), True)
        self.assertIs(parse("开启上下文统计"), True)
        self.assertIs(parse("你好"), None)
        self.assertIs(parse("什么是上下文统计"), None)
        with tempfile.TemporaryDirectory() as tmp:
            prefs = os.path.join(tmp, "agent_prefs.json")
            with mock.patch.object(app, "AGENT_PREFS_FILE", prefs):
                self.assertTrue(app.stats_visible("group:room-1"))
                app.set_stats_visible("group:room-1", False)
                self.assertFalse(app.stats_visible("group:room-1"))
                self.assertTrue(app.stats_visible("wx-user"))  # 场景隔离
                app.set_stats_visible("group:room-1", True)
                self.assertTrue(app.stats_visible("group:room-1"))

    def test_smart_fallback_hides_stats_by_natural_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs = os.path.join(tmp, "agent_prefs.json")
            with mock.patch.object(app, "AGENT_PREFS_FILE", prefs), \
                 mock.patch.object(app, "is_allowed", return_value=True):
                reply = app.smart_fallback("隐藏上下文统计", "wx-user", "用户", "", "", {"smart": True})
                self.assertIn("已隐藏", reply)
                self.assertFalse(app.stats_visible("wx-user"))

    def test_handle_command_stats_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs = os.path.join(tmp, "agent_prefs.json")
            with mock.patch.object(app, "AGENT_PREFS_FILE", prefs):
                on = app.handle_command("/统计 开", "wx-user", "用户", "", "", app.load_config())
                off = app.handle_command("/统计 关", "wx-user", "用户", "", "", app.load_config())
                self.assertIn("已开启", on)
                self.assertIn("已隐藏", off)
                self.assertFalse(app.stats_visible("wx-user"))

    def test_ai_answer_omits_stats_when_hidden(self):
        cfg = {"enabled": True, "base_url": "http://127.0.0.1:1/v1", "api_key": "k"}
        with mock.patch.object(app, "active_agent", return_value="openclaw"), \
             mock.patch.object(app, "openclaw_config", return_value=cfg), \
             mock.patch.object(app, "openclaw_active_key", return_value="wx-user"), \
             mock.patch.object(app, "openclaw_chat", return_value="回答内容"), \
             mock.patch.object(app, "stats_visible", return_value=False), \
             mock.patch.object(app, "_with_context_status",
                               side_effect=AssertionError("不应附加统计")):
            reply = app.ai_answer("问题", session_id="wx-user")
        self.assertEqual(reply, "回答内容")

    def test_receive_pipeline_sends_group_mention(self):
        source = {
            "room": {"id": "@@room", "payload": {"topic": "测试群"}},
            "from": {"payload": {"id": "@user", "name": "Z"}},
            "to": {"payload": {"name": "kindle"}},
        }
        fields = {
            "type": (None, b"text"),
            "content": (None, "艾特张三说你好".encode("utf-8")),
            "source": (None, json.dumps(source, ensure_ascii=False).encode("utf-8")),
            "isMentioned": (None, b"1"),
            "isMsgFromSelf": (None, b"0"),
            "isSystemEvent": (None, b"0"),
        }

        class _Receiver:
            def _json(self, value):
                self.result = value

        receiver = _Receiver()
        with tempfile.TemporaryDirectory() as tmp:
            memory = os.path.join(tmp, "group_memory.json")
            with mock.patch.object(app, "GROUP_MEMORY_FILE", memory), \
                 mock.patch.object(app, "identity_user_id", return_value="stable-user", create=True), \
                 mock.patch.object(app, "record_user"), \
                 mock.patch.object(app, "load_config", return_value={"auto_reply": True, "smart": True}), \
                 mock.patch.object(app, "handle_pending_reply", return_value=None), \
                 mock.patch.object(app, "handle_wizard", return_value=None), \
                 mock.patch.object(app, "is_allowed", return_value=True), \
                 mock.patch.object(app, "bot_send", return_value=(True, '{"success":true}')) as send, \
                 mock.patch.object(app, "save_record") as save:
                app.Handler._on_receive(receiver, fields)
                send.assert_called_once_with("测试群", "@张三 你好", is_room=True)
                data = receiver.result.get("data") or {}
                self.assertIn("已替你在群里艾特 张三", data.get("content", ""))
                rec = save.call_args.args[0]
                self.assertEqual(rec["roomId"], "@@room")
                self.assertEqual(rec["reply"], "✅ 已替你在群里艾特 张三：你好")
                # 群成员称呼被记入长期记忆
                self.assertEqual(app.resolve_member_name("@@room", "Z"), ("stable-user", "Z"))

    def test_group_slash_command_works_without_mention(self):
        source = {
            "room": {"id": "@@room", "payload": {"topic": "测试群"}},
            "from": {"payload": {"id": "@user", "name": "Z"}},
            "to": {"payload": {"name": "kindle"}},
        }
        fields = {
            "type": (None, b"text"),
            "content": (None, "/说明".encode("utf-8")),
            "source": (None, json.dumps(source, ensure_ascii=False).encode("utf-8")),
            "isMentioned": (None, b"0"),
            "isMsgFromSelf": (None, b"0"),
            "isSystemEvent": (None, b"0"),
        }

        class _Receiver:
            def _json(self, value):
                self.result = value

        receiver = _Receiver()
        with tempfile.TemporaryDirectory() as tmp:
            memory = os.path.join(tmp, "group_memory.json")
            with mock.patch.object(app, "GROUP_MEMORY_FILE", memory), \
                 mock.patch.object(app, "identity_user_id", return_value="stable-user", create=True), \
                 mock.patch.object(app, "record_user"), \
                 mock.patch.object(app, "load_config", return_value={"auto_reply": True, "smart": True}), \
                 mock.patch.object(app, "handle_pending_reply", return_value=None), \
                 mock.patch.object(app, "handle_wizard", return_value=None), \
                 mock.patch.object(app, "save_record"):
                app.Handler._on_receive(receiver, fields)
        data = receiver.result.get("data") or {}
        self.assertIn("功能说明", data.get("content", ""))

    def test_handle_recall_notice_reveals_cached_content(self):
        handle = self._require_callable("handle_recall_notice")
        app._recent.append({
            "roomId": "room-1", "fromId": "member-a", "from": "张三",
            "content": "明天下午三点开会", "reply": "好的",
        })
        try:
            reply = handle("张三撤回了一条消息", "member-a", "张三", "room-1")
            reply_no_from = handle("撤回了一条消息", "", "系统", "room-1")
            reply_empty = handle("你好", "member-a", "张三", "room-1")
        finally:
            app._recent.clear()
        self.assertIn("明天下午三点开会", reply)
        self.assertIn("撤回没用", reply)
        self.assertIn("明天下午三点开会", reply_no_from)
        self.assertIsNone(reply_empty)

    def test_collect_add_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            collects = os.path.join(tmp, "collects.json")
            with mock.patch.object(app, "COLLECTS_FILE", collects), \
                 mock.patch.object(app, "GROUP_MEMORY_FILE", os.path.join(tmp, "g.json")):
                item = app.collect_add("payee-a", "我", "room-1", "测试群", "张三", "100", "买奶茶")
                summary = app.collect_summary("payee-a")
                none_owed = app.collect_summary("other-user")
        self.assertEqual(item["payer_name"], "张三")
        self.assertEqual(item["amount"], 100.0)
        self.assertIn("张三 欠你 100 元", summary)
        self.assertIn("还没有人欠你钱", none_owed)

    def test_collect_command_and_urge(self):
        with tempfile.TemporaryDirectory() as tmp:
            collects = os.path.join(tmp, "collects.json")
            with mock.patch.object(app, "COLLECTS_FILE", collects), \
                 mock.patch.object(app, "GROUP_MEMORY_FILE", os.path.join(tmp, "g.json")), \
                 mock.patch.object(app, "bot_send",
                                   return_value=(True, '{"success":true}')) as send:
                recorded = app.handle_command(
                    "/收款 100 张三 买奶茶", "payee-a", "我", "room-1", "测试群", app.load_config())
                urged = app.handle_command(
                    "/催收 张三", "payee-a", "我", "room-1", "测试群", app.load_config())
            self.assertIn("已登记", recorded)
            self.assertIn("已在群里提醒 张三", urged)
            send.assert_called_once_with(
                "测试群", "@张三 你欠 我 100 元，该还啦！（买奶茶）", is_room=True)

    def test_call_command_replies_web_protocol_limit(self):
        reply = app.handle_command(
            "/打电话 张三", "wx-user", "用户", "room-1", "测试群", app.load_config())
        private = app.handle_command(
            "/打电话 张三", "wx-user", "用户", "", "", app.load_config())
        self.assertIn("网页版微信协议不支持", reply)
        self.assertIn("只能在群里", private)

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
        self.assertIn("模型提供商配置", page)
        self.assertIn("id=\"claw-gateway\"", page)
        self.assertIn("id=\"claw-provider-url\"", page)
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
                 mock.patch.object(app, "openclaw_apply_provider",
                                   return_value="已保存并应用（OpenClaw 已重启）") as apply_provider, \
                 mock.patch.object(app, "openclaw_gateway_token",
                                   return_value="gw-token", create=True), \
                 mock.patch.object(app, "valid_session", return_value=True):
                get_status, current = self._public_api_request("GET", "/api/openclaw/config")
                post_status, payload = self._public_api_request("POST", "/api/openclaw/config", {
                    "enabled": True,
                    "provider_id": "deepseek",
                    "provider_url": "https://api.deepseek.com/v1",
                    "api_key": "sk-provider-key",
                    "model_id": "deepseek-v4-flash",
                })
                saved = app.load_config()

        self.assertEqual(get_status, 200)
        self.assertNotIn("old-token", json.dumps(current))
        self.assertEqual(post_status, 200)
        self.assertTrue(payload.get("success"))
        apply_provider.assert_called_once_with(
            provider_url="https://api.deepseek.com/v1",
            api_key="sk-provider-key",
            model_id="deepseek-v4-flash",
            provider_id="deepseek",
        )
        self.assertEqual(saved["openclaw"]["base_url"], app.OPENCLAW_GATEWAY_BASE_URL)
        self.assertEqual(saved["openclaw"]["model"], "openclaw:wxbot")
        self.assertEqual(saved["openclaw"]["api_key"], "gw-token")
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
        with mock.patch.object(app, "openclaw_config", return_value={}):
            with self.assertRaisesRegex(ValueError, "OpenClaw 未配置"):
                app.ai_answer("问题", session_id="wx-user-2")

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

    def test_transcript_parser_marks_injected_compaction_summary(self):
        parse_transcript = self._require_callable("openclaw_parse_transcript")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            self._write_jsonl(path, [
                {"type": "message", "timestamp": "2026-08-07T11:00:00Z",
                 "message": {"role": "assistant", "content": [
                     {"type": "text", "text": "[compaction-summary]\n\n【历史对话压缩摘要】用户喜欢蓝色。"}]}},
                {"type": "message", "timestamp": "2026-08-07T11:00:01Z",
                 "message": {"role": "user", "content": [{"type": "text", "text": "我喜欢的颜色"}]}},
            ])
            records = parse_transcript(path)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["type"], "compaction")
        self.assertIn("用户喜欢蓝色", records[0]["summary"])
        self.assertEqual(records[1]["role"], "user")

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
                "（上下文 6.8k / 128k，5.3%；输入 4.9k / 输出 2k；缓存命中 1792 / 未命中 3088）",
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

    def test_openclaw_fetch_provider_models_returns_ids(self):
        fetch = self._require_callable("openclaw_fetch_provider_models")
        server = HTTPServer(("127.0.0.1", 0), _ModelsJsonHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            ids = fetch("http://127.0.0.1:{}/v1".format(server.server_port),
                        "sk-test", timeout=2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(ids, ["deepseek-v4-flash", "deepseek-v4-pro"])

    def test_openclaw_apply_provider_writes_config_and_restarts(self):
        apply_provider = self._require_callable("openclaw_apply_provider")
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "openclaw.json")
            self._write_json(config_path, {
                "models": {"providers": {"deepseek": {
                    "api": "openai-completions",
                    "baseUrl": "https://api.deepseek.com/v1",
                    "apiKey": "old-key",
                    "models": [{"id": "deepseek-v4-flash"}],
                }}},
                "agents": {
                    "defaults": {"model": {"primary": "deepseek/deepseek-v4-flash"}},
                    "list": [{"id": "wxbot", "model": "deepseek/deepseek-v4-flash"}],
                },
            })
            with mock.patch.object(app, "OPENCLAW_CONFIG_FILE", config_path, create=True), \
                 mock.patch.object(app, "openclaw_restart_gateway",
                                   return_value=True) as restart:
                msg = apply_provider(
                    provider_url="https://api.deepseek.com/v1",
                    api_key="new-key",
                    model_id="deepseek-v4-pro",
                    provider_id="deepseek",
                )
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)

        self.assertIn("已保存并应用", msg)
        restart.assert_called_once()
        deepseek = cfg["models"]["providers"]["deepseek"]
        self.assertEqual(deepseek["baseUrl"], "https://api.deepseek.com/v1")
        self.assertEqual(deepseek["apiKey"], "new-key")
        ids = [m["id"] for m in deepseek["models"]]
        self.assertIn("deepseek-v4-flash", ids)
        self.assertIn("deepseek-v4-pro", ids)
        self.assertEqual(cfg["agents"]["list"][0]["model"], "deepseek/deepseek-v4-pro")
        self.assertEqual(cfg["agents"]["defaults"]["model"]["primary"],
                         "deepseek/deepseek-v4-pro")

    def test_openclaw_apply_provider_keeps_existing_selected_model(self):
        apply_provider = self._require_callable("openclaw_apply_provider")
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "openclaw.json")
            self._write_json(config_path, {
                "models": {"providers": {"deepseek": {
                    "baseUrl": "https://api.deepseek.com/v1",
                    "apiKey": "key",
                    "models": [{"id": "deepseek-v4-flash"}],
                }}},
                "agents": {"list": [{"id": "wxbot", "model": "deepseek/deepseek-v4-flash"}]},
            })
            with mock.patch.object(app, "OPENCLAW_CONFIG_FILE", config_path, create=True), \
                 mock.patch.object(app, "openclaw_restart_gateway", return_value=True):
                apply_provider(
                    provider_url="https://api.deepseek.com/v1",
                    api_key="key",
                    model_id="deepseek-v4-flash",
                    provider_id="deepseek",
                )
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)

        ids = [m["id"] for m in cfg["models"]["providers"]["deepseek"]["models"]]
        self.assertEqual(ids, ["deepseek-v4-flash"])

    def test_public_openclaw_config_returns_provider_info_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "openclaw.json")
            self._write_json(config_path, {
                "gateway": {"auth": {"token": "gw-token"}},
                "models": {"providers": {"deepseek": {
                    "apiKey": "sk-secret",
                    "baseUrl": "https://api.deepseek.com/v1",
                    "models": [{"id": "deepseek-v4-flash"}, {"id": "deepseek-v4-pro"}],
                }}},
                "agents": {"list": [{"id": "wxbot", "model": "deepseek/deepseek-v4-flash"}]},
            })
            with mock.patch.object(app, "OPENCLAW_CONFIG_FILE", config_path, create=True):
                info = app.public_openclaw_config({"enabled": True})

        self.assertEqual(info["gateway_url"], app.OPENCLAW_GATEWAY_BASE_URL)
        self.assertTrue(info["gateway_ok"])
        self.assertEqual(info["provider_url"], "https://api.deepseek.com/v1")
        self.assertEqual(info["model_id"], "deepseek-v4-flash")
        self.assertEqual(info["current_model"], "deepseek/deepseek-v4-flash")
        self.assertEqual(info["models"], ["deepseek-v4-flash", "deepseek-v4-pro"])
        self.assertNotIn("sk-secret", json.dumps(info))

    def test_openclaw_config_defaults_to_local_gateway(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            self._write_json(config_path, {"openclaw": {"enabled": True}})
            with mock.patch.object(app, "CONFIG_FILE", config_path), \
                 mock.patch.object(app, "openclaw_gateway_token",
                                   return_value="gw-token", create=True):
                cfg = app.openclaw_config()

        self.assertEqual(cfg["base_url"], "http://127.0.0.1:18788/v1")
        self.assertEqual(cfg["model"], "openclaw:wxbot")
        self.assertEqual(cfg["api_key"], "gw-token")

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

    def test_ai_answer_returns_openclaw_reply_directly_without_fallback(self):
        ai_answer = self._require_callable("ai_answer")
        with mock.patch.object(
            app, "openclaw_config",
            return_value={"enabled": True, "base_url": "http://127.0.0.1:18788/v1",
                          "api_key": "k", "model": "openclaw:wxbot"},
        ), mock.patch.object(app, "openclaw_active_key", return_value="wx-user"), \
             mock.patch.object(
                 app, "openclaw_chat",
                 return_value="我没看到官方公告，建议你自己去查。",
             ) as chat:
            result = ai_answer("王者明天抽奖活动")

        # 原样返回网关回答，不做本地搜索、不做任何改写
        self.assertIn("官方公告", result)
        self.assertNotIn("本地搜索", result)
        self.assertNotIn("搜索结果", result)
        chat.assert_called_once()
        self.assertEqual(chat.call_args.args[0], "王者明天抽奖活动")

    def test_ai_answer_propagates_gateway_failure_without_fallback(self):
        ai_answer = self._require_callable("ai_answer")
        with mock.patch.object(
            app, "openclaw_config",
            return_value={"enabled": True, "base_url": "http://127.0.0.1:18788/v1",
                          "api_key": "k", "model": "openclaw:wxbot"},
        ), mock.patch.object(app, "openclaw_active_key", return_value="wx-user"), \
             mock.patch.object(
                 app, "openclaw_chat", side_effect=TimeoutError("timed out"),
             ) as chat:
            with self.assertRaises(TimeoutError):
                ai_answer("王者明天抽奖活动")

        chat.assert_called_once()

    def test_ai_answer_retries_once_when_reply_is_toolcall_text(self):
        ai_answer = self._require_callable("ai_answer")
        toolcall_text = (
            '<tool_calls>\n<invoke name="exec">\n'
            '<parameter name="command" string="true">python3 -c "print(1)"</parameter>\n'
            '</invoke>\n</tool_calls>'
        )
        with mock.patch.object(
            app, "openclaw_config",
            return_value={"enabled": True, "base_url": "http://127.0.0.1:18788/v1",
                          "api_key": "k", "model": "openclaw:wxbot"},
        ), mock.patch.object(app, "openclaw_active_key", return_value="wx-user"), \
             mock.patch.object(
                 app, "openclaw_chat",
                 side_effect=[toolcall_text, "直接回答内容"],
             ) as chat:
            result = ai_answer("问题", session_id="wx-user")

        self.assertEqual(result, "直接回答内容")
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(chat.call_args_list[0].kwargs["session_id"], "wx-user")
        self.assertEqual(chat.call_args_list[1].kwargs["session_id"], "wx-user")
        self.assertIn("不要输出工具调用标记", chat.call_args_list[1].kwargs["system_prompt"])
        self.assertIn("请重新回答", chat.call_args_list[1].args[0])

    def test_ai_answer_passes_prompt_directly_without_search_injection(self):
        ai_answer = self._require_callable("ai_answer")
        with mock.patch.object(
            app, "openclaw_config",
            return_value={"enabled": True, "base_url": "http://127.0.0.1:18788/v1",
                          "api_key": "k", "model": "openclaw:wxbot"},
        ), mock.patch.object(app, "openclaw_active_key", return_value="wx-user"), \
             mock.patch.object(app, "openclaw_chat", return_value="明天是夏日农友节") as chat:
            result = ai_answer("王者明天抽奖活动", session_id="wx-user")

        self.assertEqual(result, "明天是夏日农友节")
        chat.assert_called_once()
        # 原样透传用户问题，由 OpenClaw agent 自己决定是否联网搜索，微信侧不做搜索注入
        self.assertEqual(chat.call_args.args[0], "王者明天抽奖活动")
        self.assertNotIn("[网页搜索结果]", chat.call_args.args[0])

    # ---------------- 智能体切换 / Codex 后端 ----------------
    def test_parse_codex_jsonl_extracts_thread_reply_usage(self):
        parse = self._require_callable("_parse_codex_jsonl")
        text = (
            '{"type":"thread.started","thread_id":"019f-abc"}\n'
            '{"type":"turn.started"}\n'
            '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"你好"}}\n'
            '{"type":"item.completed","item":{"id":"i2","type":"agent_message","text":"我是Codex"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":60,"output_tokens":20}}\n'
        )
        thread_id, messages, errors, usage = parse(text)
        self.assertEqual(thread_id, "019f-abc")
        self.assertEqual(messages, ["你好", "我是Codex"])
        self.assertEqual(errors, [])
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["cached_input_tokens"], 60)

    def test_strip_bot_mentions_removes_mention_anywhere(self):
        strip = self._require_callable("strip_bot_mentions")
        self.assertEqual(strip("用个websearch 这对么 还都命中了@kindle", "kindle"),
                         "用个websearch 这对么 还都命中了")
        self.assertEqual(strip("@kindle 你好", "kindle"), "你好")
        self.assertEqual(strip("你好@kindle 啊", "kindle"), "你好 啊")
        self.assertEqual(strip("没有任何提及", "kindle"), "没有任何提及")
        self.assertEqual(strip("结尾提一下@THk", "kindle"), "结尾提一下")

    def test_codex_answer_first_turn_creates_thread_then_resumes(self):
        codex_answer = self._require_callable("codex_answer")
        calls = []
        outputs = [
            '{"type":"thread.started","thread_id":"019f-new"}\n'
            '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"第一轮回答"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":10}}\n',
            '{"type":"thread.started","thread_id":"019f-new"}\n'
            '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"第二轮回答"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":200,"cached_input_tokens":80,"output_tokens":20}}\n',
        ]

        class _Proc:
            returncode = 0

            def __init__(self, out):
                self.stdout = out
                self.stderr = ""

        def fake_run(cmd, **kwargs):
            calls.append((list(cmd), kwargs))
            return _Proc(outputs[len(calls) - 1])

        stats_seq = [
            {"persistent": 100, "call_input": 100, "cached": 0, "output": 10, "seen_line": 5, "thread": "019f-new"},
            {"persistent": 200, "call_input": 200, "cached": 80, "output": 20, "seen_line": 9, "thread": "019f-new"},
        ]

        def fake_record(user_id, thread_id):
            stats = stats_seq.pop(0)
            app._store_codex_usage(user_id, stats)
            return stats

        with mock.patch.object(app, "shutil") as sh, \
             mock.patch.object(app, "subprocess") as sp, \
             mock.patch.object(app, "codex_record_turn", side_effect=fake_record) as record:
            sh.which.return_value = "/usr/bin/codex"
            sp.run.side_effect = fake_run
            prefs_tmp = app.AGENT_PREFS_FILE
            app.AGENT_PREFS_FILE = os.path.join(tempfile.mkdtemp(), "prefs.json")
            try:
                r1 = codex_answer("你好", session_id="wx-user")
                self.assertIn("第一轮回答", r1)
                self.assertIn("Codex 智能体", r1)
                first_cmd = calls[0][0]
                self.assertIn("exec", first_cmd)
                self.assertNotIn("resume", first_cmd)
                self.assertIn("019f-new", app.codex_thread_for("wx-user"))

                r2 = codex_answer("再聊", session_id="wx-user")
                self.assertIn("第二轮回答", r2)
                self.assertIn("200", r2)
                self.assertNotIn("300", r2)
                second_cmd = calls[1][0]
                self.assertIn("resume", second_cmd)
                self.assertEqual(second_cmd[second_cmd.index("resume") + 1], "019f-new")
                usage = app.codex_usage_for("wx-user")
                self.assertEqual(usage["input"], 200)
                self.assertEqual(usage["cached"], 80)
                self.assertEqual(usage["call_input"], 200)
                self.assertEqual(record.call_count, 2)
            finally:
                app.AGENT_PREFS_FILE = prefs_tmp

    def test_codex_record_turn_tracks_persistent_context(self):
        record = self._require_callable("codex_record_turn")
        with tempfile.TemporaryDirectory() as tmp:
            sessions_root = os.path.join(tmp, "sessions")
            day_dir = os.path.join(sessions_root, "2026", "08", "08")
            os.makedirs(day_dir)
            thread_file = os.path.join(day_dir, "rollout-2026-08-08T00-00-00-019f-test.jsonl")
            prefs_tmp = app.AGENT_PREFS_FILE
            app.AGENT_PREFS_FILE = os.path.join(tmp, "prefs.json")
            old_cfg = app.CODEX_CONFIG_FILE
            app.CODEX_CONFIG_FILE = os.path.join(tmp, "config.toml")
            try:
                def append_jsonl(path, records):
                    with open(path, "a", encoding="utf-8") as handle:
                        for record in records:
                            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

                app.set_codex_thread("wx-user", "019f-test")
                # 第 1 轮：无工具，input 即对话上下文
                self._write_jsonl(thread_file, [
                    {"type": "event_msg", "payload": {"type": "user_message", "message": "你好"}},
                    {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 10254, "cached_input_tokens": 10000, "output_tokens": 80}}}},
                ])
                stats = record("wx-user", "019f-test")
                self.assertEqual(stats["persistent"], 10254)
                self.assertEqual(stats["call_input"], 10254)
                # 第 2 轮：web 搜索，单条 token_count 含瞬时搜索结果 -> 估算增量，单调不减
                append_jsonl(thread_file, [
                    {"type": "event_msg", "payload": {"type": "user_message", "message": "搜索一下"}},
                    {"type": "event_msg", "payload": {"type": "web_search_call", "action": {"type": "search", "queries": ["x"]}}},
                    {"type": "event_msg", "payload": {"type": "web_search_end", "action": {"type": "search", "queries": ["x"]}}},
                    {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 36000, "cached_input_tokens": 34000, "output_tokens": 600}}}},
                ])
                stats2 = record("wx-user", "019f-test")
                self.assertEqual(stats2["call_input"], 36000)
                self.assertTrue(stats2["estimated"])
                self.assertGreaterEqual(stats2["persistent"], stats["persistent"])
                self.assertLess(stats2["persistent"], 36000)
                # 第 3 轮：多次模型调用（工具），首条 input 即工具前的干净上下文
                append_jsonl(thread_file, [
                    {"type": "event_msg", "payload": {"type": "function_call", "id": "f1"}},
                    {"type": "event_msg", "payload": {"type": "function_call_output", "output": "ok"}},
                    {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 15000, "cached_input_tokens": 14800, "output_tokens": 100}}}},
                    {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 15600, "cached_input_tokens": 15400, "output_tokens": 300}}}},
                ])
                stats3 = record("wx-user", "019f-test")
                self.assertEqual(stats3["persistent"], 15000)
                self.assertEqual(stats3["call_input"], 15600)
                self.assertEqual(stats3["output"], 300)
                self.assertFalse(stats3["estimated"])
                saved = app.codex_usage_for("wx-user")
                self.assertEqual(saved["thread"], "019f-test")
                self.assertEqual(saved["seen_line"], 9)
            finally:
                app.AGENT_PREFS_FILE = prefs_tmp
                app.CODEX_CONFIG_FILE = old_cfg

    def test_codex_record_turn_resets_when_thread_changes(self):
        record = self._require_callable("codex_record_turn")
        with tempfile.TemporaryDirectory() as tmp:
            sessions_root = os.path.join(tmp, "sessions")
            day_dir = os.path.join(sessions_root, "2026", "08", "08")
            os.makedirs(day_dir)
            prefs_tmp = app.AGENT_PREFS_FILE
            app.AGENT_PREFS_FILE = os.path.join(tmp, "prefs.json")
            old_cfg = app.CODEX_CONFIG_FILE
            app.CODEX_CONFIG_FILE = os.path.join(tmp, "config.toml")
            try:
                old_file = os.path.join(day_dir, "rollout-2026-08-08T00-00-00-019f-old.jsonl")
                self._write_jsonl(old_file, [
                    {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 50000, "output_tokens": 100}}}},
                ])
                app.set_codex_thread("wx-user", "019f-old")
                stats = record("wx-user", "019f-old")
                self.assertEqual(stats["persistent"], 50000)
                # 新线程（如压缩后）：上下文重置，不再沿用旧线程
                new_file = os.path.join(day_dir, "rollout-2026-08-08T00-00-00-019f-new.jsonl")
                self._write_jsonl(new_file, [
                    {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 3000, "output_tokens": 50}}}},
                ])
                app.set_codex_thread("wx-user", "019f-new")
                stats2 = record("wx-user", "019f-new")
                self.assertEqual(stats2["persistent"], 3000)
            finally:
                app.AGENT_PREFS_FILE = prefs_tmp
                app.CODEX_CONFIG_FILE = old_cfg

    def test_codex_answer_fails_when_cli_missing(self):
        codex_answer = self._require_callable("codex_answer")
        with mock.patch.object(app, "shutil") as sh:
            sh.which.return_value = None
            with self.assertRaises(ValueError) as ctx:
                codex_answer("你好", session_id="wx-user")
        self.assertIn("未部署", str(ctx.exception))
    def test_codex_answer_restarts_thread_when_resume_thread_missing(self):
        # 旧 thread 已被清理时：resume 失败自动开新线程，不中断对话。
        codex_answer = self._require_callable("codex_answer")
        calls = []

        class _Proc:
            def __init__(self, returncode, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        outputs = [
            _Proc(returncode=1, stderr="Error: thread/resume: thread/resume failed: "
                  "no rollout found for thread id 019f-dead (code -32600)", stdout=""),
            '{"type":"thread.started","thread_id":"019f-fresh"}\n'
            '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"新会话回答"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":10}}\n',
        ]

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            out = outputs[len(calls) - 1]
            if isinstance(out, _Proc):
                return out
            return _Proc(0, stdout=out)

        with mock.patch.object(app, "shutil") as sh, \
             mock.patch.object(app, "subprocess") as sp:
            sh.which.return_value = "/usr/bin/codex"
            sp.run.side_effect = fake_run
            prefs_tmp = app.AGENT_PREFS_FILE
            app.AGENT_PREFS_FILE = os.path.join(tempfile.mkdtemp(), "prefs.json")
            try:
                app.set_codex_thread("wx-user", "019f-dead")
                result = codex_answer("你好", session_id="wx-user")
                self.assertIn("新会话回答", result)
                self.assertIn("resume", calls[0])
                self.assertNotIn("resume", calls[1])
                self.assertEqual(app.codex_thread_for("wx-user"), "019f-fresh")
            finally:
                app.AGENT_PREFS_FILE = prefs_tmp


    def test_agent_switch_commands_and_natural_language(self):
        handle = self._require_callable("handle_agent_switch")
        parse = self._require_callable("_parse_agent_switch_request")
        prefs_tmp = app.AGENT_PREFS_FILE
        app.AGENT_PREFS_FILE = os.path.join(tempfile.mkdtemp(), "prefs.json")
        try:
            app.set_user_agent("wx-user", "")
            self.assertEqual(parse("切换到codex"), "codex")
            self.assertEqual(parse("用 openclaw"), "openclaw")
            self.assertEqual(parse("切换智能体"), "")
            self.assertEqual(parse("恢复默认智能体"), "默认")
            self.assertIsNone(parse("你好"))
            self.assertIsNone(parse("今天天气怎么样"))

            reply = handle("codex", "wx-user")
            self.assertIn("Codex", reply)
            self.assertEqual(app.active_agent("wx-user"), "codex")
            self.assertEqual(app.user_agent("wx-user"), "codex")

            reply = handle("", "wx-user")
            self.assertIn("当前智能体", reply)

            reply = handle("默认", "wx-user")
            self.assertIn("默认", reply)
            self.assertEqual(app.user_agent("wx-user"), "")
        finally:
            app.AGENT_PREFS_FILE = prefs_tmp

    def test_ai_answer_routes_to_codex_when_selected(self):
        ai_answer = self._require_callable("ai_answer")
        with mock.patch.object(app, "active_agent", return_value="codex") as active, \
             mock.patch.object(app, "codex_answer", return_value="Codex 回答") as codex:
            reply = ai_answer("问题", session_id="wx-user")
        self.assertEqual(reply, "Codex 回答")
        codex.assert_called_once_with("问题", "wx-user")
        active.assert_called_once_with("wx-user")

    def test_codex_apply_provider_writes_config_and_logs_in(self):
        apply = self._require_callable("codex_apply_provider")
        cfg_tmp = os.path.join(tempfile.mkdtemp(), "config.toml")
        old = app.CODEX_CONFIG_FILE
        app.CODEX_CONFIG_FILE = cfg_tmp
        try:
            with mock.patch.object(app, "shutil") as sh, \
                 mock.patch.object(app, "subprocess") as sp:
                sh.which.return_value = "/usr/bin/codex"
                sp.run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                message = apply("https://api.deepseek.com/", "sk-test", "deepseek-v4-flash")
            self.assertIn("已保存并应用", message)
            with open(cfg_tmp, encoding="utf-8") as f:
                text = f.read()
            self.assertIn('model = "deepseek-v4-flash"', text)
            self.assertIn('base_url = "https://api.deepseek.com/"', text)
            self.assertIn('wire_api = "responses"', text)
            login_cmd = sp.run.call_args.args[0]
            self.assertIn("login", login_cmd)
            self.assertIn("--with-api-key", login_cmd)
        finally:
            app.CODEX_CONFIG_FILE = old

    def test_admin_saves_codex_context_limit_and_default_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            self._write_json(config_path, {})
            with mock.patch.object(app, "CONFIG_FILE", config_path), \
                 mock.patch.object(app, "valid_session", return_value=True), \
                 mock.patch.object(app, "CODEX_CONFIG_FILE", os.path.join(tmp, "codex-config.toml")), \
                 mock.patch.object(app.shutil, "which", return_value="/usr/local/bin/codex"):
                status, payload = self._public_api_request("POST", "/api/agents", {
                    "default_agent": "codex",
                    "codex_context_limit": "1000000",
                })
                saved = app.load_config()
                get_status, current = self._public_api_request("GET", "/api/agents")

        self.assertEqual(status, 200)
        self.assertTrue(payload.get("success"))
        self.assertEqual(saved["default_agent"], "codex")
        self.assertEqual(saved["codex_context_limit"], 1000000)
        self.assertEqual(get_status, 200)
        self.assertEqual(current["codex"]["context_limit"], 1000000)

    def test_codex_context_status_uses_current_turn_usage(self):
        # 当前轮次用量优先：不累加多轮，格式与 OpenClaw 回复末尾一致。
        status = self._require_callable("codex_context_status")
        prefs_tmp = app.AGENT_PREFS_FILE
        app.AGENT_PREFS_FILE = os.path.join(tempfile.mkdtemp(), "prefs.json")
        try:
            app.add_codex_usage("wx-user", {"input_tokens": 12000, "cached_input_tokens": 8000, "output_tokens": 100})
            result = status("wx-user", {"persistent": 6000, "call_input": 6000, "cached": 4000, "output": 80})
            self.assertIn("Codex 智能体", result)
            self.assertIn("缓存命中 4000", result)
            self.assertIn("未命中 2000", result)
            self.assertIn("对话上下文 6.1k / 272k", result)
            self.assertIn("输入 6k / 输出 80", result)
            self.assertNotIn("12k", result)
            self.assertNotIn("本次调用输入", result)
            self.assertNotIn("约", result)
        finally:
            app.AGENT_PREFS_FILE = prefs_tmp

    def test_codex_context_status_falls_back_to_latest_turn_usage(self):
        # 没有本轮用量时回退到最近一次记录（覆盖语义，不再累加）。
        status = self._require_callable("codex_context_status")
        prefs_tmp = app.AGENT_PREFS_FILE
        app.AGENT_PREFS_FILE = os.path.join(tempfile.mkdtemp(), "prefs.json")
        try:
            app.add_codex_usage("wx-user", {"input_tokens": 12000, "cached_input_tokens": 8000, "output_tokens": 100})
            result = status("wx-user")
            self.assertIn("Codex 智能体", result)
            self.assertIn("缓存命中 8000", result)
            self.assertIn("输入 12k / 输出 100", result)
        finally:
            app.AGENT_PREFS_FILE = prefs_tmp

    def test_codex_context_status_respects_configured_limit(self):
        # 后台配置的上下文窗口（如 deepseek 的 1M）决定显示分母。
        status = self._require_callable("codex_context_status")
        prefs_tmp = app.AGENT_PREFS_FILE
        app.AGENT_PREFS_FILE = os.path.join(tempfile.mkdtemp(), "prefs.json")
        try:
            app.add_codex_usage("wx-user", {"input_tokens": 300000, "cached_input_tokens": 280000, "output_tokens": 100})
            with mock.patch.object(app, "load_config", return_value={"codex_context_limit": 1000000}):
                result = status("wx-user", {"persistent": 300000, "call_input": 300000, "cached": 280000, "output": 100})
            self.assertIn("300.1k / 1000k", result)
            self.assertIn("30.0%", result)
        finally:
            app.AGENT_PREFS_FILE = prefs_tmp

    def test_codex_context_status_appends_call_input_when_transient(self):
        # 搜索/工具轮：对话上下文不含瞬时内容，本次调用输入单独标注，避免误读为上下文缩小
        status = self._require_callable("codex_context_status")
        prefs_tmp = app.AGENT_PREFS_FILE
        app.AGENT_PREFS_FILE = os.path.join(tempfile.mkdtemp(), "prefs.json")
        try:
            result = status("wx-user", {"persistent": 12000, "call_input": 36000,
                                        "cached": 33000, "output": 800, "estimated": True})
            self.assertIn("对话上下文 约12.8k / 272k", result)
            self.assertIn("搜索轮估算", result)
            self.assertIn("本次调用输入 36k", result)
            self.assertIn("缓存命中 33000 / 未命中 3000", result)
        finally:
            app.AGENT_PREFS_FILE = prefs_tmp

    def test_compact_uses_gateway_native_compaction_in_place(self):
        compact = self._require_callable("openclaw_compact_session")
        item = {"_transcript": [
            {"role": "user", "content": "你好，记住我喜欢蓝色"},
            {"role": "assistant", "content": "好的，记住了。"},
            {"role": "user", "content": "我喜欢的颜色是什么"},
            {"role": "assistant", "content": "蓝色。"},
        ]}
        cfg = {"enabled": True, "base_url": "http://127.0.0.1:18788/v1", "api_key": "k"}
        with mock.patch.object(app, "openclaw_active_key", return_value="wx-user"), \
             mock.patch.object(app, "_openclaw_session_by_key", return_value=item), \
             mock.patch.object(app, "openclaw_config", return_value=cfg), \
             mock.patch.object(app, "openclaw_chat",
                               return_value="用户喜欢蓝色，明天去上海。") as chat, \
             mock.patch.object(app, "_openclaw_gateway_rpc",
                               side_effect=[{"ok": True, "compacted": True, "kept": 2},
                                            {"ok": True, "messageId": "injected-1"}]) as rpc:
            reply = compact("wx-user")

        self.assertIn("已压缩上下文", reply)
        self.assertEqual(chat.call_count, 1)
        self.assertTrue(chat.call_args_list[0].kwargs["session_id"].startswith("compose:wx-user:"))
        self.assertEqual(rpc.call_count, 2)
        compact_method, compact_params = rpc.call_args_list[0].args[:2]
        self.assertEqual(compact_method, "sessions.compact")
        self.assertEqual(compact_params, {"key": "agent:wxbot:openai-user:wx-user", "maxLines": 2})
        inject_method, inject_params = rpc.call_args_list[1].args[:2]
        self.assertEqual(inject_method, "chat.inject")
        self.assertEqual(inject_params["sessionKey"], "agent:wxbot:openai-user:wx-user")
        self.assertEqual(inject_params["label"], "compaction-summary")
        self.assertIn("用户喜欢蓝色，明天去上海。", inject_params["message"])
        self.assertIn("【历史对话压缩摘要】", inject_params["message"])
        with app.OPENCLAW_USAGE_LOCK:
            self.assertNotIn("wx-user", app._OPENCLAW_LAST_USAGE)

    def test_compact_fails_when_gateway_compact_fails(self):
        compact = self._require_callable("openclaw_compact_session")
        item = {"_transcript": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，有什么可以帮你？"},
        ]}
        cfg = {"enabled": True, "base_url": "http://127.0.0.1:18788/v1", "api_key": "k"}
        with mock.patch.object(app, "openclaw_active_key", return_value="wx-user"), \
             mock.patch.object(app, "_openclaw_session_by_key", return_value=item), \
             mock.patch.object(app, "openclaw_config", return_value=cfg), \
             mock.patch.object(app, "openclaw_chat", return_value="摘要内容"), \
             mock.patch.object(app, "_openclaw_gateway_rpc",
                               return_value={"ok": False, "reason": "boom"}):
            with self.assertRaises(ValueError):
                compact("wx-user")

    def test_gateway_rpc_parses_json_stdout(self):
        rpc = self._require_callable("_openclaw_gateway_rpc")
        with mock.patch.object(app, "openclaw_config",
                               return_value={"enabled": True, "api_key": "tok"}), \
             mock.patch.object(app.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{"ok": true, "kept": 2}\n'
            run.return_value.stderr = "Config warnings...\n"
            result = rpc("sessions.compact", {"key": "k", "maxLines": 2}, timeout=30)
        self.assertEqual(result, {"ok": True, "kept": 2})
        cmd = run.call_args_list[0].args[0]
        self.assertEqual(cmd[0:3], ["docker", "exec", "openclaw"])
        self.assertIn("sessions.compact", cmd)
        self.assertIn("--params", cmd)
        self.assertIn("ws://127.0.0.1:18789", cmd)
        self.assertIn("--json", cmd)

    def test_compact_skips_when_session_has_no_transcript(self):
        compact = self._require_callable("openclaw_compact_session")
        with mock.patch.object(app, "openclaw_active_key", return_value="wx-user"), \
             mock.patch.object(app, "_openclaw_session_by_key", return_value=None), \
             mock.patch.object(app, "openclaw_config",
                               return_value={"enabled": True, "base_url": "http://x/v1", "api_key": "k"}):
            reply = compact("wx-user")
        self.assertIn("暂时不需要压缩", reply)

    def test_looks_like_compact_request_detects_intent_not_questions(self):
        detect = self._require_callable("_looks_like_compact_request")
        self.assertTrue(detect("压缩一下上下文"))
        self.assertTrue(detect("帮我压缩上下文"))
        self.assertTrue(detect("把上下文压缩一下"))
        self.assertTrue(detect("帮我精简一下历史对话"))
        self.assertTrue(detect("压缩对话"))
        self.assertTrue(detect("压缩一下上下文，小猫，把它们吃掉，小猫为啥我就说了不到十个字，上下文怎么多了12k"))
        self.assertFalse(detect("什么是上下文压缩"))
        self.assertFalse(detect("介绍一下上下文压缩的原理"))
        self.assertFalse(detect("上下文压缩和开新会话有什么区别"))
        self.assertFalse(detect("压缩包怎么打开"))
        self.assertFalse(detect("今天天气怎么样"))

    def test_looks_like_new_session_request_detects_intent_not_questions(self):
        detect = self._require_callable("_looks_like_new_session_request")
        self.assertTrue(detect("开启新的会话"))
        self.assertTrue(detect("帮我开个新对话"))
        self.assertTrue(detect("重新开一个会话"))
        self.assertTrue(detect("新开一个对话"))
        self.assertFalse(detect("什么是新会话"))
        self.assertFalse(detect("新会话和旧会话有什么区别"))
        self.assertFalse(detect("上下文压缩和开新会话有什么区别"))
        self.assertFalse(detect("你好"))

    def test_smart_fallback_compacts_on_natural_language_request(self):
        with mock.patch.object(app, "is_allowed", return_value=True), \
             mock.patch.object(app, "openclaw_compact_session",
                               return_value="已压缩上下文。") as compact:
            reply = app.smart_fallback("压缩一下上下文", "wx-user", "用户", "", "", {"smart": True})
        self.assertEqual(reply, "已压缩上下文。")
        compact.assert_called_once_with("wx-user")

    def test_smart_fallback_starts_new_session_on_natural_language(self):
        with mock.patch.object(app, "is_allowed", return_value=True), \
             mock.patch.object(app, "openclaw_start_new_session",
                               return_value="wx-user:session:new") as start_new:
            reply = app.smart_fallback("帮我开启一个新对话", "wx-user", "用户", "", "", {"smart": True})
        self.assertEqual(reply, "已开启新的会话。后续对话将从新的上下文开始。")
        start_new.assert_called_once_with("wx-user")

    def test_codex_compact_session_rebuilds_thread_with_summary(self):
        compact = self._require_callable("codex_compact_session")
        with tempfile.TemporaryDirectory() as tmp:
            sessions_root = os.path.join(tmp, "sessions")
            day_dir = os.path.join(sessions_root, "2026", "08", "08")
            os.makedirs(day_dir)
            thread_file = os.path.join(day_dir, "rollout-2026-08-08T00-00-00-019f-test.jsonl")
            self._write_jsonl(thread_file, [
                {"type": "event_msg", "payload": {"type": "user_message", "message": "记住我喜欢蓝色"}},
                {"type": "event_msg", "payload": {"type": "agent_message", "phase": "final_answer", "message": "好的记住了"}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "我喜欢的颜色是什么"}},
                {"type": "event_msg", "payload": {"type": "agent_message", "phase": "final_answer", "message": "蓝色"}},
            ])
            prefs_tmp = app.AGENT_PREFS_FILE
            app.AGENT_PREFS_FILE = os.path.join(tmp, "prefs.json")
            old_cfg = app.CODEX_CONFIG_FILE
            app.CODEX_CONFIG_FILE = os.path.join(tmp, "config.toml")
            try:
                app.set_codex_thread("wx-user", "019f-test")
                app.add_codex_usage("wx-user", {"input_tokens": 50000, "cached_input_tokens": 49000, "output_tokens": 100})
                with mock.patch.object(
                    app, "_codex_exec_new",
                    side_effect=[
                        ("019f-tmp", ["用户喜欢蓝色"], [], {}),
                        ("019f-new", ["好的"], [], {"input_tokens": 3000, "cached_input_tokens": 2000, "output_tokens": 50}),
                    ],
                ) as exec_new:
                    reply = compact("wx-user")
                self.assertIn("已压缩上下文", reply)
                self.assertIn("Codex 智能体", reply)
                self.assertIn("3k / 272k", reply)
                self.assertIn("输入 3k / 输出 50", reply)
                self.assertEqual(app.codex_thread_for("wx-user"), "019f-new")
                usage = app.codex_usage_for("wx-user")
                self.assertEqual(usage.get("input"), 3000)
                self.assertEqual(usage.get("cached"), 2000)
                self.assertFalse(os.path.exists(thread_file))
                self.assertEqual(exec_new.call_count, 2)
                self.assertIn("蓝色", exec_new.call_args_list[0].args[0])
                self.assertIn("摘要", exec_new.call_args_list[1].args[0])
            finally:
                app.AGENT_PREFS_FILE = prefs_tmp
                app.CODEX_CONFIG_FILE = old_cfg

    def test_smart_fallback_routes_compact_and_new_session_to_codex_when_active(self):
        with mock.patch.object(app, "active_agent", return_value="codex") as active, \
             mock.patch.object(app, "is_allowed", return_value=True), \
             mock.patch.object(app, "codex_compact_session",
                               return_value="已压缩 Codex 上下文", create=True) as c_compact, \
             mock.patch.object(app, "openclaw_compact_session",
                               return_value="已压缩 OpenClaw 上下文", create=True) as o_compact, \
             mock.patch.object(app, "codex_start_new_session",
                               return_value="新 Codex 会话", create=True) as c_new, \
             mock.patch.object(app, "openclaw_start_new_session",
                               return_value="新 OpenClaw 会话", create=True) as o_new:
            compact_reply = app.smart_fallback("压缩一下上下文", "wx-user", "用户", "", "", app.load_config())
            new_reply = app.smart_fallback("开启新会话", "wx-user", "用户", "", "", app.load_config())
            cmd_compact = app.handle_command("/compact", "wx-user", "用户", "", "", app.load_config())
            cmd_new = app.handle_command("开启新的会话", "wx-user", "用户", "", "", app.load_config())

        self.assertEqual(compact_reply, "已压缩 Codex 上下文")
        self.assertEqual(new_reply, "新 Codex 会话")
        self.assertEqual(cmd_compact, "已压缩 Codex 上下文")
        self.assertEqual(cmd_new, "新 Codex 会话")
        self.assertEqual(c_compact.call_count, 2)
        for call in c_compact.call_args_list:
            self.assertEqual(call.args, ("wx-user",))
        self.assertEqual(c_new.call_count, 2)
        for call in c_new.call_args_list:
            self.assertEqual(call.args, ("wx-user",))
        o_compact.assert_not_called()
        o_new.assert_not_called()
        self.assertEqual(active.call_count, 4)

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
        for msg in payload["messages"]:
            self.assertNotEqual(msg["content"], "NO_REPLY")
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
        dsml = ("<" + BS + "DSML" + BS + "tool_calls>\n"
                "<" + BS + "DSML" + BS + "invoke name=\"browser_text\">\n"
                "<" + BS + "DSML" + BS + "parameter name=\"caller\" string=\"true\">search</" + BS + "DSML" + BS + "parameter>\n"
                "<" + BS + "DSML" + BS + "parameter name=\"query\" string=\"true\">王者荣耀 明天 活动 2025</" + BS + "DSML" + BS + "parameter>\n"
                "</" + BS + "DSML" + BS + "invoke>\n"
                "</" + BS + "DSML" + BS + "tool_calls>\n根据资料，明天是夏日农友节开启日。")
        result = clean(dsml)
        self.assertNotIn("DSML", result)
        self.assertNotIn(BS, result)
        self.assertIn("夏日农友节", result)
        result = clean("<" + BS + "DSML" + BS + "tool_calls>\n"
                       "<" + BS + "DSML" + BS + "parameter name=\"action\" value=\"status\"/>\n"
                       "</" + BS + "DSML" + BS + "tool_calls>\n答案")
        self.assertNotIn("DSML", result)
        self.assertEqual(result.strip(), "答案")
        generic = clean("<" + BS + "DSML" + BS + "ear>先获取当前日期，"
                        "再搜索王者荣耀明天的具体活动时间。</" + BS + "DSML" + BS + "ear>答案")
        self.assertNotIn("DSML", generic)
        self.assertNotIn("先获取当前日期", generic)
        self.assertEqual(generic.strip(), "答案")
        inline = clean("答案<" + BS + "DSML" + BS + "parameter name=\"x\" value=\"y\"/>结束")
        self.assertNotIn("DSML", inline)
        self.assertEqual(inline.strip(), "答案结束")

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
