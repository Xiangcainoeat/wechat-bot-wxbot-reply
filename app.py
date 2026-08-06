#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信机器人「收消息自动回复 + 管理后台」服务（独立部署，零第三方依赖）

收到机器人(wechatbot-webhook)上报的消息后：
- 私聊：内容为数学表达式(加减乘除) -> 计算结果并回复
- 群聊：被 @ 且内容为数学表达式   -> 计算结果并回复（回复带 @ 发送者名字）
- 其他消息：只记录，不回复
- 可在管理后台一键开关“自动回复”

管理后台（公网 8081，登录后使用）：
- 状态总览：机器人登录状态、今日/总消息数、自动回复开关、最近报错
- 消息记录：按关键字/发送者/群筛选，分页查看
- 日志与报错：机器人日志（可只看报错）、服务报错、系统事件(登录/登出/异常)
- 管理操作：发送测试消息、开关自动回复、打开扫码登录页、下载完整记录

监听：
- 172.17.0.1:3004  内部接口（机器人容器经 172.17.0.1 访问），仅 /receive_msg /healthz /messages
- 0.0.0.0:8081     管理后台（/login /api/* 等，需登录）
"""
import html
import base64
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------- 配置 ----------------
# 所有路径/端口均可通过环境变量覆盖，默认值与线上服务器保持一致。
INT_BIND = os.environ.get("WXBOT_INT_BIND", "172.17.0.1")  # docker 网桥网关，仅容器可达
INT_PORT = int(os.environ.get("WXBOT_INT_PORT", "3004"))
VIEW_BIND = os.environ.get("WXBOT_VIEW_BIND", "0.0.0.0")   # 管理后台对外端口
VIEW_PORT = int(os.environ.get("WXBOT_VIEW_PORT", "8081"))
BASE_DIR = os.environ.get("WXBOT_BASE_DIR", "/root/wxbot-reply")
LOG_FILE = os.path.join(BASE_DIR, "messages.log")       # 消息记录(JSONL)
ERROR_LOG = os.path.join(BASE_DIR, "error.log")          # 本服务报错
SYSTEM_LOG = os.path.join(BASE_DIR, "system_events.log") # 机器人系统事件
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")      # 配置(自动回复开关)
TOKEN_FILE = os.path.join(BASE_DIR, ".view_token")       # 后台登录口令
BOT_LOG_DIR = os.environ.get("WXBOT_BOT_LOG_DIR", "/root/wxBot_logs")  # 机器人日志目录
BOT_TOKEN_FILE = os.environ.get("WXBOT_BOT_TOKEN_FILE", os.path.join(BOT_LOG_DIR, ".login_token"))  # 机器人 API token
BOT_BASE = os.environ.get("WXBOT_BOT_BASE", "http://127.0.0.1:3002")   # 机器人端口(宿主机)
PUBLIC_BASE = os.environ.get("WXBOT_PUBLIC_BASE", "http://127.0.0.1:3002")  # 后台展示的扫码登录地址(公网 IP/域名)
SESSION_TTL = 12 * 3600
MAX_RECENT = 300
LEDGER_FILE = os.path.join(BASE_DIR, "ledger.json")      # 记账数据(按人独立)
REMINDER_FILE = os.path.join(BASE_DIR, "reminders.json") # 定时提醒
SUBS_FILE = os.path.join(BASE_DIR, "subscriptions.json") # 每日推送订阅
OUTBOX_FILE = os.path.join(BASE_DIR, "ilink_outbox.json")  # 待 ClawBot 网关代发的主动消息
PERM_FILE = os.path.join(BASE_DIR, "permissions.json")   # 权限(管理员/成员白名单)
USERS_FILE = os.path.join(BASE_DIR, "users.json")        # 见过的用户(昵称->ID)
LEDGER_LOCK = threading.Lock()
REMINDER_LOCK = threading.Lock()
SUBS_LOCK = threading.Lock()
PERM_LOCK = threading.Lock()
USERS_LOCK = threading.Lock()

WMO_CODES = {
    0: "晴", 1: "基本晴", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "小毛毛雨", 55: "大毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷暴伴冰雹",
}

# 常用城市坐标表（免费地理编码服务对县级市支持差，长垣等常见城市直接内置；
# 表里没有的城市会走 Open-Meteo 在线查询）
_C = lambda n, a1, lat, lon: {"name": n, "admin1": a1, "country": "中国", "lat": lat, "lon": lon}
CITY_ALIASES = {
    "北京": _C("北京", "北京市", 39.9042, 116.4074), "beijing": _C("北京", "北京市", 39.9042, 116.4074),
    "上海": _C("上海", "上海市", 31.2304, 121.4737), "shanghai": _C("上海", "上海市", 31.2304, 121.4737),
    "广州": _C("广州", "广东省", 23.1291, 113.2644), "guangzhou": _C("广州", "广东省", 23.1291, 113.2644),
    "深圳": _C("深圳", "广东省", 22.5431, 114.0579), "shenzhen": _C("深圳", "广东省", 22.5431, 114.0579),
    "杭州": _C("杭州", "浙江省", 30.2741, 120.1551), "hangzhou": _C("杭州", "浙江省", 30.2741, 120.1551),
    "南京": _C("南京", "江苏省", 32.0603, 118.7969), "nanjing": _C("南京", "江苏省", 32.0603, 118.7969),
    "苏州": _C("苏州", "江苏省", 31.2989, 120.5853), "suzhou": _C("苏州", "江苏省", 31.2989, 120.5853),
    "成都": _C("成都", "四川省", 30.5728, 104.0668), "chengdu": _C("成都", "四川省", 30.5728, 104.0668),
    "重庆": _C("重庆", "重庆市", 29.5630, 106.5516), "chongqing": _C("重庆", "重庆市", 29.5630, 106.5516),
    "武汉": _C("武汉", "湖北省", 30.5928, 114.3055), "wuhan": _C("武汉", "湖北省", 30.5928, 114.3055),
    "西安": _C("西安", "陕西省", 34.3416, 108.9398), "xian": _C("西安", "陕西省", 34.3416, 108.9398),
    "郑州": _C("郑州", "河南省", 34.7466, 113.6254), "zhengzhou": _C("郑州", "河南省", 34.7466, 113.6254),
    "新乡": _C("新乡", "河南省", 35.3030, 113.9268), "xinxiang": _C("新乡", "河南省", 35.3030, 113.9268),
    "长垣": _C("长垣市", "河南省新乡市", 35.2005, 114.6688),
    "changyuan": _C("长垣市", "河南省新乡市", 35.2005, 114.6688),
    "changyuanxian": _C("长垣市", "河南省新乡市", 35.2005, 114.6688),
    "开封": _C("开封", "河南省", 34.7973, 114.3076), "kaifeng": _C("开封", "河南省", 34.7973, 114.3076),
    "洛阳": _C("洛阳", "河南省", 34.6197, 112.4540), "luoyang": _C("洛阳", "河南省", 34.6197, 112.4540),
    "安阳": _C("安阳", "河南省", 36.0985, 114.3924), "anyang": _C("安阳", "河南省", 36.0985, 114.3924),
    "濮阳": _C("濮阳", "河南省", 35.7618, 115.0292), "puyang": _C("濮阳", "河南省", 35.7618, 115.0292),
    "焦作": _C("焦作", "河南省", 35.2159, 113.2418), "jiaozuo": _C("焦作", "河南省", 35.2159, 113.2418),
    "许昌": _C("许昌", "河南省", 34.0357, 113.8525), "xuchang": _C("许昌", "河南省", 34.0357, 113.8525),
    "漯河": _C("漯河", "河南省", 33.5815, 114.0165), "luohe": _C("漯河", "河南省", 33.5815, 114.0165),
    "平顶山": _C("平顶山", "河南省", 33.7662, 113.1926), "pingdingshan": _C("平顶山", "河南省", 33.7662, 113.1926),
    "南阳": _C("南阳", "河南省", 32.9907, 112.5283), "nanyang": _C("南阳", "河南省", 32.9907, 112.5283),
    "商丘": _C("商丘", "河南省", 34.4143, 115.6564), "shangqiu": _C("商丘", "河南省", 34.4143, 115.6564),
    "信阳": _C("信阳", "河南省", 32.1470, 114.0913), "xinyang": _C("信阳", "河南省", 32.1470, 114.0913),
    "周口": _C("周口", "河南省", 33.6260, 114.6969), "zhoukou": _C("周口", "河南省", 33.6260, 114.6969),
    "驻马店": _C("驻马店", "河南省", 33.0114, 114.0223), "zhumadian": _C("驻马店", "河南省", 33.0114, 114.0223),
    "天津": _C("天津", "天津市", 39.3434, 117.3616), "tianjin": _C("天津", "天津市", 39.3434, 117.3616),
    "长沙": _C("长沙", "湖南省", 28.2282, 112.9388), "changsha": _C("长沙", "湖南省", 28.2282, 112.9388),
    "合肥": _C("合肥", "安徽省", 31.8206, 117.2272), "hefei": _C("合肥", "安徽省", 31.8206, 117.2272),
    "济南": _C("济南", "山东省", 36.6512, 117.1201), "jinan": _C("济南", "山东省", 36.6512, 117.1201),
    "青岛": _C("青岛", "山东省", 36.0671, 120.3826), "qingdao": _C("青岛", "山东省", 36.0671, 120.3826),
    "福州": _C("福州", "福建省", 26.0745, 119.2965), "fuzhou": _C("福州", "福建省", 26.0745, 119.2965),
    "厦门": _C("厦门", "福建省", 24.4798, 118.0894), "xiamen": _C("厦门", "福建省", 24.4798, 118.0894),
    "南昌": _C("南昌", "江西省", 28.6820, 115.8579), "nanchang": _C("南昌", "江西省", 28.6820, 115.8579),
    "昆明": _C("昆明", "云南省", 24.8801, 102.8329), "kunming": _C("昆明", "云南省", 24.8801, 102.8329),
    "贵阳": _C("贵阳", "贵州省", 26.6470, 106.6302), "guiyang": _C("贵阳", "贵州省", 26.6470, 106.6302),
    "南宁": _C("南宁", "广西壮族自治区", 22.8170, 108.3665), "nanning": _C("南宁", "广西壮族自治区", 22.8170, 108.3665),
    "海口": _C("海口", "海南省", 20.0440, 110.1999), "haikou": _C("海口", "海南省", 20.0440, 110.1999),
    "太原": _C("太原", "山西省", 37.8706, 112.5489), "taiyuan": _C("太原", "山西省", 37.8706, 112.5489),
    "石家庄": _C("石家庄", "河北省", 38.0428, 114.5149), "shijiazhuang": _C("石家庄", "河北省", 38.0428, 114.5149),
    "沈阳": _C("沈阳", "辽宁省", 41.8057, 123.4315), "shenyang": _C("沈阳", "辽宁省", 41.8057, 123.4315),
    "大连": _C("大连", "辽宁省", 38.9140, 121.6147), "dalian": _C("大连", "辽宁省", 38.9140, 121.6147),
    "长春": _C("长春", "吉林省", 43.8171, 125.3235), "changchun": _C("长春", "吉林省", 43.8171, 125.3235),
    "哈尔滨": _C("哈尔滨", "黑龙江省", 45.8038, 126.5349), "haerbin": _C("哈尔滨", "黑龙江省", 45.8038, 126.5349),
    "兰州": _C("兰州", "甘肃省", 36.0611, 103.8343), "lanzhou": _C("兰州", "甘肃省", 36.0611, 103.8343),
    "乌鲁木齐": _C("乌鲁木齐", "新疆维吾尔自治区", 43.8256, 87.6168), "wulumuqi": _C("乌鲁木齐", "新疆维吾尔自治区", 43.8256, 87.6168),
}

AI_NO_PERMISSION_MSG = (
    "🔒 AI 功能需要授权后才能使用；未授权仍可使用：计算 / 记账 / 余额 / 提醒 / 说明。\n"
    "输入 /权限 <密码> 即可开通（密码由机器人主人提供）。"
)
AI_CMDS = ("/ai",)  # 需要授权的 AI 类命令（未来接入平台后把新命令加进来）

FALLBACK_HELP = (
    "⚡ 我支持这些功能：\n"
    "  📐 直接发算式 → 自动计算（4-2+3）\n"
    "  🤖 /ai <内容> → AI 问答（需 /权限 <密码> 开通，可查天气/时间/搜索）\n"
    "  🔎 /搜索 <内容> → 实时网页搜索（赛程/新闻/最新信息）\n"
    "  📒 /记账 +算式 → 记收入\n"
    "  📒 /记账 -算式 → 记支出\n"
    "  💰 /余额 → 查看余额\n"
    "  📋 /明细 → 全部记账\n"
    "  🗑️ /清空 → 清空我的记账\n"
    "  ⏰ /提醒 <时间> <内容> → 定时提醒\n"
    "  🌤 /推送 <时间> → 每日天气推送（如 /推送 8:00）\n"
    "  🗑️ /取消推送 [编号] → 取消每日推送\n"
    "  📖 /说明 → 查看详细说明"
)

DETAIL_HELP = (
    "📖 详细说明：\n"
    "1️⃣ 自动计算：直接发算式，支持 + - × ÷ 括号 小数\n"
    "     （例：4-2+3、10÷(2+3)、12.5×2）\n"
    "2️⃣ /ai <内容>：单次调用 AI，不带上下文（需授权才能用）\n"
    "     内置工具：天气查询 / 当前时间 / 精确计算\n"
    "     （例：/ai 今天上海天气怎么样、/ai 现在几点）\n"
    "3️⃣ /记账 +算式 [备注]：记收入（例：/记账 +8×4 买菜）\n"
    "4️⃣ /记账 -算式 [备注]：记支出（例：/记账 -15×2）\n"
    "5️⃣ /余额：查看我的余额和总笔数\n"
    "6️⃣ /明细：列出我的全部记账\n"
    "7️⃣ /清空：清空我的全部记账（不可恢复）\n"
    "8️⃣ /提醒 <时间> <内容>：定时提醒\n"
    "     时间支持：X分钟后 / X小时后 / HH:MM / YYYY-MM-DD HH:MM\n"
    "     （例：/提醒 10分钟后 喝水）\n"
    "9️⃣ /提醒列表 / 取消提醒 <编号>：管理我的提醒\n"
    "🔟 /推送 <时间>：开启每日天气推送（如 /推送 8:00）\n"
    "     开启后会引导确认城市；在群里开启就推送到群，私聊开启就推送到私聊\n"
    "1️⃣1️⃣ /取消推送：取消每日推送\n"
    "1️⃣2️⃣ 群里使用：先 @机器人 再发命令或算式；\n"
    "     群提醒到点会 @ 本人，私聊提醒直接发消息\n"
    "1️⃣3️⃣ 开通 AI：/权限 <密码> 输入正确密码即授权成功（授权当前账号）；\n"
    "     密码找管理员要，后台「用户与权限」页可随时收回"
)

VIEW_TOKEN = ""
SESSIONS = {}
_sess_lock = threading.Lock()
_recent = []
_recent_lock = threading.Lock()
STATS = {"total": 0, "today": 0, "last": None, "last_error": ""}


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def load_token():
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            t = f.read().strip()
        return t or "changeme"
    except Exception:
        return "changeme"


def load_config():
    cfg = {"auto_reply": True}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_error(f"保存配置失败: {e}")


def log_error(msg):
    rec = {"time": now_str(), "msg": str(msg)}
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    STATS["last_error"] = f"{rec['time']} {rec['msg']}"


def log_system_event(rec):
    rec["time"] = now_str()
    try:
        with open(SYSTEM_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def save_record(rec):
    rec["time"] = now_str()
    with _recent_lock:
        _recent.append(rec)
        if len(_recent) > MAX_RECENT:
            del _recent[: len(_recent) - MAX_RECENT]
    STATS["total"] += 1
    if rec["time"][:10] == time.strftime("%Y-%m-%d"):
        STATS["today"] += 1
    STATS["last"] = rec
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log_error(f"写入消息记录失败: {e}")


def init_stats():
    today = time.strftime("%Y-%m-%d")
    marker = '"time": "%s' % today
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                STATS["total"] += 1
                if marker in line:
                    STATS["today"] += 1
    except Exception:
        pass


def tail_file(path, n):
    lines = deque(maxlen=n)
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for ln in f:
                lines.append(ln.rstrip("\n"))
    except Exception:
        pass
    return list(lines)


def read_messages(limit=10000):
    dq = deque(maxlen=limit)
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    dq.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return list(dq)


def bot_token():
    try:
        with open(BOT_TOKEN_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def bot_status():
    try:
        with urllib.request.urlopen(
            BOT_BASE + "/healthz?token=" + bot_token(), timeout=5
        ) as r:
            body = r.read().decode("utf-8", "ignore").strip()
            return {"reachable": True, "logged_in": body.lower() == "healthy", "raw": body}
    except Exception as e:
        return {"reachable": False, "logged_in": False, "raw": str(e)}


def bot_send(to, content, is_room=False, name=None):
    """主动推送（旧机器人 /webhook/msg/v2）。
    该接口群聊按「群名」找，私聊按「id 或 昵称」找；返回 (成功?, 原始响应)。
    群聊：to 传群名（topic）；私聊：to 传 fromId，失败自动按昵称 name 重试。"""
    def _post(t):
        payload = {"to": t, "isRoom": is_room, "data": {"content": content}}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            BOT_BASE + "/webhook/msg/v2?token=" + bot_token(),
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore")
        except Exception as e:
            return 0, str(e)

    def _ok(resp):
        try:
            return bool(json.loads(resp).get("success"))
        except Exception:
            return True

    if is_room:
        code, resp = _post(to)
    else:
        code, resp = _post({"id": to})
        if not _ok(resp) and name:
            code, resp = _post(name)
    return _ok(resp), resp


# ---------------- 会话 ----------------
def new_session():
    sid = secrets.token_hex(16)
    with _sess_lock:
        SESSIONS[sid] = time.time() + SESSION_TTL
    return sid


def valid_session(sid):
    with _sess_lock:
        exp = SESSIONS.get(sid)
        if not exp:
            return False
        if time.time() > exp:
            del SESSIONS[sid]
            return False
        return True


def drop_session(sid):
    with _sess_lock:
        SESSIONS.pop(sid, None)


# ---------------- multipart 解析 ----------------
def parse_multipart(content_type, body):
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type or "")
    if not m:
        return {}
    boundary = (m.group(1) or m.group(2) or "").strip()
    if not boundary:
        return {}
    delim = ("--" + boundary).encode("utf-8")
    parts = {}
    for block in body.split(delim):
        if block.startswith(b"\r\n"):
            block = block[2:]
        if block.endswith(b"\r\n"):
            block = block[:-2]
        if not block or block == b"--":
            continue
        sep = block.find(b"\r\n\r\n")
        if sep == -1:
            continue
        headers = block[:sep].decode("utf-8", "ignore")
        data = block[sep + 4:]
        nm = re.search(r'name="([^"]+)"', headers)
        if not nm:
            continue
        fm = re.search(r'filename="([^"]*)"', headers)
        parts[nm.group(1)] = (fm.group(1) if fm else None, data)
    return parts


def parse_urlencoded(body):
    try:
        qs = body.decode("utf-8", "ignore")
    except Exception:
        qs = ""
    return {k: v[0] for k, v in urllib.parse.parse_qs(qs).items()}


# ---------------- 数学表达式 ----------------
CHINESE_OPS = {
    "加上": "+", "减去": "-", "乘以": "*", "除以": "/",
    "加": "+", "减": "-", "乘": "*", "除": "/",
}
_CH_OP_RE = re.compile("|".join(sorted(CHINESE_OPS, key=len, reverse=True)))
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def normalize_expr(s):
    s = (s or "").strip()
    s = re.sub(r"@\s*[\u4e00-\u9fa5\w\-]+", "", s)  # 去掉 @提及
    s = s.replace("（", "(").replace("）", ")")
    s = s.replace("，", ",").replace("。", "")
    s = s.replace("×", "*").replace("÷", "/").replace("x", "*").replace("X", "*")
    s = s.replace("＋", "+").replace("－", "-").replace("＊", "*").replace("／", "/")
    s = re.sub(r"[０-９]", lambda m: chr(ord(m.group(0)) - 0xFEE0), s)  # 全角数字
    s = s.replace("．", ".")  # 全角小数点
    s = _CH_OP_RE.sub(lambda m: CHINESE_OPS[m.group(0)], s)
    s = re.sub(r"(等于|是多少|是多少呀|得多少|多少|答案|结果)[?？=]*$", "", s)
    s = re.sub(r"[?？=]+$", "", s)
    return s.strip()


def is_math_expr(s):
    if not s or len(s) > 100:
        return False
    if not re.search(r"\d", s):
        return False
    return bool(re.fullmatch(r"[\d\s+\-*/().]*", s))


