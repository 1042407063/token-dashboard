# -*- coding: utf-8 -*-
"""边界自测：增量索引最容易出错的地方，一条一条验。

跑法：python tools/selftest.py
全部通过返回 0，有失败返回 1。
"""

import datetime
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import config  # noqa: E402

TMP = tempfile.mkdtemp(prefix="tp-selftest-")
config.PROJECTS_DIR = os.path.join(TMP, "projects")
config.INDEX_DB = os.path.join(TMP, "index.db")
os.makedirs(config.PROJECTS_DIR, exist_ok=True)

import indexer  # noqa: E402
import aggregate  # noqa: E402
import store as store_mod  # noqa: E402

PASS, FAIL = [], []
SID = "11111111-2222-3333-4444-555555555555"


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [OK]   %s %s" % (name, detail))
    else:
        FAIL.append(name)
        print("  [FAIL] %s %s" % (name, detail))


_n = [0]


def record(total=100, conv="c1", model="m1", uid=None, ts=None, cached=5):
    _n[0] += 1
    return {
        "id": uid or "uid-%d" % _n[0],
        "timestamp": ts or int(time.time() * 1000),
        "sessionId": SID,
        "cwd": "D:\\中文目录\\演示项目",
        "type": "assistant",
        "providerData": {
            "model": model,
            "conversationRequestId": conv,
            "usage": {
                "requests": 1,
                "inputTokens": total - 10,
                "outputTokens": 10,
                "totalTokens": total,
                "inputTokensDetails": [{"cached_tokens": cached}],
                "outputTokensDetails": [{"reasoning_tokens": 0}],
            },
        },
    }


def line(**kw):
    return json.dumps(record(**kw), ensure_ascii=False) + "\n"


class Env:
    _seq = [0]

    def __init__(self):
        # 每个用例独立的数据根，避免用例之间互相被扫描到
        self.root = tempfile.mkdtemp(dir=TMP)
        config.PROJECTS_DIR = self.root
        self.dir = tempfile.mkdtemp(dir=self.root)
        self.path = os.path.join(self.dir, SID + ".jsonl")
        Env._seq[0] += 1
        self.store = store_mod.Store(os.path.join(TMP, "index-%d.db" % Env._seq[0]))
        self.store.init_schema()
        self.ix = indexer.Indexer(self.store)

    def write(self, text, append=False):
        mode = "ab" if append else "wb"
        with open(self.path, mode) as f:
            f.write(text.encode("utf-8") if isinstance(text, str) else text)

    def scan(self, budget=None):
        self.ix.scan_once(budget_ms=budget)
        return self.store.scalar("SELECT COUNT(*) FROM events") or 0

    def total(self):
        return self.store.scalar("SELECT SUM(total) FROM events") or 0


def case_half_line():
    print("\n[1] 末尾半行未写完")
    e = Env()
    a = line(total=100, uid="a")
    b = line(total=200, uid="b")
    e.write(a + b[:40])                     # b 只写了一半
    n = e.scan()
    check("半行不计数", n == 1, "events=%d" % n)
    check("总量只含完整行", e.total() == 100, "total=%d" % e.total())

    e.write(b[40:], append=True)            # 补完
    n = e.scan()
    check("补完后计数", n == 2, "events=%d" % n)
    check("总量正确", e.total() == 300, "total=%d" % e.total())


def case_truncate():
    print("\n[2] 文件被截断重写")
    e = Env()
    e.write(line(total=100, uid="a") + line(total=200, uid="b"))
    e.scan()
    check("初始 2 条", e.total() == 300, "total=%d" % e.total())

    e.write(line(total=50, uid="c"))        # 重写成更短的
    e.scan()
    check("截断后不残留", e.store.scalar("SELECT COUNT(*) FROM events") == 1,
          "events=%d" % e.store.scalar("SELECT COUNT(*) FROM events"))
    check("截断后总量", e.total() == 50, "total=%d" % e.total())

    e.scan()
    e.scan()
    check("重复扫描不翻倍", e.total() == 50, "total=%d" % e.total())


def case_touch():
    print("\n[3] 只改 mtime 不改内容")
    e = Env()
    e.write(line(total=100, uid="a"))
    e.scan()
    st = os.stat(e.path)
    os.utime(e.path, ns=(st.st_atime_ns, st.st_mtime_ns + 3_000_000_000))
    n = e.scan()
    check("不重复计数", n == 1 and e.total() == 100, "events=%d total=%d" % (n, e.total()))


