# -*- coding: utf-8 -*-
"""性能基线：冷启建库耗时 + 稳态增量轮询耗时。

跑法：python tools/bench.py
用独立的临时索引库，不动正式 data/index.db。
"""

import os
import shutil
import statistics
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import config  # noqa: E402
import indexer  # noqa: E402
import store as store_mod  # noqa: E402


def main():
    config.setup_console()
    if not os.path.isdir(config.PROJECTS_DIR):
        print("数据目录不存在：%s" % config.PROJECTS_DIR)
        return 1

    tmp = tempfile.mkdtemp(prefix="tp-bench-")
    db = os.path.join(tmp, "bench.db")
    store = store_mod.Store(db)
    store.init_schema()
    ix = indexer.Indexer(store)

    print("数据目录：%s" % config.PROJECTS_DIR)
    print("冷启建库 …")
    t0 = time.perf_counter()
    ix.scan_once()
    cold = time.perf_counter() - t0
    events = store.scalar("SELECT COUNT(*) FROM events") or 0
    files = store.scalar("SELECT COUNT(*) FROM files") or 0
    print("  冷启 %.2fs / %d 文件 / %d 条记录" % (cold, files, events))

    print("稳态轮询 20 次 …")
    samples = []
    for _ in range(20):
        t = time.perf_counter()
        ix.scan_once(budget_ms=config.SCAN_BUDGET_MS)
        samples.append((time.perf_counter() - t) * 1000)
        time.sleep(0.05)

    samples.sort()
    p50 = statistics.median(samples)
    p95 = samples[int(len(samples) * 0.95) - 1]
    print("  p50 %.1fms / p95 %.1fms / max %.1fms"
          % (p50, p95, max(samples)))

    after = store.scalar("SELECT COUNT(*) FROM events") or 0
    delta = after - events
    print("  重复扫描后记录数 %d（冷启时 %d，运行中新增 %d 条，属正常）"
          % (after, events, delta))

    size = os.path.getsize(db) / 1048576
    print("索引库体积：%.2f MB" % size)

    # 只允许因运行中会话追加而增加，绝不允许翻倍（那才是重复计数）
    ok = delta < 100 and size < 20
    print("\n结论：%s" % ("达标（无重复计数、体积正常）" if ok else "需要检查"))

    store.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
