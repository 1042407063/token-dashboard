# -*- coding: utf-8 -*-
"""打包成可直接拷走的 zip。

包里只放运行所需文件：源码、web/、tools/ 与文档。
data/、dist/、开发状态一律排除；包内文件名全部为 ASCII，
避免不同解压工具对中文名的编码差异。

用法：
    python tools/pack.py
    python tools/pack.py --out D:/somewhere
"""

import argparse
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFIX = "token-panel/"

SKIP_DIRS = {"data", "dist", "__pycache__", ".git", ".idea", ".vscode",
             ".pytest_cache", ".workbuddy"}
SKIP_EXT = {".pyc", ".pyo", ".tmp", ".log", ".db", ".db-shm", ".db-wal", ".pid"}


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
    out = os.path.join(out_dir, "token-panel-%s.zip" % ver)

    count, total = 0, 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in sorted(filenames):
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, ROOT).replace("\\", "/")
                if os.path.splitext(fn)[1].lower() in SKIP_EXT:
                    continue
                if any(ord(c) > 127 for c in rel):
                    raise SystemExit("包内文件名必须为 ASCII：%s" % rel)
                z.write(p, PREFIX + rel)
                count += 1
                total += os.path.getsize(p)
        if not any(i.filename == PREFIX + "VERSION" for i in z.infolist()):
            z.writestr(PREFIX + "VERSION", ver + "\n")
            count += 1

    print("版本    : %s" % ver)
    print("文件数  : %d（原始 %.2f MB）" % (count, total / 1048576))
    print("压缩包  : %s" % out)
    print("大小    : %.2f MB" % (os.path.getsize(out) / 1048576))
    print("检查    : 包内文件名全部 ASCII，已排除 data/ 与开发状态")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
