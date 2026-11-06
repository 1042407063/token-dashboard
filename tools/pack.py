# -*- coding: utf-8 -*-
"""打包成可以拷到别的电脑上直接运行的 zip。

排除的东西：data/（索引与日志，属于这台机器）、dist/、__pycache__、.pyc。
包含的东西：全部源码 + web/ + tools/ + 文档 + 一个写给"接收方"的说明。

用法：
    python tools/pack.py
    python tools/pack.py --out D:\\somewhere
"""

import argparse
import datetime
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 这些目录不进包：data 是运行时数据，别人拿到会看到你的用量；
# .workbuddy 是开发时的助手记忆与工作区状态，跟项目本身无关
SKIP_DIRS = {"data", "dist", "__pycache__", ".git", ".idea", ".vscode", ".pytest_cache",
             ".workbuddy"}
SKIP_EXT = {".pyc", ".pyo", ".tmp", ".log"}
# 首次运行.txt 由下面的 writestr 按当前版本统一生成，遍历时跳过，
# 否则 zip 里会出现两个同名条目（解压工具会提示覆盖）
SKIP_FILES = {"docs/_patched_server_sample.py", "首次运行.txt"}

FIRST_RUN = """这是 token-panel 的发行包（版本 {ver}）
================================================

它是什么
  一个只在本机运行的 WorkBuddy 用量看板。读取 ~/.workbuddy 下的转录，
  统计 token 与积分消耗，展现在浏览器里。不上传任何数据。

运行前要有的东西
  1. 这台电脑装过 WorkBuddy，并且用它聊过天（数据来源就是它的转录）
  2. Python 3.7 或更高（WorkBuddy 自带的 Python 也算；装过 WorkBuddy 就有）

怎么启动
  双击 start-panel.bat
  启动器会依次检查解释器、数据源、端口，然后在浏览器打开
  http://127.0.0.1:8770/

  再次双击会显示"已在运行"，此时：
    按 S        停止面板
    按其他键    只关窗口，面板继续在后台跑

不想用启动器的话
  python server.py --open

第一次启动会扫一遍历史转录，大约几十秒；之后都是增量扫描，很快。

出问题看哪里
  data/panel.log          运行日志
  python tools/selftest.py 自检

更多说明见 README.md。
"""


def read_version():
    try:
        with open(os.path.join(ROOT, "VERSION"), "r", encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def main(argv=None):
    ap = argparse.ArgumentParser(description="打一个可移植的 zip 包")
    ap.add_argument("--out", default="", help="输出目录，默认 dist/")
    args = ap.parse_args(argv)

    ver = read_version()
    out_dir = args.out or os.path.join(ROOT, "dist")
    os.makedirs(out_dir, exist_ok=True)
    name = "token-panel-%s.zip" % ver
    out = os.path.join(out_dir, name)

    count = 0
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, ROOT).replace("\\", "/")
                if rel in SKIP_FILES:
                    continue
                if os.path.splitext(fn)[1].lower() in SKIP_EXT:
                    continue
                if rel == name:
                    continue
                z.write(p, "token-panel/" + rel)
                count += 1
                total += os.path.getsize(p)

        z.writestr("token-panel/首次运行.txt", FIRST_RUN.format(ver=ver))
        if "VERSION" not in {os.path.basename(f) for f in os.listdir(ROOT)}:
            z.writestr("token-panel/VERSION", ver + "\n")

    size = os.path.getsize(out) / 1024 / 1024
    print("版本      : %s" % ver)
    print("文件数    : %d（原始 %.1f MB）" % (count, total / 1024 / 1024))
    print("压缩包    : %s" % out)
    print("压缩后    : %.2f MB" % size)
    print("打包时间  : %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    print()
    print("已排除 data/（索引与日志属于这台机器）、__pycache__、.pyc")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
