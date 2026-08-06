#!/usr/bin/env bash
# ============================================================
# 微信机器人「自动回复 + 管理后台」Linux 一键安装脚本
# 用法：cd /root/wxbot-reply && bash deploy/install.sh
# 要求：root 权限、已安装 docker、已安装 python3（任意 3.8+）
# ============================================================
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ 请用 root 运行：sudo bash deploy/install.sh" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "❌ 未检测到 docker，请先安装：curl -fsSL https://get.docker.com | bash" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ 未检测到 python3，请先安装（CentOS: yum install -y python3 / Ubuntu: apt install -y python3）" >&2
  exit 1
fi

PYTHON_BIN="$(command -v python3)"
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_DIR="${WXBOT_BASE_DIR:-/root/wxbot-reply}"
LOG_DIR="${WXBOT_BOT_LOG_DIR:-/root/wxBot_logs}"
INT_PORT="${WXBOT_INT_PORT:-3004}"
VIEW_PORT="${WXBOT_VIEW_PORT:-8081}"
BOT_PORT="${WXBOT_BOT_PORT:-3002}"
BOT_IMAGE="${WXBOT_BOT_IMAGE:-dannicool/docker-wechatbot-webhook}"

echo "==> 安装目录：$BASE_DIR"
echo "==> 机器人日志：$LOG_DIR"
mkdir -p "$BASE_DIR" "$LOG_DIR"

# ---------- 1. 复制主服务 ----------
cp "$PKG_DIR/app.py" "$BASE_DIR/app.py"
echo "==> 已复制 app.py"

# ---------- 2. 生成/保留 token ----------
if [ ! -f "$LOG_DIR/.login_token" ]; then
  openssl rand -hex 16 > "$LOG_DIR/.login_token"
  echo "==> 已生成机器人 token：$LOG_DIR/.login_token"
else
  echo "==> 保留已有机器人 token：$LOG_DIR/.login_token"
fi

if [ ! -f "$BASE_DIR/.view_token" ]; then
  openssl rand -hex 12 > "$BASE_DIR/.view_token"
  echo "==> 已生成后台口令：$BASE_DIR/.view_token"
else
  echo "==> 保留已有后台口令：$BASE_DIR/.view_token"
fi

# ---------- 3. 配置文件 ----------
if [ ! -f "$BASE_DIR/config.json" ]; then
  cp "$PKG_DIR/deploy/config.example.json" "$BASE_DIR/config.json"
  echo "==> 已生成 $BASE_DIR/config.json（请修改 AI 配置）"
else
  echo "==> 保留已有 config.json"
fi

# ---------- 4. 会话文件（必须存在，否则 docker 会挂成目录） ----------
touch /root/wxBot_session.json

# ---------- 5. systemd 服务 ----------
UNIT=/etc/systemd/system/wxbot-reply.service
cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=WeChat Bot Auto Reply Service
After=network.target

[Service]
ExecStart=$PYTHON_BIN $BASE_DIR/app.py
Restart=always
RestartSec=3
WorkingDirectory=$BASE_DIR
UNIT_EOF
# 自定义了环境变量时写入 unit（默认值由 app.py 内置，无需写）
[ -n "${WXBOT_INT_PORT:-}" ] && echo "Environment=WXBOT_INT_PORT=$INT_PORT" >> "$UNIT"
[ -n "${WXBOT_VIEW_PORT:-}" ] && echo "Environment=WXBOT_VIEW_PORT=$VIEW_PORT" >> "$UNIT"
[ -n "${WXBOT_BOT_LOG_DIR:-}" ] && echo "Environment=WXBOT_BOT_LOG_DIR=$LOG_DIR" >> "$UNIT"
[ -n "${WXBOT_BOT_BASE:-}" ] && echo "Environment=WXBOT_BOT_BASE=$WXBOT_BOT_BASE" >> "$UNIT"
[ -n "${WXBOT_PUBLIC_BASE:-}" ] && echo "Environment=WXBOT_PUBLIC_BASE=$WXBOT_PUBLIC_BASE" >> "$UNIT"
echo "Environment=WXBOT_BASE_DIR=$BASE_DIR" >> "$UNIT"
echo "Environment=WXBOT_BOT_TOKEN_FILE=$LOG_DIR/.login_token" >> "$UNIT"
echo "" >> "$UNIT"
echo "[Install]" >> "$UNIT"
echo "WantedBy=multi-user.target" >> "$UNIT"

systemctl daemon-reload
systemctl enable --now wxbot-reply
sleep 2
systemctl is-active wxbot-reply >/dev/null && echo "==> wxbot-reply 服务已启动" || { echo "❌ 服务启动失败，日志："; journalctl -u wxbot-reply -n 30 --no-pager; exit 1; }

# ---------- 6. 机器人容器 ----------
if docker ps -a --format '{{.Names}}' | grep -qx wxBotWebhook; then
  echo "==> 容器 wxBotWebhook 已存在，直接启动"
  docker start wxBotWebhook >/dev/null
else
  echo "==> 拉取并启动容器：$BOT_IMAGE"
  docker run -d --name wxBotWebhook --restart unless-stopped \
    -p "$BOT_PORT:3001" \
    -e "LOGIN_API_TOKEN=$(cat "$LOG_DIR/.login_token")" \
    -e "RECVD_MSG_API=http://172.17.0.1:$INT_PORT/receive_msg" \
    -e ACCEPT_RECVD_MSG_MYSELF=false \
    -v "$LOG_DIR:/app/log" \
    -v /root/wxBot_session.json:/app/loginSession.memory-card.json \
    "$BOT_IMAGE"
fi

# ---------- 7. 完成 ----------
BOT_TOKEN="$(cat "$LOG_DIR/.login_token")"
VIEW_TOKEN="$(cat "$BASE_DIR/.view_token")"
echo ""
echo "============================================================"
echo "✅ 安装完成"
echo ""
echo "1) 扫码登录（用手机微信扫）:"
echo "   http://<服务器IP>:$BOT_PORT/login?token=$BOT_TOKEN"
echo ""
echo "2) 管理后台:"
echo "   http://<服务器IP>:$VIEW_PORT   口令: $VIEW_TOKEN"
echo ""
echo "3) 改 AI 配置:"
echo "   vi $BASE_DIR/config.json   （或登录后台「AI 配置」页填写）"
echo "============================================================"
