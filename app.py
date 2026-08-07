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
import json
import hashlib
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
import base64
import html
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
OPENCLAW_SESSIONS_FILE = os.path.join(BASE_DIR, "openclaw_sessions.json")
# OpenClaw 原生索引和 transcript 只读；路径可通过环境变量覆盖，便于测试和容器部署。
OPENCLAW_INDEX_FILE = os.environ.get(
    "WXBOT_OPENCLAW_INDEX_FILE",
    "/root/openclaw/openclaw_space/agents/wxbot/sessions/sessions.json",
)
OPENCLAW_TRANSCRIPT_DIR = os.environ.get(
    "WXBOT_OPENCLAW_TRANSCRIPT_DIR",
    "/root/openclaw/openclaw_space/agents/wxbot/sessions",
)
OPENCLAW_CONFIG_FILE = os.environ.get(
    "WXBOT_OPENCLAW_CONFIG_FILE",
    "/root/openclaw/openclaw_space/openclaw.json",
)
# 兼容部署脚本和外部诊断工具使用的旧常量名。
OPENCLAW_SESSION_FILE = OPENCLAW_SESSIONS_FILE
OPENCLAW_SESSION_INDEX_FILE = OPENCLAW_INDEX_FILE
OPENCLAW_SESSIONS_INDEX_FILE = OPENCLAW_INDEX_FILE
OPENCLAW_TRANSCRIPTS_DIR = OPENCLAW_TRANSCRIPT_DIR
OPENCLAW_SESSION_DIR = OPENCLAW_TRANSCRIPT_DIR
PERM_FILE = os.path.join(BASE_DIR, "permissions.json")   # 权限(管理员/成员白名单)
USERS_FILE = os.path.join(BASE_DIR, "users.json")        # 见过的用户(昵称->ID)
IDENTITY_FILE = os.path.join(BASE_DIR, "identities.json") # 稳定用户ID与微信临时ID映射
LEDGER_LOCK = threading.Lock()
REMINDER_LOCK = threading.Lock()
SUBS_LOCK = threading.Lock()
PERM_LOCK = threading.Lock()
USERS_LOCK = threading.Lock()
IDENTITY_LOCK = threading.Lock()
OUTBOX_LOCK = threading.Lock()
OPENCLAW_REGISTRY_LOCK = threading.Lock()
OPENCLAW_USAGE_LOCK = threading.Lock()
_OPENCLAW_LAST_USAGE = {}
_OPENCLAW_SESSION_LOCKS = {}
_OPENCLAW_SESSION_LOCKS_GUARD = threading.Lock()

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
    "AI 功能需要先授权才能使用。输入 /权限 <密码> 即可开通（密码由机器人主人提供）。\n"
    "未授权仍可使用：计算、记账、余额、明细，以及 /提醒、/推送 命令。\n"
    "详细用法发 /说明"
)
AI_FAILURE_MSG = "AI 暂时不可用，请稍后重试。"
WECHAT_SYSTEM_PROMPT = (
    "你正在通过微信向用户回复。输出必须是适合微信聊天窗口的纯文本。"
    "不要使用 Markdown，不要使用加粗符号、反引号、井号标题、表格、项目符号或 emoji。"
    "用短段落和简单的数字编号表达，控制长度，直接回答问题，不要解释这些规则。"
    "不要主动暴露内部 agent 名称、工作区路径、provider 或配置细节。"
    "遇到新闻、人物动态、天气、赛事、活动、价格或其他需要核实最新信息的问题时，"
    "必须先使用网页工具搜索核实后再回答，不要回复无法确认，也不要让用户自己去查。"
    "不要展示工具调用过程、工具参数、来源链接或内部等待文本，直接给出核实后的结论。"
    "如果用户问你是谁、基于什么智能体或运行环境，统一回答：我是 OpenClaw 智能体。"
)
OPENCLAW_COMPOSE_SYSTEM_PROMPT = (
    "你正在通过微信向用户回复。输出必须是适合微信聊天窗口的纯文本，不要使用 Markdown、"
    "加粗符号、反引号、井号标题、表格、项目符号或 emoji，用短段落和简单的数字编号表达，控制长度。"
    "以下是刚搜索到的资料，请直接根据这些资料回答用户的问题。"
    "只能使用资料中明确出现的事实，资料里没有的活动名、日期、玩法、奖品一律不要编造。"
    "如果资料里没有用户问的活动的明确信息，直接告诉用户：根据搜索到的资料暂时没有找到明确预告，"
    "建议以游戏内活动中心或官方公告为准。"
    "不要提及搜索过程、资料格式或来源链接，不要回复需要查一下、稍等之类的话。"
)
# 敷衍回答标记：命中任一即认为 OpenClaw 没有真正搜索/回答问题。
_EVASIVE_MARKERS = (
    "无法确认", "没法确认", "无法实时确认", "不能确认", "无法核实", "无法实时核实",
    "没看到", "没有看到", "没找到", "没有搜到", "没搜到", "未找到", "未搜到",
    "暂未发现", "尚未官宣", "暂未官宣", "还没官宣",
    "自己去查", "自己查", "自行查询", "自己去搜", "自己搜",
    "查一下", "需要查", "查询一下", "我先查", "我去查", "回头查",
    "稍等", "稍后", "才能确认", "核实一下", "确认一下",
    "发我一下", "发给我", "你发我", "把活动名", "链接发我", "截图发我",
    "去官网", "自己去官网", "去官方网站",
    "以游戏内活动中心为准", "以游戏内为准", "以官方为准", "以实际为准",
    "看官方微博", "关注官方微博", "官方微博和公众号", "官方公众号", "官方微博或公众号",
)
AI_CMDS = ("/ai", "/搜索", "/search")  # 需要授权的 OpenClaw 类命令
SESSION_COMMANDS = {
    "/compact", "/压缩上下文", "压缩上下文",
    "/new", "/新会话", "新会话", "开启新的会话", "开启新会话",
}

AUTOMATION_ACTIONS = {"set_reminder", "set_daily_push"}
AUTOMATION_HINTS = (
    "提醒", "闹钟", "叫我", "通知我", "别忘", "到点", "推送", "每日", "每天", "定时",
)
OPENCLAW_ROUTE_PROMPT = (
    "你只负责判断微信消息是否要触发两个本地动作。"
    "用户明确要求设置一次提醒时，只输出一行 JSON："
    '{"action":"set_reminder","time":"时间","content":"提醒内容"}。'
    "用户明确要求设置每日天气推送时，只输出一行 JSON："
    '{"action":"set_daily_push","time":"时间","city":"城市"}。'
    '其他情况只输出一行 JSON：{"action":"chat"}。'
    "不要回答用户问题，不要输出 Markdown 或解释。"
)


def _looks_like_automation(text):
    return any(k in text for k in AUTOMATION_HINTS)

FALLBACK_HELP = (
    "我支持这些功能（群里请先 @我 再发）：\n"
    "\n"
    "【无需授权】\n"
    "1. 计算：直接发算式，如 12×8-4\n"
    "2. 记账：/记账 +100、/记账 -30；查询 /余额、/明细、/清空\n"
    "3. 提醒：/提醒 10分钟后 喝水，或直接说“10分钟后提醒我喝水”\n"
    "4. 每日推送：/推送 8:00，每天自动推天气\n"
    "\n"
    "【需授权】（/权限 <密码> 开通）\n"
    "5. AI 路由：OpenClaw 识别提醒和推送并执行\n"
    "   例：“明天早上8点提醒我开会”“每天八点推送北京市朝阳区天气”\n"
    "6. AI 问答：直接问我任何问题，统一由 OpenClaw 回答\n"
    "\n"
    "详细用法：/说明"
)

DETAIL_HELP = (
    "功能说明（群里请先 @我 再发）：\n"
    "\n"
    "一、无需授权就能用\n"
    "1. 计算：直接发算式，支持 + - × ÷ 括号、小数\n"
    "   例：12×8-4、10÷(2+3)\n"
    "2. 记账：/记账 以 + 或 - 开头（AI 不会代记）\n"
    "   /记账 +8×4；/记账 -15×2；可加备注：/记账 +100 买菜\n"
    "3. 记账查询：/余额 看余额和笔数；/明细 看全部记录；/清空 清空记录\n"
    "4. 定时提醒：/提醒 <时间> <内容>\n"
    "   例：/提醒 10分钟后 喝水\n"
    "   时间支持：10分钟后 / 2小时后 / 14:30 / 9点 / 9点半 / 明天9点\n"
    "   群聊提醒会 @ 你，私聊提醒直接发消息\n"
    "5. 每日天气推送：/推送 <时间>，如 /推送 8:00\n"
    "   会自动引导确认城市；可设置多条；/取消推送 [编号] 取消\n"
    "\n"
    "二、AI 功能（需先 /权限 <密码> 开通，密码由机器人主人提供）\n"
    "6. AI 路由：OpenClaw 识别提醒和每日推送，信息不全时会追问补齐\n"
    "   例：“明天早上8点提醒我开会” → 自动设提醒\n"
    "   例：“每天八点推送北京市朝阳区天气” → 自动开每日推送\n"
    "7. AI 问答：直接问我任何问题（闲聊、问答、查资料都可以），统一由 OpenClaw 回答\n"
    "\n"
    "记账例外：账本必须用 /记账 + / - 精确格式，AI 不会代记"
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
    cfg = {"auto_reply": True, "smart": True}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def public_config(cfg):
    """返回可交给后台页面的配置摘要，绝不包含任何 provider secret。"""
    claw = cfg.get("openclaw") or {}
    return {
        "auto_reply": bool(cfg.get("auto_reply", True)),
        "smart": bool(cfg.get("smart", True)),
        "openclaw": {
            "enabled": bool(claw.get("enabled", False)),
            "configured": bool(claw.get("base_url") and claw.get("api_key")),
        },
    }


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


# ---------------- OpenClaw 会话与上下文 ----------------
_OPENCLAW_INDEX_PREFIX = "agent:wxbot:openai-user:"
_OPENCLAW_MAX_TRANSCRIPT_BYTES = 4 * 1024 * 1024
_OPENCLAW_MAX_TRANSCRIPT_RECORDS = 5000


def _short_ref(value, prefix="ref"):
    """把内部 ID 映射为稳定的短引用，后台和日志对外只使用该引用。"""
    digest = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:10]
    return "{}-{}".format(prefix, digest)


def short_id(value, prefix="ref"):
    return _short_ref(value, prefix)


def public_user_ref(value):
    """把稳定身份或传输别名转换成后台可展示的短用户引用。"""
    value = str(value or "").strip()
    stable = identity_existing_user_id(value) or value
    return _short_ref(stable, "u") if stable else ""


def public_display_name(value, user_id=""):
    """展示昵称；旧数据把微信 ID 当昵称时改成短用户引用。"""
    name = str(value or "").strip()
    user_id = str(user_id or "").strip()
    stable = identity_existing_user_id(user_id) or user_id
    if not name:
        return "未命名用户"
    if name in {user_id, stable}:
        return public_user_ref(stable)
    return public_log_line(name)


def public_json_value(value, key=""):
    """递归处理管理接口响应中的用户/群聊 ID字段。"""
    if isinstance(value, dict):
        result = {}
        for child_key, child in value.items():
            if child_key in {"fromId", "from_id", "target_id", "sender_id", "user_id"}:
                result[child_key] = public_user_ref(child)
            elif child_key in {"roomId", "room_id"}:
                result[child_key] = _short_ref(child, "r") if child else ""
            else:
                result[child_key] = public_json_value(child, child_key)
        return result
    if isinstance(value, list):
        return [public_json_value(child, key) for child in value]
    return value


def public_log_line(value):
    """隐藏日志文本中的已登记微信 ID，避免管理后台重新展示长传输 ID。"""
    text = str(value or "")
    try:
        with IDENTITY_LOCK:
            registry = _identity_registry_load()
            replacements = {}
            for transport_id, canonical in (registry.get("aliases") or {}).items():
                transport_id = str(transport_id or "")
                canonical = str(canonical or "")
                if len(transport_id) >= 20 and canonical:
                    replacements[transport_id] = _short_ref(canonical, "u")
            for canonical in (registry.get("identities") or {}):
                canonical = str(canonical or "")
                if len(canonical) >= 20:
                    replacements.setdefault(canonical, _short_ref(canonical, "u"))
        for raw, replacement in sorted(replacements.items(), key=lambda pair: len(pair[0]), reverse=True):
            text = text.replace(raw, replacement)
        text = re.sub(
            r"@@[A-Za-z0-9_-]{40,}",
            lambda match: _short_ref(match.group(0), "r"),
            text,
        )
        text = re.sub(
            r"@[A-Za-z0-9_-]{40,}(?:@im\.wechat)?",
            lambda match: _short_ref(match.group(0), "u"),
            text,
        )
    except Exception:
        pass
    return text


