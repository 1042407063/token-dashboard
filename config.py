# -*- coding: utf-8 -*-
"""集中配置：路径、端口、扫描预算。

这里是全项目唯一的"路径来源"。任何模块都不要自己拼路径，
否则中文目录名、盘符大小写、短路径的问题会散落到各处。
"""

import os
import sys

APP_NAME = "token-panel"
# 2：新增 accounts / account_switches 两张表（纯增量，不动 events/credits 的列）
SCHEMA_VERSION = 2

# ---------- 本项目自身路径 ----------
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
INDEX_DB = os.path.join(DATA_DIR, "index.db")
LOG_PATH = os.path.join(DATA_DIR, "panel.log")
PID_PATH = os.path.join(DATA_DIR, "panel.pid")
WEB_DIR = os.path.join(ROOT, "web")
VENDOR_DIR = os.path.join(WEB_DIR, "vendor")

# ---------- WorkBuddy 数据源 ----------
HOME = os.path.expanduser("~")
WORKBUDDY_DIR = os.path.join(HOME, ".workbuddy")
PROJECTS_DIR = os.path.join(WORKBUDDY_DIR, "projects")   # JSONL 转录（必需）
SESSIONS_DIR = os.path.join(WORKBUDDY_DIR, "sessions")   # 心跳，用于识别当前会话
WB_DB = os.path.join(WORKBUDDY_DIR, "workbuddy.db")      # credit（可选）
# 项目空间显示名：在 WorkBuddy 里重命名空间只改这个文件，磁盘目录名不变
WORKSPACE_NAMES_PATH = os.path.join(WORKBUDDY_DIR, "workspace-display-names.json")

# ---------- 账号显示名 ----------
# 转录与本地库里只有 user_id（UUID），没有昵称，也不联网去查。
# 所以「没单独命名的账号」必须自带 UUID 短码 —— 否则多个账号会显示成同一个名字。
#
# 想给某个账号起昵称，按完整 UUID 或前 8 位填进 ACCOUNT_ALIASES 即可；
# 前 8 位可在看板上悬停账号名看到，例如：
#   "1a2b3c4d": "工作号",
#
# "__none__" 只用于「未归属」会话。旧版那个 "default" 已弃用：
# 一个固定字符串不能同时代表多个真实账号，那正是多个账号同名的原因。
ACCOUNT_NAME_TEMPLATE = "账号 {short}"   # 占位符：{short}=UUID 前 8 位、{uid}=完整 UUID
ACCOUNT_ALIASES = {
    "__none__": "未归属",
    # 在这里填昵称，例如：
    # "1a2b3c4d": "工作号",
    # "5e6f7a8b": "个人号",
}

# ---------- 服务 ----------
# 仅局域网：绑全部网卡，来源由 server._host_ok() 限制在回环 + RFC1918 私有网段
HOST = "0.0.0.0"
DEFAULT_PORT = 8770
PORT_SPAN = 10
MAX_SSE_CLIENTS = 4

# ---------- 扫描 ----------
CHUNK = 4 * 1024 * 1024          # 单次最多读 4MB
MAX_LINE = 8 * 1024 * 1024       # 单行硬上限，超过则整段丢弃
SCAN_INTERVAL = 1.0              # 秒，有客户端在看时的索引间隔
IDLE_INTERVAL = 5.0              # 秒，没人在看时的索引间隔（省 CPU）
WAIT_SLICE = 1.0                 # 秒，分片等待上限：即使漏了唤醒，切回实时也不超过一拍
SCAN_BUDGET_MS = 80              # 单拍预算，超了就下一拍继续
TICK = 1.5                       # 秒，SSE 推送间隔（也是变更合并窗口）
PING_INTERVAL = 10.0             # 秒，数据没变化时的心跳间隔
CACHE_TTL = 1.0                  # 秒，已废弃：缓存改为版本驱动，见 CACHE_MAX_AGE
CACHE_MAX_AGE = 60.0             # 秒，版本没变时的兜底重算上限（防漏 bump 导致永久停更）
CHECKPOINT_INTERVAL = 300.0      # 秒，按时间压 WAL（按拍数会随降频被拉长到 25 分钟）
CREDIT_REFRESH_CEIL = 600        # 秒，db 长期不变时也强制全量复核一次

# 时间戳合理区间，防异常值把 datetime 搞崩
TS_MIN = 946684800000            # 2000-01-01
TS_MAX = 4102444800000           # 2100-01-01


def ensure_dirs():
    """创建运行时目录。"""
    os.makedirs(DATA_DIR, exist_ok=True)


def setup_console():
    """pythonw 下没有控制台，print 中文会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