def tokenize_math(s):
    tokens = []
    i = 0
    while i < len(s):
        c = s[i]
        if c in " \t":
            i += 1
            continue
        m = _NUM_RE.match(s, i)
        if m:
            tokens.append(float(m.group(0)))
            i = m.end()
            continue
        if c in "+-*/()":
            tokens.append(c)
            i += 1
            continue
        raise ValueError("无法识别的字符")
    return tokens


class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self):
        t = self.peek()
        self.pos += 1
        return t

    def parse(self):
        v = self.expr()
        if self.peek() is not None:
            raise ValueError("表达式格式不正确")
        return v

    def expr(self):
        v = self.term()
        while self.peek() in ("+", "-"):
            op = self.next()
            r = self.term()
            v = v + r if op == "+" else v - r
        return v

    def term(self):
        v = self.factor()
        while self.peek() in ("*", "/"):
            op = self.next()
            r = self.factor()
            if op == "/" and r == 0:
                raise ZeroDivisionError("除数不能为 0")
            v = v * r if op == "*" else v / r
        return v

    def factor(self):
        t = self.peek()
        if t is None:
            raise ValueError("表达式不完整")
        if t == "(":
            self.next()
            v = self.expr()
            if self.next() != ")":
                raise ValueError("括号不匹配")
            return v
        if t == "-":
            self.next()
            return -self.factor()
        if t == "+":
            self.next()
            return self.factor()
        if isinstance(t, (int, float)):
            self.next()
            return t
        raise ValueError("无法识别的字符")


def evaluate_math(s):
    return _Parser(tokenize_math(s)).parse()


def fmt_signed(v):
    return ("+" if v > 0 else "") + fmt_number(v)


def fmt_number(v):
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError("结果无效")
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    s = f"{v:.8f}".rstrip("0").rstrip(".")
    if abs(v) >= 1e12 or (0 < abs(v) < 1e-6):
        s = f"{v:.6g}"
    return s




# ---------------- 权限系统 ----------------
def load_permissions():
    try:
        with open(PERM_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"members": {}}