def _openclaw_registry_load():
    try:
        with open(OPENCLAW_SESSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        users = data.get("users") if isinstance(data, dict) else None
        return {"users": users if isinstance(users, dict) else {}}
    except Exception:
        return {"users": {}}


def _openclaw_registry_save(data):
    path = OPENCLAW_SESSIONS_FILE
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temp = path + ".tmp"
    try:
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    except Exception as e:
        try:
            if os.path.exists(temp):
                os.unlink(temp)
        except Exception:
            pass
        log_error("保存 OpenClaw 会话注册表失败: {}".format(e))


def openclaw_active_key(user_id):
    """返回微信用户当前 Gateway user key；首次使用保持原微信 ID。"""
    user_id = str(user_id or "").strip()
    if not user_id:
        return ""
    with OPENCLAW_REGISTRY_LOCK:
        data = _openclaw_registry_load()
        entry = data["users"].get(user_id) or {}
        key = str(entry.get("active_key") or "").strip()
        if key:
            return key
        data["users"][user_id] = {
            "active_key": user_id,
            "created_at": now_str(),
        }
        _openclaw_registry_save(data)
        return user_id


def openclaw_start_new_session(user_id):
    """为用户创建新的 Gateway user key；原 session 和 transcript 保留。"""
    user_id = str(user_id or "").strip()
    if not user_id:
        return ""
    key = "{}:session:{}".format(user_id, secrets.token_hex(8))
    with OPENCLAW_REGISTRY_LOCK:
        data = _openclaw_registry_load()
        data["users"][user_id] = {
            "active_key": key,
            "created_at": now_str(),
        }
        _openclaw_registry_save(data)
    return key


def _openclaw_user_from_key(key):
    key = str(key or "")
    if not key.startswith(_OPENCLAW_INDEX_PREFIX):
        return None
    user_key = key[len(_OPENCLAW_INDEX_PREFIX):]
    if user_key.startswith("route:"):
        return None
    user_id = user_key.split(":session:", 1)[0]
    return identity_existing_user_id(user_id) or user_id


def _openclaw_content_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                if item.get("type") in (None, "text") and item.get("text") is not None:
                    chunks.append(str(item.get("text")))
        return "".join(chunks).strip()
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "").strip()
    return ""


def openclaw_parse_transcript(path):
    """解析 OpenClaw JSONL transcript，只保留微信可查看的输入、输出和压缩事件。"""
    records = []
    if not path or not os.path.isfile(path):
        return records
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > _OPENCLAW_MAX_TRANSCRIPT_BYTES:
                f.seek(size - _OPENCLAW_MAX_TRANSCRIPT_BYTES)
                f.readline()
            raw_lines = f.readlines(_OPENCLAW_MAX_TRANSCRIPT_RECORDS * 4096)
    except Exception:
        return records
    for raw in raw_lines[-_OPENCLAW_MAX_TRANSCRIPT_RECORDS:]:
        try:
            item = json.loads(raw.decode("utf-8", "ignore"))
        except Exception:
            continue
        timestamp = item.get("timestamp") or ""
        if item.get("type") == "message":
            message = item.get("message") or {}
            role = str(message.get("role") or "")
            content = _openclaw_content_text(message.get("content"))
            if role in ("user", "assistant") and content:
                rec = {"role": role, "content": content, "timestamp": timestamp}
                usage = message.get("usage") or {}
                if isinstance(usage, dict):
                    rec["usage"] = {
                        k: usage.get(k) for k in ("input", "output", "cacheRead", "totalTokens")
                        if usage.get(k) is not None
                    }
                records.append(rec)
            continue
        if item.get("type") in ("compaction", "compaction_start", "compaction_end"):
            message = item.get("message") or {}
            rec = {
                "type": "compaction",
                "timestamp": timestamp,
                "summary": str(item.get("summary") or message.get("summary") or ""),
            }
            before = item.get("tokensBefore", message.get("tokensBefore"))
            after = item.get("tokensAfter", message.get("tokensAfter"))
            if before is not None:
                rec["tokens_before"] = before
            if after is not None:
                rec["tokens_after"] = after
            records.append(rec)
    return records


def _openclaw_transcript_path(session_id, entry, transcript_dir):
    transcript_dir = transcript_dir or OPENCLAW_TRANSCRIPT_DIR
    candidate = ""
    if isinstance(entry, dict):
        candidate = str(entry.get("sessionFile") or "")
    if candidate:
        candidate = os.path.basename(candidate)
        if not candidate.endswith(".jsonl"):
            candidate = ""
    if not candidate:
        candidate = str(session_id or "") + ".jsonl"
    return os.path.join(transcript_dir, candidate)


def _openclaw_time(value):
    try:
        stamp = float(value) / 1000 if float(value) > 100000000000 else float(value)
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
    except Exception:
        return ""


def openclaw_parse_sessions_index(index_path=None, transcript_dir=None):
    """读取原生 sessions.json，过滤非 wxbot/route session 并附加 transcript 摘要。"""
    claw = openclaw_config()
    index_path = index_path or claw.get("session_index") or OPENCLAW_INDEX_FILE
    transcript_dir = transcript_dir or claw.get("transcript_dir") or OPENCLAW_TRANSCRIPT_DIR
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"sessions": []}
    if not isinstance(data, dict):
        return {"sessions": []}
    sessions = []
    for key, entry in data.items():
        user_id = _openclaw_user_from_key(key)
        if not user_id or not isinstance(entry, dict):
            continue
        session_id = str(entry.get("sessionId") or "").strip()
        if not session_id:
            continue
        transcript = openclaw_parse_transcript(
            _openclaw_transcript_path(session_id, entry, transcript_dir)
        )
        messages = [r for r in transcript if r.get("role") in ("user", "assistant")]
        compactions = [r for r in transcript if r.get("type") == "compaction"]
        latest_usage = next((r.get("usage") for r in reversed(messages) if r.get("usage")), {})
        used = (_context_used_from_usage(latest_usage)
                or _positive_usage_value(
                    entry, "totalTokens", "inputTokens", "input_tokens"
                ))
        breakdown = _usage_breakdown(latest_usage)
        limit = (_positive_usage_value(
            entry, "contextWindow", "contextTokens", "contextWindowTokens"
        ) or 128000)
        sessions.append({
            "user_ref": _short_ref(user_id, "u"),
            "session_ref": _short_ref(key, "s"),
            "session_id": session_id,
            "updated_at": _openclaw_time(entry.get("updatedAt")),
            "context_used": used,
            "context_limit": limit,
            "context_cached": breakdown[1] if breakdown else None,
            "context_miss": breakdown[2] if breakdown else None,
            "message_count": len(messages),
            "compaction_count": len(compactions),
            "_user_id": user_id,
            "_session_key": key,
            "_transcript": transcript,
        })
    sessions.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return {"sessions": sessions}


def read_openclaw_sessions(index_path=None, transcript_dir=None):
    return openclaw_parse_sessions_index(index_path, transcript_dir)


def read_openclaw_transcript(session_id, transcript_dir=None):
    transcript_dir = transcript_dir or OPENCLAW_TRANSCRIPT_DIR
    path = str(session_id or "")
    if not path.endswith(".jsonl"):
        path = os.path.join(transcript_dir, path + ".jsonl")
    return openclaw_parse_transcript(path)


def format_context_usage(used, limit, cached=None, miss=None):
    if used is None or limit is None:
        return ""
    try:
        used = float(used)
        limit = float(limit)
        if limit <= 0:
            return ""
        def fmt(value):
            if value >= 1000:
                text = "{:.1f}".format(value / 1000).rstrip("0").rstrip(".")
                return text + "k"
            return str(int(value))
        result = "（上下文 {} / {}，{:.1f}%）".format(fmt(used), fmt(limit), used / limit * 100)
        if cached is not None and miss is not None:
            result = result[:-1] + "；缓存命中 {} / 未命中 {}）".format(int(cached), int(miss))
        return result
    except Exception:
        return ""


def _positive_usage_value(data, *keys):
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        try:
            if value is not None and float(value) > 0:
                return value
        except (TypeError, ValueError):
            continue
    return None


def _usage_int(data, *keys):
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _usage_breakdown(usage):
    """跨格式解析 usage，返回 (上下文总 token, 缓存命中, 未命中) 或 None。

    OpenClaw/litellm：input 只含新 token，cacheRead 是缓存命中，总数=input+cacheRead。
    DeepSeek/OpenAI：prompt_tokens 已含缓存命中，命中在 prompt_tokens_details.cached_tokens
    或 prompt_cache_hit_tokens，未命中可显式给出或由总数减命中得出。
    """
    if not isinstance(usage, dict):
        return None
    fresh = _usage_int(usage, "input", "inputTokens")
    cached = _usage_int(usage, "cacheRead", "cacheReadTokens")
    if fresh is not None and cached is not None and (fresh + cached) > 0:
        return fresh + cached, cached, fresh
    prompt = _usage_int(usage, "prompt_tokens", "promptTokens")
    if prompt is not None and prompt > 0:
        details = usage.get("prompt_tokens_details")
        hit = (_usage_int(details, "cached_tokens")
               or _usage_int(usage, "prompt_cache_hit_tokens", "cacheReadInputTokens")
               or 0)
        miss = (_usage_int(usage, "prompt_cache_miss_tokens", "cacheMissInputTokens")
                or max(prompt - hit, 0))
        return prompt, hit, miss
    return None


def _context_used_from_usage(usage):
    parts = _usage_breakdown(usage)
    return parts[0] if parts else None


def _openclaw_context_status(session_key):
    with OPENCLAW_USAGE_LOCK:
        usage = dict(_OPENCLAW_LAST_USAGE.get(str(session_key)) or {})
    if usage:
        parts = _usage_breakdown(usage)
        limit = _positive_usage_value(usage, "contextTokens", "contextWindow") or 128000
        if parts:
            result = format_context_usage(parts[0], limit, parts[1], parts[2])
            if result:
                return result
    # 与后台同源：优先 transcript 最新一条 usage，其次原生索引
    try:
        index_path = openclaw_config().get("session_index") or OPENCLAW_INDEX_FILE
        transcript_dir = openclaw_config().get("transcript_dir") or OPENCLAW_TRANSCRIPT_DIR
        with open(index_path, "r", encoding="utf-8") as f:
            entry = (json.load(f) or {}).get(_OPENCLAW_INDEX_PREFIX + str(session_key)) or {}
        session_id = str(entry.get("sessionId") or "")
        if session_id:
            path = _openclaw_transcript_path(session_id, entry, transcript_dir)
            messages = [r for r in openclaw_parse_transcript(path)
                        if r.get("role") in ("user", "assistant")]
            latest = next((r.get("usage") for r in reversed(messages) if r.get("usage")), {})
            parts = _usage_breakdown(latest)
            if parts:
                limit = (_positive_usage_value(
                    entry, "contextWindow", "contextTokens", "contextWindowTokens"
                ) or 128000)
                return format_context_usage(parts[0], limit, parts[1], parts[2])
        used = _positive_usage_value(entry, "totalTokens", "inputTokens", "input_tokens")
        limit = _positive_usage_value(entry, "contextWindow", "contextTokens") or 128000
        return format_context_usage(used, limit) if used is not None else ""
    except Exception:
        pass
    return ""


def _with_context_status(reply, session_key):
    reply = clean_wechat_reply(reply)
    status = _openclaw_context_status(session_key)
    if status and status not in reply:
        return (reply + "\n" + status).strip()
    return reply


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


def extract_wechat_login_qr(html):
    """从 wechatbot-webhook 登录页提取当前二维码 URL。"""
    match = re.search(r"qrcode\.makeCode\(\s*(['\"])(.*?)\1\s*\)", str(html or ""), re.S)
    return match.group(2).strip() if match else ""


def fetch_wechat_login_qr(timeout=8):
    url = BOT_BASE + "/login?token=" + urllib.parse.quote(bot_token())
    with urllib.request.urlopen(url, timeout=timeout) as response:
        html = response.read().decode("utf-8", "ignore")
    return extract_wechat_login_qr(html)


def fetch_wechat_qrcode_script(timeout=8):
    url = BOT_BASE + "/static/qrcode.min.js?token=" + urllib.parse.quote(bot_token())
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


# ---------------- 微信身份归一化 ----------------
def _identity_registry_load():
    try:
        with open(IDENTITY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("identity registry must be an object")
    except Exception:
        data = {}
    data.setdefault("version", 1)
    data.setdefault("identities", {})
    data.setdefault("aliases", {})
    return data


def _identity_registry_save(data):
    os.makedirs(os.path.dirname(IDENTITY_FILE) or ".", exist_ok=True)
    tmp = IDENTITY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, IDENTITY_FILE)
    try:
        os.chmod(IDENTITY_FILE, 0o600)
    except OSError:
        pass