def case_chinese_and_subagent():
    print("\n[4] 中文路径 + 子代理目录")
    e = Env()
    sub = os.path.join(e.dir, "subagents")
    os.makedirs(sub, exist_ok=True)
    p = os.path.join(sub, "agent-abc.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write(line(total=77, uid="sa"))
    e.scan()
    row = e.store.query("SELECT project, kind, session_id, cwd FROM events LIMIT 1")[0]
    check("kind=subagent", row["kind"] == "subagent", row["kind"])
    check("session 取上级目录", row["session_id"] == SID, row["session_id"])
    check("中文 cwd 未损坏", "中文目录" in row["cwd"], row["cwd"])
    check("项目名未损坏", row["project"] and "\\" not in row["project"], row["project"])


def case_bom_and_badline():
    print("\n[5] BOM 与坏行")
    e = Env()
    e.write(b"\xef\xbb\xbf" + line(total=100, uid="bom").encode("utf-8") + "{坏行\n".encode("utf-8"))
    e.scan()
    check("BOM 行仍能解析", e.total() == 100, "total=%d" % e.total())
    check("坏行被跳过不中断", e.store.scalar("SELECT COUNT(*) FROM events") == 1)


def case_long_line():
    print("\n[6] 超长单行跳过")
    e = Env()
    old = config.CHUNK
    config.CHUNK = 512
    try:
        huge = json.dumps({"id": "huge", "type": "tool_result",
                           "pad": "x" * 5000}, ensure_ascii=False) + "\n"
        e.write(huge + line(total=123, uid="after"))
        e.scan()
        check("跳过超长行后继续解析", e.total() == 123, "total=%d" % e.total())
    finally:
        config.CHUNK = old


def case_no_usage():
    print("\n[7] 无 usage 记录被忽略")
    e = Env()
    e.write(json.dumps({"id": "x", "type": "user", "message": {"content": "hi"}}) + "\n")
    n = e.scan()
    check("不产生事件", n == 0, "events=%d" % n)


def case_fallback_usage():
    print("\n[8] message.usage 兜底口径")
    e = Env()
    rec = {"id": "m", "timestamp": int(time.time() * 1000), "sessionId": SID,
           "message": {"usage": {"input_tokens": 300, "output_tokens": 20,
                                 "cache_read_input_tokens": 250}}}
    e.write(json.dumps(rec) + "\n")
    e.scan()
    row = e.store.query("SELECT input, output, total, cache_read, src FROM events")[0]
    check("input", row["input"] == 300, row["input"])
    check("total 回填", row["total"] == 320, row["total"])
    check("cache", row["cache_read"] == 250, row["cache_read"])
    check("src=message", row["src"] == "message", row["src"])


def case_no_double_count():
    print("\n[9] providerData 与 message 同时存在只计一次")
    e = Env()
    rec = record(total=500, uid="both")
    rec["message"] = {"usage": {"input_tokens": 999, "output_tokens": 999}}
    e.write(json.dumps(rec, ensure_ascii=False) + "\n")
    e.scan()
    check("只计一次", e.store.scalar("SELECT COUNT(*) FROM events") == 1 and e.total() == 500,
          "total=%d" % e.total())


def case_file_removed():
    print("\n[10] 文件消失后历史不倒退")
    e = Env()
    e.write(line(total=100, uid="a"))
    e.scan()
    os.remove(e.path)
    e.scan()
    check("位点被清理", e.store.scalar("SELECT COUNT(*) FROM files") == 0)
    check("历史事件保留", e.total() == 100, "total=%d" % e.total())


def case_seq():
    print("\n[11] 数据版本号 seq")
    e = Env()
    v0 = e.store.seq()

    e.scan()                                   # 空目录扫一遍
    check("空扫描不涨版本", e.store.seq() == v0, "+%d" % (e.store.seq() - v0))

    e.write(line(total=100, uid="a"))
    e.scan()
    v1 = e.store.seq()
    check("新增事件涨版本", v1 > v0, "+%d" % (v1 - v0))

    e.scan()                                   # 尺寸没变，不重读
    check("重复扫描不涨版本", e.store.seq() == v1, "+%d" % (e.store.seq() - v1))

    e.write(line(total=100, uid="a"), append=True)   # 追加一条已存在的 uid
    e.scan()
    check("重复行不涨版本", e.store.seq() == v1, "+%d" % (e.store.seq() - v1))

    e.write(line(total=50, uid="c"))           # 截断重写
    e.scan()
    check("截断重写涨版本", e.store.seq() > v1, "+%d" % (e.store.seq() - v1))


def case_discover_cache():
    print("\n[12] 目录缓存不能漏掉增删")
    e = Env()
    e.write(line(total=100, uid="a"))
    check("首次发现 1 个文件", len(e.ix.discover()) == 1)
    check("首次索引 1 条", e.scan() == 1)

    p2 = os.path.join(e.dir, "22222222-3333-4444-5555-666666666666.jsonl")
    with open(p2, "w", encoding="utf-8") as f:
        f.write(line(total=200, uid="b"))
    check("新增文件被发现", len(e.ix.discover()) == 2)
    check("新增文件被索引", e.scan() == 2, "events=%d" % e.scan())

    os.remove(p2)
    check("删除文件被发现", len(e.ix.discover()) == 1)

    # 目录 mtime 不变（同一文件追加），但内容必须照读
    e.write(line(total=300, uid="c"), append=True)
    check("追加内容仍被索引", e.scan() == 3, "events=%d" % e.scan())


def case_acct_filter():
    print("\n[13] 账号过滤：三档相加 == 全站")
    e = Env()
    today = datetime.date.today().isoformat()
    ts = int(time.time() * 1000)

    def ev(uid, sid, total):
        return {"uid": uid, "path": "p", "ts": ts, "day": today, "hour": 10,
                "session_id": sid, "project": "proj", "cwd": "", "model": "m",
                "agent": "", "kind": "main", "conv_req": "", "input": total - 10,
                "output": 10, "cache_read": 0, "reasoning": 0, "total": total,
                "requests": 1, "src": "providerData"}

    e.store.insert_events([
        ev("u1", "s1", 100),      # 账号 A
        ev("u2", "s2", 200),      # 账号 B
        ev("u3", "s3", 300),      # accounts 里根本没有这个会话 -> 未归属
        ev("u4", None, 400),      # session_id 为 NULL -> 也必须算未归属（NOT IN 的经典坑）
    ])
    e.store.upsert_accounts([
        {"session_id": "s1", "user_id": "A", "title": "", "cwd": "", "last_activity_at": 1},
        {"session_id": "s2", "user_id": "B", "title": "", "cwd": "", "last_activity_at": 1},
    ], "d1")

    agg = aggregate.Aggregator(e.store)
    allv = agg.today()["tokens"]
    a = agg.today("A")["tokens"]
    b = agg.today("B")["tokens"]
    none = agg.today(aggregate.ACCT_NONE)["tokens"]
    check("全站 1000", allv == 1000, "all=%d" % allv)
    check("账号 A 只含自己", a == 100, "a=%d" % a)
    check("账号 B 只含自己", b == 200, "b=%d" % b)
    check("未归属含未知会话与 NULL 会话", none == 700, "none=%d" % none)
    check("三档守恒", a + b + none == allv, "%d+%d+%d=%d" % (a, b, none, a + b + none))
    check("缓存不串号", agg.today("A")["tokens"] == 100 and agg.today("B")["tokens"] == 200)


def case_schema_upgrade():
    print("\n[14] schema 1 -> 2 增量升级")
    path = os.path.join(TMP, "upgrade.db")
    s = store_mod.Store(path)
    s.init_schema()
    s.set_meta("schema_version", "1")          # 模拟改造前的老库
    s.init_schema()
    check("版本推进到当前版本",
          s.get_meta("schema_version") == str(config.SCHEMA_VERSION),
          "ver=%s" % s.get_meta("schema_version"))
    check("accounts 表已建",
          s.scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                   "AND name='accounts'") == 1)
    check("account_switches 表已建",
          s.scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                   "AND name='account_switches'") == 1)
    s.init_schema()
    check("重复执行幂等", s.get_meta("schema_version") == str(config.SCHEMA_VERSION))
    s.close()


