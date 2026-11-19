# -*- coding: utf-8 -*-
"""集中配置：路径、端口、扫描预算。

所有路径都从这里出发。用户可改的项放在 data/settings.json，
升级替换代码不会丢；数据源目录还可用 --workbuddy-dir 覆盖。
"""

import json
import os
import sys
import threading

APP_NAME = "token-panel"
# 2：新增 accounts / account_switches 两张表（纯增量，不动 events/credits 的列）
SCHEMA_VERSION = 2

# ---------- 本项目自身路径 ----------
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
INDEX_DB = os.path.join(DATA_DIR, "index.db")
LOG_PATH = os.path.join(DATA_DIR, "panel.log")
PID_PATH = os.path.join(DATA_DIR, "panel.pid")
WEB_DIR = os.path.join(ROOT, "web")
VENDOR_DIR = os.path.join(WEB_DIR, "vendor")

# ---------- WorkBuddy 数据源（全程只读）----------
HOME = os.path.expanduser("~")
WORKBUDDY_DIR = os.path.join(HOME, ".workbuddy")
PROJECTS_DIR = os.path.join(WORKBUDDY_DIR, "projects")
SESSIONS_DIR = os.path.join(WORKBUDDY_DIR, "sessions")
WB_DB = os.path.join(WORKBUDDY_DIR, "workbuddy.db")
WORKSPACE_NAMES_PATH = os.path.join(WORKBUDDY_DIR, "workspace-display-names.json")


def apply_workbuddy_dir(path=None):
    """覆盖数据源根目录，下游所有路径一起重算。"""
    global WORKBUDDY_DIR, PROJECTS_DIR, SESSIONS_DIR, WB_DB, WORKSPACE_NAMES_PATH
    chosen = path or os.environ.get("WORKBUDDY_DIR")
    if not chosen:
        return
    WORKBUDDY_DIR = os.path.abspath(os.path.expanduser(chosen))
    PROJECTS_DIR = os.path.join(WORKBUDDY_DIR, "projects")
    SESSIONS_DIR = os.path.join(WORKBUDDY_DIR, "sessions")
    WB_DB = os.path.join(WORKBUDDY_DIR, "workbuddy.db")
    WORKSPACE_NAMES_PATH = os.path.join(WORKBUDDY_DIR, "workspace-display-names.json")


# ---------- 账号显示名（settings.json 可覆盖）----------
ACCOUNT_NAME_TEMPLATE = "账号 {short}"
ACCOUNT_ALIASES = {"__none__": "未归属"}

# ---------- 服务 ----------
HOST = "127.0.0.1"      # 只监听本机回环，不对外暴露
DEFAULT_PORT = 8770
PORT_SPAN = 10
MAX_SSE_CLIENTS = 4

# ---------- 扫描与缓存 ----------
CHUNK = 4 * 1024 * 1024
MAX_LINE = 8 * 1024 * 1024
SCAN_INTERVAL = 1.0
IDLE_INTERVAL = 5.0
WAIT_SLICE = 1.0
SCAN_BUDGET_MS = 80
TICK = 1.5
PING_INTERVAL = 10.0
CACHE_MAX_AGE = 60.0
CACHE_MAX_ENTRIES = 64       # 聚合缓存条目上限，超出按 LRU 淘汰
CHECKPOINT_INTERVAL = 300.0
CREDIT_REFRESH_CEIL = 600

TS_MIN = 946684800000
TS_MAX = 4102444800000


def load_settings(path=None):
    """读取用户配置并覆盖默认值。文件缺失或格式错误一律用默认值。"""
    global ACCOUNT_NAME_TEMPLATE, ACCOUNT_ALIASES, DEFAULT_PORT
    try:
        with open(path or SETTINGS_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(cfg, dict):
        return {}
    tpl = cfg.get("account_name_template")
    if isinstance(tpl, str) and tpl.strip():
        ACCOUNT_NAME_TEMPLATE = tpl
    aliases = cfg.get("account_aliases")
    if isinstance(aliases, dict):
        merged = {"__none__": "未归属"}
        merged.update({str(k): str(v) for k, v in aliases.items()})
        ACCOUNT_ALIASES = merged
    port = cfg.get("port")
    if isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535:
        DEFAULT_PORT = port
    return cfg


_ALIAS_LOCK = threading.Lock()


def set_account_aliases(names):
    """批量更新账号显示名，原子写回 data/settings.json 并同步内存配置。

    names: {uid 或 "__none__": 昵称}；空字符串表示删除该别名（恢复默认）。
    只改 account_aliases 一项，端口、模板等其他配置原样保留。
    """
    global ACCOUNT_ALIASES
    with _ALIAS_LOCK:
        ensure_dirs()
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                cfg = {}
        except (OSError, ValueError):
            cfg = {}
        old = cfg.get("account_aliases")
        merged = dict(old) if isinstance(old, dict) else {}
        for uid, name in (names or {}).items():
            uid, name = str(uid).strip(), str(name).strip()
            if not uid:
                continue
            if name:
                merged[uid] = name
            else:
                merged.pop(uid, None)
        cfg["account_aliases"] = merged
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, SETTINGS_PATH)
        live = {"__none__": "未归属"}
        live.update(merged)
        ACCOUNT_ALIASES = live
        return dict(live)


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def setup_console():
    """pythonw 下没有控制台，print 中文会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


apply_workbuddy_dir()
load_settings()