def _identity_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _identity_avatar_seq(payload):
    avatar = urllib.parse.unquote(str((payload or {}).get("avatar") or ""))
    match = re.search(r"(?:[?&]|^)seq=(\d+)(?:&|$)", avatar)
    return match.group(1) if match else ""


def _identity_match_keys(payload):
    payload = payload if isinstance(payload, dict) else {}
    keys = []

    def add(kind, value):
        value = _identity_text(value)
        if value:
            keys.append(kind + ":" + hashlib.sha256(value.encode("utf-8")).hexdigest())

    add("weixin", payload.get("weixin"))
    phones = payload.get("phone") or []
    if not isinstance(phones, (list, tuple, set)):
        phones = [phones]
    for phone in sorted({_identity_text(v) for v in phones if _identity_text(v)}):
        add("phone", phone)
    avatar_seq = _identity_avatar_seq(payload)
    if avatar_seq and avatar_seq != "0":
        add("avatar", avatar_seq)

    profile = {
        "name": _identity_text(payload.get("name")),
        "alias": _identity_text(payload.get("alias")),
        "gender": str(payload.get("gender") or ""),
        "province": _identity_text(payload.get("province")),
        "city": _identity_text(payload.get("city")),
        "signature": _identity_text(payload.get("signature")),
        "avatar_seq": avatar_seq,
    }
    profile_signal = any(profile[key] for key in (
        "alias", "gender", "province", "city", "signature", "avatar_seq"
    ))
    if profile["name"] and profile_signal:
        add("profile", json.dumps(profile, ensure_ascii=False, sort_keys=True))
    return sorted(set(keys))


def identity_user_id(payload, transport_id=""):
    """把会随私聊、群聊或重新登录变化的微信 ID 映射为固定用户主键。"""
    payload = payload if isinstance(payload, dict) else {}
    transport_id = str(transport_id or payload.get("id") or "").strip()
    if not transport_id:
        return ""
    keys = _identity_match_keys(payload)
    with IDENTITY_LOCK:
        data = _identity_registry_load()
        identities = data["identities"]
        aliases = data["aliases"]
        canonical = str(aliases.get(transport_id) or "")
        if not canonical:
            candidates = []
            key_set = set(keys)
            for user_id, item in identities.items():
                if key_set.intersection(item.get("match_keys") or []):
                    candidates.append(user_id)
            if len(candidates) == 1:
                canonical = candidates[0]
            elif transport_id in identities:
                canonical = transport_id
            else:
                # 第一次见到时固定为主键；后续临时 ID 只作为别名加入。
                canonical = transport_id

        item = identities.setdefault(canonical, {
            "created_at": now_str(),
            "match_keys": [],
            "transport_ids": [],
        })
        changed = aliases.get(transport_id) != canonical
        aliases[transport_id] = canonical
        transports = list(item.get("transport_ids") or [])
        if transport_id not in transports:
            transports.append(transport_id)
            item["transport_ids"] = transports[-20:]
            changed = True
        merged_keys = sorted(set(item.get("match_keys") or []).union(keys))
        if merged_keys != item.get("match_keys"):
            item["match_keys"] = merged_keys
            changed = True
        if item.get("current_transport_id") != transport_id:
            item["current_transport_id"] = transport_id
            changed = True
        name = str(payload.get("alias") or payload.get("name") or "").strip()
        if name and item.get("name") != name:
            item["name"] = name
            changed = True
        if changed:
            item["updated_at"] = now_str()
            _identity_registry_save(data)
        return canonical


def identity_transport_id(user_id):
    """返回稳定用户主键当前对应的微信临时发送 ID。"""
    user_id = str(user_id or "").strip()
    if not user_id:
        return ""
    with IDENTITY_LOCK:
        data = _identity_registry_load()
        canonical = str(data["aliases"].get(user_id) or user_id)
        item = data["identities"].get(canonical) or {}
        return str(item.get("current_transport_id") or user_id)


def identity_existing_user_id(value):
    value = str(value or "").strip()
    if not value:
        return ""
    with IDENTITY_LOCK:
        data = _identity_registry_load()
        if value in data["aliases"]:
            return str(data["aliases"][value])
        if value in data["identities"]:
            return value
    return ""


def bot_send(to, content, is_room=False, name=None):
    """主动推送（旧机器人 /webhook/msg/v2）。
    该接口群聊按「群名」找，私聊按「id 或 昵称」找；返回 (成功?, 原始响应)。
    群聊：to 传群名（topic）；私聊：to 传 fromId，失败自动按昵称 name 重试。"""
    content = clean_wechat_reply(content)

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
        code, resp = _post({"id": identity_transport_id(to)})
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
    m = re.match(r"^(?:(今天|明天|今晚|明早|明晚|每天|每晚)\s*)?(?:(早上|早晨|上午|中午|下午|晚上|凌晨)\s*)?(\d{1,2}):(\d{2})$", s)
    if m:
        day_s, per_s, hh_s, mm_s = m.groups()
        hh, mm = int(hh_s), int(mm_s)
        if per_s in ("下午", "晚上") or day_s in ("今晚", "明晚") or (day_s or "").startswith("晚"):
            if hh < 12:
                hh += 12
        day_add = 1 if day_s in ("明天", "明早", "明晚") else 0
        t = time.mktime(time.localtime())
        lt = list(time.localtime(t))
        lt[3], lt[4], lt[5] = hh, mm, 0
        ts = time.mktime(time.localtime(time.mktime(tuple(lt)))) + day_add * 86400
        if ts <= now:
            ts += 86400
        return ts
    m = re.match(r"^(?:(今天|明天|今晚|明早|明晚|每天|每晚)\s*)?(?:(早上|早晨|上午|中午|下午|晚上|凌晨)\s*)?(\d{1,2})\s*点(?:\s*(\d{1,2})\s*分?|\s*(半))?$", s)
    if m:
        day_s, per_s, hh_s, mm_s, half = m.groups()
        hh = int(hh_s)
        mm = int(mm_s) if mm_s else (30 if half else 0)
        if per_s in ("下午", "晚上") or day_s in ("今晚", "明晚") or (day_s or "").startswith("晚"):
            if hh < 12:
                hh += 12
        day_add = 1 if day_s in ("明天", "明早", "明晚") else 0
        t = time.mktime(time.localtime())
        lt = list(time.localtime(t))
        lt[3], lt[4], lt[5] = hh, mm, 0
        ts = time.mktime(time.localtime(time.mktime(tuple(lt)))) + day_add * 86400
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
    """解析推送时间，支持：8 / 8:30 / 8点 / 8点30 / 8点半 / 八点 / 八点半 等。返回 "HH:MM" 或 None。"""
    s = (s or "").strip()
    m = re.match(r"^([0-9一二三四五六七八九十]+)\s*[:：点]\s*([0-9一二三四五六七八九十]{1,2})?\s*(半)?\s*(?:分)?$", s)
    if m:
        h, mi = cn_to_int(m.group(1)), cn_to_int(m.group(2)) if m.group(2) else (30 if m.group(3) else 0)
    else:
        m = re.match(r"^([0-9一二三四五六七八九十]+)$", s)
        if not m:
            return None
        h, mi = cn_to_int(m.group(1)), 0
    if h is not None and mi is not None and 0 <= h <= 23 and 0 <= mi <= 59:
        return "%02d:%02d" % (h, mi)
    return None


def split_push_time_city(rest):
    """把「时间 城市」拆开，如「8:00 北京」「八点半 北京市朝阳区」。返回 (HH:MM, 城市) 或 (None, '')。"""
    rest = (rest or "").strip()
    m = re.match(r"^\s*([0-9一二三四五六七八九十]+(?:\s*[:：点]\s*(?:[0-9一二三四五六七八九十]{1,2}|\s*半)?\s*(?:分)?)?)\s*(.*)$",
                 rest, re.S)
    if m:
        hm = parse_push_time(m.group(1))
        if hm:
            return hm, (m.group(2) or "").strip()
    return parse_push_time(rest), ""


