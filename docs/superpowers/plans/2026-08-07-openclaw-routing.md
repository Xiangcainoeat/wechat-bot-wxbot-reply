# OpenClaw 路由收敛实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 删除 wxbot 自有 web_search 能力，让普通 AI 对话统一交给 OpenClaw，并保留提醒与每日推送的触发能力。

**架构：** 本地服务只保留确定性提醒、每日推送和兼容命令；非命令自然语言不再调用旧 AI 路由或 Bing，而是直接调用 OpenClaw。OpenClaw agent 禁用网页工具，只保留其提醒/推送所需的 automation、messaging 能力。

**技术栈：** Python 3 标准库、OpenClaw Gateway Chat Completions、unittest、systemd、Docker。

---

### 任务 1：建立回归测试

**文件：**
- 修改：`test_openclaw.py`

- [x] 添加测试，断言旧搜索/旧 AI 路由符号已移除，搜索问题由 `smart_fallback` 调用 `ai_answer`。
- [x] 运行单测确认路由收敛后的行为。

### 任务 2：收敛本地路由

**文件：**
- 修改：`app.py:1775-1807,2032-2347,2354-2414`

- [x] 删除本地 `web_search` 函数及其网页抓取、搜索摘要和工具注册。
- [x] 让自然语言路由只处理提醒、每日推送；其余问题直接调用 `ai_answer`。
- [x] 让 `ai_answer` 只走 OpenClaw，未配置时返回统一安全错误，不回退旧 AI。
- [x] 将 `/搜索`、`/search` 兼容命令直接交给 OpenClaw。

### 任务 3：更新 OpenClaw 工作区与部署说明

**文件：**
- 修改：服务器 `/root/openclaw/openclaw_space/workspace-wxbot/AGENTS.md`、`IDENTITY.md`
- 修改：`README.md`、`deploy/config.example.json`

- [x] 备份并更新微信纯文本和统一身份规范。
- [x] 将微信入口智能体工具白名单调整为提醒/推送所需的 automation、messaging，明确拒绝网页工具。
- [x] 删除文档中 web_search/Bing 说明，说明普通问答统一由 OpenClaw 处理。

### 任务 4：验证、部署、实测

**文件：**
- 修改：服务器 `/root/wxbot-reply/app.py`

- [x] 运行 unittest、py_compile、git diff --check。
- [x] 上传 `app.py`/`README.md`，重启 `wxbot-reply`。
- [x] 检查 OpenClaw 容器和工具配置。
- [x] 实测身份、普通问答、搜索类问题、提醒，并确认微信文本无 Markdown/emoji/内部名称。
