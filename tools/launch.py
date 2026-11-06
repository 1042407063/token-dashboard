# -*- coding: utf-8 -*-
"""token panel 启动器。

设计目标：
  * 全程只有**一个**控制台窗口 —— 不再有"闪一下又弹一个"的窗口；
  * 每个阶段都有一行彩色状态，成功/警告/失败用不同颜色与符号区分；
  * 服务以后台无窗口方式拉起，本窗口只负责显示与收尾；
  * 结束时不自动关闭，等用户按键，避免"没看清就没了"。

注意：本文件是 UTF-8，中文只出现在这里；start-panel.bat 保持纯 ASCII，
避免 cmd 按 GBK 读 UTF-8 造成的乱码与误执行（这个坑踩过两次）。
"""

import ctypes
import json
import re
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "data", "panel.log")

LOGO = r"""
   _    __   ___
  | |  |  | |   |     T O K E N
  | |  |  | |   |     P A N E L
  |_|  |__| |___|     workbuddy 用量 · 纯本地
"""


# ---------------- 终端能力 ----------------
def enable_ansi():
    """打开 Windows 控制台的 VT 序列支持，否则 \033[... 会原样打出来。"""
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
    """读一个按键就返回，不需要回车。管道/重定向时退回按行读。

    msvcrt.getwch() 正是 cmd 的 pause 用的机制；它不回显，所以调用方要自己
    把用户的选择显式打印出来，否则按了键屏幕上没有反应。
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    if os.name == "nt" and sys.stdin.isatty():
        try:
            import msvcrt
            # 启动那几秒里如果按过键，缓冲区会把它们立刻消费掉，
            # 看起来像"没等就关了"。先把残留按键排空再等。
            while msvcrt.kbhit():
                msvcrt.getwch()
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):      # 方向键等功能键的首字节，吞掉第二个字节
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
    """按东亚宽度算显示宽度，保证方框对得齐。"""
    import unicodedata
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def visible(s):
    """去掉颜色码后的真实文本，用来算宽度。"""
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


# ---------------- 环境探测 ----------------
def find_python():
    """优先用 WorkBuddy 自带解释器；退到 PATH 上的 python，跳过 0 字节占位符。"""
    base = os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "python", "versions")
    if os.path.isdir(base):
        for name in sorted(os.listdir(base), reverse=True):
            p = os.path.join(base, name, "python.exe")
            if os.path.exists(p):
                return p
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, "python.exe")
        try:
            if os.path.exists(p) and os.path.getsize(p) > 0:
                return p
        except OSError:
            continue
    return None


def healthz(port, timeout=0.4):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def port_free(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def stop_panel(port):
    """停掉面板：优先用 healthz 报的 pid，退回 data/panel.pid。数据与索引保留。"""
    info = healthz(port)
    pid = info.get("pid") if info else None
    if not pid:
        try:
            with open(os.path.join(ROOT, "data", "panel.pid"), "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
        except Exception:
            pid = None
    if not pid:
        return None
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    try:
        os.remove(os.path.join(ROOT, "data", "panel.pid"))
    except OSError:
        pass
    for _ in range(25):                       # 最多等 5 秒确认端口释放
        time.sleep(0.2)
        if not healthz(port, 0.25):
            break
    return pid


def tail_log(n=6):
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            return [ln.rstrip() for ln in f.readlines()[-n:]]
    except OSError:
        return []


# ---------------- 主流程 ----------------
def main():
    enable_ansi()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    os.system("chcp 65001 >nul 2>nul")

    print()
    print(C.CY + C.B + LOGO + C.R)
    print(f"  {C.DIM}v1.0 · 本地运行，数据不出这台机器{C.R}")
    print(f"  {C.GY}{'-' * 52}{C.R}")
    print()

    total = 4
    cfg = {}
    # 读端口配置（失败就用 8770）
    port = 8770
    try:
        sys.path.insert(0, ROOT)
        import config
        port = getattr(config, "DEFAULT_PORT", 8770)
        cfg["projects"] = getattr(config, "PROJECTS_DIR", "")
    except Exception:
        cfg["projects"] = os.path.join(os.path.expanduser("~"), ".workbuddy", "projects")

    # 1/4 解释器
    step(1, total, "检查运行环境 ", end="")
    py = find_python()
    if not py:
        fail("找不到可用的 python.exe")
        print(f"  {C.DIM}提示：请确认 WorkBuddy 已安装，或用系统 Python 3 重试。{C.R}")
        return 1
    ok(py.replace(os.path.expanduser("~"), "~"))

    # 2/4 数据源
    step(2, total, "检查数据源 ", end="")
    if not os.path.isdir(cfg["projects"]):
        fail("找不到 ~/.workbuddy/projects")
        print(f"  {C.DIM}这个目录是 WorkBuddy 的转录存放处，缺了就统计不到用量。{C.R}")
        return 1
    ok("~/.workbuddy/projects 可读")

    # 3/4 端口
    step(3, total, "检查端口 ", end="")
    live = healthz(port)
    info = live
    if live:
        warn(f"{port} 上已经有一个面板在跑（pid {live.get('pid')}）")
        port = live.get("port") or port
        already = True
    elif port_free(port):
        ok(f"{port} 空闲")
        already = False
    else:
        warn(f"{port} 被占用，服务会自动顺延到 {port + 1}–{port + 9}")
        already = False

    # 4/4 启动
    if already:
        step(4, total, "跳过启动 ", end="")
        ok("复用已有实例")
    else:
        step(4, total, "启动服务 ", end="")
        pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else py
        flags = 0
        if os.name == "nt":
            flags = 0x00000008 | 0x08000000      # DETACHED_PROCESS | CREATE_NO_WINDOW
        subprocess.Popen(
            [exe, os.path.join(ROOT, "server.py")],
            cwd=ROOT, creationflags=flags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        print(f" {C.DIM}已拉起，等待就绪{C.R} ", end="", flush=True)
        info = None
        for i in range(80):                      # 最多 20 秒
            time.sleep(0.25)
            info = healthz(port)
            if info:
                break
            if i % 2 == 0:
                print(f"{C.GY}.{C.R}", end="", flush=True)
        print()
        if not info:
            fail("服务在 20 秒内没有响应")
            for ln in tail_log():
                print(f"  {C.GY}{ln}{C.R}")
            print(f"  {C.DIM}完整日志：{LOG_PATH}{C.R}")
            return 1
        print(f"  {C.GR}√{C.R} 服务就绪，pid {info.get('pid')}")

    actual = (info or {}).get("port") or port
    url = "http://127.0.0.1:%d/" % actual

    print()
    # 方框里不放 √ 这类"歧义宽度"字符：控制台按双宽渲染，边框会错位
    # 已经在跑用青色、刚启动用绿色，一眼能分出这两种情况
    tone = C.CY if already else C.GR
    word = "已  在  运  行" if already else "已  启  动"
    box([
        f"{tone}{C.B}{word}{C.R}",
        f"{C.WH}{url}{C.R}",
        f"{C.GY}数据来自本机转录，不上传任何内容{C.R}",
    ], color=tone)

    try:
        webbrowser.open(url)
    except Exception:
        pass

    print()
    if already:
        print(f"  {C.DIM}面板已经在后台运行，不需要重复启动；浏览器已为你打开。{C.R}")
    else:
        print(f"  {C.DIM}关闭本窗口不会停止面板，面板在后台独立运行。{C.R}")
    print()

    key = read_key(f"  {C.WH}按 {C.B}S{C.R}{C.WH} 停止面板（关闭程序） · "
                   f"按其他任意键只关闭窗口 ...{C.R}")
    print()

    if key.strip().lower() == "s":
        print(f"  {C.DIM}正在停止 ...{C.R}", end="", flush=True)
        pid = stop_panel(actual)
        if pid:
            print(f"\r  {C.GR}√{C.R} 面板已停止（pid {pid}），数据与索引都保留。")
            print(f"  {C.DIM}下次双击即可重新启动。{C.R}")
        else:
            print(f"\r  {C.YE}!{C.R} 没找到正在运行的面板，可能已经被关掉了。")
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