def cn_to_int(s):
    """中文数字/阿拉伯数字 -> int（仅需 0-59 范围）。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    digits = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
              "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
              "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20,
              "二十一": 21, "二十二": 22, "二十三": 23}
    if s in digits:
        return digits[s]
    if "十" in s:
        parts = s.split("十")
        if len(parts) == 2:
            tens = digits.get(parts[0]) if parts[0] else 1
            ones = digits.get(parts[1]) if parts[1] else 0
            if tens is not None and ones is not None:
                return tens * 10 + ones
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


# ---------------- 分步引导（/推送 /提醒 不写全参数时逐步收集） ----------------
WIZARD = {}
WIZARD_TTL = 600
_wizard_lock = threading.Lock()


def wizard_get(from_id):
    with _wizard_lock:
        w = WIZARD.get(from_id)
        if w and time.time() - w.get("ts", 0) > WIZARD_TTL:
            WIZARD.pop(from_id, None)
            return None
        return dict(w) if w else None


def wizard_set(from_id, w):
    w["ts"] = time.time()
    with _wizard_lock:
        WIZARD[from_id] = w


def wizard_clear(from_id):
    with _wizard_lock:
        WIZARD.pop(from_id, None)


def wizard_start_push(from_id, from_name, room_id, room_name):
    wizard_set(from_id, {
        "step": "push_time",
        "from_id": from_id, "from_name": from_name,
        "room_id": room_id, "room_name": room_name,
    })
    return "请输入推送时间（例如：8 / 8:30 / 8点30），或发 取消 中止"


def wizard_start_remind(from_id, from_name, room_id, room_name):
    wizard_set(from_id, {
        "step": "remind_time",
        "from_id": from_id, "from_name": from_name,
        "room_id": room_id, "room_name": room_name,
    })
    return "请输入提醒时间（例如：10分钟后 / 14:30 / 明天9点），或发 取消 中止"


def _wizard_create_push(w, c):
    label = c["name"] + ("·" + c["admin1"] if c.get("admin1") else "")
    upsert_sub({
        "from_id": w.get("from_id"), "from_name": w.get("from_name") or "",
        "room_id": w.get("room_id") or "", "room_name": w.get("room_name") or "",
        "time": w.get("time"), "city": c["name"], "city_label": label,
        "lat": c["lat"], "lon": c["lon"],
        "channel": "ilink" if (w.get("from_id") or "").endswith("@im.wechat")
                   or (w.get("room_id") or "").endswith("@chatroom") else "web",
    })
    wizard_clear(w.get("from_id"))
    where = "群聊「%s」" % w.get("room_name") if w.get("room_id") else "私聊"
    return ("每日推送已开启：每天 {} 在{}推送「{} 天气」。\n"
            "  修改：/推送 <时间>；取消：/取消推送".format(w.get("time"), where, label))


def handle_wizard(from_id, text):
    """处理分步引导的下一步输入；没有进行中的引导返回 None。"""
    w = wizard_get(from_id)
    if not w:
        return None
    text = (text or "").strip()
    cancel = text.lower() in ("取消", "cancel", "q", "0", "中止")
    if w.get("step") == "push_time":
        if cancel:
            wizard_clear(from_id)
            return "已取消"
        hm = parse_push_time(text)
        if not hm:
            return "时间格式不认识。请输入时间，例如：8 / 8:30 / 8点30（或发 取消 中止）"
        w["time"] = hm
        w["step"] = "push_city"
        wizard_set(from_id, w)
        return "已记录推送时间 {}。请输入地点/城市（例如：北京 / 上海 / 长垣），或发 取消 中止".format(hm)
    if w.get("step") == "push_city":
        if cancel:
            wizard_clear(from_id)
            return "已取消"
        cands = geocode_city(text)
        if not cands:
            return "没找到城市「{}」，请重新输入（例如：北京 / 上海 / 长垣），或发 取消 中止".format(text)
        if len(cands) > 1:
            w["cands"] = cands[:3]
            w["step"] = "push_city_choose"
            wizard_set(from_id, w)
            lines = ["找到多个城市，请回复数字选择："]
            for i, c in enumerate(cands[:3], 1):
                lines.append("{}. {}（{}）".format(i, c["name"], c.get("admin1") or ""))
            lines.append("0. 取消")
            return "\n".join(lines)
        return _wizard_create_push(w, cands[0])
    if w.get("step") == "push_city_choose":
        if cancel:
            wizard_clear(from_id)
            return "已取消"
        try:
            idx = int(text) - 1
        except Exception:
            return "请回复数字（1-3）或 0 取消"
        cands = w.get("cands") or []
        if not (0 <= idx < len(cands)):
            return "数字不对，请回复 1-{} 或 0 取消".format(len(cands))
        return _wizard_create_push(w, cands[idx])
    if w.get("step") == "remind_time":
        if cancel:
            wizard_clear(from_id)
            return "已取消"
        m = _REMIND_RE.match(text)
        ts = parse_remind_time(m.group(1)) if m else None
        if not ts:
            return "时间格式不认识。请输入时间，例如：10分钟后 / 14:30 / 明天9点（或发 取消 中止）"
        w["time_raw"] = m.group(1)
        w["ts_at"] = ts
        w["step"] = "remind_content"
        wizard_set(from_id, w)
        return "已记录提醒时间 {}。请输入提醒内容（例如：喝水），或发 取消 中止".format(fmt_remind_time(ts))
    if w.get("step") == "remind_content":
        if cancel:
            wizard_clear(from_id)
            return "已取消"
        if not text:
            return "请输入提醒内容（例如：喝水），或发 取消 中止"
        rid = add_reminder(w.get("from_id"), w.get("from_name"), w.get("room_id"), w.get("room_name"),
                           w.get("ts_at"), text)
        wizard_clear(from_id)
        return "提醒已设置（编号 {}）：{}（{}）".format(rid, text, fmt_remind_time(w.get("ts_at")))
    return None


def cmd_push(rest, from_id, from_name, room_id, room_name):
    """/推送 命令处理：支持「时间」或「时间 城市」一句话开启（含分步确认）。"""
    rest = (rest or "").strip()
    subs = get_subs(from_id)
    if not rest:
        if subs:
            lines = ["你的每日推送（{} 条）：".format(len(subs))]
            for i, s in enumerate(subs, 1):
                where = "群聊「%s」" % s.get("room_name") if s.get("room_id") else "私聊"
                lines.append("{}. 每天 {} 在{}推送「{} 天气」".format(
                    i, s.get("time"), where, s.get("city_label") or s.get("city")))
            lines.append("  新增：/推送 <时间>（会逐步引导）；取消：/取消推送 <编号>")
            return "\n".join(lines)
        return wizard_start_push(from_id, from_name, room_id, room_name)
    hm, city = split_push_time_city(rest)
    if not hm:
        return ("时间格式不认识，请用数字：8 / 8:30 / 8点30 / 八点\n"
                "  例：/推送 8:00 或 /推送 8:00 北京")
    for s in subs:
        if s.get("time") == hm and (s.get("room_id") or "") == (room_id or ""):
            return ("你已有一个每天 {} 的推送（同一位置）。\n"
                    "  查看：/推送；取消旧的：/取消推送 <编号>".format(hm))
    if city:
        cands = geocode_city(city)
        if not cands:
            return ("没找到城市「{}」，请检查是否有错别字，或用 /推送 <时间> 重新设置。".format(city))
        if len(cands) == 1:
            c = cands[0]
            label = c["name"] + ("·" + c["admin1"] if c.get("admin1") else "")
            upsert_sub({
                "from_id": from_id, "from_name": from_name,
                "room_id": room_id, "room_name": room_name,
                "time": hm, "city": c["name"], "city_label": label,
                "lat": c["lat"], "lon": c["lon"],
                "channel": "ilink" if (from_id.endswith("@im.wechat")
                                       or (room_id or "").endswith("@chatroom")) else "web",
            })
            where = "群聊「%s」" % room_name if room_id else "私聊"
            return ("每日推送已开启：每天 {} 在{}推送「{} 天气」。\n"
                    "  修改：/推送 <时间>；取消：/取消推送".format(hm, where, label))
        set_pending(from_id, {
            "state": "await_confirm", "time": hm,
            "from_id": from_id, "from_name": from_name,
            "room_id": room_id, "room_name": room_name,
            "candidates": cands[:3],
        })
        lines = ["找到多个城市，请回复数字选择："]
        for i, c in enumerate(cands[:3], 1):
            lines.append("{}. {}（{}{}）".format(i, c["name"], c.get("admin1") or "", c.get("country") or ""))
        lines.append("0. 取消")
        return "\n".join(lines)
    set_pending(from_id, {
        "state": "await_city", "time": hm,
        "from_id": from_id, "from_name": from_name,
        "room_id": room_id, "room_name": room_name,
    })
    return ("已记录推送时间 {}。\n"
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
    item = dict(item or {})
    if "text" in item:
        item["text"] = clean_wechat_reply(item.get("text"))
    with OUTBOX_LOCK:
        try:
            with open(OUTBOX_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception as e:
            log_error(f"写出站消息失败: {e}")


def _outbox_pending_unlocked():
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


def outbox_pending():
    with OUTBOX_LOCK:
        return _outbox_pending_unlocked()


def outbox_done(ids):
    temp_path = OUTBOX_FILE + ".tmp"
    with OUTBOX_LOCK:
        keep = [it for it in _outbox_pending_unlocked() if it.get("id") not in ids]
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                for it in keep:
                    f.write(json.dumps(it, ensure_ascii=False) + "\n")
            os.replace(temp_path, OUTBOX_FILE)
        except Exception as e:
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except Exception:
                pass
            log_error(f"更新出站消息失败: {e}")


# ---------------- AI 调用（OpenAI 兼容） ----------------
def ai_config():
    # 兼容旧内部调用；实际唯一 AI 配置是 OpenClaw Gateway。
    return openclaw_config()


def openclaw_config():
    cfg = load_config()
    return cfg.get("openclaw") or {}


def public_openclaw_config(claw):
    result = {
        "enabled": bool((claw or {}).get("enabled", False)),
        "base_url": str((claw or {}).get("base_url") or ""),
        "model": str((claw or {}).get("model") or "openclaw:wxbot"),
        "session_index": str((claw or {}).get("session_index") or OPENCLAW_INDEX_FILE),
        "transcript_dir": str((claw or {}).get("transcript_dir") or OPENCLAW_TRANSCRIPT_DIR),
    }
    key = str((claw or {}).get("api_key") or "")
    result["api_key"] = "********" if key else ""
    return result


def clean_wechat_reply(text):
    """清掉模型偶尔输出的 Markdown 装饰和 emoji，保留适合微信的纯文本。"""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    has_internal_trace = bool(re.search(r"(?m)^[ \t]*to=[^\n]*[ \t]*$", text))
    text = re.sub(r"(?m)^[ \t]*to=[^\n]*(?:\n|$)", "", text)
    text = re.sub(r"(?m)^[ \t]*total_languages=\d+[ \t]*(?:\n|$)", "", text)
    text = re.sub(r"\[\[reply_to_[^\]]+\]\]", "", text)
    text = re.sub(r"(?m)^[ \t]*\{\s*\"(?:command|pattern)\".*\}[ \t]*(?:\n|$)", "", text)
    if has_internal_trace:
        text = re.sub(r"(?m)^[ \t]*\{\s*\"action\".*\}[ \t]*(?:\n|$)", "", text)
    text = re.sub(r"\[([^\]\n]+)\]\((?:[^()\n]|\([^()\n]*\))*\)", r"\1", text)
    plain_lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                continue
            line = " ".join(cells)
        plain_lines.append(line)
    text = "\n".join(plain_lines)
    text = re.sub(r"\*\*|__|`{1,3}|~~", "", text)
    text = re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]*", "", text)
    text = re.sub(r"(?m)^[ \t]*[*+\-•][ \t]+", "", text)
    text = re.sub(r"(?m)^[ \t]*>[ \t]?", "", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2300-\u23FF\u2600-\u27BF\uFE0F]", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def wechat_outbound_text(reply, sender_name="", in_room=False):
    text = f"@{sender_name} {reply}" if in_room else str(reply or "")
    return clean_wechat_reply(text)


def geocode_city(name):
    """城市名 -> 候选 [{name, admin1, country, lat, lon}]（中文/拼音/英文均可）"""
    raw = (name or "").strip()
    # 查询名候选：原样 → 去掉尾部市/县/区 → 最前面的市/省 → 最后一段区/县名
    queries = [raw]
    s = re.sub(r"[市县区]$", "", raw)
    if s and s != raw:
        queries.append(s)
    m = re.match(r"^(.{2,8}?(?:省|市))", raw)
    if m:
        t = re.sub(r"[省市]$", "", m.group(1))
        if t and t not in queries:
            queries.append(t)
    m2 = re.search(r"([\u4e00-\u9fa5]{2,6}?[县区])$", raw)
    if m2:
        t = re.sub(r"[县区]$", "", m2.group(1))
        if t and t not in queries:
            queries.append(t)
    for q in queries:
        k = re.sub(r"[市县区]$", "", q).lower()
        hit = CITY_ALIASES.get(k) or CITY_ALIASES.get(q.lower())
        if hit:
            return [dict(hit)]
        url = ("https://geocoding-api.open-meteo.com/v1/search?name="
               + urllib.parse.quote(q) + "&count=3&language=zh&format=json")
        try:
            with urllib.request.urlopen(url, timeout=12) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
        except Exception:
            data = {}
        out = []
        for it in data.get("results", []):
            out.append({
                "name": it.get("name") or "",
                "admin1": it.get("admin1") or "",
                "country": it.get("country") or "",
                "lat": it.get("latitude"),
                "lon": it.get("longitude"),
            })
        if out:
            return out
    return []


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


def ai_chat(prompt, cfg=None, timeout=40):
    ai = cfg if cfg is not None else ai_config()
    base = (ai.get("base_url") or "").strip().rstrip("/")
    key = (ai.get("api_key") or "").strip()
    model = (ai.get("model") or "").strip()
    if not base or not key or not model:
        raise ValueError("AI 未配置：请在管理后台「AI 配置」填写接口地址 / API Key / 模型")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        raise ValueError("HTTP Error {}".format(e.code))
    except urllib.error.URLError as e:
        raise ValueError("连接失败: {}".format(e.reason))
    try:
        content = (data["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        raise ValueError("AI 返回格式异常")
    return clean_wechat_reply(content) or "（无回复）"


def _openclaw_session_lock(session_id):
    with _OPENCLAW_SESSION_LOCKS_GUARD:
        lock = _OPENCLAW_SESSION_LOCKS.get(session_id)
        if lock is None:
            if len(_OPENCLAW_SESSION_LOCKS) >= 1024:
                for key, old in list(_OPENCLAW_SESSION_LOCKS.items()):
                    if not old.locked():
                        _OPENCLAW_SESSION_LOCKS.pop(key, None)
                        if len(_OPENCLAW_SESSION_LOCKS) < 1024:
                            break
            lock = threading.Lock()
            _OPENCLAW_SESSION_LOCKS[session_id] = lock
        return lock


def openclaw_chat(prompt, session_id="", cfg=None, timeout=60,
                  system_prompt=WECHAT_SYSTEM_PROMPT, sanitize=True):
    """通过 OpenClaw Gateway 问答；同一用户的请求按会话串行。"""
    if not session_id:
        return _openclaw_chat_request(
            prompt, session_id=session_id, cfg=cfg, timeout=timeout,
            system_prompt=system_prompt, sanitize=sanitize,
        )
    with _openclaw_session_lock(str(session_id)):
        return _openclaw_chat_request(
            prompt, session_id=session_id, cfg=cfg, timeout=timeout,
            system_prompt=system_prompt, sanitize=sanitize,
        )


def _openclaw_chat_request(prompt, session_id="", cfg=None, timeout=60,
                           system_prompt=WECHAT_SYSTEM_PROMPT, sanitize=True):
    """通过 OpenClaw Gateway 问答；session_id 用于按微信用户维持连续会话。"""
    claw = cfg if cfg is not None else openclaw_config()
    base = (claw.get("base_url") or "").strip().rstrip("/")
    key = (claw.get("api_key") or "").strip()
    model = (claw.get("model") or "openclaw:wxbot").strip()
    if not base or not key:
        raise ValueError("OpenClaw 未配置：缺少 Gateway 地址或 token")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }
    if session_id:
        payload["user"] = str(session_id)
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = (e.read() or b"").decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        raise ValueError("OpenClaw HTTP Error {}: {}".format(e.code, body or e.reason))
    except urllib.error.URLError as e:
        raise ValueError("OpenClaw 连接失败: {}".format(e.reason))
    try:
        content = (data["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        raise ValueError("OpenClaw 返回格式异常: " + json.dumps(data, ensure_ascii=False)[:300])
    if session_id:
        usage = data.get("usage") or {}
        if isinstance(usage, dict):
            with OPENCLAW_USAGE_LOCK:
                _OPENCLAW_LAST_USAGE[str(session_id)] = dict(usage)
    if not sanitize:
        return content
    return clean_wechat_reply(content) or "（没有返回内容）"


# ---------------- 本地搜索兜底（搜狗→Bing，无需 API key） ----------------
_SEARCH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)


def _fetch_search_html(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": _SEARCH_UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _sogou_results(query, max_results=3):
    """搜狗网页搜索（中文覆盖好，偶发验证页返回空）。返回 [(标题, 摘要, URL)]。"""
    url = "https://www.sogou.com/web?query=" + urllib.parse.quote(query)
    body = _fetch_search_html(url)
    items = []
    for m in re.finditer(r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', body, re.S):
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if not title:
            continue
        href = html.unescape(m.group(1)).strip()
        tail = body[m.end():m.end() + 4000]
        snippet = ""
        for pat in (
            r'<div[^>]*class="[^"]*(?:str_info|space-txt|fz-mid|text-layout|fz-text)[^"]*"[^>]*>(.*?)</div>',
            r'<p[^>]*class="[^"]*str_info[^"]*"[^>]*>(.*?)</p>',
        ):
            sm = re.search(pat, tail, re.S)
            if sm:
                snippet = html.unescape(re.sub(r"<[^>]+>", " ", sm.group(1)))
                snippet = re.sub(r"\s+", " ", snippet).strip()
                break
        items.append((title, snippet, href))
        if len(items) >= max_results:
            break
    return items


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


def _bing_results(query, max_results=3):
    """Bing 网页搜索（无需 key，稳定但中文覆盖一般）。返回 [(标题, 摘要, URL)]。"""
    url = ("https://cn.bing.com/search?q=" + urllib.parse.quote(query)
           + "&setlang=zh-hans&count=10")
    body = _fetch_search_html(url)
    items = []
    for m in re.finditer(r'<li class="b_algo".*?</li>', body, re.S):
        block = m.group(0)
        hm = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not hm:
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", hm.group(2))).strip()
        if not title:
            continue
        pm = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = ""
        if pm:
            snippet = html.unescape(re.sub(r"<[^>]+>", "", pm.group(1))).strip()
        href = _bing_real_url(html.unescape(hm.group(1)))
        items.append((title, snippet, href))
        if len(items) >= max_results:
            break
    return items


def local_web_search(query, max_results=3):
    """搜狗→Bing 本地搜索兜底；返回格式化纯文本，无结果返回空串。"""
    query = re.sub(r"@\S+\s*", "", str(query or "")).strip()
    if not query:
        return ""
    items = []
    try:
        items = _sogou_results(query, max_results)
        if not items:  # 搜狗偶发验证页，重试一次
            items = _sogou_results(query, max_results)
    except Exception:
        items = []
    if not items:
        try:
            items = _bing_results(query, max_results)
        except Exception:
            items = []
    if not items:
        return ""
    lines = []
    seen = set()
    n = 0
    for title, snippet, href in items:
        key = title[:40]
        if key in seen:
            continue
        seen.add(key)
        n += 1
        lines.append("{} {}".format(n, title))
        if snippet:
            lines.append("   {}".format(snippet))
    return "\n".join(lines)


def _looks_evasive_reply(text):
    """判断 OpenClaw 回复是否为“无法确认/建议自己查”式敷衍回答。"""
    text = str(text or "")
    return any(marker in text for marker in _EVASIVE_MARKERS)


def _openclaw_search_retry(prompt, key, cfg, original, compose=True):
    """OpenClaw 回复敷衍/失败时：本地搜索资料，让模型直接整合出完整回答。"""
    try:
        results = local_web_search(prompt)
        if not results:
            return original or AI_FAILURE_MSG
        if compose:
            try:
                answer = openclaw_chat(
                    "用户问题：{}\n\n以下是搜索到的资料：\n{}".format(prompt, results),
                    session_id=key, cfg=cfg,
                    system_prompt=OPENCLAW_COMPOSE_SYSTEM_PROMPT, timeout=40,
                )
                if answer and not _looks_evasive_reply(answer):
                    return answer
            except Exception:
                pass
        return "AI 搜索通道暂时不稳定，已用本地搜索兜底，直接给你搜到的结果：\n" + results
    except Exception:
        pass
    return original or AI_FAILURE_MSG


def ai_answer(prompt, session_id=""):
    """所有微信 AI 问答统一使用 OpenClaw；敷衍/失败时本地搜索并整合完整回答。"""
    claw = openclaw_config()
    enabled = claw.get("enabled", False)
    if enabled and claw.get("base_url") and claw.get("api_key"):
        key = openclaw_active_key(session_id) if session_id else ""
        try:
            reply = openclaw_chat(prompt, session_id=key, cfg=claw)
        except Exception:
            reply = ""
        if not reply or _looks_evasive_reply(reply):
            reply = _openclaw_search_retry(
                prompt, key, claw, reply, compose=bool(reply))
        return _with_context_status(reply, key) if key else reply
    raise ValueError("OpenClaw 未配置：请启用 Gateway 并填写地址和 token")


def openclaw_compact_session(user_id):
    """在当前 Gateway session 中执行原生 /compact。"""
    key = openclaw_active_key(user_id)
    if not key:
        raise ValueError("缺少微信用户会话")
    reply = openclaw_chat("/compact", session_id=key, sanitize=False)
    return _with_context_status(reply or "已压缩当前上下文。", key)


def openclaw_route(text, session_id=""):
    """让 OpenClaw 只判断提醒/每日推送；其他消息返回 chat。"""
    raw = openclaw_chat(
        text,
        session_id="route:" + str(session_id or "anonymous"),
        system_prompt=OPENCLAW_ROUTE_PROMPT,
        sanitize=False,
    )
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return "chat_answer", {"question": text}
    try:
        data = json.loads(match.group(0))
    except Exception:
        return "chat_answer", {"question": text}
    action = str(data.pop("action", "chat") or "chat")
    if action not in AUTOMATION_ACTIONS:
        return "chat_answer", {"question": text}
    return action, data


def _openclaw_native_model_ids(path=None):
    path = path or OPENCLAW_CONFIG_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        return []
    if not isinstance(config, dict):
        return []
    ids = []
    seen = set()

    def add(value, provider=""):
        value = str(value or "").strip()
        provider = str(provider or "").strip()
        if not value:
            return
        model_id = value if "/" in value or not provider else provider + "/" + value
        if model_id not in seen:
            seen.add(model_id)
            ids.append(model_id)

    providers = ((config.get("models") or {}).get("providers") or {})
    if isinstance(providers, dict):
        for provider, provider_config in providers.items():
            if not isinstance(provider_config, dict):
                continue
            models = provider_config.get("models") or []
            if isinstance(models, list):
                for model in models:
                    if isinstance(model, dict):
                        add(model.get("id") or model.get("name"), provider)
                    else:
                        add(model, provider)

    defaults = ((config.get("agents") or {}).get("defaults") or {})
    default_models = defaults.get("models") or {}
    if isinstance(default_models, dict):
        for model_id in default_models:
            add(model_id)
    primary = ((defaults.get("model") or {}).get("primary")
               if isinstance(defaults.get("model"), dict) else "")
    add(primary)
    agents = (config.get("agents") or {}).get("list") or []
    if isinstance(agents, list):
        for agent in agents:
            if isinstance(agent, dict):
                add(agent.get("model"))
    return ids


def openclaw_fetch_models(timeout=15):
    claw = openclaw_config()
    base = (claw.get("base_url") or "").strip().rstrip("/")
    key = (claw.get("api_key") or "").strip()
    if not base or not key:
        raise ValueError("请先填写 OpenClaw Gateway 地址和 token")
    req = urllib.request.Request(
        base + "/models",
        headers={"Authorization": "Bearer " + key},
        method="GET",
    )
    gateway_error = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        items = data.get("data") or data.get("models") or []
        ids = []
        for item in items if isinstance(items, list) else []:
            model_id = item if isinstance(item, str) else (item.get("id") or item.get("name"))
            if model_id and model_id not in ids:
                ids.append(model_id)
        if ids:
            return ids
        gateway_error = "Gateway 未返回模型列表"
    except Exception as e:
        gateway_error = str(e)[:160]

    ids = _openclaw_native_model_ids()
    if ids:
        return ids
    raise ValueError("获取模型失败：{}".format(gateway_error or "未找到 OpenClaw 模型配置"))


LEDGER_FORMAT_HINT = (
    "记账需要精确格式（AI 不会代记）：\n"
    "  /记账 +8×4 记收入 32\n"
    "  /记账 -15×2 记支出 30\n"
    "  备注：/记账 +100 买菜"
)

# 路由后的补参追问：from_id -> {"tool","args","missing","text","rounds","ts"}
PENDING_ROUTE = {}
PENDING_TTL = 600
_route_lock = threading.Lock()

ROUTE_QUESTIONS = {
    "set_reminder": {
        "time": "几点提醒？例如：10分钟后 / 14:30 / 明天9点",
        "content": "提醒你做什么？例如：提醒我10分钟后喝水",
    },
    "set_daily_push": {
        "time": "每天几点推送天气？例如：8:00 / 8点30",
        "city": "推送哪个城市的天气？例如：北京 / 上海 / 长垣",
    },
}


def route_pending_get(from_id):
    with _route_lock:
        p = PENDING_ROUTE.get(from_id)
        if p and time.time() - p.get("ts", 0) > PENDING_TTL:
            PENDING_ROUTE.pop(from_id, None)
            return None
        return dict(p) if p else None


def route_pending_set(from_id, tool, args, missing, text, rounds):
    with _route_lock:
        PENDING_ROUTE[from_id] = {
            "tool": tool, "args": args, "missing": missing,
            "text": text, "rounds": rounds, "ts": time.time(),
        }


def route_pending_clear(from_id):
    with _route_lock:
        PENDING_ROUTE.pop(from_id, None)


def _ask_route(from_id, tool, args, missing, text, rounds):
    qs = [ROUTE_QUESTIONS.get(tool, {}).get(k) for k in missing]
    qs = [q for q in qs if q]
    route_pending_set(from_id, tool, args, missing, text, rounds)
    return " ".join(qs) if qs else "请补充一下信息"


def _do_reminder_route(args, text, from_id, from_name, room_id, room_name, rounds):
    t = (args.get("time") or "").strip()
    content = (args.get("content") or "").strip()
    if not t and not content:
        return _ask_route(from_id, "set_reminder", args, ["time", "content"], text, rounds)
    if not t:
        return _ask_route(from_id, "set_reminder", args, ["time"], text, rounds)
    if not content:
        return _ask_route(from_id, "set_reminder", args, ["content"], text, rounds)
    m = _REMIND_RE.match((t + " " + content).strip())
    if not m or not parse_remind_time(m.group(1)):
        return _ask_route(from_id, "set_reminder", args, ["time"], text, rounds)
    route_pending_clear(from_id)
    return do_remind("{} {}".format(t, content), from_id, from_name, room_id, room_name)


def _do_push_route(args, text, from_id, from_name, room_id, room_name, rounds):
    t = (args.get("time") or "").strip()
    city = (args.get("city") or "").strip()
    hm = parse_push_time(t)
    if not hm:
        return _ask_route(from_id, "set_daily_push", args, ["time"], text, rounds)
    if city:
        cands = geocode_city(city)
        if not cands:
            return _ask_route(from_id, "set_daily_push", args, ["city"], text, rounds)
        route_pending_clear(from_id)
        return cmd_push("{} {}".format(hm, city), from_id, from_name, room_id, room_name)
    route_pending_clear(from_id)
    return cmd_push(hm, from_id, from_name, room_id, room_name)


def dispatch_route(tool, args, text, from_id, from_name, room_id, room_name, rounds=1):
    """执行路由到的功能；参数缺失时记 pending 并追问（最多 3 轮）。"""
    try:
        if tool == "set_reminder":
            return _do_reminder_route(args, text, from_id, from_name, room_id, room_name, rounds)
        if tool == "set_daily_push":
            return _do_push_route(args, text, from_id, from_name, room_id, room_name, rounds)
        if tool == "chat_answer":
            route_pending_clear(from_id)
            if not is_allowed(from_id):
                return AI_NO_PERMISSION_MSG
            q = (args.get("question") or text or "").strip()
            try:
                return ai_answer(q, session_id=from_id)
            except Exception as e:
                log_error(f"OpenClaw 问答失败: {e}")
                return AI_FAILURE_MSG
    except Exception as e:
        log_error(f"意图执行失败: {e}")
        return None
    return None


def smart_fallback(text, from_id, from_name, room_id, room_name, cfg):
    """普通消息由 OpenClaw 回答；提醒和推送由 OpenClaw 判断后交给本地执行。"""
    text = (text or "").strip()
    if len(text) < 2:
        return None
    if not is_allowed(from_id):
        return AI_NO_PERMISSION_MSG

    def answer_openclaw():
        try:
            return ai_answer(text, session_id=from_id)
        except Exception as e:
            log_error(f"OpenClaw 问答失败: {e}")
            return AI_FAILURE_MSG

    pending = route_pending_get(from_id)
    if pending:
        if pending.get("rounds", 1) >= 3:
            route_pending_clear(from_id)
            return "没收到需要的参数，已取消。可以直接重新说一遍，例如：提醒我10分钟后喝水 / 上海天气"
        ctx = ("之前你说：{old}。你想执行功能「{tool}」，还缺：{missing}。"
               "用户现在补充说：{new}。请调用对应工具并补全所有参数。").format(
            old=pending.get("text", ""), tool=pending.get("tool", ""),
            missing="、".join(pending.get("missing", [])), new=text)
        try:
            tool, args = openclaw_route(ctx, from_id)
        except Exception as e:
            log_error(f"OpenClaw 补参路由失败: {e}")
            return AI_FAILURE_MSG
        if tool != pending.get("tool"):
            route_pending_clear(from_id)
            if tool == "chat_answer":
                return answer_openclaw()
            return dispatch_route(tool, args, text, from_id, from_name, room_id, room_name, rounds=1)
        return dispatch_route(tool, args, text, from_id, from_name, room_id, room_name,
                              rounds=pending.get("rounds", 1) + 1)

    if cfg.get("smart", True) and _looks_like_automation(text):
        try:
            tool, args = openclaw_route(text, from_id)
        except Exception as e:
            log_error(f"OpenClaw 自动化路由失败: {e}")
            return AI_FAILURE_MSG
        if tool in AUTOMATION_ACTIONS:
            return dispatch_route(
                tool, args, text, from_id, from_name, room_id, room_name, rounds=1
            )
    return answer_openclaw()


# ---------------- 命令处理 ----------------
def handle_command(text, from_id, from_name, room_id, room_name, cfg):
    """返回回复文本；无需回复返回 None。"""
    raw_text = (text or "").strip()
    if raw_text.lower() in SESSION_COMMANDS:
        if raw_text.lower() in ("/compact", "/压缩上下文", "压缩上下文"):
            try:
                return openclaw_compact_session(from_id)
            except Exception as e:
                log_error("OpenClaw 上下文压缩失败: {}".format(e))
                return AI_FAILURE_MSG
        try:
            openclaw_start_new_session(from_id)
            return "已开启新的会话。后续对话将从新的上下文开始。"
        except Exception as e:
            log_error("OpenClaw 新会话失败: {}".format(e))
            return AI_FAILURE_MSG
    cmd = text.split(None, 1)[0].lower()
    rest = text[len(cmd):].strip()

    if cmd in ("/ai",):
        if not rest:
            return "⚠️ 用法：/ai 后面加空格再写内容，例如：/ai 今天上海天气怎么样"
        try:
            return ai_answer(rest, session_id=from_id)
        except Exception as e:
            log_error(f"AI 调用失败: {e}")
            return AI_FAILURE_MSG

    if cmd in ("/搜索", "/search"):
        if not rest:
            return "用法：/搜索 <内容>，例如：/搜索 今天有什么足球比赛"
        try:
            return ai_answer(rest, session_id=from_id)
        except Exception as e:
            log_error(f"OpenClaw 问答失败: {e}")
            return AI_FAILURE_MSG

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
            return "✅ 授权成功！现在可以直接 @我 提问，或用自然语言设置提醒和每日推送。"
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
    r"|(?:(?:今天|明天|今晚|明早|明晚|每天|每晚)\s*)?(?:(?:早上|早晨|上午|中午|下午|晚上|凌晨)\s*)?\d{1,2}:\d{2}"  # 明天8:00 / 下午3:30
    r"|\d+\s*秒(?:钟)?(?:后)?"                             # 30秒后
    r"|\d+\s*分钟?(?:钟)?(?:后)?"                          # 10分钟后
    r"|\d+\s*小时?(?:后)?"                                 # 2小时后
    r"|(?:(?:今天|明天|今晚|明早|明晚|每天|每晚)\s*)?(?:(?:早上|早晨|上午|中午|下午|晚上|凌晨)\s*)?\d{1,2}\s*点(?:\s*\d{1,2}\s*分?|\s*半)?"  # 9点 / 明天9点半 / 明天早上9点
    r")\s*(.*)$", re.S)


def do_remind(rest, from_id, from_name, room_id, room_name=""):
    rest = rest.strip()
    if not rest:
        return wizard_start_remind(from_id, from_name, room_id, room_name)
    m = _REMIND_RE.match(rest)
    if not m:
        return "时间格式不认识。请输入时间，例如：10分钟后 / 14:30 / 明天9点（或发 取消 中止）"
    ts = parse_remind_time(m.group(1))
    content = (m.group(2) or "").strip()
    if not ts:
        return "时间格式不认识。请输入时间，例如：10分钟后 / 14:30 / 明天9点（或发 取消 中止）"
    if not content:
        wizard_set(from_id, {
            "step": "remind_content",
            "time_raw": m.group(1), "ts_at": ts,
            "from_id": from_id, "from_name": from_name,
            "room_id": room_id, "room_name": room_name,
        })
        return "已记录提醒时间 {}。请输入提醒内容（例如：喝水），或发 取消 中止".format(fmt_remind_time(ts))
    rid = add_reminder(from_id, from_name, room_id, room_name, ts, content)
    return "提醒已设置（编号 {}）：{}（{}）".format(rid, content, fmt_remind_time(ts))


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

WECHAT_LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>微信扫码登录</title>
<script src="/wechat-login/qrcode.js"></script>
<style>
body{font-family:-apple-system,'PingFang SC',sans-serif;background:#eef1f5;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;color:#20252b}
.login-box{width:360px;background:#fff;border:1px solid #e2e6ec;border-radius:8px;padding:28px;box-sizing:border-box;text-align:center;box-shadow:0 4px 18px rgba(0,0,0,.08)}
h1{font-size:21px;margin:0 0 8px}.status{font-size:15px;color:#667085;margin-bottom:18px;min-height:22px}
#qrcode{width:300px;height:300px;margin:0 auto;display:flex;align-items:center;justify-content:center}
#qrcode img,#qrcode canvas{display:block;max-width:100%}
.actions{display:flex;gap:10px;justify-content:center;margin-top:18px}.actions a,.actions button{border:1px solid #cfd6df;background:#fff;color:#26384c;border-radius:6px;padding:9px 14px;text-decoration:none;font-size:15px;cursor:pointer}
.actions button{background:#2f6fed;color:#fff;border-color:#2f6fed}
</style></head>
<body><main class="login-box"><h1>微信扫码登录</h1><div class="status" id="status">正在获取二维码</div>
<div id="qrcode"></div><div class="actions"><a href="/">返回后台</a><button type="button" onclick="refreshQr()">刷新二维码</button></div></main>
<script>
var qr=null,lastCode='';
async function refreshQr(){
  var status=document.getElementById('status');
  try{
    var response=await fetch('/api/wechat-login/qrcode',{cache:'no-store'});
    var data=await response.json();
    if(data.logged_in){status.textContent='微信已登录';document.getElementById('qrcode').innerHTML='';return}
    if(!data.success||!data.qr_url){status.textContent=data.message||'暂时无法获取二维码';return}
    status.textContent='请使用微信扫描二维码';
    if(data.qr_url!==lastCode){
      var box=document.getElementById('qrcode');box.innerHTML='';
      qr=new QRCode(box,{width:300,height:300});qr.makeCode(data.qr_url);lastCode=data.qr_url;
    }
  }catch(error){status.textContent='登录入口连接失败，请刷新重试'}
}
refreshQr();setInterval(refreshQr,15000);
</script></body></html>"""

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
nav{display:flex;background:#fff;border-bottom:1px solid #e2e6ec;padding:0 20px;gap:4px;overflow-x:auto}
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
.plain{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;max-width:420px}
.reply{color:#0a7d33;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}
.messages-table{table-layout:fixed;min-width:920px}
.table-scroll{overflow-x:auto;width:100%}
.session-actions{display:flex;gap:6px;flex-wrap:wrap}.session-actions .btn{padding:6px 9px}
.transcript-row{border-bottom:1px solid #edf0f4;padding:12px 2px}.transcript-role{font-size:13px;color:#667085;margin-bottom:5px}.transcript-text{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.55}
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
form.inline input[type=text],form.inline input[type=password],form.inline textarea{padding:9px 10px;border:1px solid #ccd2da;border-radius:6px;font-size:15px;font-family:inherit}
form.inline textarea{min-height:60px}
form.inline .row{display:flex;gap:12px;align-items:center}
a.link{color:#2f6fed;font-size:15px}
.badge{padding:2px 8px;border-radius:10px;font-size:14px}
.badge.on{background:#dff5e5;color:#0a7d33}
.badge.off{background:#fdeaea;color:#c0392b}
</style></head>
<body>
<header><h1>微信机器人管理后台</h1>
<div class="right"><span id="hdr-status">连接中…</span><a id="login-entry" href="/wechat-login" target="_blank">扫码登录</a><a href="/api/export" download>下载记录</a><form method="post" action="/logout" style="display:inline"><button type="submit">退出登录</button></form></div>
</header>
<nav>
<button class="active" onclick="switchTab('tab-status',this)">状态总览</button>
<button onclick="switchTab('tab-overview',this)">用户总览</button>
<button onclick="switchTab('tab-msgs',this)">消息记录</button>
<button onclick="switchTab('tab-logs',this)">日志与报错</button>
<button onclick="switchTab('tab-mgmt',this)">管理操作</button>
<button onclick="switchTab('tab-users',this)">用户与权限</button>
<button onclick="switchTab('tab-sessions',this);loadOpenClawSessions()">会话与上下文</button>
<button onclick="switchTab('tab-ai',this);loadOpenClawConfig()">OpenClaw 配置</button>
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
  <div class="table-scroll"><table class="messages-table"><colgroup><col style="width:150px"><col style="width:100px"><col style="width:150px"><col style="width:230px"><col style="width:55px"><col style="width:235px"></colgroup><thead><tr><th>时间</th><th>会话</th><th>发送者</th><th>内容</th><th>@我</th><th>自动回复</th></tr></thead>
  <tbody id="msg-body"></tbody></table></div>
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
    <div class="sub" style="font-size:15px">规则：所有人可用 计算 / 记账 / 余额 / 明细 / 提醒 / 说明；只有已授权用户能使用 AI 问答（授权/取消在下方表格操作）。</div>
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

<div id="tab-sessions" class="tab">
  <div class="panel">
    <div class="toolbar"><button onclick="loadOpenClawSessions()">刷新会话</button><span id="session-result" class="dim"></span></div>
    <div class="table-scroll"><div id="session-list">加载中</div></div>
  </div>
  <div class="panel">
    <h3>对话记录</h3>
    <div id="session-detail" class="dim">选择一个会话查看输入、输出和压缩记录</div>
  </div>
</div>

<div id="tab-ai" class="tab">
  <div class="panel">
    <h3>OpenClaw Gateway 配置</h3>
    <form class="inline" onsubmit="event.preventDefault();saveOpenClawConfig()">
      <label class="switch"><input type="checkbox" id="claw-enabled"> 启用 OpenClaw 对话</label>
      <label>Gateway Base URL</label>
      <input type="text" id="claw-base" placeholder="http://127.0.0.1:18788/v1" autocomplete="off">
      <label>Gateway Token</label>
      <input type="password" id="claw-key" placeholder="输入新 token；留着掩码表示不修改" autocomplete="new-password">
      <label>模型 <span id="claw-model-status" class="dim"></span></label>
      <div class="row">
        <input type="text" id="claw-model" placeholder="openclaw:wxbot" style="flex:1">
        <button class="btn2" type="button" onclick="fetchOpenClawModels()">获取模型</button>
      </div>
      <label>Session 索引文件</label>
      <input type="text" id="claw-index" placeholder="/root/openclaw/openclaw_space/agents/wxbot/sessions/sessions.json">
      <label>Transcript 目录</label>
      <input type="text" id="claw-transcripts" placeholder="/root/openclaw/openclaw_space/agents/wxbot/sessions">
      <div class="row">
        <button type="submit">保存配置</button>
        <button class="btn2" type="button" onclick="testOpenClaw()">测试连接</button>
        <span id="claw-result" style="font-size:15px;color:#666;word-break:break-all"></span>
      </div>
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
      tr.innerHTML='<td>'+esc(fmtTime(m.time))+'</td><td>'+esc(m.room||'私聊')+'</td><td>'+esc(m.from)+(m.user_ref?'<div class="dim">'+esc(m.user_ref)+'</div>':'')+'</td>'
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
async function loadOpenClawConfig(){
  try{
    var d=await api('/api/openclaw/config');var a=d.openclaw||{};
    $('claw-enabled').checked=!!a.enabled;$('claw-base').value=a.base_url||'';$('claw-key').value=a.api_key||'';
    $('claw-model').value=a.model||'openclaw:wxbot';$('claw-index').value=a.session_index||'';$('claw-transcripts').value=a.transcript_dir||'';
    $('claw-model-status').textContent=a.base_url?(a.model?'已配置 '+esc(a.model):'已填地址，未配模型'):'未配置';
  }catch(e){}
}
function openClawForm(){
  return {enabled:$('claw-enabled').checked,base_url:$('claw-base').value.trim(),api_key:$('claw-key').value.trim(),model:$('claw-model').value.trim(),session_index:$('claw-index').value.trim(),transcript_dir:$('claw-transcripts').value.trim()};
}
async function saveOpenClawConfig(){
  try{
    await api('/api/openclaw/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(openClawForm())});
    $('claw-result').textContent='已保存';loadOpenClawConfig();
  }catch(e){$('claw-result').textContent='保存失败'}
}
async function testOpenClaw(){
  $('claw-result').textContent='测试中';
  try{
    var d=await api('/api/openclaw/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(openClawForm())});
    $('claw-result').textContent=d.success?esc(d.reply).slice(0,120):esc(d.message||'连接失败');
  }catch(e){$('claw-result').textContent='请求失败'}
}
async function fetchOpenClawModels(){
  $('claw-model-status').textContent='获取中';
  try{
    var d=await api('/api/openclaw/models');
    if(!d.success){$('claw-model-status').textContent=esc(d.message);return}
    if(d.models&&d.models.length){
      var opts=d.models.map(function(m){return '<option value="'+esc(m)+'">'+esc(m)+'</option>'}).join('');
      var cur=$('claw-model').value.trim();
      $('claw-model').outerHTML='<input type="text" id="claw-model" list="claw-model-list" placeholder="openclaw:wxbot" style="flex:1"><datalist id="claw-model-list">'+opts+'</datalist>';
      $('claw-model').value=cur;
    }
    $('claw-model-status').textContent='共 '+(d.models?d.models.length:0)+' 个模型';
  }catch(e){$('claw-model-status').textContent='获取失败'}
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
      h+='<tr><td>'+esc(r.ref)+'</td><td>'+esc(fmtRemind(r.at))+'</td><td>'+esc(r.from_name||r.user_ref)+'</td>'
        +'<td class="plain">'+esc(r.text)+'</td><td><button class="btn" data-rid="'+esc(r.ref)+'" onclick="cancelReminder(this)">取消</button></td></tr>';
    });
    $('reminder-list').innerHTML=h+'</tbody></table>';
  }catch(e){}
}
async function cancelReminder(btn){
  var id=btn?btn.getAttribute('data-rid'):'';
  if(!id)return;
  try{
    await api('/api/reminders/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ref:id})});
    loadReminders();
  }catch(e){}
}
async function loadSubs(){
  try{
    var d=await api('/api/subs');var ss=d.subs||[];
    if(!ss.length){$('subs-list').innerHTML='<span class="dim">暂无订阅</span>';return}
    var h='<table><thead><tr><th>时间</th><th>用户</th><th>推送位置</th><th>城市</th><th>最近推送</th><th>操作</th></tr></thead><tbody>';
    ss.forEach(function(s){
      var where=s.room_ref?('群聊 '+esc(s.room_name||s.room_ref)):'私聊';
      h+='<tr><td>'+esc(s.time)+'</td><td>'+esc(s.from_name||s.user_ref)+'</td><td>'+where+'</td>'
        +'<td>'+esc(s.city_label||s.city)+'</td><td>'+esc(s.last_sent||'-')+'</td>'
        +'<td><button class="btn" data-rid="'+esc(s.ref)+'" onclick="cancelSub(this)">取消</button></td></tr>';
    });
    $('subs-list').innerHTML=h+'</tbody></table>';
  }catch(e){}
}
async function cancelSub(btn){
  var id=btn?btn.getAttribute('data-rid'):'';
  if(!id)return;
  try{
    await api('/api/subs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'cancel',ref:id})});
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
      h+='<tr><td>'+esc(u.name)+'<br><span class="dim">'+esc(u.user_ref)+'</span></td><td>'+roleTxt+'</td>'
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
    var h='<table><thead><tr><th>昵称</th><th>用户引用</th><th>最后活跃</th><th>余额</th><th>状态</th><th>操作</th></tr></thead><tbody>';
    us.forEach(function(u){
      var roleTxt=u.role==='member'?'<span class="badge on">已授权（含 AI）</span>':'<span class="badge off">未授权（基础功能）</span>';
      var btns=u.role==='member'
        ?'<button class="btn btn2" data-a="revoke" data-f="'+esc(u.user_ref)+'" onclick="userAct(this)">取消授权</button>'
        :'<button class="btn" data-a="grant" data-f="'+esc(u.user_ref)+'" onclick="userAct(this)">授权</button>';
      h+='<tr><td>'+esc(u.name)+'</td><td class="plain">'+esc(u.user_ref)+'</td><td>'+esc(u.last_seen)+'</td>'
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
    await api('/api/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:action,user_ref:fid})});
    loadUsers();refreshStatus();
  }catch(e){}
}
async function loadOpenClawSessions(){
  var list=$('session-list'),result=$('session-result');
  try{
    var d=await api('/api/openclaw/sessions');var sessions=d.sessions||[];
    if(!sessions.length){list.innerHTML='<span class="dim">暂无 OpenClaw 会话</span>';return}
    var h='<table><thead><tr><th>用户</th><th>会话引用</th><th>最近更新</th><th>上下文</th><th>消息</th><th>压缩</th><th>操作</th></tr></thead><tbody>';
    sessions.forEach(function(s){
      var state=s.active?'<span class="badge on">当前</span> ':'';
      h+='<tr><td>'+esc(s.user_name)+'<br><span class="dim">'+esc(s.user_ref)+'</span></td>'
        +'<td>'+state+esc(s.session_ref)+'</td><td>'+esc(s.updated_at||'-')+'</td>'
        +'<td>'+esc(s.context_text||'暂无统计')+'</td><td>'+esc(s.message_count)+'</td><td>'+esc(s.compaction_count)+'</td>'
        +'<td><div class="session-actions"><button class="btn" onclick="viewOpenClawSession(\\''+esc(s.session_ref)+'\\')">查看</button>'
        +'<button class="btn btn2" onclick="openClawSessionAction(\\'compact\\',\\''+esc(s.session_ref)+'\\')">压缩</button>'
        +'<button class="btn btn2" onclick="openClawSessionAction(\\'new\\',\\''+esc(s.session_ref)+'\\')">新会话</button>'
        +'<button class="btn btn2" onclick="openClawSessionAction(\\'activate\\',\\''+esc(s.session_ref)+'\\')">设为当前</button></div></td></tr>';
    });
    list.innerHTML=h+'</tbody></table>';result.textContent='共 '+sessions.length+' 个会话';
  }catch(e){list.innerHTML='<span class="bad">会话加载失败</span>'}
}
async function viewOpenClawSession(ref){
  var detail=$('session-detail');detail.textContent='加载中';
  try{
    var d=await api('/api/openclaw/sessions/'+encodeURIComponent(ref));
    var rows=(d.messages||[]).map(function(m){
      var role=m.role==='user'?'用户输入':'OpenClaw 输出';
      return '<div class="transcript-row"><div class="transcript-role">'+role+' · '+esc(m.timestamp||'')+'</div><div class="transcript-text">'+esc(m.content||'')+'</div></div>';
    });
    (d.compactions||[]).forEach(function(c){
      rows.push('<div class="transcript-row"><div class="transcript-role">上下文压缩 · '+esc(c.timestamp||'')+'</div><div class="transcript-text">'+esc(c.summary||'已压缩上下文')+'</div></div>');
    });
    detail.className='';detail.innerHTML=rows.join('')||'<span class="dim">该会话暂无消息</span>';
  }catch(e){detail.className='bad';detail.textContent='会话详情加载失败'}
}
async function openClawSessionAction(action,ref){
  var result=$('session-result');result.textContent='处理中';
  try{
    var d=await api('/api/openclaw/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:action,session_ref:ref})});
    result.textContent=d.success?(d.reply||'操作完成'):(d.message||'操作失败');
    await loadOpenClawSessions();
  }catch(e){result.textContent='操作失败'}
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
loadOpenClawConfig();
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


# ---------------- 后台会话 API 数据 ----------------
def _openclaw_session_public(item, user_name=""):
    return {
        "user_ref": item.get("user_ref", ""),
        "session_ref": item.get("session_ref", ""),
        "user_name": public_display_name(user_name, item.get("_user_id")),
        "updated_at": item.get("updated_at", ""),
        "context_used": item.get("context_used"),
        "context_limit": item.get("context_limit"),
        "context_text": format_context_usage(
            item.get("context_used"), item.get("context_limit"),
            item.get("context_cached"), item.get("context_miss"),
        ),
        "message_count": item.get("message_count", 0),
        "compaction_count": item.get("compaction_count", 0),
        "active": bool(item.get("_active")),
    }


def _openclaw_session_records():
    result = openclaw_parse_sessions_index()
    with OPENCLAW_REGISTRY_LOCK:
        registry = _openclaw_registry_load().get("users", {})
    with USERS_LOCK:
        users = load_users().get("users", {})
    names = {str(k): str(v.get("name") or "") for k, v in users.items() if isinstance(v, dict)}
    sessions = result.get("sessions", [])
    for item in sessions:
        active_key = str((registry.get(item.get("_user_id")) or {}).get("active_key") or item.get("_user_id") or "")
        item["_active"] = item.get("_session_key") == _OPENCLAW_INDEX_PREFIX + active_key
    return sessions, names


def _find_openclaw_session(session_ref=""):
    sessions, names = _openclaw_session_records()
    wanted = str(session_ref or "").strip()
    for item in sessions:
        if item.get("session_ref") == wanted:
            return item, names
    # 命令接口只接受短引用；兼容旧后台首次升级时传来的占位引用。
    if len(sessions) == 1 and wanted in ("session-old", "current"):
        return sessions[0], names
    return None, names


def _openclaw_activate_session(user_id, session_key):
    with OPENCLAW_REGISTRY_LOCK:
        data = _openclaw_registry_load()
        data["users"][str(user_id)] = {
            "active_key": str(session_key),
            "created_at": now_str(),
        }
        _openclaw_registry_save(data)
    return str(session_key)


def _public_message_record(record):
    item = dict(record or {})
    from_id = item.pop("fromId", "")
    room_id = item.pop("roomId", "")
    stable_id = identity_existing_user_id(from_id) or from_id
    sender = str(item.get("from") or "")
    if sender:
        item["from"] = public_display_name(sender, stable_id)
    item["user_ref"] = _short_ref(stable_id, "u") if stable_id else ""
    item["room_ref"] = _short_ref(room_id, "r") if room_id else ""
    item["reply"] = clean_wechat_reply(item.get("reply"))
    return item


def _public_user_record(fid, info, ledger, members):
    stable_fid = identity_existing_user_id(fid) or fid
    l = ledger.get(fid) or ledger.get(stable_fid)
    return {
        "user_ref": public_user_ref(stable_fid),
        "name": public_display_name(info.get("name"), stable_fid) or "未命名用户",
        "last_seen": info.get("last_seen") or "",
        "balance": l["balance"] if l else 0,
        "count": len(l["entries"]) if l else 0,
        "role": "member" if fid in members or stable_fid in members else None,
    }


def _resolve_user_ref(value):
    value = str(value or "").strip()
    if not value:
        return ""
    identity_id = identity_existing_user_id(value)
    if identity_id:
        return identity_id
    with USERS_LOCK:
        users = load_users().get("users", {})
    with PERM_LOCK:
        members = load_permissions().get("members", {})
    for fid in set(users) | set(members):
        if value == fid or value == _short_ref(fid, "u"):
            return fid
    return value if len(value) < 80 else ""


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
        if path == "/wechat-login/qrcode.js":
            if not self._require_auth():
                return
            try:
                self._send(fetch_wechat_qrcode_script(), 200, "application/javascript; charset=utf-8",
                           {"Cache-Control": "no-store"})
            except Exception as e:
                log_error("加载微信二维码脚本失败: {}".format(e))
                self._send("", 502, "application/javascript; charset=utf-8")
            return
        if path == "/wechat-login":
            if not self._authed():
                self._redirect("/login")
            else:
                self._send(WECHAT_LOGIN_PAGE, 200, "text/html; charset=utf-8",
                           {"Cache-Control": "no-store"})
            return
        if path == "/api/export":
            if not self._require_auth():
                return
            exported = []
            for line in tail_file(LOG_FILE, 100000):
                try:
                    exported.append(json.dumps(_public_message_record(json.loads(line)), ensure_ascii=False))
                except Exception:
                    exported.append(public_log_line(line))
            self._send("\n".join(exported), 200, "text/plain; charset=utf-8")
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
                "config": public_config(cfg),
                "stats": {
                    "total": STATS["total"],
                    "today": STATS["today"],
                    "last": _public_message_record(STATS["last"]) if STATS["last"] else None,
                    "last_error": STATS["last_error"],
                    "since": STATS.get("since", ""),
                },
                "login_url": "/wechat-login",
            })
        elif path == "/api/wechat-login/qrcode":
            status = bot_status()
            if status.get("logged_in"):
                self._json({"success": True, "logged_in": True, "qr_url": ""})
                return
            try:
                qr_url = fetch_wechat_login_qr()
            except Exception as e:
                log_error("获取微信登录二维码失败: {}".format(e))
                qr_url = ""
            self._json({
                "success": bool(qr_url),
                "logged_in": False,
                "qr_url": qr_url,
                "message": "" if qr_url else "暂时无法获取二维码，请稍后刷新",
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
            self._json({"total": total, "page": page, "limit": limit,
                        "items": [_public_message_record(item) for item in items]})
        elif path == "/api/logs":
            only_err = (qs.get("err") or ["0"])[0] == "1"
            bot_lines = self._bot_log_lines(only_err)
            error_lines = tail_file(ERROR_LOG, 200)
            system_lines = tail_file(SYSTEM_LOG, 200)
            self._json({
                "botLines": [public_log_line(line) for line in bot_lines],
                "errorLines": [public_log_line(l.get("time") + " " + l.get("msg")) if isinstance(l, dict) else public_log_line(l) for l in error_lines],
                "systemLines": [public_log_line(l) for l in system_lines] if isinstance(system_lines, list) and system_lines and isinstance(system_lines[0], str) else [public_log_line(json.dumps(l, ensure_ascii=False) if isinstance(l, dict) else l) for l in system_lines],
            })
        elif path == "/api/config":
            self._json(public_config(load_config()))
        elif path == "/api/openclaw/config":
            self._json({"success": True, "openclaw": public_openclaw_config(openclaw_config())})
        elif path == "/api/openclaw/models":
            try:
                ids = openclaw_fetch_models()
                self._json({"success": True, "models": ids})
            except Exception as e:
                self._json({"success": False, "message": str(e)[:200]})
        elif path.startswith("/api/ai"):
            self._json({"success": False, "message": "旧 AI 配置已移除，请使用 OpenClaw 配置"}, 410)
        elif path == "/api/reminders":
            with REMINDER_LOCK:
                data = load_reminders()
            reminders = []
            for item in data["reminders"]:
                public = dict(item)
                rid = public.pop("id", "")
                fid = public.pop("from_id", "")
                room_id = public.pop("room_id", "")
                public["ref"] = _short_ref(rid, "r")
                public["user_ref"] = public_user_ref(fid)
                public["room_ref"] = _short_ref(room_id, "r") if room_id else ""
                public["from_name"] = public_display_name(public.get("from_name"), fid)
                public["room_name"] = public_log_line(public.get("room_name") or "")
                reminders.append(public)
            self._json({"success": True, "reminders": reminders})
        elif path == "/api/subs":
            with SUBS_LOCK:
                d = load_subs()
            subs = []
            for item in d.get("subscriptions", []):
                public = dict(item)
                sid = public.pop("id", "")
                fid = public.pop("from_id", "")
                room_id = public.pop("room_id", "")
                public["ref"] = _short_ref(sid, "s")
                public["user_ref"] = public_user_ref(fid)
                public["room_ref"] = _short_ref(room_id, "r") if room_id else ""
                public["from_name"] = public_display_name(public.get("from_name"), fid)
                public["room_name"] = public_log_line(public.get("room_name") or "")
                subs.append(public)
            self._json({"success": True, "subs": subs})
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
                if rec.get("type") == "text" and (rec.get("reply") or "").strip().startswith("🤖"):
                    fid = rec.get("fromId") or ""
                    st = ai_stats.setdefault(fid, {"count": 0, "last": ""})
                    st["count"] += 1
                    st["last"] = rec.get("time") or st["last"]
            out = []
            for fid, info in users.items():
                l = ledger.get(fid)
                my_subs = [{"time": s.get("time"), "city": s.get("city_label") or s.get("city"),
                            "room": public_log_line(s.get("room_name") or "")}
                           for s in subs if s.get("from_id") == fid]
                my_rem = [r for r in reminders if r.get("from_id") == fid]
                ai = ai_stats.get(fid, {})
                out.append({
                    "user_ref": public_user_ref(fid), "name": public_display_name(info.get("name"), fid) or "未命名用户",
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
                "members": [
                    {"user_ref": public_user_ref(fid),
                     "name": public_display_name(info.get("name"), fid) or "未命名用户"}
                    for fid, info in p.get("members", {}).items()
                ],
            })
        elif path == "/api/users":
            with USERS_LOCK:
                users = load_users()["users"]
            ledger = load_ledger()["users"]
            with PERM_LOCK:
                p = load_permissions()
            members = p.get("members", {})
            out = []
            seen = set()
            for fid, info in users.items():
                out.append(_public_user_record(fid, info, ledger, members))
                seen.add(fid)
            for fid, info in members.items():
                if fid not in seen:
                    out.append(_public_user_record(fid, info, ledger, members))
            out.sort(key=lambda x: x["last_seen"], reverse=True)
            self._json({"success": True, "users": out})
        elif path == "/api/openclaw/sessions":
            sessions, names = _openclaw_session_records()
            self._json({
                "success": True,
                "sessions": [_openclaw_session_public(item, names.get(item.get("_user_id"), ""))
                             for item in sessions],
            })
        elif path.startswith("/api/openclaw/sessions/"):
            session_ref = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            item, names = _find_openclaw_session(session_ref)
            if not item:
                self._json({"success": False, "message": "session 不存在"}, 404)
                return
            transcript = item.get("_transcript") or []
            messages = [
                {"role": rec.get("role"), "content": clean_wechat_reply(rec.get("content")),
                 "timestamp": rec.get("timestamp", "")}
                for rec in transcript if rec.get("role") in ("user", "assistant")
            ]
            compactions = [dict(rec) for rec in transcript if rec.get("type") == "compaction"]
            self._json({
                "success": True,
                "session": _openclaw_session_public(item, names.get(item.get("_user_id"), "")),
                "messages": messages,
                "compactions": compactions,
            })
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
            if path == "/api/openclaw/sessions":
                action = str(data.get("action") or "").strip().lower()
                item, _names = _find_openclaw_session(data.get("session_ref"))
                if action == "new":
                    if not item:
                        self._json({"success": False, "message": "session 不存在"}, 404)
                        return
                    key = openclaw_start_new_session(item.get("_user_id"))
                    self._json({"success": True, "active_session_ref": _short_ref(key, "s")})
                elif action == "compact":
                    if not item:
                        self._json({"success": False, "message": "session 不存在"}, 404)
                        return
                    try:
                        reply = openclaw_compact_session(item.get("_user_id"))
                        self._json({"success": True, "reply": clean_wechat_reply(reply)})
                    except Exception:
                        self._json({"success": False, "message": "上下文压缩失败"}, 500)
                elif action == "activate":
                    if not item:
                        self._json({"success": False, "message": "session 不存在"}, 404)
                        return
                    _openclaw_activate_session(item.get("_user_id"), item.get("_session_key"))
                    self._json({"success": True, "active_session_ref": item.get("session_ref")})
                else:
                    self._json({"success": False, "message": "unknown action"})
            elif path == "/api/openclaw/config":
                cfg = load_config()
                claw = cfg.setdefault("openclaw", {})
                if "enabled" in data:
                    claw["enabled"] = bool(data.get("enabled"))
                for field in ("base_url", "model", "session_index", "transcript_dir"):
                    if field in data:
                        claw[field] = str(data.get(field) or "").strip()
                new_key = str(data.get("api_key") or "").strip()
                if new_key and "*" not in new_key:
                    claw["api_key"] = new_key
                elif "api_key" in data and not new_key:
                    claw["api_key"] = ""
                cfg.pop("ai", None)
                save_config(cfg)
                self._json({"success": True, "config": public_config(cfg)})
            elif path == "/api/openclaw/test":
                try:
                    tmp = dict(openclaw_config())
                    for field in ("enabled", "base_url", "model", "session_index", "transcript_dir"):
                        if field in data:
                            tmp[field] = bool(data[field]) if field == "enabled" else str(data[field] or "").strip()
                    new_key = str(data.get("api_key") or "").strip()
                    if new_key and "*" not in new_key:
                        tmp["api_key"] = new_key
                    reply = openclaw_chat("你好，请只回复：连接正常", cfg=tmp, timeout=20)
                    self._json({"success": True, "reply": reply})
                except Exception as e:
                    self._json({"success": False, "message": str(e)[:200]})
            elif path == "/api/config":
                cfg = load_config()
                if "auto_reply" in data:
                    cfg["auto_reply"] = bool(data["auto_reply"])
                save_config(cfg)
                self._json({"success": True, "config": public_config(cfg)})
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
                parsed = public_json_value(parsed)
                self._json({"success": ok, "status": 200, "bot": parsed})
            elif path.startswith("/api/ai"):
                self._json({"success": False, "message": "旧 AI 配置已移除，请使用 OpenClaw 配置"}, 410)
            elif path == "/api/reminders/cancel":
                rid = str(data.get("ref") or data.get("id") or "").strip()
                with REMINDER_LOCK:
                    d = load_reminders()
                    kept = [r for r in d["reminders"]
                            if r.get("id") != rid and _short_ref(r.get("id"), "r") != rid]
                    removed = len(d["reminders"]) - len(kept)
                    d["reminders"] = kept
                    save_reminders(d)
                self._json({"success": removed > 0, "removed": removed})
            elif path == "/api/subs":
                action = str(data.get("action") or "")
                if action == "cancel":
                    sid = str(data.get("ref") or data.get("id") or "").strip()
                    with SUBS_LOCK:
                        d = load_subs()
                        n = len(d.get("subscriptions", []))
                        d["subscriptions"] = [s for s in d.get("subscriptions", [])
                                              if s.get("id") != sid and
                                              _short_ref(s.get("id"), "s") != sid]
                        if len(d["subscriptions"]) != n:
                            save_subs(d)
                    self._json({"success": True})
                else:
                    self._json({"success": False, "message": "unknown action"})
            elif path == "/api/permissions":
                with PERM_LOCK:
                    p = load_permissions()
                self._json({"success": True, "members": [
                    {"user_ref": public_user_ref(fid),
                     "name": public_display_name(info.get("name"), fid) or "未命名用户"}
                    for fid, info in p.get("members", {}).items()
                ]})
            elif path == "/api/users":
                action = str(data.get("action") or "")
                raw_fid = str(data.get("from_id") or "").strip()
                fid = _resolve_user_ref(raw_fid) if raw_fid else _resolve_user_ref(data.get("user_ref"))
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
            or ""
        )
        transport_id = from_payload.get("id") or ""
        from_id = identity_user_id(from_payload, transport_id)
        if not sender_name:
            sender_name = "用户 " + public_user_ref(from_id) if from_id else "未知"
        record_user(from_id, sender_name)

        reply = None
        cfg = load_config()
        if mtype == "text" and cfg.get("auto_reply", True):
            text_in = content.strip()
            # 每日推送城市确认流程：有待确认时优先处理（群里可不用 @机器人）
            pending_reply = handle_pending_reply(from_id, text_in) if text_in else None
            wizard_reply = handle_wizard(from_id, text_in) if (pending_reply is None and text_in) else None
            if pending_reply is not None:
                reply = pending_reply
            elif wizard_reply is not None:
                reply = wizard_reply
            elif (not in_room) or mentioned:
                # 群聊里可能带 @机器人 前缀（如 "@kindle /余额"），先剥掉再判断命令
                cmd_text = re.sub(r"^@\s*[\u4e00-\u9fa5\w\-]+", "", text_in).strip()
                cmd_word = cmd_text.split(None, 1)[0].lower() if cmd_text else ""
                # 权限规则：AI 类命令需要授权；普通功能（计算/记账/提醒等）人人可用
                if ((cmd_text.startswith("/") and cmd_word in AI_CMDS)
                        or cmd_text.strip().lower() in SESSION_COMMANDS) and not is_allowed(from_id):
                    reply = AI_NO_PERMISSION_MSG
                elif cmd_text.startswith("/") or cmd_text.strip().lower() in SESSION_COMMANDS:
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
                        reply = smart_fallback(cmd_text, from_id, sender_name, room_id, room_name, cfg)
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

        text = wechat_outbound_text(reply, sender_name, in_room)
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
