# OpenClaw 会话与上下文后台设计

## 目标

在不恢复 wxbot 本地 `web_search` 的前提下，恢复 OpenClaw 原生网页工具，并让每个微信用户拥有可切换、可查看、可压缩的独立 OpenClaw session。后台只展示短引用，不把微信长 ID 直接排进表格。

## 边界

- 普通问答继续通过 OpenClaw Gateway；OpenClaw agent 允许 `group:web`，仍拒绝 runtime、fs、nodes、ui 和 elevated。
- 提醒、每日推送仍是 wxbot 唯一的本地动作。
- session transcript 和 session 索引只读展示；后台不改写历史 transcript。
- “新会话”通过为同一微信用户生成新的 Gateway `user` key 实现，旧 session 保留。
- “压缩上下文”通过向当前 Gateway session 发送原生 `/compact` 实现。

## 数据模型

wxbot 新增 `openclaw_sessions.json`，只保存当前激活 session：

```json
{
  "users": {
    "<wechat-id>": {
      "active_key": "<wechat-id>:session:<random>",
      "created_at": "2026-08-07 14:00:00"
    }
  }
}
```

OpenClaw 原生 `sessions.json` 作为历史索引来源，对应 `.jsonl` 文件作为 transcript 来源。wxbot 根据 `agent:wxbot:openai-user:` 前缀解析用户和 session，不把 `route:` 内部路由会话展示给后台。

## 微信交互

- `压缩上下文`、`/compact`：向当前 session 发送 `/compact`，返回 OpenClaw 的结果并附上下文用量。
- `开启新的会话`、`/new`、`/新会话`：切换到新 key，旧 session 仍可在后台查看。
- 每次 AI 普通回答末尾追加 `（上下文 <used> / <limit>，<percent>%）`；动作确认和错误提示不追加。

## 后台接口

- `GET /api/openclaw/sessions`：返回按用户聚合的短引用和 session 摘要。
- `GET /api/openclaw/sessions/<session-ref>`：返回该 session 的用户输入、助手输出和压缩事件。
- `POST /api/openclaw/sessions`：接受 `compact`、`new`、`activate`，请求只传短 `session_ref`，完整 key 由服务端解析。

返回字段只包含 `user_ref`、`session_ref`、昵称、时间、上下文 token、上下文上限、压缩次数和消息正文。完整微信 ID、Gateway key 和 transcript 路径不返回给浏览器。

## 后台界面

增加“会话与上下文”页，分为用户列表、session 列表和 transcript 详情三块。消息记录页改为固定列宽、短 ID 和可换行正文，历史回复也经过纯文本清理，避免长 ID 和模型内部痕迹撑坏布局。

## 错误处理与安全

- OpenClaw session 文件缺失、损坏或字段不完整时返回空列表或降级摘要，不影响微信收发。
- 压缩失败返回固定中文错误，不把 Gateway 错误正文发送给微信。
- 所有新后台接口复用现有管理员 session 鉴权。
- 读 transcript 设置文件大小和消息条数上限，避免后台请求读取无界文件。

## 验证

- 单元测试覆盖 session key 切换、索引解析、transcript 解析、上下文格式化、compact/new API 分派和长 ID 脱敏。
- 运行 `python3 -m unittest discover -v`、`python3 -m py_compile app.py test_openclaw.py` 和 `git diff --check`。
- 线上验证 OpenClaw `group:web` 权限、普通实时问题、`/compact`、新 session 和后台接口。