def case_accounts_digest():
    print("\n[15] 账号映射：只有归属变化才 bump")
    e = Env()
    row = {"session_id": "s1", "user_id": "A", "title": "t", "cwd": "",
           "last_activity_at": 1}
    v0 = e.store.seq()
    r1 = e.store.upsert_accounts([row], "d-a")
    v1 = e.store.seq()
    check("首次写入 bump", bool(r1) and v1 != v0, "seq %d->%d" % (v0, v1))

    r2 = e.store.upsert_accounts([dict(row, title="改了标题")], "d-a")
    v2 = e.store.seq()
    check("只改标题不 bump", (not r2) and v2 == v1, "seq %d->%d" % (v1, v2))

    r3 = e.store.upsert_accounts([dict(row, user_id="B")], "d-b")
    check("改绑账号要 bump", bool(r3) and e.store.seq() != v2)

    # 源库里消失的会话必须被裁掉，否则它的历史用量会永远挂在某个账号名下
    time.sleep(0.003)
    e.store.upsert_accounts([{"session_id": "s9", "user_id": "C", "title": "",
                              "cwd": "", "last_activity_at": 1}], "d-c")
    n = e.store.scalar("SELECT COUNT(*) FROM accounts")
    check("陈旧映射被裁剪", n == 1, "rows=%s" % n)


