# -*- coding: utf-8 -*-
"""统一命令行入口：start / stop / status / once。

跨平台：Windows 后台用 DETACHED_PROCESS + CREATE_NO_WINDOW，
macOS / Linux 用 start_new_session。端口顺延与单实例判定都在这里收口，
tools/launch.py 只保留 Windows 的控制台界面。
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import config  # noqa: E402

PORTS = range(config.DEFAULT_PORT, config.DEFAULT_PORT + config.PORT_SPAN)


def healthz(port, timeout=0.4):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port,
                                   timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def find_panel():
    """扫描全部顺延端口，返回 (port, info)；没找到返回 (None, None)。"""
    for p in PORTS:
        info = healthz(p)
        if info and info.get("app") == config.APP_NAME:
            return p, info
    return None, None


def port_free(port):
    s = socket.socket()
    try:
        s.bind((config.HOST, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def wait_ready(timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        port, info = find_panel()
        if info:
            return port, info
        time.sleep(0.25)
    return None, None


def start(open_browser=True, extra=None, quiet=False):
    """返回 (返回码, 端口, healthz 信息, 是否本来就在运行)。"""
    global PORTS
    port, info = find_panel()
    if info:
        url = "http://127.0.0.1:%d/" % (info.get("port") or port)
        if not quiet:
            print("面板已在运行：%s" % url)
        if open_browser:
            webbrowser.open(url)
        return 0, port, info, True

    if not os.path.isdir(config.PROJECTS_DIR):
        print("找不到数据源：%s" % config.PROJECTS_DIR, file=sys.stderr)
        print("装过 WorkBuddy 才会有这个目录；也可用 --workbuddy-dir 指定。",
              file=sys.stderr)
        return 1, None, None, False

    cmd = [sys.executable, os.path.join(ROOT, "server.py")] + list(extra or [])
    kwargs = {"cwd": ROOT, "stdin": subprocess.DEVNULL,
              "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
              "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)

    port, info = wait_ready()
    if not info:
        print("服务在 20 秒内没有响应，请看 %s" % config.LOG_PATH, file=sys.stderr)
        return 1, None, None, False
    url = "http://127.0.0.1:%d/" % (info.get("port") or port)
    if not quiet:
        print("面板已启动：%s (pid %s)" % (url, info.get("pid")))
    if open_browser:
        webbrowser.open(url)
    return 0, port, info, False


def stop():
    port, info = find_panel()
    pid = info.get("pid") if info else None
    if not pid:
        try:
            with open(config.PID_PATH, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            pid = None
    if not pid:
        print("面板没有在运行。")
        return 0
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, check=False)
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        pass
    for _ in range(25):
        time.sleep(0.2)
        if not healthz(port or config.DEFAULT_PORT, 0.25):
            break
    try:
        os.remove(config.PID_PATH)
    except OSError:
        pass
    print("面板已停止（pid %s），数据与索引保留。" % pid)
    return 0


def status():
    port, info = find_panel()
    if not info:
        print("未运行")
        return 1
    print("地址        : http://127.0.0.1:%d/" % (info.get("port") or port))
    print("pid         : %s" % info.get("pid"))
    print("运行时长    : %s 秒" % info.get("uptime_s"))
    print("在线客户端  : %s" % info.get("clients"))
    print("扫描间隔    : %s 秒" % info.get("scan_interval"))
    return 0


def once(extra=None):
    import server
    return server.main(["--once"] + list(extra or []))


def main(argv=None):
    global PORTS
    ap = argparse.ArgumentParser(description="Token 面板统一入口")
    ap.add_argument("action", nargs="?", default="start",
                    choices=["start", "stop", "status", "once"])
    ap.add_argument("--workbuddy-dir", default="", help="覆盖 WorkBuddy 数据源目录")
    ap.add_argument("--no-open", action="store_true", help="启动后不打开浏览器")
    ap.add_argument("--port", type=int, default=0, help="指定起始端口")
    ap.add_argument("--no-credit", action="store_true", help="不读 workbuddy.db")
    args = ap.parse_args(argv)

    config.apply_workbuddy_dir(args.workbuddy_dir)
    if args.port:
        PORTS = range(args.port, args.port + config.PORT_SPAN)
    extra = []
    if args.port:
        extra += ["--port", str(args.port)]
    if args.no_credit:
        extra += ["--no-credit"]

    if args.action == "start":
        return start(open_browser=not args.no_open, extra=extra)[0]
    if args.action == "stop":
        return stop()
    if args.action == "status":
        return status()
    return once(extra)


if __name__ == "__main__":
    sys.exit(main())