def save_permissions(p):
    try:
        with open(PERM_FILE, "w", encoding="utf-8") as f:
            json.dump(p, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_error(f"保存权限失败: {e}")


def is_allowed(from_id):
    if not from_id:
        return False
    with PERM_LOCK:
        p = load_permissions()
        return from_id in p.get("members", {})


def grant_member(from_id, name, granted_by):
    with PERM_LOCK:
        p = load_permissions()
        p.setdefault("members", {})
        p["members"][from_id] = {
            "name": name or from_id, "granted_by": granted_by or "",
            "granted_at": now_str(),
        }
        save_permissions(p)


def revoke_member(from_id):
    with PERM_LOCK:
        p = load_permissions()
        if p.get("members", {}).pop(from_id, None) is not None:
            save_permissions(p)
            return True
        return False


# ---------------- 用户注册表 ----------------
def load_users():
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": {}}


def save_users(u):
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(u, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_error(f"保存用户表失败: {e}")


def record_user(from_id, name):
    if not from_id:
        return
    with USERS_LOCK:
        u = load_users()
        old = u["users"].get(from_id, {})
        if (old.get("name") or "") != (name or "") or not old.get("last_seen"):
            u["users"][from_id] = {
                "name": name or old.get("name") or from_id,
                "last_seen": now_str(),
            }
            save_users(u)


# ---------------- 记账 ----------------
def load_ledger():
    try:
        with open(LEDGER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": {}}


def save_ledger(ledger):
    try:
        with open(LEDGER_FILE, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_error(f"保存记账失败: {e}")


def ledger_entry(from_id, op, expr, note, amount):
    with LEDGER_LOCK:
        ledger = load_ledger()
        u = ledger["users"].setdefault(from_id, {"entries": [], "balance": 0})
        u["entries"].append({
            "t": now_str(), "op": op, "expr": expr, "note": note,
            "amount": float(amount), "balance": round(u["balance"] + amount, 8),
        })
        u["balance"] = round(u["balance"] + amount, 8)
        save_ledger(ledger)


def ledger_summary(from_id):
    with LEDGER_LOCK:
        ledger = load_ledger()
        u = ledger["users"].get(from_id, {"entries": [], "balance": 0})
        income = sum(e["amount"] for e in u["entries"] if e["amount"] > 0)
        expense = sum(e["amount"] for e in u["entries"] if e["amount"] < 0)
        return {
            "balance": u["balance"],
            "count": len(u["entries"]),
            "income": income,
            "expense": expense,
            "entries": u["entries"],
        }


def ledger_clear(from_id):
    with LEDGER_LOCK:
        ledger = load_ledger()
        u = ledger["users"].get(from_id)
        n = len(u["entries"]) if u else 0
        ledger["users"][from_id] = {"entries": [], "balance": 0}
        save_ledger(ledger)
        return n


# ---------------- 定时提醒 ----------------
def load_reminders():
    try:
        with open(REMINDER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"reminders": []}


def save_reminders(data):
    try:
        with open(REMINDER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_error(f"保存提醒失败: {e}")


def parse_remind_time(s):
    s = (s or "").strip()
    now = time.time()
    m = re.match(r"^(\d+)\s*秒(?:钟)?(?:后)?$", s)
    if m:
        return now + int(m.group(1))
    m = re.match(r"^(\d+)\s*分钟?(?:钟)?(?:后)?$", s)
    if m:
        return now + int(m.group(1)) * 60
    m = re.match(r"^(\d+)\s*小时?(?:后)?$", s)
    if m:
        return now + int(m.group(1)) * 3600
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        t = time.mktime(time.localtime())
        lt = list(time.localtime(t))
        lt[3], lt[4], lt[5] = hh, mm, 0
        ts = time.mktime(time.localtime(time.mktime(tuple(lt))))
        if ts <= now:
            ts += 86400
        return ts
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})$", s)
    if m:
        y, mo, d, hh, mm = (int(x) for x in m.groups())
        return time.mktime((y, mo, d, hh, mm, 0, 0, 0, -1))
    return None


def fmt_remind_time(ts):
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def channel_of(from_id, room_id):
    if (from_id or "").endswith("@im.wechat") or (room_id or "").endswith("@chatroom"):
        return "ilink"
    return "web"


def add_reminder(from_id, from_name, room_id, room_name, at, text):
    with REMINDER_LOCK:
        data = load_reminders()
        rid = secrets.token_hex(4)
        data["reminders"].append({
            "id": rid, "from_id": from_id, "from_name": from_name,
            "room_id": room_id, "room_name": room_name, "at": at, "text": text,
            "created": now_str(),
        })
        save_reminders(data)
        return rid


def cancel_reminder(from_id, rid):
    with REMINDER_LOCK:
        data = load_reminders()
        kept = [r for r in data["reminders"]
                if not (r.get("id") == rid and r.get("from_id") == from_id)]
        removed = len(data["reminders"]) - len(kept)
        data["reminders"] = kept
        save_reminders(data)
        return removed


# ---------------- 每日推送订阅（/推送 时间 + 城市确认） ----------------
def load_subs():
    try:
        with open(SUBS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"subscriptions": [], "pending": {}}


def save_subs(data):
    try:
        with open(SUBS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_error(f"保存订阅失败: {e}")


def parse_push_time(s):
    """解析推送时间，支持：8 / 8:30 / 08:30 / 8点 / 8点30 / 8点30分。返回 "HH:MM" 或 None。"""
    s = (s or "").strip()
    m = re.match(r"^(\d{1,2})\s*[:：点]\s*(\d{1,2})?\s*(?:分)?$", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
    else:
        m = re.match(r"^(\d{1,2})$", s)
        if not m:
            return None
        h, mi = int(m.group(1)), 0
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return "%02d:%02d" % (h, mi)
    return None


def get_pending(from_id):
    with SUBS_LOCK:
        d = load_subs()
        return d.get("pending", {}).get(from_id)


def set_pending(from_id, p):
    with SUBS_LOCK:
        d = load_subs()
        d.setdefault("pending", {})
        if p is None:
            d["pending"].pop(from_id, None)
        else:
            d["pending"][from_id] = p
        save_subs(d)


def get_subs(from_id):
    with SUBS_LOCK:
        d = load_subs()
        return [dict(s) for s in d.get("subscriptions", [])
                if s.get("from_id") == from_id]


def upsert_sub(s):
    with SUBS_LOCK:
        d = load_subs()
        subs = d.setdefault("subscriptions", [])
        if s.get("id"):
            for i, x in enumerate(subs):
                if x.get("id") == s["id"]:
                    subs[i].update(s)
                    save_subs(d)
                    return subs[i]
        s["id"] = secrets.token_hex(4)
        s["created"] = now_str()
        s["last_sent"] = ""
        subs.append(s)
        save_subs(d)
        return s


def remove_sub(sid):
    with SUBS_LOCK:
        d = load_subs()
        subs = d.get("subscriptions", [])
        n = len(subs)
        d["subscriptions"] = [x for x in subs if x.get("id") != sid]
        if len(d["subscriptions"]) != n:
            save_subs(d)
            return True
    return False


def cmd_push(rest, from_id, from_name, room_id, room_name):
    """/推送 命令处理（含分步确认）。"""
    rest = (rest or "").strip()
    subs = get_subs(from_id)
    if not rest:
        if not subs:
            return ("⏰ 每日推送用法：/推送 <时间>\n"
                    "  例：/推送 8:00（每天 8 点推送天气）\n"
                    "  先设时间，再按提示确认城市。可设置多条；取消：/取消推送")
        lines = ["🌤 你的每日推送（{} 条）：".format(len(subs))]
        for i, s in enumerate(subs, 1):
            where = "群聊「%s」" % s.get("room_name") if s.get("room_id") else "私聊"
            lines.append("{}. 每天 {} 在{}推送「{} 天气」".format(
                i, s.get("time"), where, s.get("city_label") or s.get("city")))
        lines.append("  新增：/推送 <时间>；取消：/取消推送 <编号>")
        return "\n".join(lines)
    hm = parse_push_time(rest)
    if not hm:
        return ("⚠️ 时间格式不认识，请用数字：8 / 8:30 / 8点30\n"
                "  例：/推送 8:00")
    for s in subs:
        if s.get("time") == hm and (s.get("room_id") or "") == (room_id or ""):
            return ("⚠️ 你已有一个每天 {} 的推送（同一位置）。\n"
                    "  查看：/推送；取消旧的：/取消推送 <编号>".format(hm))
    set_pending(from_id, {
        "state": "await_city", "time": hm,
        "from_id": from_id, "from_name": from_name,
        "room_id": room_id, "room_name": room_name,
    })
    return ("⏰ 已记录推送时间 {}。\n"
            "现在请回复你的城市（如：长垣 / 长垣县 / changyuan），我来确认。"
            .format(hm))


def cmd_unsub(rest, from_id):
    rest = (rest or "").strip()
    subs = get_subs(from_id)
    if not subs:
        return "你还没有开启每日推送（/推送 <时间> 开启）"
    if rest:
        try:
            n = int(rest)
        except Exception:
            return "⚠️ 用法：/取消推送 <编号>（编号见 /推送）"
        if not (1 <= n <= len(subs)):
            return "⚠️ 编号不对，用 /推送 查看你的推送编号"
        s = subs[n - 1]
        remove_sub(s["id"])
        return ("🗑️ 已取消第 {} 条推送：每天 {} {}「{} 天气」".format(
            n, s.get("time"), "群聊" if s.get("room_id") else "私聊",
            s.get("city_label") or s.get("city")))
    if len(subs) == 1:
        remove_sub(subs[0]["id"])
        set_pending(from_id, None)
        return "🗑️ 已取消每日推送"
    lines = ["你有 {} 条每日推送，请指定取消哪条：".format(len(subs))]
    for i, s in enumerate(subs, 1):
        where = "群聊「%s」" % s.get("room_name") if s.get("room_id") else "私聊"
        lines.append("{}. 每天 {} 在{}推送「{} 天气」".format(
            i, s.get("time"), where, s.get("city_label") or s.get("city")))
    lines.append("  回复：/取消推送 <编号>")
    return "\n".join(lines)


def handle_pending_reply(from_id, text):
    """处理城市确认流程的回复。返回回复文本或 None（无待确认）。"""
    pending = get_pending(from_id)
    if not pending:
        return None
    state = pending.get("state")
    if state == "await_city":
        cands = geocode_city(text.strip())
        if not cands:
            return ("❌ 没找到城市「{}」，请检查是否有错别字，或试试拼音（如 changyuan）。\n"
                    "重新输入城市名：".format(text.strip()))
        pending["candidates"] = cands[:3]
        pending["state"] = "await_confirm"
        set_pending(from_id, pending)
        if len(cands) == 1:
            c = cands[0]
            label = c["name"] + ("·" + c["admin1"] if c.get("admin1") else "")
            return ("📍 找到：{}（{}）\n"
                    "确认请回复 1，重新输入回复 2，取消回复 0".format(label, c.get("country") or ""))
        lines = ["📍 找到多个城市，请回复数字选择："]
        for i, c in enumerate(cands, 1):
            lines.append("{}. {}（{}{}）".format(i, c["name"], c.get("admin1") or "", c.get("country") or ""))
        lines.append("0. 取消")
        return "\n".join(lines)
    if state == "await_confirm":
        choice = text.strip()
        cands = pending.get("candidates") or []
        if choice in ("1", "2", "3") and 1 <= int(choice) <= len(cands):
            c = cands[int(choice) - 1]
            label = c["name"] + ("·" + c["admin1"] if c.get("admin1") else "")
            s = {
                "from_id": pending["from_id"], "from_name": pending.get("from_name") or "",
                "room_id": pending.get("room_id") or "", "room_name": pending.get("room_name") or "",
                "time": pending.get("time"), "city": c["name"],
                "city_label": label, "lat": c["lat"], "lon": c["lon"],
                "channel": "ilink" if (pending["from_id"].endswith("@im.wechat")
                                       or (pending.get("room_id") or "").endswith("@chatroom")) else "web",
            }
            upsert_sub(s)
            set_pending(from_id, None)
            where = "群聊「%s」" % s["room_name"] if s["room_id"] else "私聊"
            return ("✅ 每日推送已开启：每天 {} 在{}推送「{} 天气」。\n"
                    "  修改：/推送 <时间>；取消：/取消推送"
                    .format(s["time"], where, label))
        if choice in ("0", "取消", "cancel", "q"):
            set_pending(from_id, None)
            return "已取消设置"
        if choice in ("2", "重输", "重新输入"):
            pending["state"] = "await_city"
            set_pending(from_id, pending)
            return "请重新输入城市名："
        return "没看懂，请回复：1 确认 / 2 重新输入 / 0 取消"
    return None


def fire_sub(s, today):
    """到点推送一条每日天气。"""
    label = s.get("city_label") or s.get("city") or ""
    try:
        w = fetch_weather(s.get("lat"), s.get("lon"))
        lines = ["🌤 每日推送 {} {}".format(
            time.strftime("%m月%d日", time.localtime()),
            time.strftime("%A", time.localtime()))]
        lines.append(format_weather(label, w))
    except Exception as e:
        log_error("每日推送天气获取失败: {}".format(str(e)[:120]))
        lines = ["🌤 每日推送 {} {}".format(
            time.strftime("%m月%d日", time.localtime()),
            time.strftime("%A", time.localtime()))]
        lines.append("📍 {} 天气获取失败，请稍后查看".format(label))
    text = "\n".join(lines)
    ch = s.get("channel") or "web"
    ok = False
    if ch == "ilink":
        if s.get("room_id"):
            log_error("每日推送跳过：iLink 群聊暂不支持 ({})".format(s.get("id")))
        else:
            outbox_push({"id": "sub-" + str(s.get("id")), "target_id": s.get("from_id"),
                         "text": text, "at": now_str()})
            ok = True
    else:
        if s.get("room_id"):
            name = s.get("from_name") or ""
            ok, resp = bot_send(s.get("room_name") or s.get("room_id"), "@{} {}".format(name, text), True)
        else:
            ok, resp = bot_send(s.get("from_id") or "", text, False, name=s.get("from_name"))
        if not ok:
            log_error("每日推送发送失败: {}".format(str(resp)[:200]))
    with SUBS_LOCK:
        d = load_subs()
        for x in d.get("subscriptions", []):
            if x.get("id") == s.get("id"):
                x["last_sent"] = today
                save_subs(d)
                break
    if ok:
        log_system_event({"type": "subscription", "content": "每日推送已发送: " + text[:100]})


# ---------------- 主动消息出站（ClawBot 网关代发） ----------------
def outbox_push(item):
    try:
        with open(OUTBOX_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception as e:
        log_error(f"写出站消息失败: {e}")


def outbox_pending():
    items = []
    try:
        with open(OUTBOX_FILE, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        items.append(json.loads(ln))
                    except Exception:
                        pass
    except Exception:
        pass
    return items


def outbox_done(ids):
    keep = [it for it in outbox_pending() if it.get("id") not in ids]
    try:
        with open(OUTBOX_FILE, "w", encoding="utf-8") as f:
            for it in keep:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
    except Exception as e:
        log_error(f"更新出站消息失败: {e}")


# ---------------- AI 调用（OpenAI 兼容） ----------------
def ai_config():
    cfg = load_config()
    return cfg.get("ai") or {}


def geocode_city(name):
    """城市名 -> 候选 [{name, admin1, country, lat, lon}]（中文/拼音/英文均可）"""
    key = re.sub(r"[市县区]$", "", (name or "").strip()).lower()
    hit = CITY_ALIASES.get(key) or CITY_ALIASES.get((name or "").strip().lower())
    if hit:
        return [dict(hit)]
    url = ("https://geocoding-api.open-meteo.com/v1/search?name="
           + urllib.parse.quote(name) + "&count=3&language=zh&format=json")
    with urllib.request.urlopen(url, timeout=12) as r:
        data = json.loads(r.read().decode("utf-8", "ignore"))
    out = []
    for it in data.get("results", []):
        out.append({
            "name": it.get("name") or "",
            "admin1": it.get("admin1") or "",
            "country": it.get("country") or "",
            "lat": it.get("latitude"),
            "lon": it.get("longitude"),
        })
    return out


def fetch_weather(lat, lon):
    url = ("https://api.open-meteo.com/v1/forecast?latitude={}&longitude={}"
           "&current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
           "&timezone=Asia%2FShanghai&forecast_days=1").format(lat, lon)
    with urllib.request.urlopen(url, timeout=12) as r:
        d = json.loads(r.read().decode("utf-8", "ignore"))
    c = d.get("current", {})
    day = d.get("daily", {})
    def wtext(code):
        return WMO_CODES.get(code, "未知")
    return {
        "time": c.get("time", ""),
        "temp": c.get("temperature_2m"),
        "feels": c.get("apparent_temperature"),
        "humidity": c.get("relative_humidity_2m"),
        "wind": c.get("wind_speed_10m"),
        "precip": c.get("precipitation"),
        "code": wtext(c.get("weather_code")),
        "tmax": (day.get("temperature_2m_max") or [None])[0],
        "tmin": (day.get("temperature_2m_min") or [None])[0],
        "rain_prob": (day.get("precipitation_probability_max") or [None])[0],
        "day_code": wtext((day.get("weather_code") or [None])[0]),
    }


def format_weather(label, w):
    lines = ["📍 {} 天气".format(label),
             "实时：{}°C，体感 {}°C，湿度 {}%，{}，风速 {} km/h".format(
                 fmt_number(w["temp"]), fmt_number(w["feels"]),
                 w["humidity"] if w["humidity"] is not None else "-",
                 w["code"], w["wind"] if w["wind"] is not None else "-")]
    if w["tmax"] is not None:
        lines.append("今日：{}，最高 {}°C / 最低 {}°C，降水概率 {}%".format(
            w["day_code"], fmt_number(w["tmax"]), fmt_number(w["tmin"]),
            w["rain_prob"] if w["rain_prob"] is not None else "-"))
    return "\n".join(lines)


def _bing_real_url(href):
    """Bing 结果链接是跳转地址，解析 u= 参数还原真实 URL。"""
    try:
        m = re.search(r"[?&]u=([A-Za-z0-9_\-=%]+)", href or "")
        if not m:
            return href
        b = m.group(1).replace("%3D", "=")
        if b.startswith("a1"):
            b = b[2:]
        b += "=" * (-len(b) % 4)
        u = urllib.parse.unquote(
            base64.urlsafe_b64decode(b.encode("utf-8")).decode("utf-8", "ignore"))
        return u if u.startswith("http") else href
    except Exception:
        return href


def web_search(query, max_results=5):
    """Bing 网页搜索（无需 key，国内可访问）。返回格式化结果文本。"""
    url = "https://cn.bing.com/search?q=" + urllib.parse.quote(query) + "&setlang=zh-hans"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read().decode("utf-8", "ignore")
    items = []
    for m in re.finditer(r'<li class="b_algo".*?</li>', body, re.S):
        block = m.group(0)
        hm = re.search(r"<h2[^>]*>\s*<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", block, re.S)
        if not hm:
            continue
        href = html.unescape(hm.group(1))
        title = re.sub(r"<[^>]+>", "", hm.group(2)).strip()
        pm = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = re.sub(r"<[^>]+>", "", pm.group(1)).strip() if pm else ""
        items.append((title, _bing_real_url(href), snippet))
        if len(items) >= max_results:
            break
    if not items:
        return "没有搜到相关结果"
    lines = []
    for i, (t, u, s) in enumerate(items, 1):
        lines.append("{}. {}".format(i, t))
        if s:
            lines.append("   {}".format(s))
        if u and "bing.com/ck" not in u and "go.micro" not in u:
            lines.append("   {}".format(u))
    return "\n".join(lines)


AI_TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "实时搜索互联网，可查最新新闻、体育赛程、比分、天气之外的任何实时信息",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索关键词，尽量具体"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "查询指定城市的实时天气和今日预报，城市可以是中文名、拼音或英文",
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string", "description": "城市名，如：上海、长垣、changyuan"}},
            "required": ["city"]}}},
    {"type": "function", "function": {
        "name": "get_current_datetime",
        "description": "获取当前日期、时间和星期",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "calculate",
        "description": "精确计算数学表达式，支持 + - * / 括号 小数",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string", "description": "数学表达式，如 8*4+2"}},
            "required": ["expression"]}}},
]


def ai_tool_call(name, args):
    try:
        if name == "web_search":
            q = (args.get("query") or "").strip()
            if not q:
                return "缺少搜索关键词"
            return web_search(q)
        if name == "get_weather":
            city = (args.get("city") or "").strip()
            if not city:
                return "缺少城市名"
            cands = geocode_city(city)
            if not cands:
                return "未找到城市：{}，请检查是否有错别字".format(city)
            c = cands[0]
            label = c["name"] + ("·" + c["admin1"] if c.get("admin1") else "")
            return format_weather(label, fetch_weather(c["lat"], c["lon"]))
        if name == "get_current_datetime":
            return time.strftime("%Y-%m-%d %H:%M:%S %A", time.localtime())
        if name == "calculate":
            expr = normalize_expr(args.get("expression") or "")
            if not is_math_expr(expr):
                return "表达式无法计算"
            return "{} = {}".format(args.get("expression"), fmt_number(evaluate_math(expr)))
        return "未知工具"
    except Exception as e:
        return "工具调用失败: {}".format(str(e)[:100])


MAX_TOOL_ROUNDS = 8      # 单次 /ai 最多工具轮次（每轮可能含多个并行工具调用）
TOOL_RETRIES = 2         # 网关 5xx/429/连接失败时的重试次数


def ai_chat(prompt, cfg=None, timeout=40):
    ai = cfg if cfg is not None else ai_config()
    base = (ai.get("base_url") or "").strip().rstrip("/")
    key = (ai.get("api_key") or "").strip()
    model = (ai.get("model") or "").strip()
    if not base or not key or not model:
        raise ValueError("AI 未配置：请在管理后台「AI 配置」填写接口地址 / API Key / 模型")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    messages = [{"role": "user", "content": prompt}]
    seen_calls = set()

    def _call(tools):
        payload = {"model": model, "messages": messages, "stream": False}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        last_err = None
        for attempt in range(TOOL_RETRIES + 1):
            req = urllib.request.Request(
                url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8", "ignore"))
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = (e.read() or b"").decode("utf-8", "ignore")[:300]
                except Exception:
                    pass
                last_err = ValueError("HTTP Error {}: {}".format(e.code, body or e.reason))
                if e.code in (429, 500, 502, 503, 504) and attempt < TOOL_RETRIES:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_err
            except urllib.error.URLError as e:
                last_err = ValueError("连接失败: {}".format(e.reason))
                if attempt < TOOL_RETRIES:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_err
        raise last_err

    for _round in range(MAX_TOOL_ROUNDS):
        data = _call(AI_TOOLS)
        try:
            msg = data["choices"][0]["message"]
        except Exception:
            raise ValueError("AI 返回格式异常: " + json.dumps(data, ensure_ascii=False)[:300])
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            content = (msg.get("content") or "").strip()
            return content or "（无回复）"
        # 去掉与本次回答中重复的工具调用，避免同一查询反复搜索
        fresh = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            key2 = (fn.get("name") or "", fn.get("arguments") or "")
            if key2 in seen_calls:
                continue
            seen_calls.add(key2)
            fresh.append(tc)
        if not fresh:
            break  # 全是重复调用，直接进入不带工具的总结轮
        messages.append({
            "role": "assistant",
            "content": msg.get("content") or None,
            "tool_calls": [
                {"id": tc.get("id") or ("call-%d" % i), "type": "function",
                 "function": {"name": tc.get("function", {}).get("name", ""),
                              "arguments": tc.get("function", {}).get("arguments", "{}")}}
                for i, tc in enumerate(fresh)
            ],
        })
        for tc in fresh:
            fn = tc.get("function", {})
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            result = ai_tool_call(name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or "",
                "content": result,
            })
    # 工具轮次用尽：去掉工具，让模型基于已有搜索结果直接总结，而不是报错
    messages.append({
        "role": "user",
        "content": "请根据上面已有的搜索结果和信息，直接回答我最初的问题。如果信息不足，就如实说明没有查到，"
                   "并告诉我可以去哪个官方渠道查看。不要再调用任何工具。"
    })
    data = _call(None)
    try:
        content = (data["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        raise ValueError("AI 返回格式异常: " + json.dumps(data, ensure_ascii=False)[:300])
    return content or "（没搜到足够信息，请换个问法或稍后再试）"


def ai_fetch_models(timeout=15):
    ai = ai_config()
    base = (ai.get("base_url") or "").strip().rstrip("/")
    key = (ai.get("api_key") or "").strip()
    if not base or not key:
        raise ValueError("请先填写接口地址和 API Key")
    req = urllib.request.Request(
        base + "/models",
        headers={"Authorization": "Bearer " + key},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "ignore"))
    ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
    return ids


# ---------------- 命令处理 ----------------
def handle_command(text, from_id, from_name, room_id, room_name, cfg):
    """返回回复文本；无需回复返回 None。"""
    cmd = text.split(None, 1)[0].lower()
    rest = text[len(cmd):].strip()

    if cmd in ("/ai",):
        if not rest:
            return "⚠️ 用法：/ai 后面加空格再写内容，例如：/ai 今天上海天气怎么样"
        try:
            return "🤖 " + ai_chat(rest)
        except Exception as e:
            log_error(f"AI 调用失败: {e}")
            return "⚠️ AI 调用失败：" + str(e)[:120]

    if cmd in ("/搜索", "/search"):
        if not rest:
            return "⚠️ 用法：/搜索 <内容>，例如：/搜索 今天有什么足球比赛"
        try:
            return "🔎 搜索结果：\n" + web_search(rest)
        except Exception as e:
            log_error(f"搜索失败: {e}")
            return "⚠️ 搜索失败：" + str(e)[:120]

    if cmd == "/记账":
        return do_ledger(rest, from_id)

    if cmd == "/余额":
        s = ledger_summary(from_id)
        if s["count"] == 0:
            return "💰 还没有记账，用 /记账 +算式 或 /记账 -算式 开始吧"
        return (f"💰 我的余额：{fmt_signed(s['balance'])} ｜ 共 {s['count']} 笔"
                f"（收入 +{fmt_number(s['income'])}，支出 {fmt_number(s['expense'])}）")

    if cmd == "/明细":
        s = ledger_summary(from_id)
        if s["count"] == 0:
            return "📋 还没有记账记录"
        lines = [f"📋 我的全部记账（共 {s['count']} 笔）："]
        for e in s["entries"][-50:]:
            note = f"（{e['note']}）" if e.get("note") else ""
            lines.append(f"  {e['t'][5:16]}  {e['op']}{e['expr']} → {fmt_signed(e['amount'])}{note}")
        lines.append(f"  当前余额：{fmt_signed(s['balance'])}")
        if s["count"] > 50:
            lines.append(f"  （仅显示最近 50 笔，共 {s['count']} 笔）")
        return "\n".join(lines)

    if cmd == "/清空":
        n = ledger_clear(from_id)
        return f"🗑️ 已清空我的全部记账（{n} 笔），余额归 0。"

    if cmd == "/说明":
        return DETAIL_HELP

    if cmd == "/权限":
        if not rest:
            return "🔑 用法：/权限 <密码>，密码正确就授权成功（开通当前账号的 AI）"
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                admin_pwd = f.read().strip()
        except Exception:
            admin_pwd = ""
        if rest.strip() == admin_pwd:
            grant_member(from_id, from_name, "password-cmd")
            return "✅ 授权成功！你现在可以使用 /ai 了（工具：天气 / 时间 / 精确计算 / 联网搜索）。"
        return "❌ 密码错误"

    if cmd in ("/帮助", "/help", "/指令"):
        return FALLBACK_HELP

    if cmd == "/提醒":
        return do_remind(rest, from_id, from_name, room_id, room_name)

    if cmd == "/推送":
        return cmd_push(rest, from_id, from_name, room_id, room_name)

    if cmd == "/取消推送":
        return cmd_unsub(rest, from_id)

    if cmd == "/提醒列表":
        with REMINDER_LOCK:
            data = load_reminders()
            mine = [r for r in data["reminders"] if r.get("from_id") == from_id]
        if not mine:
            return "⏰ 你还没有设置提醒"
        lines = [f"⏰ 我的提醒（共 {len(mine)} 个）："]
        for r in sorted(mine, key=lambda x: x.get("at", 0)):
            lines.append(f"  {r['id']}  {fmt_remind_time(r['at'])}  {r['text']}")
        lines.append("  取消：/取消提醒 <编号>")
        return "\n".join(lines)

    if cmd == "/取消提醒":
        rid = (rest.split()[0] if rest.split() else "").strip()
        if not rid:
            return "⚠️ 用法：/取消提醒 <编号>（编号见 /提醒列表）"
        if cancel_reminder(from_id, rid):
            return f"🗑️ 已取消提醒 {rid}"
        return f"⚠️ 没有找到编号为 {rid} 的提醒（只能取消自己的）"

    return None


def do_ledger(rest, from_id):
    rest = rest.strip()
    if not rest:
        return "⚠️ 记账用法：\n  /记账 +8×4 → 记收入 32\n  /记账 -15×2 → 记支出 30\n  支持备注：/记账 +100 买菜"
    if rest[0] not in ("+", "-"):
        return "⚠️ 记账用 + 和 - 开头，不支持「加/减」：\n  /记账 +100  记收入\n  /记账 -100  记支出"
    op = rest[0]
    body = rest[1:].strip()
    if not body:
        return "⚠️ + 或 - 后面要跟算式，例如：/记账 +8×4"
    # 尝试整个作为表达式；不行则取第一个空格前为表达式，其余为备注
    expr = body
    note = ""
    if not is_math_expr(normalize_expr(expr)):
        parts = body.split(None, 1)
        if len(parts) == 2 and is_math_expr(normalize_expr(parts[0])):
            expr, note = parts[0], parts[1]
    expr_n = normalize_expr(expr)
    if not is_math_expr(expr_n):
        return "⚠️ 算式算不出来，例如：/记账 +8×4"
    try:
        val = evaluate_math(expr_n)
        amount = val if op == "+" else -val
    except ZeroDivisionError:
        return "⚠️ 除数不能为 0"
    except Exception:
        return "⚠️ 算式算不出来，例如：/记账 +8×4"
    ledger_entry(from_id, op, expr.strip(), note, amount)
    s = ledger_summary(from_id)
    note_s = f"（{note}）" if note else ""
    return (f"✓ 已记账：{op}{expr.strip()} = {fmt_signed(amount)}{note_s}"
            f" ｜ 余额：{fmt_signed(s['balance'])}")


_REMIND_RE = re.compile(
    r"^(\d{4}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}"       # 2026-08-08 09:00
    r"|\d{1,2}:\d{2}"                                     # 14:30
    r"|\d+\s*秒(?:钟)?(?:后)?"                             # 30秒后
    r"|\d+\s*分钟?(?:钟)?(?:后)?"                          # 10分钟后
    r"|\d+\s*小时?(?:后)?"                                 # 2小时后
    r")\s*(.*)$", re.S)


def do_remind(rest, from_id, from_name, room_id, room_name=""):
    rest = rest.strip()
    if not rest:
        return "⚠️ 提醒用法：/提醒 <时间> <内容>\n  时间：10分钟后 / 2小时后 / 14:30 / 2026-08-08 09:00\n  例：/提醒 10分钟后 喝水"
    m = _REMIND_RE.match(rest)
    if not m:
        return "⚠️ 提醒用法：/提醒 <时间> <内容>，例如：/提醒 10分钟后 喝水"
    ts = parse_remind_time(m.group(1))
    content = (m.group(2) or "").strip()
    if not ts:
        return ("⚠️ 时间格式不认识，支持：\n  10分钟后 / 2小时后\n  14:30（今天，过了则明天）\n  2026-08-08 09:00")
    if not content:
        return "⚠️ 提醒内容不能为空，例如：/提醒 10分钟后 喝水"
    rid = add_reminder(from_id, from_name, room_id, room_name, ts, content)
    return f"⏰ 提醒已设置（编号 {rid}）：{content}（{fmt_remind_time(ts)}）"


def reminder_worker():
    """定时器线程：到点触发提醒 + 每日推送订阅。ilink 入出站队列，web 直接调旧机器人发送。"""
    while True:
        time.sleep(15)
        try:
            with REMINDER_LOCK:
                data = load_reminders()
                now = time.time()
                due = [r for r in data["reminders"] if r.get("at", 0) <= now]
                if due:
                    data["reminders"] = [r for r in data["reminders"] if r.get("at", 0) > now]
                    save_reminders(data)
            for r in due:
                _fire_reminder(r)
            today = time.strftime("%Y-%m-%d", time.localtime())
            hm = time.strftime("%H:%M", time.localtime())
            with SUBS_LOCK:
                sdata = load_subs()
                due_subs = [dict(s) for s in sdata.get("subscriptions", [])
                            if s.get("time") == hm and s.get("last_sent") != today]
            for s in due_subs:
                fire_sub(s, today)
        except Exception as e:
            log_error(f"定时器线程异常: {e}")


def _fire_reminder(r):
    try:
        ch = channel_of(r.get("from_id"), r.get("room_id"))
        text = f"⏰ 提醒：{r.get('text', '')}"
        if ch == "ilink":
            if r.get("room_id"):
                log_error(f"提醒跳过：iLink 暂不支持群聊推送 ({r.get('id')})")
                return
            outbox_push({
                "id": r["id"], "target_id": r.get("from_id"), "text": text, "at": now_str(),
            })
            log_system_event({"type": "reminder", "content": f"提醒已入 iLink 出站队列: {text}"})
            return
        # web 通道：群聊按群名发，私聊按 id 发（失败自动按昵称重试）
        if r.get("room_id"):
            name = r.get("from_name") or r.get("from_id") or ""
            text = f"@{name} {text}"
            ok, resp = bot_send(r.get("room_name") or r["room_id"], text, True)
        else:
            ok, resp = bot_send(r.get("from_id") or "", text, False, name=r.get("from_name"))
        if ok:
            log_system_event({"type": "reminder", "content": f"提醒已发送: {text}"})
        else:
            log_error(f"提醒发送失败: {str(resp)[:200]}")
            outbox_push({"id": "retry-" + r["id"], "target_id": r.get("from_id"),
                         "text": text, "at": now_str(), "retry": True})
    except Exception as e:
        log_error(f"触发提醒异常: {e}")


# ---------------- 前端页面 ----------------
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>微信机器人管理后台 - 登录</title>
<style>
body{font-family:-apple-system,'PingFang SC',sans-serif;background:#eef1f5;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}
.card{background:#fff;border-radius:10px;padding:36px 40px;box-shadow:0 4px 18px rgba(0,0,0,.08);width:320px}
h1{font-size:22px;margin:0 0 18px;text-align:center}
input{width:100%;box-sizing:border-box;padding:11px 12px;border:1px solid #ccd2da;border-radius:6px;font-size:17px;margin-bottom:12px}
button{width:100%;padding:11px;background:#2f6fed;color:#fff;border:0;border-radius:6px;font-size:17px;cursor:pointer}
button:hover{background:#2658c4}
.err{color:#d33;font-size:15px;text-align:center;margin-top:10px}
.tip{color:#888;font-size:14px;text-align:center;margin-top:14px}
</style></head>
<body><div class="card">
<h1>微信机器人管理后台</h1>
<form method="post" action="/login">
<input type="password" name="token" placeholder="访问口令" autocomplete="current-password" autofocus required>
<button type="submit">登 录</button>
</form>
<div class="err">__ERR__</div>
<div class="tip">口令保存在服务器 /root/wxbot-reply/.view_token</div>
</div></body></html>"""

ADMIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>微信机器人管理后台</title>
<style>
body{font-family:-apple-system,'PingFang SC',sans-serif;margin:0;background:#eef1f5;color:#222;font-size:16px}
header{background:#1f2d3d;color:#fff;padding:0 20px;height:52px;display:flex;align-items:center;justify-content:space-between}
header h1{font-size:19px;margin:0}
header .right{display:flex;gap:12px;align-items:center;font-size:15px}
header a,header button{color:#cfe0ff;background:none;border:0;cursor:pointer;font-size:15px;text-decoration:none}
nav{display:flex;background:#fff;border-bottom:1px solid #e2e6ec;padding:0 20px;gap:4px}
nav button{border:0;background:none;padding:12px 16px;font-size:16px;cursor:pointer;color:#555;border-bottom:2px solid transparent}
nav button.active{color:#2f6fed;border-bottom-color:#2f6fed;font-weight:600}
main{padding:20px;max-width:1200px;margin:0 auto}
.tab{display:none}
.tab.active{display:block}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-bottom:18px}
.card{background:#fff;border-radius:8px;padding:16px;box-shadow:0 2px 8px rgba(0,0,0,.05)}
.card .label{font-size:14px;color:#888;margin-bottom:6px}
.card .value{font-size:24px;font-weight:600}
.card .sub{font-size:14px;color:#666;margin-top:6px;word-break:break-all}
.ok{color:#0a7d33}.bad{color:#c0392b}.warn{color:#b7791f}
table{width:100%;border-collapse:collapse;background:#fff;font-size:15px;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.05)}
th,td{border-bottom:1px solid #eef0f4;padding:8px 10px;text-align:left;vertical-align:top}
th{background:#f6f8fa;font-weight:600;white-space:nowrap}
.plain{white-space:pre-wrap;word-break:break-all;max-width:420px}
.reply{color:#0a7d33}
.dim{color:#999}
.toolbar{display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
.toolbar input[type=text]{padding:8px 10px;border:1px solid #ccd2da;border-radius:6px;font-size:15px;min-width:240px}
.toolbar button,.btn{padding:8px 14px;border:0;border-radius:6px;font-size:15px;cursor:pointer;background:#2f6fed;color:#fff}
.toolbar button:hover{background:#2658c4}
.btn2{background:#64748b}
.btn2:hover{background:#4b5a6d}
pre{background:#0f1b2d;color:#c9d6e8;padding:14px;border-radius:8px;font-size:14px;overflow:auto;max-height:480px;white-space:pre-wrap;word-break:break-all}
.panel{background:#fff;border-radius:8px;padding:16px;box-shadow:0 2px 8px rgba(0,0,0,.05);margin-bottom:16px}
.panel h3{margin:0 0 12px;font-size:17px}
label.switch{display:inline-flex;align-items:center;gap:8px;font-size:16px;cursor:pointer}
form.inline{display:flex;flex-direction:column;gap:10px;max-width:560px}
form.inline label{font-size:15px;color:#555}
form.inline input[type=text],form.inline textarea{padding:9px 10px;border:1px solid #ccd2da;border-radius:6px;font-size:15px;font-family:inherit}
form.inline textarea{min-height:60px}
form.inline .row{display:flex;gap:12px;align-items:center}
a.link{color:#2f6fed;font-size:15px}
.badge{padding:2px 8px;border-radius:10px;font-size:14px}
.badge.on{background:#dff5e5;color:#0a7d33}
.badge.off{background:#fdeaea;color:#c0392b}
</style></head>
<body>
<header><h1>🤖 微信机器人管理后台</h1>
<div class="right"><span id="hdr-status">连接中…</span><a href="/api/export" download>下载完整记录</a><form method="post" action="/logout" style="display:inline"><button type="submit">退出登录</button></form></div>
</header>
<nav>
<button class="active" onclick="switchTab('tab-status',this)">状态总览</button>
<button onclick="switchTab('tab-overview',this)">用户总览</button>
<button onclick="switchTab('tab-msgs',this)">消息记录</button>
<button onclick="switchTab('tab-logs',this)">日志与报错</button>
<button onclick="switchTab('tab-mgmt',this)">管理操作</button>
<button onclick="switchTab('tab-users',this)">用户与权限</button>
<button onclick="switchTab('tab-ai',this)">AI 配置</button>
</nav>
<main>
<div id="tab-status" class="tab active">
  <div class="cards">
    <div class="card"><div class="label">机器人登录状态</div><div class="value" id="bot-status">-</div><div class="sub" id="bot-status-sub"></div></div>
    <div class="card"><div class="label">自动回复</div><div class="value" id="auto-reply-badge">-</div><div class="sub">管理操作页可开关</div></div>
    <div class="card"><div class="label">今日消息数</div><div class="value" id="today-count">-</div><div class="sub" id="today-count-sub"></div></div>
    <div class="card"><div class="label">累计消息数</div><div class="value" id="total-count">-</div></div>
    <div class="card"><div class="label">授权用户数</div><div class="value" id="perm-count">-</div><div class="sub" id="perm-count-sub"></div></div>
  </div>
  <div class="panel"><h3>最近一条消息</h3><div class="sub" id="last-msg" style="font-size:15px">-</div></div>
  <div class="panel"><h3>最近报错</h3><div class="sub" id="last-error" style="font-size:15px;color:#c0392b">-</div></div>
</div>

<div id="tab-overview" class="tab">
  <div class="cards">
    <div class="card"><div class="label">总用户数</div><div class="value" id="ov-total">-</div></div>
    <div class="card"><div class="label">已授权</div><div class="value" id="ov-members">-</div></div>
    <div class="card"><div class="label">每日推送</div><div class="value" id="ov-subs">-</div></div>
    <div class="card"><div class="label">待触发提醒</div><div class="value" id="ov-reminders">-</div></div>
  </div>
  <div class="panel">
    <h3>用户功能总览</h3>
    <div class="sub" style="font-size:15px;margin-bottom:10px">每位用户设置的 每日推送 / 定时提醒 / 记账 / AI 使用 一览（含未授权用户）。</div>
    <div id="overview-list">-</div>
  </div>
</div>

<div id="tab-msgs" class="tab">
  <div class="toolbar">
    <input type="text" id="msg-q" placeholder="搜索：内容 / 发送者 / 群名" onkeydown="if(event.key==='Enter')loadMessages(0)">
    <button onclick="loadMessages(0)">搜索</button>
    <span id="msg-page" style="font-size:15px;color:#666"></span>
  </div>
  <table><thead><tr><th>时间</th><th>会话</th><th>发送者</th><th>内容</th><th>@我</th><th>自动回复</th></tr></thead>
  <tbody id="msg-body"></tbody></table>
  <div class="toolbar" style="margin-top:12px">
    <button onclick="loadMessages((window._mpage||0)-1)">上一页</button>
    <button onclick="loadMessages((window._mpage||0)+1)">下一页</button>
    <label style="font-size:15px;color:#666"><input type="checkbox" id="msg-auto" onchange="msgAuto=this.checked"> 自动刷新</label>
  </div>
</div>

<div id="tab-logs" class="tab">
  <div class="toolbar">
    <label style="font-size:15px"><input type="checkbox" id="log-err-only" onchange="loadLogs()"> 只看报错</label>
    <label style="font-size:15px"><input type="checkbox" id="log-auto" checked onchange="toggleLogAuto()"> 自动刷新(5秒)</label>
  </div>
  <div class="panel"><h3>机器人日志（尾部 200 行）</h3><pre id="bot-log">加载中…</pre></div>
  <div class="panel"><h3>服务报错（error.log）</h3><pre id="err-log">-</pre></div>
  <div class="panel"><h3>系统事件（登录/登出/异常，system_events.log）</h3><pre id="sys-log">-</pre></div>
</div>

<div id="tab-mgmt" class="tab">
  <div class="panel">
    <h3>自动回复开关</h3>
    <label class="switch"><input type="checkbox" id="auto-reply" onchange="saveAutoReply()"> 开启自动回复（关闭后只记录、不回复）</label>
  </div>
  <div class="panel">
    <h3>发送测试消息</h3>
    <form class="inline" onsubmit="event.preventDefault();sendMsg()">
      <label>接收方（微信昵称；群聊请填群名并勾选“群聊”）</label>
      <input type="text" id="send-to" placeholder="例如：张三">
      <label class="row"><input type="checkbox" id="send-room"> 群聊消息</label>
      <label>内容</label>
      <textarea id="send-content" placeholder="例如：你好"></textarea>
      <div class="row"><button type="submit">发 送</button><span id="send-result" style="font-size:14px;color:#666;word-break:break-all"></span></div>
    </form>
  </div>
  <div class="panel">
    <h3>扫码登录</h3>
    <div class="sub" style="font-size:15px">机器人在线状态：<span id="login-url-wrap"></span></div>
  </div>
  <div class="panel">
    <h3>定时提醒（全部用户，到点自动触发）</h3>
    <div id="reminder-list">-</div>
  </div>
  <div class="panel">
    <h3>每日推送订阅（用户开启 /推送 后按时间自动推送天气）</h3>
    <div id="subs-list">-</div>
  </div>
</div>

<div id="tab-users" class="tab">
  <div class="panel">
    <h3>授权用户（授权后才能用 AI；未授权仍可用基础功能）</h3>
    <div class="sub" style="font-size:15px">规则：所有人可用 计算 / 记账 / 余额 / 明细 / 提醒 / 说明；只有已授权用户能用 /ai 等 AI 功能（授权/取消在下方表格操作）。</div>
    <div id="user-list">-</div>
  </div>
  <div class="panel">
    <h3>手动添加用户</h3>
    <form class="inline" onsubmit="event.preventDefault();grantManual()">
      <label>微信 ID（fromId）</label>
      <input type="text" id="grant-id" placeholder="粘贴 fromId">
      <label>昵称（可选）</label>
      <input type="text" id="grant-name" placeholder="例如：张三">
      <div class="row"><button type="submit">授权此人</button><span id="grant-result" style="font-size:15px;color:#666"></span></div>
    </form>
  </div>
</div>

<div id="tab-ai" class="tab">
  <div class="panel">
    <h3>AI 接口配置（供 /ai 命令使用，OpenAI 兼容格式）</h3>
    <form class="inline" onsubmit="event.preventDefault();saveAI()">
      <label>接口地址 Base URL（如 https://api.deepseek.com 或 https://你的网关/v1）</label>
      <input type="text" id="ai-base" placeholder="https://api.example.com" autocomplete="off">
      <label>API Key</label>
      <input type="text" id="ai-key" placeholder="sk-..." autocomplete="off">
      <label>模型 <span id="ai-model-status" class="dim"></span></label>
      <div class="row">
        <input type="text" id="ai-model" placeholder="模型名，如 deepseek-chat" style="flex:1">
        <button class="btn2" type="button" onclick="fetchModels()">获取模型列表</button>
      </div>
      <div class="row">
        <button type="submit">保存配置</button>
        <button class="btn2" type="button" onclick="testAI()">测试连接</button>
        <span id="ai-result" style="font-size:15px;color:#666;word-break:break-all"></span>
      </div>
      <div class="dim" style="font-size:14px">保存后机器人在微信里发 /ai 内容 即可使用；AI 为单次问答，不携带上下文。</div>
    </form>
  </div>
</div>
</main>
<script>
function $(id){return document.getElementById(id)}
function esc(s){var d=document.createElement('div');d.textContent=(s==null?'':String(s));return d.innerHTML}
function switchTab(id,btn){
  document.querySelectorAll('.tab').forEach(function(t){t.classList.remove('active')});
  document.querySelectorAll('nav button').forEach(function(b){b.classList.remove('active')});
  $(id).classList.add('active');btn.classList.add('active');
}
async function api(path,opts){
  var r=await fetch(path,opts||{});
  if(r.status===401){location.href='/login';throw new Error('unauthorized')}
  return r.json();
}
function fmtTime(t){return t||''}
async function refreshStatus(){
  try{
    var s=await api('/api/status');
    var b=$('bot-status');
    if(s.bot.logged_in){b.textContent='已登录';b.className='value ok';$('hdr-status').textContent='机器人已登录';}
    else if(s.bot.reachable){b.textContent='未登录';b.className='value warn';$('hdr-status').textContent='机器人未登录';}
    else{b.textContent='连接失败';b.className='value bad';$('hdr-status').textContent='机器人连接失败';}
    $('bot-status-sub').textContent='服务端口: '+s.bot.raw;
    var ab=$('auto-reply-badge');
    ab.textContent=s.config.auto_reply?'已开启':'已关闭';
    ab.className='value '+(s.config.auto_reply?'ok':'bad');
    $('today-count').textContent=s.stats.today;
    $('total-count').textContent=s.stats.total;
    if(s.perm){
      $('perm-count').textContent=s.perm.members;
      $('perm-count-sub').textContent='授权后才能用 AI';
    }
    $('today-count-sub').textContent='记录起始: '+s.stats.since;
    var lm=s.stats.last;
    $('last-msg').textContent=lm?(fmtTime(lm.time)+' | '+(lm.room||'私聊')+' | '+lm.from+': '+lm.content+(lm.reply?'  → '+lm.reply:'')):'暂无消息';
    $('last-error').textContent=s.stats.last_error||'暂无报错';
    $('auto-reply').checked=!!s.config.auto_reply;
    $('login-url-wrap').innerHTML='<a class="link" target="_blank" href="'+esc(s.login_url)+'">打开扫码登录页</a>';
  }catch(e){}
}
async function loadMessages(page){
  var q=encodeURIComponent($('msg-q').value.trim());
  try{
    var d=await api('/api/messages?page='+(page||0)+'&q='+q);
    var body=$('msg-body');body.innerHTML='';
    if(!d.items.length){body.innerHTML='<tr><td colspan="6" style="color:#999">暂无消息</td></tr>'}
    d.items.forEach(function(m){
      var tr=document.createElement('tr');
      tr.innerHTML='<td>'+esc(fmtTime(m.time))+'</td><td>'+esc(m.room||'私聊')+'</td><td>'+esc(m.from)+(m.fromId?'<div class="dim">'+esc(m.fromId)+'</div>':'')+'</td>'
        +'<td class="plain">'+esc(m.content)+'</td><td>'+(m.isMentioned?'是':'')+'</td>'
        +'<td class="reply">'+esc(m.reply||'')+'</td>';
      body.appendChild(tr);
    });
    var pages=Math.max(1,Math.ceil(d.total/d.limit));
    $('msg-page').textContent='第 '+(d.page+1)+' / '+pages+' 页（共 '+d.total+' 条）';
    window._mpage=d.page;
  }catch(e){}
}
async function loadLogs(){
  try{
    var err=$('log-err-only').checked?'1':'0';
    var d=await api('/api/logs?err='+err);
    $('bot-log').textContent=(d.botLines||[]).join('\\n')||'（无）';
    $('err-log').textContent=(d.errorLines||[]).join('\\n')||'（无报错）';
    $('sys-log').textContent=(d.systemLines||[]).join('\\n')||'（无系统事件）';
  }catch(e){}
}
function toggleLogAuto(){if($('log-auto').checked){loadLogs()}}
async function saveAutoReply(){
  var on=$('auto-reply').checked;
  await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({auto_reply:on})});
  refreshStatus();
}
async function sendMsg(){
  var body={to:$('send-to').value.trim(),isRoom:$('send-room').checked,content:$('send-content').value};
  try{
    var r=await api('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    $('send-result').textContent=JSON.stringify(r);
  }catch(e){$('send-result').textContent='请求失败'}
}
async function loadAI(){
  try{
    var d=await api('/api/ai');var a=d.ai||{};
    $('ai-base').value=a.base_url||'';$('ai-key').value=a.api_key||'';$('ai-model').value=a.model||'';
    $('ai-model-status').textContent=a.base_url?(a.model?'已配置 '+esc(a.model):'已填地址，未配模型'):'未配置';
  }catch(e){}
}
async function saveAI(){
  var body={base_url:$('ai-base').value.trim(),api_key:$('ai-key').value.trim(),model:$('ai-model').value.trim()};
  try{
    var d=await api('/api/ai',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    $('ai-result').textContent='✅ 已保存';loadAI();
  }catch(e){$('ai-result').textContent='保存失败'}
}
async function testAI(){
  $('ai-result').textContent='测试中…';
  var body={base_url:$('ai-base').value.trim(),api_key:$('ai-key').value.trim(),model:$('ai-model').value.trim()};
  try{
    var d=await api('/api/ai/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    $('ai-result').textContent=d.success?('✅ '+esc(d.reply).slice(0,120)):('❌ '+esc(d.message||'未知错误'));
  }catch(e){$('ai-result').textContent='请求失败'}
}
async function fetchModels(){
  $('ai-model-status').textContent='获取中…';
  try{
    var d=await api('/api/ai/models');
    if(!d.success){$('ai-model-status').textContent='❌ '+esc(d.message);return}
    if(d.models&&d.models.length){
      var opts=d.models.map(function(m){return '<option value="'+esc(m)+'">'+esc(m)+'</option>'}).join('');
      var cur=$('ai-model').value.trim();
      $('ai-model').outerHTML='<input type="text" id="ai-model" list="ai-model-list" placeholder="模型名" style="flex:1"><datalist id="ai-model-list">'+opts+'</datalist>';
      $('ai-model').value=cur;
    }
    $('ai-model-status').textContent='✅ 共 '+(d.models?d.models.length:0)+' 个模型（可在输入框下拉选择）';
  }catch(e){$('ai-model-status').textContent='获取失败'}
}
function fmtRemind(at){
  if(!at)return '';
  var d=new Date(at*1000);function p(n){return (n<10?'0':'')+n}
  return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes());
}
async function loadReminders(){
  try{
    var d=await api('/api/reminders');var rs=d.reminders||[];
    if(!rs.length){$('reminder-list').innerHTML='<span class="dim">暂无提醒</span>';return}
    var h='<table><thead><tr><th>编号</th><th>触发时间</th><th>发送者</th><th>内容</th><th>操作</th></tr></thead><tbody>';
    rs.sort(function(a,b){return (a.at||0)-(b.at||0)}).forEach(function(r){
      h+='<tr><td>'+esc(r.id)+'</td><td>'+esc(fmtRemind(r.at))+'</td><td>'+esc(r.from_name||r.from_id)+'</td>'
        +'<td class="plain">'+esc(r.text)+'</td><td><button class="btn" data-rid="'+esc(r.id)+'" onclick="cancelReminder(this)">取消</button></td></tr>';
    });
    $('reminder-list').innerHTML=h+'</tbody></table>';
  }catch(e){}
}
async function cancelReminder(btn){
  var id=btn?btn.getAttribute('data-rid'):'';
  if(!id)return;
  try{
    await api('/api/reminders/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})});
    loadReminders();
  }catch(e){}
}
async function loadSubs(){
  try{
    var d=await api('/api/subs');var ss=d.subs||[];
    if(!ss.length){$('subs-list').innerHTML='<span class="dim">暂无订阅</span>';return}
    var h='<table><thead><tr><th>时间</th><th>用户</th><th>推送位置</th><th>城市</th><th>最近推送</th><th>操作</th></tr></thead><tbody>';
    ss.forEach(function(s){
      var where=s.room_id?('群聊 '+esc(s.room_name||s.room_id)):'私聊';
      h+='<tr><td>'+esc(s.time)+'</td><td>'+esc(s.from_name||s.from_id)+'</td><td>'+where+'</td>'
        +'<td>'+esc(s.city_label||s.city)+'</td><td>'+esc(s.last_sent||'-')+'</td>'
        +'<td><button class="btn" data-rid="'+esc(s.id)+'" onclick="cancelSub(this)">取消</button></td></tr>';
    });
    $('subs-list').innerHTML=h+'</tbody></table>';
  }catch(e){}
}
async function cancelSub(btn){
  var id=btn?btn.getAttribute('data-rid'):'';
  if(!id)return;
  try{
    await api('/api/subs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'cancel',id:id})});
    loadSubs();
  }catch(e){}
}
async function loadOverview(){
  try{
    var d=await api('/api/overview');
    var s=d.summary||{};
    $('ov-total').textContent=s.total||0;
    $('ov-members').textContent=s.members||0;
    $('ov-subs').textContent=s.subs||0;
    $('ov-reminders').textContent=s.reminders||0;
    var us=d.users||[];
    if(!us.length){$('overview-list').innerHTML='<span class="dim">暂无用户</span>';return}
    var h='<table><thead><tr><th>用户</th><th>状态</th><th>每日推送</th><th>提醒</th><th>记账</th><th>AI 使用</th><th>最后活跃</th></tr></thead><tbody>';
    us.forEach(function(u){
      var roleTxt=u.role?'<span class="badge on">已授权</span>':'<span class="badge off">未授权</span>';
      var subsTxt='—';
      if(u.subs&&u.subs.length){
        subsTxt=u.subs.map(function(x){return x.time+' '+(x.room?('群·'+x.room):'私聊')+' '+x.city}).join('<br>');
      }
      var aiTxt=u.ai_count?('共 '+u.ai_count+' 次'+(u.ai_last?'<br><span class="dim">'+esc(u.ai_last)+'</span>':'')):'—';
      var balance=(Number(u.balance)>0?'+':'')+u.balance;
      h+='<tr><td>'+esc(u.name)+'<br><span class="dim">'+esc(u.fromId)+'</span></td><td>'+roleTxt+'</td>'
        +'<td>'+subsTxt+'</td><td>'+u.reminders+' 条</td>'
        +'<td>'+esc(balance)+'（'+u.count+'笔）</td><td>'+aiTxt+'</td>'
        +'<td>'+esc(u.last_seen||'-')+'</td></tr>';
    });
    $('overview-list').innerHTML=h+'</tbody></table>';
  }catch(e){}
}
function fmtSigned(v){v=Number(v||0);return (v>0?'+':'')+v}
async function loadUsers(){
  try{
    var d=await api('/api/users');var us=d.users||[];
    if(!us.length){$('user-list').innerHTML='<span class="dim">还没有见过任何用户（有人发消息后才会出现在这里）</span>';return}
    var h='<table><thead><tr><th>昵称</th><th>微信ID</th><th>最后活跃</th><th>余额</th><th>状态</th><th>操作</th></tr></thead><tbody>';
    us.forEach(function(u){
      var roleTxt=u.role==='member'?'<span class="badge on">已授权（含 AI）</span>':'<span class="badge off">未授权（基础功能）</span>';
      var btns=u.role==='member'
        ?'<button class="btn btn2" data-a="revoke" data-f="'+esc(u.fromId)+'" onclick="userAct(this)">取消授权</button>'
        :'<button class="btn" data-a="grant" data-f="'+esc(u.fromId)+'" onclick="userAct(this)">授权</button>';
      h+='<tr><td>'+esc(u.name)+'</td><td class="plain">'+esc(u.fromId)+'</td><td>'+esc(u.last_seen)+'</td>'
        +'<td>'+esc(fmtSigned(u.balance))+'</td><td>'+roleTxt+'</td><td>'+btns+'</td></tr>';
    });
    $('user-list').innerHTML=h+'</tbody></table>';
  }catch(e){}
}
async function userAct(btn){
  var action=btn.getAttribute('data-a');
  var fid=btn.getAttribute('data-f');
  if(!action||!fid)return;
  try{
    await api('/api/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:action,from_id:fid})});
    loadUsers();refreshStatus();
  }catch(e){}
}
async function grantManual(){
  var fid=$('grant-id').value.trim();
  if(!fid){$('grant-result').textContent='请填微信 ID';return}
  try{
    await api('/api/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'grant',from_id:fid,name:$('grant-name').value.trim()})});
    $('grant-result').textContent='✅ 已授权';$('grant-id').value='';$('grant-name').value='';loadUsers();refreshStatus();
  }catch(e){$('grant-result').textContent='授权失败'}
}
refreshStatus();
loadMessages(0);
loadLogs();
loadAI();
loadUsers();
loadReminders();
loadSubs();
loadOverview();
setInterval(refreshStatus,5000);
setInterval(function(){if(msgAuto)loadMessages(window._mpage||0)},8000);
setInterval(function(){if($('log-auto')&&$('log-auto').checked)loadLogs()},5000);
setInterval(loadReminders,30000);
setInterval(loadSubs,30000);
setInterval(loadOverview,30000);
setInterval(loadUsers,30000);
</script>
</body></html>"""


# ---------------- HTTP Handler ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "WxBotAdmin/1.0"

    def log_message(self, fmt, *args):
        pass

    # ---- 基础工具 ----
    def _send(self, obj, status=200, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(obj, (dict, list)):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        elif isinstance(obj, str):
            data = obj.encode("utf-8")
        else:
            data = obj
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _json(self, obj, status=200):
        self._send(obj, status)

    def _redirect(self, loc):
        self._send("", 302, "text/plain", {"Location": loc})

    def _cookie(self, name):
        c = self.headers.get("Cookie") or ""
        m = re.search(name + r"=([^;]+)", c)
        return m.group(1) if m else ""

    def _authed(self):
        return valid_session(self._cookie("wxbot_admin"))

    def _require_auth(self):
        if not self._authed():
            self._json({"success": False, "message": "unauthorized"}, 401)
            return False
        return True

    # ---- GET ----
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        if path == "/healthz":
            self._json({"status": "ok"})
            return
        if path == "/messages" and not getattr(self.server, "is_public", False):
            with _recent_lock:
                data = list(_recent)
            self._json({"recent": data})
            return
        if path == "/pending_ilink" and not getattr(self.server, "is_public", False):
            self._json({"items": outbox_pending()})
            return
        if getattr(self.server, "is_public", False):
            self._public_get(path, url)
        else:
            self._json({"success": False, "message": "not found"}, 404)

    def _public_get(self, path, url):
        if path in ("/", "/index.html"):
            if not self._authed():
                self._redirect("/login")
            else:
                self._send(ADMIN_PAGE, 200, "text/html; charset=utf-8")
            return
        if path == "/login":
            if self._authed():
                self._redirect("/")
            else:
                self._send(LOGIN_PAGE.replace("__ERR__", ""), 200, "text/html; charset=utf-8")
            return
        if path == "/api/export":
            if not self._require_auth():
                return
            data = tail_file(LOG_FILE, 100000)
            self._send("\n".join(data), 200, "text/plain; charset=utf-8")
            return
        if path.startswith("/api/"):
            if not self._require_auth():
                return
            self._api_get(path, url)
            return
        self._json({"success": False, "message": "not found"}, 404)

    def _api_get(self, path, url):
        qs = urllib.parse.parse_qs(url.query)
        if path == "/api/status":
            bot = bot_status()
            cfg = load_config()
            with PERM_LOCK:
                _p = load_permissions()
            self._json({
                "perm": {
                    "members": len(_p.get("members", {})),
                },
                "bot": bot,
                "config": cfg,
                "stats": {
                    "total": STATS["total"],
                    "today": STATS["today"],
                    "last": STATS["last"],
                    "last_error": STATS["last_error"],
                    "since": STATS.get("since", ""),
                },
                "login_url": PUBLIC_BASE + "/login?token=" + bot_token(),
            })
        elif path == "/api/messages":
            q = (qs.get("q") or [""])[0].strip()
            try:
                page = max(0, int((qs.get("page") or ["0"])[0]))
            except Exception:
                page = 0
            limit = 50
            items = read_messages()
            if q:
                low = q.lower()
                items = [m for m in items if low in (m.get("content") or "").lower()
                         or low in (m.get("from") or "").lower()
                         or low in (m.get("room") or "").lower()]
            total = len(items)
            items = list(reversed(items))[page * limit:(page + 1) * limit]
            self._json({"total": total, "page": page, "limit": limit, "items": items})
        elif path == "/api/logs":
            only_err = (qs.get("err") or ["0"])[0] == "1"
            bot_lines = self._bot_log_lines(only_err)
            error_lines = tail_file(ERROR_LOG, 200)
            system_lines = tail_file(SYSTEM_LOG, 200)
            self._json({
                "botLines": bot_lines,
                "errorLines": [l.get("time") + " " + l.get("msg") if isinstance(l, dict) else str(l) for l in error_lines],
                "systemLines": system_lines if isinstance(system_lines, list) and system_lines and isinstance(system_lines[0], str) else [json.dumps(l, ensure_ascii=False) if isinstance(l, dict) else str(l) for l in system_lines],
            })
        elif path == "/api/config":
            self._json(load_config())
        elif path == "/api/ai":
            ai = dict(ai_config())
            key = ai.get("api_key") or ""
            if key:
                ai["api_key"] = (key[:6] + "****" + key[-4:]) if len(key) > 12 else "****"
            self._json({"success": True, "ai": ai})
        elif path == "/api/ai/models":
            try:
                ids = ai_fetch_models()
                self._json({"success": True, "models": ids})
            except Exception as e:
                self._json({"success": False, "message": str(e)[:200]})
        elif path == "/api/reminders":
            with REMINDER_LOCK:
                data = load_reminders()
            self._json({"success": True, "reminders": data["reminders"]})
        elif path == "/api/subs":
            with SUBS_LOCK:
                d = load_subs()
            self._json({"success": True, "subs": d.get("subscriptions", [])})
        elif path == "/api/overview":
            with USERS_LOCK:
                users = load_users()["users"]
            with PERM_LOCK:
                p = load_permissions()
            with SUBS_LOCK:
                sd = load_subs()
            with REMINDER_LOCK:
                rd = load_reminders()
            ledger = load_ledger()["users"]
            members = p.get("members", {})
            subs = sd.get("subscriptions", [])
            reminders = rd.get("reminders", [])
            ai_stats = {}
            for ln in tail_file(LOG_FILE, 200000):
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                if rec.get("type") == "text" and (rec.get("content") or "").strip().startswith("/ai"):
                    fid = rec.get("fromId") or ""
                    st = ai_stats.setdefault(fid, {"count": 0, "last": ""})
                    st["count"] += 1
                    st["last"] = rec.get("time") or st["last"]
            out = []
            for fid, info in users.items():
                l = ledger.get(fid)
                my_subs = [{"time": s.get("time"), "city": s.get("city_label") or s.get("city"),
                            "room": s.get("room_name") or ""}
                           for s in subs if s.get("from_id") == fid]
                my_rem = [r for r in reminders if r.get("from_id") == fid]
                ai = ai_stats.get(fid, {})
                out.append({
                    "fromId": fid, "name": info.get("name") or fid,
                    "last_seen": info.get("last_seen") or "",
                    "role": "member" if fid in members else None,
                    "balance": l["balance"] if l else 0,
                    "count": len(l["entries"]) if l else 0,
                    "subs": my_subs, "reminders": len(my_rem),
                    "ai_count": ai.get("count", 0), "ai_last": ai.get("last", ""),
                })
            out.sort(key=lambda x: x["last_seen"], reverse=True)
            self._json({"success": True, "users": out, "summary": {
                "total": len(out), "members": len(members),
                "subs": len(subs), "reminders": len(reminders)}})
        elif path == "/api/permissions":
            with PERM_LOCK:
                p = load_permissions()
            self._json({
                "success": True,
                "members": p.get("members", {}),
            })
        elif path == "/api/users":
            with USERS_LOCK:
                users = load_users()["users"]
            ledger = load_ledger()["users"]
            with PERM_LOCK:
                p = load_permissions()
            members = p.get("members", {})
            out = []
            for fid, info in users.items():
                l = ledger.get(fid)
                out.append({
                    "fromId": fid,
                    "name": info.get("name") or fid,
                    "last_seen": info.get("last_seen") or "",
                    "balance": l["balance"] if l else 0,
                    "count": len(l["entries"]) if l else 0,
                    "role": "member" if fid in members else None,
                })
            for fid, info in members.items():
                if not any(u["fromId"] == fid for u in out):
                    out.append({"fromId": fid, "name": info.get("name") or fid, "last_seen": "",
                                "balance": 0, "count": 0, "role": "member"})
            out.sort(key=lambda x: x["last_seen"], reverse=True)
            self._json({"success": True, "users": out})
        else:
            self._json({"success": False, "message": "not found"}, 404)

    def _bot_log_lines(self, only_err):
        """取机器人日志目录里最近3个日志文件的尾部，合并后倒序返回"""
        try:
            files = sorted(
                [os.path.join(BOT_LOG_DIR, f) for f in os.listdir(BOT_LOG_DIR) if f.startswith("app.") and f.endswith(".log")],
                key=os.path.getmtime,
                reverse=True,
            )[:3]
        except Exception:
            files = []
        merged = []
        for fp in files:
            merged.extend(tail_file(fp, 300))
        merged = [ln for ln in merged if ln.strip()]
        if only_err:
            merged = [ln for ln in merged if re.search(r"error|fail|exception", ln, re.I)]
        return list(reversed(merged))

    # ---- POST ----
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/pending_ilink/done" and not getattr(self.server, "is_public", False):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length > 0 else b""
                data = json.loads(body.decode("utf-8", "ignore") or "{}")
                outbox_done(data.get("ids") or [])
                self._json({"success": True})
            except Exception as e:
                log_error(f"出站确认异常: {e}")
                self._json({"success": False, "message": "internal error"}, 500)
            return
        if path == "/receive_msg" and not getattr(self.server, "is_public", False):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length > 0 else b""
                fields = parse_multipart(self.headers.get("Content-Type", ""), body)
                self._on_receive(fields)
            except Exception as e:
                log_error(f"处理回调异常: {e}")
                self._json({"success": False, "message": "internal error"}, 500)
            return
        if getattr(self.server, "is_public", False):
            self._public_post(path)
            return
        self._json({"success": False, "message": "not found"}, 404)

    def _public_post(self, path):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        if path == "/login":
            form = parse_urlencoded(body)
            token = (form.get("token") or "").strip()
            if token and token == VIEW_TOKEN:
                sid = new_session()
                self._send("", 302, "text/plain", {
                    "Location": "/",
                    "Set-Cookie": f"wxbot_admin={sid}; Path=/; HttpOnly; Max-Age=43200",
                })
            else:
                self._send(LOGIN_PAGE.replace("__ERR__", "口令错误，请重试"), 401, "text/html; charset=utf-8")
            return
        if path == "/logout":
            drop_session(self._cookie("wxbot_admin"))
            self._redirect("/login")
            return
        if path.startswith("/api/"):
            if not self._require_auth():
                return
            try:
                data = json.loads(body.decode("utf-8", "ignore") or "{}")
            except Exception:
                data = {}
            if path == "/api/config":
                cfg = load_config()
                if "auto_reply" in data:
                    cfg["auto_reply"] = bool(data["auto_reply"])
                save_config(cfg)
                self._json({"success": True, "config": cfg})
            elif path == "/api/send":
                to = str(data.get("to") or "").strip()
                content = str(data.get("content") or "").strip()
                is_room = bool(data.get("isRoom"))
                name = str(data.get("name") or "").strip() or None
                if not to or not content:
                    self._json({"success": False, "message": "接收方和内容不能为空"})
                    return
                ok, resp = bot_send(to, content, is_room, name=name)
                try:
                    parsed = json.loads(resp)
                except Exception:
                    parsed = {"raw": resp}
                self._json({"success": ok, "status": 200, "bot": parsed})
            elif path == "/api/ai":
                cfg = load_config()
                ai = cfg.setdefault("ai", {})
                if "base_url" in data:
                    ai["base_url"] = str(data.get("base_url") or "").strip()
                if "model" in data:
                    ai["model"] = str(data.get("model") or "").strip()
                new_key = str(data.get("api_key") or "").strip()
                if "api_key" in data:
                    if new_key and "****" not in new_key:
                        ai["api_key"] = new_key
                    elif not new_key:
                        ai["api_key"] = ""
                save_config(cfg)
                self._json({"success": True, "config": cfg})
            elif path == "/api/ai/test":
                try:
                    tmp = dict(ai_config())
                    if data.get("base_url"):
                        tmp["base_url"] = str(data["base_url"]).strip()
                    if data.get("model"):
                        tmp["model"] = str(data["model"]).strip()
                    k = str(data.get("api_key") or "").strip()
                    if k and "****" not in k:
                        tmp["api_key"] = k
                    if not tmp.get("base_url") or not tmp.get("api_key") or not tmp.get("model"):
                        self._json({"success": False, "message": "请先填写接口地址 / API Key / 模型"})
                        return
                    reply = ai_chat("你好，请只回复：连接正常", cfg=tmp)
                    self._json({"success": True, "reply": reply})
                except Exception as e:
                    log_error(f"AI 测试失败: {e}")
                    self._json({"success": False, "message": str(e)[:200]})
            elif path == "/api/reminders/cancel":
                rid = str(data.get("id") or "").strip()
                with REMINDER_LOCK:
                    d = load_reminders()
                    kept = [r for r in d["reminders"] if r.get("id") != rid]
                    removed = len(d["reminders"]) - len(kept)
                    d["reminders"] = kept
                    save_reminders(d)
                self._json({"success": removed > 0, "removed": removed})
            elif path == "/api/subs":
                action = str(data.get("action") or "")
                if action == "cancel":
                    sid = str(data.get("id") or "").strip()
                    with SUBS_LOCK:
                        d = load_subs()
                        n = len(d.get("subscriptions", []))
                        d["subscriptions"] = [s for s in d.get("subscriptions", [])
                                              if s.get("id") != sid]
                        if len(d["subscriptions"]) != n:
                            save_subs(d)
                    self._json({"success": True})
                else:
                    self._json({"success": False, "message": "unknown action"})
            elif path == "/api/permissions":
                with PERM_LOCK:
                    p = load_permissions()
                self._json({"success": True, "members": p.get("members", {})})
            elif path == "/api/users":
                action = str(data.get("action") or "")
                fid = str(data.get("from_id") or "").strip()
                name = str(data.get("name") or "").strip()
                if action == "grant":
                    if not fid:
                        self._json({"success": False, "message": "缺少 from_id"})
                        return
                    if name:
                        record_user(fid, name)
                    grant_member(fid, name or fid, "admin-panel")
                    self._json({"success": True})
                elif action == "revoke":
                    self._json({"success": revoke_member(fid)})
                else:
                    self._json({"success": False, "message": "unknown action"})
            else:
                self._json({"success": False, "message": "not found"}, 404)
            return
        self._json({"success": False, "message": "not found"}, 404)

    # ---- 收消息核心逻辑 ----
    def _on_receive(self, fields):
        val = lambda n: (fields.get(n) or (None, b""))[1]
        mtype = val("type").decode("utf-8", "ignore")
        content_b = val("content")
        filename = (fields.get("content") or (None, b""))[0] or ""
        source_raw = val("source").decode("utf-8", "ignore")
        is_mentioned = val("isMentioned").decode("utf-8", "ignore").strip() == "1"
        is_self = val("isMsgFromSelf").decode("utf-8", "ignore").strip() == "1"
        is_system = val("isSystemEvent").decode("utf-8", "ignore").strip() == "1"

        if is_system:
            log_system_event({
                "type": mtype,
                "content": (content_b.decode("utf-8", "ignore") or "")[:500],
                "source": source_raw[:2000],
            })
            self._json({"success": True})
            return
        if is_self:
            self._json({"success": True})
            return

        source = {}
        if source_raw:
            try:
                source = json.loads(source_raw)
            except Exception:
                source = {}

        room = source.get("room") or {}
        room_payload = (room.get("payload") or {}) if isinstance(room, dict) else {}
        from_contact = source.get("from") or {}
        from_payload = (from_contact.get("payload") or {}) if isinstance(from_contact, dict) else {}
        to_payload = (source.get("to") or {}).get("payload") or {}

        bot_name = to_payload.get("name") or to_payload.get("alias") or ""
        room_name = room_payload.get("topic") or room.get("topic") or ""
        room_id = room.get("id") or ""
        in_room = bool(room_id)

        if mtype == "file":
            content = "[文件] " + filename
        else:
            content = content_b.decode("utf-8", "ignore") or ""

        mentioned = is_mentioned
        if bot_name and not mentioned:
            mentioned = bool(re.search(r"@\s*" + re.escape(bot_name), content))

        sender_name = (
            from_payload.get("alias")
            or from_payload.get("name")
            or from_payload.get("id")
            or "未知"
        )
        from_id = from_payload.get("id") or ""
        record_user(from_id, sender_name)

        reply = None
        cfg = load_config()
        if mtype == "text" and cfg.get("auto_reply", True):
            text_in = content.strip()
            # 每日推送城市确认流程：有待确认时优先处理（群里可不用 @机器人）
            pending_reply = handle_pending_reply(from_id, text_in) if text_in else None
            if pending_reply is not None:
                reply = pending_reply
            elif (not in_room) or mentioned:
                # 群聊里可能带 @机器人 前缀（如 "@kindle /余额"），先剥掉再判断命令
                cmd_text = re.sub(r"^@\s*[\u4e00-\u9fa5\w\-]+", "", text_in).strip()
                cmd_word = cmd_text.split(None, 1)[0].lower() if cmd_text else ""
                # 权限规则：AI 类命令需要授权；普通功能（计算/记账/提醒等）人人可用
                if cmd_text.startswith("/") and cmd_word in AI_CMDS and not is_allowed(from_id):
                    reply = AI_NO_PERMISSION_MSG
                elif cmd_text.startswith("/"):
                    reply = handle_command(cmd_text, from_id, sender_name, room_id, room_name, cfg)
                    if reply is None:
                        reply = FALLBACK_HELP
                else:
                    expr = normalize_expr(text_in)
                    if is_math_expr(expr):
                        try:
                            result = evaluate_math(expr)
                            reply = f"{expr} = {fmt_number(result)}"
                        except ZeroDivisionError:
                            reply = "除数不能为 0"
                        except ValueError:
                            reply = None
                    if reply is None:
                        reply = FALLBACK_HELP

        rec = {
            "type": mtype,
            "room": room_name,
            "roomId": room_id,
            "from": sender_name,
            "fromId": from_id,
            "content": content[:1000],
            "isMentioned": 1 if is_mentioned else 0,
            "reply": reply,
        }
        save_record(rec)

        if reply is None:
            self._json({"success": True})
            return

        text = f"@{sender_name} {reply}" if in_room else reply
        self._json({"success": True, "data": {"type": "text", "content": text}})


# ---------------- 启动 ----------------
def main():
    global VIEW_TOKEN
    os.makedirs(BASE_DIR, exist_ok=True)
    VIEW_TOKEN = load_token()
    init_stats()
    STATS["since"] = time.strftime("%Y-%m-%d")

    servers = [
        (INT_BIND, INT_PORT, False),
        (VIEW_BIND, VIEW_PORT, True),
    ]
    started = 0
    for bind, port, is_public in servers:
        try:
            srv = ThreadingHTTPServer((bind, port), Handler)
            srv.is_public = is_public
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            print(f"listening on {bind}:{port} (public={is_public})", flush=True)
            started += 1
        except Exception as e:
            print(f"failed to listen {bind}:{port}: {e}", flush=True)
            log_error(f"监听失败 {bind}:{port}: {e}")
    if started == 0:
        return
    threading.Thread(target=reminder_worker, daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
