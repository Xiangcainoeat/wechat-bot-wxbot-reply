# OpenClaw 会话与上下文后台实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 恢复 OpenClaw 原生网页工具，并为微信用户提供独立 session、上下文压缩、新会话和后台历史查看。

**架构：** app.py 维护每个微信用户的 active Gateway user key，读取 OpenClaw 原生 session 索引和 JSONL transcript；后台通过短引用操作 session，微信命令通过 Gateway 原生 `/compact` 或本地 key 轮换执行。

**技术栈：** Python 3 标准库、OpenClaw Gateway Chat Completions、内嵌 HTML/CSS/JavaScript、unittest。

---

### 任务 1：锁定 session 和上下文行为测试

**文件：**
- 修改：`test_openclaw.py`

- [x] 添加失败测试：active key 默认保持原微信 ID，新 session 生成不同 key 且旧 key 不删除。
- [x] 添加失败测试：session 索引只展示 wxbot 用户 session，解析输入/输出 transcript 和压缩记录。
- [x] 添加失败测试：上下文格式化输出 used/limit/percent，缺失索引安全降级。
- [x] 添加失败测试：`/compact` 和新会话命令分别调用 Gateway 或切换 key。

### 任务 2：实现 session 注册和 OpenClaw 原生控制

**文件：**
- 修改：`app.py`

- [x] 增加 `openclaw_sessions.json` 的加载、保存和锁保护。
- [x] 增加 `openclaw_active_key()`、`openclaw_start_new_session()`、`openclaw_compact_session()`。
- [x] 让 `ai_answer()` 使用 active key，并为普通 AI 回复追加上下文 token 状态。
- [x] 让 `/compact`、`/压缩上下文`、`/new`、`/新会话` 和自然语言别名进入控制逻辑。

### 任务 3：实现 OpenClaw session 读取 API

**文件：**
- 修改：`app.py`
- 修改：`test_openclaw.py`

- [x] 解析 `sessions.json` 和 session JSONL，限制 transcript 文件大小与消息条数。
- [x] 生成稳定的 `user_ref`、`session_ref`，API 不返回完整 ID、Gateway key 或路径。
- [x] 增加 `GET /api/openclaw/sessions`、`GET /api/openclaw/sessions/<ref>`。
- [x] 增加 `POST /api/openclaw/sessions` 的 `compact`、`new`、`activate` 操作并复用管理员鉴权。

### 任务 4：改造管理后台展示

**文件：**
- 修改：`app.py`

- [x] 删除“显示完整 ID”开关，所有消息、用户、提醒和订阅表格只显示短引用。
- [x] 固定消息记录列宽，历史回复经过纯文本清理，避免长 ID、Markdown 和工具痕迹破坏布局。
- [x] 增加“会话与上下文”导航页、用户/session 列表、transcript 详情和操作按钮。

### 任务 5：恢复 OpenClaw 原生网页能力与部署

**文件：**
- 修改：`README.md`
- 修改：`deploy/config.example.json`
- 修改：服务器 `openclaw.json`、`AGENTS.md`

- [x] 将 wxbot agent 的 `group:web` 加入 allow 并从 deny 移除，保留其他高风险 deny。
- [x] 更新微信提示词和文档，说明网页工具由 OpenClaw 内部调用，不展示调用痕迹。
- [x] 备份服务器 OpenClaw 配置和工作区规范，重启 OpenClaw 与 wxbot-reply。

### 任务 6：完整验证

- [x] 运行 unittest、py_compile、git diff --check。
- [x] 线上验证普通实时问题不走本地搜索、`/compact`、新 session、后台 session API 和 ID 脱敏。
- [x] 核对服务 active、OpenClaw healthy、网页工具 allow 配置和工作树状态。