def case_account_labels():
    print("\n[16] 账号显示名：四个账号必须可区分")
    uids = ["aaaa1111-1111-4111-8111-111111111111",
            "bbbb2222-2222-4222-8222-222222222222",
            "cccc3333-3333-4333-8333-333333333333",
            "dddd4444-4444-4444-8444-444444444444"]
    e = Env()
    e.store.upsert_accounts(
        [{"session_id": "s%d" % i, "user_id": u, "title": "t", "cwd": "",
          "last_activity_at": 1} for i, u in enumerate(uids)], "d-labels")
    agg = aggregate.Aggregator(e.store)

    names = [x["short"] for x in agg.accounts()["items"]]
    check("四个账号都有名字", len(names) == 4 and all(names), str(names))
    check("四个名字互不相同", len(set(names)) == 4, "unique=%d" % len(set(names)))
    check("短码来自各自 user_id",
          all(any(u[:8] in n for n in names) for u in uids), str(names))
    check("未归属不等于任何账号名", aggregate._short("") not in names)
    check("饼图与按钮用同一套名字",
          all(agg._label(u) in names for u in uids))

    saved = dict(config.ACCOUNT_ALIASES)
    try:
        # 手滑给两个账号配了同一个昵称，也要自动补短码
        config.ACCOUNT_ALIASES = {"__none__": "未归属",
                                  uids[0]: "同名", uids[1]: "同名"}
        agg._label_cache = None
        dup = [x["short"] for x in agg.accounts()["items"]]
        check("手动重名仍可区分", len(set(dup)) == 4, str(dup))

        # 按前 8 位自定义昵称
        config.ACCOUNT_ALIASES = {"__none__": "未归属", uids[0][:8]: "工作号"}
        agg._label_cache = None
        got = {x["user_id"]: x["short"] for x in agg.accounts()["items"]}
        check("前 8 位别名生效", got.get(uids[0]) == "工作号", str(got.get(uids[0])))

        # 旧版那个 universal 兜底名即使被恢复，也不能再让四个账号同名
        config.ACCOUNT_ALIASES = {"default": "通用兜底名"}
        agg._label_cache = None
        old = [x["short"] for x in agg.accounts()["items"]]
        check("旧 default 配置不再造成同名", len(set(old)) == 4, str(old))
    finally:
        config.ACCOUNT_ALIASES = saved
        agg._label_cache = None


def case_workspace_labels():
    print("\n[17] 项目空间改名：标签跟着变且缓存及时失效")
    path = os.path.join(TMP, "workspace-display-names.json")
    saved = getattr(config, "WORKSPACE_NAMES_PATH", None)
    config.WORKSPACE_NAMES_PATH = path
    tick = [time.time_ns()]

    def write(name):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "workspaces": {
                "d:/projects/demo": {
                    "path": "D:/Projects/demo",
                    "displayName": name}}}, f, ensure_ascii=False)
        tick[0] += 1000000000
        os.utime(path, ns=(tick[0], tick[0]))

    cwd = r"d:\Projects\demo"
    try:
        write("旧名字")
        names0, v0 = aggregate.workspace_names()
        check("读到显示名", aggregate._project_label("p", cwd, names0) == "旧名字",
              str(aggregate._project_label("p", cwd, names0)))
        check("子目录也能命中工作区",
              aggregate._project_label("p", cwd + r"\src", names0) == "旧名字")
        check("未配置的工作区回退目录名",
              aggregate._project_label("p", r"D:\Other\Sample", names0) == "Sample")

        write("新名字")
        names1, v1 = aggregate.workspace_names()
        check("改名后版本推进", v1 != v0, "%s -> %s" % (v0, v1))
        check("改名后标签立即更新",
              aggregate._project_label("p", cwd, names1) == "新名字")

        with open(path, "w", encoding="utf-8") as f:
            f.write('{"version":1,"workspaces":')
        tick[0] += 1000000000
        os.utime(path, ns=(tick[0], tick[0]))
        names2, v2 = aggregate.workspace_names()
        check("写入中途保留上一份有效名字",
              aggregate._project_label("p", cwd, names2) == "新名字" and v2 == v1)

        e = Env()
        agg = aggregate.Aggregator(e.store)
        write("再改一次")
        check("缓存版本随改名变化", agg._ver()[-1] != v1)
    finally:
        if saved is None:
            del config.WORKSPACE_NAMES_PATH
        else:
            config.WORKSPACE_NAMES_PATH = saved
        aggregate._WS_CACHE.update({"stamp": None, "map": {}})


def main():
    config.setup_console()
    print("临时目录：%s" % TMP)
    try:
        case_half_line()
        case_truncate()
        case_touch()
        case_chinese_and_subagent()
        case_bom_and_badline()
        case_long_line()
        case_no_usage()
        case_fallback_usage()
        case_no_double_count()
        case_file_removed()
        case_seq()
        case_discover_cache()
        case_acct_filter()
        case_schema_upgrade()
        case_accounts_digest()
        case_account_labels()
        case_workspace_labels()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n" + "=" * 46)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
