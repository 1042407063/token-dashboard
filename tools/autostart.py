# -*- coding: utf-8 -*-
"""开机自启：在用户启动文件夹放一个启动脚本。

不用注册表 Run 键：用文件方式最稳，不需要管理员权限，删文件即可撤销。
生成的脚本调用 panel.py start，与手动启动走同一套端口/数据源检查。

编码说明：启动脚本里可能含中文路径，必须按本机 ANSI 代码页（中文 Windows
为 cp936）写入，cmd.exe 才能正确解析。

用法：
  python tools/autostart.py install
  python tools/autostart.py uninstall
  python tools/autostart.py status
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PANEL = os.path.join(ROOT, "panel.py")

STARTUP = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
ENTRY = os.path.join(STARTUP, "token-panel.cmd")


def pythonw(exe=None):
    exe = exe or sys.executable or "pythonw.exe"
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def body(exe=None):
    return (
        "@echo off\r\n"
        "rem token-panel autostart entry\r\n"
        "start \"\" \"%s\" \"%s\" start --no-open\r\n" % (pythonw(exe), PANEL)
    )


def install(exe=None):
    if not os.path.exists(PANEL):
        print("找不到 panel.py：%s" % PANEL)
        return 1
    if exe and not os.path.exists(exe):
        print("解释器不存在：%s" % exe)
        return 1
    os.makedirs(STARTUP, exist_ok=True)
    with open(ENTRY, "w", encoding="mbcs", errors="replace", newline="") as f:
        f.write(body(exe))
    print("已写入启动项：%s" % ENTRY)
    print("解释器：%s" % pythonw(exe))
    print("入口　：%s" % PANEL)
    print("下次登录 Windows 时会自动在后台启动面板。")
    print("撤销：运行 uninstall-autostart.bat")
    return 0


def uninstall():
    if os.path.exists(ENTRY):
        os.remove(ENTRY)
        print("已删除启动项：%s" % ENTRY)
    else:
        print("没有找到启动项，无需处理。")
    return 0


def status():
    print("启动项路径：%s" % ENTRY)
    if os.path.exists(ENTRY):
        print("状态：已启用")
        try:
            with open(ENTRY, "r", encoding="mbcs", errors="replace") as f:
                for line in f:
                    if line.strip().startswith("start "):
                        print("内容：%s" % line.strip())
        except OSError as e:
            print("读取失败：%s" % e)
    else:
        print("状态：未启用")
    return 0


def main(argv):
    args = argv[1:]
    exe = None
    if "--python" in args:
        i = args.index("--python")
        if i + 1 >= len(args):
            print("--python 需要一个解释器路径")
            return 2
        exe = args[i + 1]
        del args[i:i + 2]
    action = (args[0] if args else "status").lower()
    if action == "install":
        return install(exe)
    if action == "uninstall":
        return uninstall()
    if action == "status":
        return status()
    print("用法：python tools/autostart.py [install [--python EXE]|uninstall|status]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
