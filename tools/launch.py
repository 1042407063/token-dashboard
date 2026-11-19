# -*- coding: utf-8 -*-
"""Windows 单窗口启动器：只负责显示，实际动作全部交给 panel.py。

本文件是 UTF-8，中文只出现在这里；start-panel.bat 保持纯 ASCII，
避免 cmd 按 GBK 读 UTF-8 造成的乱码与误执行。
"""

import ctypes
import os
import re
import sys
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "data", "panel.log")
sys.path.insert(0, ROOT)

import config  # noqa: E402
import panel  # noqa: E402

LOGO = r"""
   _    __   ___
  | |  |  | |   |     T O K E N
  | |  |  | |   |     P A N E L
  |_|  |__| |___|     workbuddy 用量 · 纯本地
"""


def enable_ansi():
    """打开 Windows 控制台的 VT 序列支持，否则颜色码会原样打出来。"""
    if os.name != "nt":
        return
    try:
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


class C:
    R = "\033[0m"
    B = "\033[1m"
    DIM = "\033[2m"
    CY = "\033[96m"
    GR = "\033[92m"
    YE = "\033[93m"
    RD = "\033[91m"
    GY = "\033[90m"
    WH = "\033[97m"


def read_key(prompt):
    """读一个按键就返回；管道/重定向时退回按行读。"""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    if os.name == "nt" and sys.stdin.isatty():
        try:
            import msvcrt
            while msvcrt.kbhit():          # 排空启动期间误按的键
                msvcrt.getwch()
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                msvcrt.getwch()
                return ""
            return ch
        except Exception:
            pass
    try:
        line = sys.stdin.readline()
        return line[:1] if line else ""
    except Exception:
        return ""


def wait_any_key(prompt="  按任意键关闭窗口 ..."):
    read_key(prompt)


def wcwidth(s):
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def visible(s):
    return ANSI_RE.sub("", s)


def box(lines, color=C.GR):
    inner = max(wcwidth(visible(x)) for x in lines) + 6
    print("  " + color + "+" + "-" * inner + "+" + C.R)
    for x in lines:
        pad = inner - 3 - wcwidth(visible(x))
        print("  " + color + "|" + C.R + "   " + x + " " * max(0, pad) + color + "|" + C.R)
    print("  " + color + "+" + "-" * inner + "+" + C.R)


def step(i, n, text, end=""):
    print(f"  {C.GY}[{i}/{n}]{C.R} {text}", end=end, flush=True)


def ok(text):
    print(f" {C.GR}√{C.R} {text}")


def warn(text):
    print(f" {C.YE}!{C.R} {C.YE}{text}{C.R}")


def fail(text):
    print(f" {C.RD}×{C.R} {C.RD}{text}{C.R}")


def version():
    try:
        with open(os.path.join(ROOT, "VERSION"), "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def tail_log(n=6):
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            return [ln.rstrip() for ln in f.readlines()[-n:]]
    except OSError:
        return []


def main():
    enable_ansi()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if os.name == "nt":
        os.system("chcp 65001 >nul 2>nul")

    print()
    print(C.CY + C.B + LOGO + C.R)
    print(f"  {C.DIM}v{version()} · 本地运行，数据不出这台机器{C.R}")
    print(f"  {C.GY}{'-' * 52}{C.R}")
    print()

    total = 4
    # 1/4 解释器（bat 已经找过；这里只显示当前用的那个）
    step(1, total, "检查运行环境 ", end="")
    ok(sys.executable.replace(os.path.expanduser("~"), "~"))

    # 2/4 数据源
    step(2, total, "检查数据源 ", end="")
    if not os.path.isdir(config.PROJECTS_DIR):
        fail("找不到 WorkBuddy 转录目录")
        print(f"  {C.DIM}缺少 {config.PROJECTS_DIR}，统计不到用量。{C.R}")
        return 1
    ok("~/.workbuddy/projects 可读")

    # 3/4 端口：扫描全部顺延端口，避免只探默认端口造成误判
    step(3, total, "检查端口 ", end="")
    live_port, live = panel.find_panel()
    already = bool(live)
    if already:
        warn(f"{live_port} 上已经有一个面板在跑（pid {live.get('pid')}）")
    elif panel.port_free(config.DEFAULT_PORT):
        ok(f"{config.DEFAULT_PORT} 空闲")
    else:
        warn(f"{config.DEFAULT_PORT} 被占用，服务会自动顺延到 "
             f"{config.DEFAULT_PORT + 1}–{config.DEFAULT_PORT + config.PORT_SPAN - 1}")

    # 4/4 启动
    if already:
        step(4, total, "跳过启动 ", end="")
        ok("复用已有实例")
        actual = live.get("port") or live_port
    else:
        step(4, total, "启动服务 ", end="")
        print(f" {C.DIM}已拉起，等待就绪{C.R} ", end="", flush=True)
        rc, port, info, _ = panel.start(open_browser=False, quiet=True)
        print()
        if rc:
            fail("服务在 20 秒内没有响应")
            for ln in tail_log():
                print(f"  {C.GY}{ln}{C.R}")
            print(f"  {C.DIM}完整日志：{LOG_PATH}{C.R}")
            return 1
        ok("服务就绪，pid %s" % info.get("pid"))
        actual = info.get("port") or port

    url = "http://127.0.0.1:%d/" % actual
    print()
    # 方框里不放"√"这类歧义宽度字符，控制台按双宽渲染会让边框错位
    tone = C.CY if already else C.GR
    box([
        f"{tone}{C.B}{'已  在  运  行' if already else '已  启  动'}{C.R}",
        f"{C.WH}{url}{C.R}",
        f"{C.GY}数据来自本机转录，不上传任何内容{C.R}",
    ], color=tone)

    try:
        webbrowser.open(url)
    except Exception:
        pass

    print()
    print(f"  {C.DIM}{'面板已经在后台运行，不需要重复启动。' if already else '关闭本窗口不会停止面板。'}{C.R}")
    print()
    key = read_key(f"  {C.WH}按 {C.B}S{C.R}{C.WH} 停止面板 · "
                   f"按其他任意键只关闭窗口 ...{C.R}")
    print()

    if key.strip().lower() == "s":
        panel.stop()
        print(f"  {C.DIM}数据与索引都保留，下次双击即可重新启动。{C.R}")
        print()
        wait_any_key()
    else:
        print(f"  {C.DIM}窗口已关闭，面板继续在后台运行。{C.R}")

    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
