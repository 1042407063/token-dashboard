# -*- coding: utf-8 -*-
"""聚合层：把索引库变成前端要的 JSON。

所有切日切时都在建索引时算好了（events.day / events.hour），
这里只做 GROUP BY，不再碰时区换算 —— 否则跨日会错 8 小时。
"""

import datetime
import json
import os
import re
import threading
import time

import config

# 只认真正的会话 uuid；"interactive-31220" 这类 CLI 宿主伪会话必须排除
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                     re.I)

# 轮数口径：一次模型调用 = 一个请求 id；没有 id 的记录退回用 uid 兜底
TURNS = "COUNT(DISTINCT COALESCE(conv_req, uid))"

# 账号维度的特殊取值：前端"未归属"档。真实 user_id 是 UUID，不可能撞上。
ACCT_NONE = "__none__"
_ACCT_ATTR = "user_id IS NOT NULL AND user_id<>''"


def _acct_sql(acct, col="session_id"):
    """返回 (布尔表达式, 参数)，可以无条件接在 WHERE / AND 后面。

    三档语义：
      None      -> "1=1"，全部（SQLite 会折叠掉，结果与改造前一致）
      __none__  -> 未归属：不在"有 user_id 的 accounts 行"里
      具体 uid  -> 该账号

    必须 COALESCE：session_id 允许为 NULL，裸写 `NOT IN` 会让该行表达式变 NULL
    而被静默丢弃 —— "未归属"少算、"全部"却正确，两边永远对不上且不报错。
    """
    if acct is None:
        return "1=1", ()
    if acct == ACCT_NONE:
        return ("COALESCE(%s,'') NOT IN "
                "(SELECT session_id FROM accounts WHERE %s)" % (col, _ACCT_ATTR), ())
    return ("%s IN (SELECT session_id FROM accounts WHERE user_id=?)" % col, (acct,))


# WorkBuddy 把「项目空间显示名」单独存在 workspace-display-names.json：
# 界面里重命名空间时只改这个文件，磁盘目录名保持不变。
# 所以项目标签不能只用 os.path.basename(cwd)，否则永远停在初次创建时的名字。
_WS_CACHE = {"stamp": None, "map": {}}
_WS_LOCK = threading.Lock()          # 多个 HTTP 线程会同时经由 _ver() 读这份缓存


def _norm_ws_path(p):
    """路径统一成小写 + 正斜杠，盘符/分隔符大小写都不影响匹配。"""
    return (p or "").replace("\\", "/").rstrip("/").lower()


def workspace_stamp():
    """显示名文件的 (mtime_ns, size)，用作缓存与 SSE 推送的版本信号。"""
    try:
        st = os.stat(getattr(config, "WORKSPACE_NAMES_PATH", ""))
        return (st.st_mtime_ns, st.st_size)
    except (OSError, TypeError):
        return (0, 0)


def workspace_names():
    """返回 ({规范化工作区路径: 显示名}, 版本)。

    文件由 WorkBuddy 写入，写到一半时可能不是合法 JSON；这时保留上一份
    有效映射且不推进版本，下一拍再读，避免界面闪回旧名字。
    """
    stamp = workspace_stamp()
    with _WS_LOCK:
        cache = _WS_CACHE
        if cache["stamp"] == stamp:
            return cache["map"], stamp
        try:
            with open(config.WORKSPACE_NAMES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            out = {}
            for key, val in (data.get("workspaces") or {}).items():
                if not isinstance(val, dict):
                    continue
                name = str(val.get("displayName") or "").strip()
                p = _norm_ws_path(val.get("path") or key)
                if name and p:
                    out[p] = name
            cache["stamp"], cache["map"] = stamp, out
        except (OSError, ValueError, TypeError, AttributeError):
            pass                   # 半截 JSON / 暂时不可读：沿用旧值，下拍重试
        return cache["map"], cache["stamp"] or stamp


def _display_name(cwd, names):
    """先精确匹配 cwd，再逐级向上找工作区根（会话可能开在项目子目录里）。"""
    p = _norm_ws_path(cwd)
    while p:
        if p in names:
            return names[p]
        parent = p.rsplit("/", 1)[0] if "/" in p else ""
        if not parent or parent == p:
            break
        p = parent
    return None


def _project_label(project, cwd, names):
    """项目标签优先级：WorkBuddy 显示名 > 目录名 > 索引里的编码名。"""
    return (_display_name(cwd, names)
            or os.path.basename((cwd or "").rstrip("\\/"))
            or project or "未知")


def _none_label():
    """「未归属」会话的显示名（accounts 表里没有 user_id 的那些）。"""
    alias = getattr(config, "ACCOUNT_ALIASES", {}) or {}
    return alias.get("__none__") or "未归属"


def _short(uid):
    """单个账号的基础显示名：显式别名 > 带 UUID 短码的模板名。

    这里刻意不设「所有账号共用的兜底名」—— 那会让四个不同 user_id
    在界面上显示成同一个名字。别名只是显示层，不参与任何 SQL 归属判断。
    """
    if not uid:
        return _none_label()
    alias = getattr(config, "ACCOUNT_ALIASES", {}) or {}
    name = alias.get(uid) or alias.get(uid[:8])
    if name:
        return str(name)
    tpl = getattr(config, "ACCOUNT_NAME_TEMPLATE", "账号 {short}") or "账号 {short}"
    try:
        return tpl.format(short=uid[:8], uid=uid)
    except (KeyError, IndexError, ValueError):
        return "账号 " + uid[:8]


# 前端可选的统计区间：(相对今天回退几天, 跨度几天)
RANGES = {
    "today": (0, 1),
    "yesterday": (1, 1),
    "before": (2, 1),      # 前天
    "7d": (0, 7),
    "30d": (0, 30),
}


def _r(v, n=1):
    return round(v, n) if isinstance(v, (int, float)) else 0


class Aggregator:
    def __init__(self, store):
        self.store = store
        self._memo = {}
        self._pending = {}          # key -> Event，单飞用：同一 key 只让一个线程算 SQL
        self._lock = threading.Lock()
        self._cache_hit = 0
        self._cache_miss = 0
        self._cur = {"sid": None, "last_ts": 0, "resolved_at": 0, "source": None,
                     "title": None, "project": None, "cwd": None}
        self._label_cache = None       # (monotonic, {uid: 唯一显示名})

    # ---------- 账号显示名 ----------
    def _labels(self):
        """uid -> 唯一显示名，按库里真实存在的账号集合做重名去重。

        即使手滑给两个账号配了同一个昵称，也会自动补上 UUID 短码，
        保证前端按钮、饼图、切号时间线里永远能区分。accounts() 每
        1.5 秒被拉一次，缓存 2 秒足够，避免每拍多一次 SQL。
        """
        now = time.monotonic()
        hit = self._label_cache
        if hit and now - hit[0] < 2.0:
            return hit[1]
        rows = self.store.query(
            "SELECT DISTINCT user_id FROM accounts WHERE %s" % _ACCT_ATTR)
        labels = {r["user_id"]: _short(r["user_id"]) for r in rows}
        counts = {}
        for name in labels.values():
            counts[name] = counts.get(name, 0) + 1
        for uid, name in list(labels.items()):
            if counts[name] > 1 and uid[:8] not in name:
                labels[uid] = "%s · %s" % (name, uid[:8])
        self._label_cache = (now, labels)
        return labels

    def _label(self, uid):
        """统一出口：所有对外 JSON 里的账号名都走这里。"""
        if not uid:
            return _none_label()
        return self._labels().get(uid) or _short(uid)

    # ---------- 积分口径 ----------
    # workbuddy.db 的 session_usage 只覆盖"它自己记过的请求"，没记过的请求根本
    # 没有积分数。所以对外必须同时给出"多少轮真的拿到了积分记录"，
    # 让前端能把"没有记录"显示成 —，而不是伪装成 0.00 分。
    def _credit_span(self, s, e, w, ap):
        """区间内：积分合计 + 拿到积分记录的请求数（分母是区间总轮数）。"""
        row = self.store.query(
            "SELECT COALESCE(SUM(value),0) v, COUNT(*) n, COUNT(DISTINCT day) d "
            "FROM credits WHERE day BETWEEN ? AND ? AND %s" % w, (s, e) + ap)[0]
        reqs = self.store.scalar(
            "SELECT COUNT(DISTINCT conv_req) FROM events "
            "WHERE day BETWEEN ? AND ? AND COALESCE(conv_req,'')<>'' "
            "AND conv_req IN (SELECT key FROM credits) AND %s" % w,
            (s, e) + ap) or 0
        return {"value": _r(row.get("v") or 0, 2), "records": row.get("n") or 0,
                "days": row.get("d") or 0, "reqs": int(reqs)}

    def _sess_meta(self, sid):
        """会话标题 / cwd：sessions_meta 优先，缺了退回 accounts。

        accounts 来自 sessions 表且不带 deleted_at 过滤，软删会话的标题在那里
        仍然有效；只认 sessions_meta 会让这些会话在界面上退化成八位十六进制。
        """
        row = self.store.query(
            "SELECT COALESCE(NULLIF(m.title,''), NULLIF(a.title,'')) title, "
            "COALESCE(NULLIF(m.cwd,''), NULLIF(a.cwd,'')) cwd "
            "FROM (SELECT ? AS sid) s "
            "LEFT JOIN sessions_meta m ON m.session_id=s.sid "
            "LEFT JOIN accounts a ON a.session_id=s.sid", (sid,))
        r = row[0] if row else {}
        return (r.get("title") or "").strip(), (r.get("cwd") or "").strip()

    def _sess_model(self, sid, day_from=None, day_to=None):
        """会话用过的模型里 token 最多的那个（一个会话可能中途换模型）。"""
        sql = ("SELECT COALESCE(NULLIF(model,''),'') m FROM events WHERE session_id=? "
               "AND COALESCE(model,'') NOT IN ('','未知')")
        params = [sid]
        if day_from:
            sql += " AND day BETWEEN ? AND ?"
            params += [day_from, day_to]
        sql += " GROUP BY m ORDER BY SUM(total) DESC LIMIT 1"
        return self.store.scalar(sql, tuple(params)) or ""

    # ---------- 记忆化 ----------
    def _ver(self):
        """数据版本 + 日期序号 + 项目显示名版本。

        日期进版本是因为"今日"这个语义会随零点改变，而没有新事件时 seq 不会涨。
        显示名文件不写 index.db，改名时 seq 也不会涨 —— 少了这一项，
        缓存会一直用旧的项目标签，直到 CACHE_MAX_AGE 兜底重算。
        """
        return (self.store.seq(), self._today().toordinal(),
                workspace_names()[1])

    def workspace_version(self):
        """给 SSE 广播用：显示名文件变了就 bump，让前端及时重拉。"""
        return workspace_names()[1]

    def _cached(self, key, fn, ttl=None):
        """版本驱动缓存：数据没变就直接返回，变了才重算。

        ttl 现在不是"刷新周期"而是"兜底上限"：万一哪天漏了一个 bump，
        最多滞后这么久就会自愈，不会永久停更。所以这里给的值比原来大一个量级。

        单飞：同一个 key 并发请求时只有一个人真正跑 SQL，其余等待（3s 上限）。
        没有这一层，4 个标签页同时刷新会把同一条慢查询跑 4 遍。
        """
        max_age = config.CACHE_MAX_AGE if ttl is None else ttl
        ver = self._ver()

        with self._lock:
            hit = self._memo.get(key)
            if hit and hit[0] == ver and time.monotonic() - hit[1] < max_age:
                self._cache_hit += 1
                return hit[2]
            ev = self._pending.get(key)
            owner = ev is None
            if owner:
                ev = threading.Event()
                self._pending[key] = ev

        mine = ev if owner else None
        if mine is None:
            ev.wait(3.0)                       # 等主算者；超时就自己算，绝不卡死
            with self._lock:
                hit = self._memo.get(key)
                if hit and hit[0] == ver and time.monotonic() - hit[1] < max_age:
                    self._cache_hit += 1
                    return hit[2]

        try:
            val = fn()
        except Exception:
            self._release(key, mine)            # 抛异常也必须唤醒等待者，否则该 key 永久挂起
            raise

        with self._lock:
            self._cache_miss += 1
            cur = self._memo.get(key)
            if cur is None or cur[0] <= ver:    # 慢算者不许覆盖更新的版本
                self._memo[key] = (ver, time.monotonic(), val)
            if mine is not None:
                self._pending.pop(key, None)
                mine.set()
        return val

    def _release(self, key, ev):
        if ev is None:
            return
        with self._lock:
            if self._pending.get(key) is ev:
                self._pending.pop(key, None)
            ev.set()

    def cache_stats(self):
        with self._lock:
            return {"hit": self._cache_hit, "miss": self._cache_miss,
                    "keys": len(self._memo), "pending": len(self._pending)}

    def invalidate(self):
        with self._lock:
            self._memo.clear()

    # ---------- 账号参数 ----------
    def normalize_acct(self, raw):
        """白名单化：只允许 None / __none__ / 库里真实存在的 user_id。

        不这么做有两个后果：acct 直接来自 query string，会拼进 `_memo` 的 key，
        而 `_memo` 从不淘汰 —— 构造随机参数就能把内存打满；同时也让非法值
        悄悄退化成"全部"，比报错更符合看板的使用预期。
        """
        a = (raw or "").strip()
        if not a or a == "all":
            return None
        if a == ACCT_NONE:
            return ACCT_NONE
        if len(a) > 64:                       # user_id 是 UUID，超长一定是噪声
            return None
        return a if self.store.account_of_user(a) else None

    # ---------- 基础 ----------
    def _today(self):
        return datetime.date.today()

    def all_time(self, acct=None):
        def run():
            w, ap = _acct_sql(acct)
            row = self.store.query(
                "SELECT SUM(total) t, SUM(input) i, SUM(output) o, SUM(cache_read) c, "
                "SUM(requests) rq, %s turns, COUNT(DISTINCT session_id) s, "
                "COUNT(DISTINCT day) d, MIN(day) first_day, MAX(day) last_day "
                "FROM events WHERE %s" % (TURNS, w), ap)
            r = row[0] if row else {}
            return {
                "tokens": r.get("t") or 0,
                "input": r.get("i") or 0,
                "output": r.get("o") or 0,
                "cache_read": r.get("c") or 0,
                "requests": r.get("rq") or 0,
                "turns": r.get("turns") or 0,
                "sessions": r.get("s") or 0,
                "days": r.get("d") or 0,
                "first_day": r.get("first_day"),
                "last_day": r.get("last_day"),
                "cache_hit_rate": _r((r.get("c") or 0) / (r.get("i") or 1) * 100),
            }
        return self._cached(("all_time", acct), run)

    def today(self, acct=None):
        # 整点进 key：yesterday_same_time 依赖"现在几点"，跨整点时不能沿用上一小时的结果
        now_hour = datetime.datetime.now().hour

        def run():
            d = self._today().isoformat()
            prev = (self._today() - datetime.timedelta(days=1)).isoformat()
            w, ap = _acct_sql(acct)

            row = self.store.query(
                "SELECT SUM(total) t, SUM(input) i, SUM(output) o, SUM(cache_read) c, "
                "SUM(requests) rq, %s turns, COUNT(*) n FROM events "
                "WHERE day=? AND %s" % (TURNS, w), (d,) + ap)[0]
            yrow = self.store.query(
                "SELECT SUM(total) t FROM events WHERE day=? AND hour<=? AND %s" % w,
                (prev, now_hour) + ap)[0]

            hours = [0] * 24
            turns_h = [0] * 24
            for r in self.store.query(
                    "SELECT hour, SUM(total) t, %s tn FROM events "
                    "WHERE day=? AND %s GROUP BY hour" % (TURNS, w), (d,) + ap):
                h = r["hour"]
                if isinstance(h, int) and 0 <= h < 24:
                    hours[h] = r["t"] or 0
                    turns_h[h] = r["tn"] or 0

            yesterday = [0] * 24
            for r in self.store.query(
                    "SELECT hour, SUM(total) t FROM events "
                    "WHERE day=? AND %s GROUP BY hour" % w, (prev,) + ap):
                h = r["hour"]
                if isinstance(h, int) and 0 <= h < 24:
                    yesterday[h] = r["t"] or 0

            yt = yrow.get("t") or 0
            cur = row.get("t") or 0
            peak_h = max(range(24), key=lambda i: hours[i]) if any(hours) else None
            return {
                "day": d,
                "tokens": cur,
                "input": row.get("i") or 0,
                "output": row.get("o") or 0,
                "cache_read": row.get("c") or 0,
                "requests": row.get("rq") or 0,
                "turns": row.get("turns") or 0,
                "records": row.get("n") or 0,
                "cache_hit_rate": _r((row.get("c") or 0) / (row.get("i") or 1) * 100),
                "yesterday_same_time": yt,
                "delta_vs_yesterday": _r((cur - yt) / yt * 100) if yt else None,
                "hours": hours,
                "turns_hours": turns_h,
                "yesterday_hours": yesterday,
                "now_hour": now_hour,
                "peak_hour": peak_h,
                "peak_tokens": hours[peak_h] if peak_h is not None else 0,
            }
        return self._cached(("today", now_hour, acct), run)

    def series(self, days=30, acct=None):
        def run():
            end = self._today()
            start = end - datetime.timedelta(days=days - 1)
            w, ap = _acct_sql(acct)
            rows = {r["day"]: r for r in self.store.query(
                "SELECT day, SUM(total) t, SUM(input) i, SUM(output) o, SUM(cache_read) c, "
                "%s turns FROM events WHERE day>=? AND %s GROUP BY day ORDER BY day"
                % (TURNS, w), (start.isoformat(),) + ap)}
            # 每天的积分与"拿到积分记录的请求数"，趋势图 tooltip 要成对显示
            cr = {x["day"]: x["v"] or 0 for x in self.store.query(
                "SELECT day, SUM(value) v FROM credits WHERE day>=? AND %s GROUP BY day" % w,
                (start.isoformat(),) + ap)}
            crq = {x["day"]: x["n"] or 0 for x in self.store.query(
                "SELECT day, COUNT(DISTINCT conv_req) n FROM events WHERE day>=? "
                "AND COALESCE(conv_req,'')<>'' AND conv_req IN (SELECT key FROM credits) "
                "AND %s GROUP BY day" % w, (start.isoformat(),) + ap)}
            out = []
            for k in range(days):
                d = (start + datetime.timedelta(days=k)).isoformat()
                r = rows.get(d) or {}
                out.append({"date": d, "tokens": r.get("t") or 0,
                            "input": r.get("i") or 0, "output": r.get("o") or 0,
                            "cache_read": r.get("c") or 0, "turns": r.get("turns") or 0,
                            "credit": _r(cr.get(d, 0), 2),
                            "credit_reqs": crq.get(d, 0)})
            return out
        return self._cached(("series", days, acct), run)

    def avg_daily(self, days=30, acct=None):
        s = self.series(days, acct)
        live = [x["tokens"] for x in s if x["tokens"] > 0]
        return int(sum(live) / len(live)) if live else 0

    def window(self, days=30, acct=None):
        """近 N 天的真实累计与覆盖天数。

        注意：累计必须来自实际求和，不能用"活跃日日均 × 天数"——
        那样会把只有 16 天数据的窗口按 30 天放大，算出比历史总和还大的数。
        """
        s = self.series(days, acct)
        total = sum(x["tokens"] for x in s)
        active = sum(1 for x in s if x["tokens"] > 0)
        return {
            "days": days,
            "tokens": total,
            "active_days": active,
            "avg_active": int(total / active) if active else 0,   # 有使用的日子口径
            "avg_calendar": int(total / days) if days else 0,     # 自然日口径
        }

    def period(self, key="today", acct=None):
        """按选定区间聚合：今天 / 昨天 / 前天 / 近 7 天 / 近 30 天。

        与 today() 的区别：这里把 token 与**积分**一起算，并按模型、项目、账号、会话拆开，
        前端切换区间只需一次请求。
        """
        offset, span = RANGES.get(key, RANGES["today"])
        end = self._today() - datetime.timedelta(days=offset)
        start = end - datetime.timedelta(days=span - 1)
        s, e = start.isoformat(), end.isoformat()

        def run():
            w, ap = _acct_sql(acct)
            r = self.store.query(
                "SELECT SUM(total) t, SUM(input) i, SUM(output) o, SUM(cache_read) c, "
                "SUM(requests) rq, %s turns, COUNT(*) n FROM events "
                "WHERE day BETWEEN ? AND ? AND %s" % (TURNS, w), (s, e) + ap)[0]

            hours = [0] * 24
            for x in self.store.query(
                    "SELECT hour, SUM(total) t FROM events "
                    "WHERE day BETWEEN ? AND ? AND %s GROUP BY hour" % w, (s, e) + ap):
                h = x["hour"]
                if isinstance(h, int) and 0 <= h < 24:
                    hours[h] = x["t"] or 0

            cspan = self._credit_span(s, e, w, ap)
            credit = cspan["value"]

            # 按小时积分：与 hours 同一口径（多日区间就是各小时合计）
            credit_hours = [0] * 24
            for x in self.store.query(
                    "SELECT hour, SUM(value) v FROM credits "
                    "WHERE day BETWEEN ? AND ? AND %s GROUP BY hour" % w, (s, e) + ap):
                h = x["hour"]
                if isinstance(h, int) and 0 <= h < 24:
                    credit_hours[h] = x["v"] or 0

            # 单日区间时带上"前一天"同样的小时分布，用来画对照；多日区间不需要
            prev_hours = [0] * 24
            if span == 1:
                prev = (start - datetime.timedelta(days=1)).isoformat()
                for x in self.store.query(
                        "SELECT hour, SUM(total) t FROM events "
                        "WHERE day=? AND %s GROUP BY hour" % w, (prev,) + ap):
                    h = x["hour"]
                    if isinstance(h, int) and 0 <= h < 24:
                        prev_hours[h] = x["t"] or 0

            def split(col, need_label=False):
                ev = self.store.query(
                    "SELECT COALESCE(NULLIF(%s,''),'未知') k, SUM(total) t, %s turns "
                    "FROM events WHERE day BETWEEN ? AND ? AND %s "
                    "GROUP BY k ORDER BY t DESC" % (col, TURNS, w), (s, e) + ap)
                cr = {x["k"]: x["v"] or 0 for x in self.store.query(
                    "SELECT COALESCE(NULLIF(%s,''),'未知') k, SUM(value) v FROM credits "
                    "WHERE day BETWEEN ? AND ? AND %s GROUP BY k" % (col, w),
                    (s, e) + ap)}
                # 每组"真的拿到了积分记录"的请求数：没有就是没有，不能标成 0 分
                crq = {x["k"]: x["n"] or 0 for x in self.store.query(
                    "SELECT COALESCE(NULLIF(%s,''),'未知') k, COUNT(DISTINCT conv_req) n "
                    "FROM events WHERE day BETWEEN ? AND ? AND COALESCE(conv_req,'')<>'' "
                    "AND conv_req IN (SELECT key FROM credits) AND %s GROUP BY k"
                    % (col, w), (s, e) + ap)}
                labels = {}
                if need_label:
                    names, _ = workspace_names()
                    # 项目目录名是编码后的路径；优先用 WorkBuddy 里的空间显示名，
                    # 改名后磁盘目录不变，只靠 basename 会永远停在旧名
                    for x in self.store.query(
                            "SELECT project p, cwd, COUNT(*) n FROM events WHERE cwd<>'' "
                            "GROUP BY p, cwd ORDER BY n DESC"):
                        if x["p"] not in labels:
                            labels[x["p"]] = _project_label(x["p"], x["cwd"], names)
                tot = sum((x["t"] or 0) for x in ev) or 1
                return [{"name": x["k"], "label": labels.get(x["k"]) or x["k"],
                         "tokens": x["t"] or 0, "turns": x["turns"] or 0,
                         "credit": _r(cr.get(x["k"], 0), 2),
                         "credit_reqs": crq.get(x["k"], 0),
                         "share": _r((x["t"] or 0) / tot * 100)} for x in ev]

            # 按账号拆分：events LEFT JOIN accounts，空值归"未归属"。
            # 必须跟着 acct 一起过滤，否则筛了某个账号、饼图却还在展示全部账号，
            # 中心数字会和"区间累计"对不上。
            aw, aap = _acct_sql(acct, "e.session_id")
            acct_rows = self.store.query(
                "SELECT COALESCE(NULLIF(a.user_id,''),'') k, SUM(e.total) t, "
                "COUNT(DISTINCT COALESCE(e.conv_req,e.uid)) turns "
                "FROM events e LEFT JOIN accounts a ON a.session_id=e.session_id "
                "WHERE e.day BETWEEN ? AND ? AND %s GROUP BY k ORDER BY t DESC" % aw,
                (s, e) + aap)
            acct_total = sum((x["t"] or 0) for x in acct_rows) or 1
            # 账号维度的积分：credits 里只有 session_id，必须借 accounts 换成账号
            aw2, aap2 = _acct_sql(acct, "c.session_id")
            acct_cr = {x["k"]: x["v"] or 0 for x in self.store.query(
                "SELECT COALESCE(NULLIF(a.user_id,''),'') k, SUM(c.value) v "
                "FROM credits c LEFT JOIN accounts a ON a.session_id=c.session_id "
                "WHERE c.day BETWEEN ? AND ? AND %s GROUP BY k" % aw2, (s, e) + aap2)}
            acct_crq = {x["k"]: x["n"] or 0 for x in self.store.query(
                "SELECT COALESCE(NULLIF(a.user_id,''),'') k, COUNT(DISTINCT e.conv_req) n "
                "FROM events e LEFT JOIN accounts a ON a.session_id=e.session_id "
                "WHERE e.day BETWEEN ? AND ? AND COALESCE(e.conv_req,'')<>'' "
                "AND e.conv_req IN (SELECT key FROM credits) AND %s GROUP BY k" % aw,
                (s, e) + aap)}
            by_account = [{"name": x["k"] or ACCT_NONE, "label": self._label(x["k"]),
                           "tokens": x["t"] or 0, "turns": x["turns"] or 0,
                           "credit": _r(acct_cr.get(x["k"], 0), 2),
                           "credit_reqs": acct_crq.get(x["k"], 0),
                           "share": _r((x["t"] or 0) / acct_total * 100)}
                          for x in acct_rows]

            sid_credit = {x["session_id"]: x["v"] or 0 for x in self.store.query(
                "SELECT session_id, SUM(value) v FROM credits "
                "WHERE day BETWEEN ? AND ? AND %s GROUP BY session_id" % w, (s, e) + ap)}
            sid_crq = {x["session_id"]: x["n"] or 0 for x in self.store.query(
                "SELECT session_id, COUNT(DISTINCT conv_req) n FROM events "
                "WHERE day BETWEEN ? AND ? AND COALESCE(conv_req,'')<>'' "
                "AND conv_req IN (SELECT key FROM credits) AND %s GROUP BY session_id"
                % w, (s, e) + ap)}
            names, _ = workspace_names()
            sess = []
            for x in self.store.query(
                    "SELECT session_id, SUM(total) t, %s turns, MIN(ts) f, MAX(ts) l "
                    "FROM events WHERE day BETWEEN ? AND ? AND session_id IS NOT NULL "
                    "AND %s GROUP BY session_id ORDER BY t DESC LIMIT 5" % (TURNS, w),
                    (s, e) + ap):
                sid = x["session_id"]
                title, cwd = self._sess_meta(sid)
                # 没有标题的老会话（自动化工作区、被清理过的会话）只能靠项目名兜底；
                # 这里必须取事件里的 cwd，否则会退化成 c-Users-10424-WorkBuddy-xxx 编码名
                prow = self.store.query(
                    "SELECT project, cwd FROM events WHERE session_id=? "
                    "AND (project IS NOT NULL OR COALESCE(cwd,'')<>'') ORDER BY ts LIMIT 1",
                    (sid,))
                pr = prow[0] if prow else {}
                proj = pr.get("project") or ""
                sess.append({
                    "session_id": sid,
                    "title": (title or _project_label(proj, pr.get("cwd") or cwd, names)
                              or "未命名会话"),
                    "project": proj,
                    "cwd": pr.get("cwd") or cwd,
                    "model": self._sess_model(sid, s, e),
                    "tokens": x["t"] or 0, "turns": x["turns"] or 0,
                    "credit": _r(sid_credit.get(sid, 0), 2),
                    "credit_reqs": sid_crq.get(sid, 0),
                    "first_ts": x["f"], "last_ts": x["l"],
                })

            # 每条会话的"历史累计"，卡片第三行要用（区间内 vs 全会话）
            for it in sess:
                sid = it["session_id"]
                it["total_tokens"] = self.store.scalar(
                    "SELECT SUM(total) FROM events WHERE session_id=?", (sid,)) or 0
                it["total_credit"] = _r(self.store.scalar(
                    "SELECT SUM(value) FROM credits WHERE session_id=?", (sid,)) or 0, 2)

            return {
                "key": key, "start": s, "end": e, "days": span,
                "tokens": r.get("t") or 0,
                "input": r.get("i") or 0,
                "output": r.get("o") or 0,
                "cache_read": r.get("c") or 0,
                "requests": r.get("rq") or 0,
                "turns": r.get("turns") or 0,
                "records": r.get("n") or 0,
                "cache_hit_rate": _r((r.get("c") or 0) / (r.get("i") or 1) * 100),
                "credit": _r(credit, 2),
                "credit_reqs": cspan["reqs"],
                "credit_days": cspan["days"],
                "credit_records": cspan["records"],
                "credit_hours": credit_hours,
                "hours": hours,
                "prev_hours": prev_hours,
                "by_model": split("model"),
                "by_project": split("project", need_label=True),
                "by_account": by_account,
                "top_sessions": sess,
            }
        return self._cached(("period", key, acct), run)

    def top_sessions(self, days=1, limit=5, acct=None):
        def run():
            start = (self._today() - datetime.timedelta(days=days - 1)).isoformat()
            w, ap = _acct_sql(acct)
            rows = self.store.query(
                "SELECT session_id, SUM(total) t, "
                "COUNT(DISTINCT COALESCE(conv_req, uid)) turns, "
                "MIN(ts) first_ts, MAX(ts) last_ts FROM events "
                "WHERE day>=? AND session_id IS NOT NULL AND %s "
                "GROUP BY session_id ORDER BY t DESC LIMIT ?" % w,
                (start,) + ap + (limit,))
            out = []
            names, _ = workspace_names()
            for r in rows:
                sid = r["session_id"]
                title, cwd = self._sess_meta(sid)
                prow = self.store.query(
                    "SELECT project, cwd FROM events WHERE session_id=? "
                    "AND (project IS NOT NULL OR COALESCE(cwd,'')<>'') ORDER BY ts LIMIT 1",
                    (sid,))
                pr = prow[0] if prow else {}
                proj = pr.get("project") or ""
                out.append({
                    "session_id": sid,
                    "title": (title or _project_label(proj, pr.get("cwd") or cwd, names) or ""),
                    "project": proj, "cwd": pr.get("cwd") or cwd,
                    "model": self._sess_model(sid),
                    "account": self.store.account_of(sid),
                    "tokens": r["t"] or 0, "turns": r["turns"] or 0,
                    "credit": _r(self.store.scalar(
                        "SELECT SUM(value) FROM credits WHERE session_id=?", (sid,)) or 0, 2),
                    "first_ts": r["first_ts"], "last_ts": r["last_ts"],
                })
            return out
        return self._cached(("topsess", days, limit, acct), run)

    def heatmap(self, weeks=52, acct=None):
        def run():
            start = self._today() - datetime.timedelta(days=weeks * 7 - 1)
            w, ap = _acct_sql(acct)
            rows = self.store.query(
                "SELECT day, SUM(total) t, %s turns FROM events WHERE day>=? AND %s "
                "GROUP BY day ORDER BY day" % (TURNS, w), (start.isoformat(),) + ap)
            cr = {x["day"]: x["v"] or 0 for x in self.store.query(
                "SELECT day, SUM(value) v FROM credits WHERE day>=? AND %s GROUP BY day" % w,
                (start.isoformat(),) + ap)}
            return [{"date": r["day"], "tokens": r["t"] or 0, "turns": r["turns"] or 0,
                     "credit": _r(cr.get(r["day"], 0), 2)}
                    for r in rows]
        return self._cached(("heat", weeks, acct), run, ttl=120.0)

    def breakdown(self, by="model", days=None, acct=None):
        by = by if by in ("model", "project", "account") else "model"

        def run():
            conds, params = [], []
            if days:
                start = (self._today() - datetime.timedelta(days=days - 1)).isoformat()
                conds.append("day>=?")
                params.append(start)

            if by == "account":
                w, ap = _acct_sql(acct, "e.session_id")
                cols = ("COALESCE(NULLIF(a.user_id,''),'') k, SUM(e.total) t, "
                        "COUNT(DISTINCT COALESCE(e.conv_req,e.uid)) turns, COUNT(*) n")
                frm = "events e LEFT JOIN accounts a ON a.session_id=e.session_id"
            else:
                col = "project" if by == "project" else "model"
                w, ap = _acct_sql(acct)
                cols = ("COALESCE(NULLIF(%s,''),'未知') k, SUM(total) t, %s turns, "
                        "COUNT(*) n" % (col, TURNS))
                frm = "events"
            conds.append(w)
            params.extend(ap)

            sql = "SELECT %s FROM %s WHERE %s GROUP BY k ORDER BY t DESC" % (
                cols, frm, " AND ".join(conds))
            rows = self.store.query(sql, tuple(params))
            total = sum((r["t"] or 0) for r in rows) or 1

            labels = {}
            if by == "account":
                labels = {r["k"]: self._label(r["k"]) for r in rows}
            elif by == "project":
                names, _ = workspace_names()
                # 同上：优先工作区显示名，目录名只作兜底
                for r in self.store.query(
                        "SELECT project p, cwd, COUNT(*) n FROM events "
                        "WHERE cwd<>'' GROUP BY p, cwd ORDER BY n DESC"):
                    if r["p"] not in labels:
                        labels[r["p"]] = _project_label(r["p"], r["cwd"], names)

            return [{"name": r["k"], "label": labels.get(r["k"]) or r["k"],
                     "tokens": r["t"] or 0,
                     "turns": r["turns"] or 0, "records": r["n"] or 0,
                     "share": _r((r["t"] or 0) / total * 100)} for r in rows]
        return self._cached(("bd", by, days, acct), run)

    # ---------- 当前会话 ----------
    def _heartbeats(self):
        """sessions/*.json：lastHeartbeat 最新且 kind=interactive 即当前会话。
        文件可能为空（正在写入），必须容忍解析失败。"""
        out = []
        d = config.SESSIONS_DIR
        if not os.path.isdir(d):
            return out
        try:
            names = os.listdir(d)
        except OSError:
            return out
        for fn in names:
            if not fn.endswith(".json"):
                continue
            p = os.path.join(d, fn)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    obj = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(obj, dict) or obj.get("kind") != "interactive":
                continue
            sid = obj.get("sessionId")
            if not sid or not UUID_RE.match(str(sid)):
                continue                  # 伪会话：interactive-<pid>
            cwd = obj.get("cwd") or ""
            if "workbuddy-host-cli" in cwd:
                continue
            out.append({
                "sid": sid,
                "hb": obj.get("lastHeartbeat") or 0,
                "cwd": cwd,
                "pid": obj.get("pid"),
            })
        out.sort(key=lambda x: x["hb"], reverse=True)
        return out

    def resolve_session(self):
        """识别"本会话"，全程不依赖数据库。

        优先级不是简单看心跳：sessions/*.json 里混有 CLI 宿主的伪会话
        （sessionId 形如 interactive-31220，cwd 指向 Temp），心跳往往还最新。
        真正可信的信号是"谁正在产生 usage 事件"。所以以事件为准，心跳只用来补充 cwd。
        """
        now = time.monotonic()
        with self._lock:
            if self._cur["sid"] and now - self._cur["resolved_at"] < 10:
                return dict(self._cur)

        now_ms = int(time.time() * 1000)
        cand, src = None, None

        recent = self.store.query(
            "SELECT session_id sid, MAX(ts) last_ts FROM events "
            "WHERE kind='main' AND ts>? AND session_id IS NOT NULL "
            "GROUP BY session_id ORDER BY last_ts DESC", (now_ms - 30 * 60 * 1000,))
        hbs = self._heartbeats()          # 已过滤伪会话，按心跳倒序
        fresh_hb = next((h for h in hbs if now_ms - h["hb"] < 10 * 60 * 1000), None)

        if recent:
            top = recent[0]
            # 刚开的新会话还没有 usage：心跳明显更新时才让位
            if fresh_hb and fresh_hb["hb"] > top["last_ts"] + 60000:
                cand, src = fresh_hb, "heartbeat"
            else:
                cand, src = {"sid": top["sid"], "last_ts": top["last_ts"]}, "recent"
        elif fresh_hb:
            cand, src = fresh_hb, "heartbeat"

        if not cand:
            rows = self.store.query(
                "SELECT session_id sid, mtime_ns FROM files WHERE kind='main' "
                "ORDER BY mtime_ns DESC LIMIT 1")
            if rows:
                cand, src = {"sid": rows[0]["sid"]}, "mtime"

        if not cand:
            rows = self.store.query(
                "SELECT session_id sid, MAX(ts) last_ts FROM events "
                "GROUP BY session_id ORDER BY last_ts DESC LIMIT 1")
            if rows:
                cand, src = {"sid": rows[0]["sid"], "last_ts": rows[0]["last_ts"]}, "fallback"

        if not cand:
            return {"sid": None, "source": None}

        row = self.store.query(
            "SELECT MAX(ts) last_ts, MIN(ts) first_ts FROM events WHERE session_id=?",
            (cand["sid"],))[0]
        last = row.get("last_ts") or 0
        first = row.get("first_ts") or 0

        with self._lock:
            old = self._cur
            # 抖动保护：新会话必须明显更新才切换
            if (old["sid"] and old["sid"] != cand["sid"]
                    and last < old["last_ts"] + 60000
                    and time.monotonic() - old["resolved_at"] < 120):
                return dict(old)

            proj = self.store.scalar(
                "SELECT project FROM events WHERE session_id=? AND project IS NOT NULL LIMIT 1",
                (cand["sid"],)) or ""
            cwd = self.store.scalar(
                "SELECT cwd FROM events WHERE session_id=? AND cwd<>'' LIMIT 1",
                (cand["sid"],)) or ""
            hb = next((h for h in hbs if h["sid"] == cand["sid"]), None)
            if hb and hb.get("cwd"):
                cwd = hb["cwd"]
            self._cur = {
                "sid": cand["sid"], "last_ts": last, "resolved_at": time.monotonic(),
                "source": src, "project": proj, "cwd": cwd, "first_ts": first,
            }
            return dict(self._cur)

    def session(self):
        cur = self.resolve_session()
        sid = cur.get("sid")
        if not sid:
            return {"session_id": None, "tokens": 0, "active": False, "source": None,
                    "account": None, "account_short": None}

        def run():
            row = self.store.query(
                "SELECT SUM(total) t, SUM(input) i, SUM(output) o, SUM(cache_read) c, "
                "SUM(requests) rq, %s turns, COUNT(*) n, MIN(ts) first_ts, MAX(ts) last_ts "
                "FROM events WHERE session_id=?" % TURNS, (sid,))[0]
            models = self.store.query(
                "SELECT COALESCE(NULLIF(model,''),'未知') m, SUM(total) t FROM events "
                "WHERE session_id=? GROUP BY m ORDER BY t DESC", (sid,))
            start = int(time.time() * 1000) - 60 * 60 * 1000
            per_min = self.store.query(
                "SELECT (ts/60000)*60000 AS t, SUM(total) AS v FROM events "
                "WHERE session_id=? AND ts>=? GROUP BY t ORDER BY t", (sid, start))
            total = row.get("t") or 0
            title, cwd = self._sess_meta(sid)
            if not title:
                title = _project_label(cur.get("project"), cwd or cur.get("cwd"),
                                       workspace_names()[0]) or ""
            today = self._today().isoformat()
            # 本对话的积分数：累计 + 今天。credit 表按 day 存，可直接按天过滤
            credit_total = self.store.scalar(
                "SELECT SUM(value) FROM credits WHERE session_id=?", (sid,)) or 0
            credit_today = self.store.scalar(
                "SELECT SUM(value) FROM credits WHERE session_id=? AND day=?",
                (sid, today)) or 0
            # 有积分记录的请求数：分母是轮数，前端据此判断"是 0 分还是没记录"
            credit_reqs_today = self.store.scalar(
                "SELECT COUNT(DISTINCT conv_req) FROM events WHERE session_id=? AND day=? "
                "AND COALESCE(conv_req,'')<>'' AND conv_req IN (SELECT key FROM credits)",
                (sid, today)) or 0
            credit_reqs_total = self.store.scalar(
                "SELECT COUNT(DISTINCT conv_req) FROM events WHERE session_id=? "
                "AND COALESCE(conv_req,'')<>'' AND conv_req IN (SELECT key FROM credits)",
                (sid,)) or 0
            mcr = {x["m"]: x["v"] or 0 for x in self.store.query(
                "SELECT COALESCE(NULLIF(model,''),'未知') m, SUM(value) v FROM credits "
                "WHERE session_id=? GROUP BY m", (sid,))}
            tokens_today = self.store.scalar(
                "SELECT SUM(total) FROM events WHERE session_id=? AND day=?",
                (sid, today)) or 0
            turns_today = self.store.scalar(
                "SELECT %s FROM events WHERE session_id=? AND day=?" % TURNS,
                (sid, today)) or 0
            models = [{"model": m["m"], "tokens": m["t"] or 0,
                       "credit": _r(mcr.get(m["m"], 0), 2),
                       "share": _r((m["t"] or 0) / (total or 1) * 100)} for m in models]
            last = row.get("last_ts") or 0
            first = row.get("first_ts") or 0
            dur = max(0, (last - first) // 1000)
            recent = [r for r in per_min if r["v"]]
            return {
                "session_id": sid,
                "title": title,
                "project": cur.get("project") or "",
                "cwd": cur.get("cwd") or "",
                "source": cur.get("source"),
                "model": models[0]["model"] if models else None,
                "tokens": total,
                "credit_total": _r(credit_total, 2),
                "credit_reqs_total": credit_reqs_total,
                "tokens_today": tokens_today,
                "turns_today": turns_today,
                "credit_today": _r(credit_today, 2),
                "credit_reqs_today": credit_reqs_today,
                "input": row.get("i") or 0,
                "output": row.get("o") or 0,
                "cache_read": row.get("c") or 0,
                "turns": row.get("turns") or 0,
                "requests": row.get("rq") or 0,
                "records": row.get("n") or 0,
                "first_ts": first,
                "last_ts": last,
                "duration_s": dur,
                "rate_per_min": int(total / (dur / 60)) if dur > 30 else 0,
                "per_minute": [{"t": r["t"], "tokens": r["v"]} for r in recent[-60:]],
                "models": models,
            }

        # idle_s / active / delta_60s 每秒钟都在变，不能进缓存，否则状态灯会冻住
        base = self._cached(("sess", sid), run, ttl=30.0)
        now = int(time.time() * 1000)
        last = base.get("last_ts") or 0
        out = dict(base)
        out["idle_s"] = max(0, (now - last) // 1000) if last else None
        out["active"] = bool(last and now - last < 300000)
        out["delta_60s"] = sum(x["tokens"] for x in base.get("per_minute") or []
                               if x["t"] >= now - 60000)
        # 归属不参与过滤：这是"本会话"，被筛掉会让人以为没有活跃会话
        uid = self.store.account_of(sid)
        out["account"] = uid
        out["account_short"] = self._label(uid) if uid else None
        return out

    # ---------- credit ----------
    def credits(self, days=30, acct=None):
        """积分总览：全历史合计 + **真实覆盖率**。

        mapped_ratio 只说明"积分行有没有对上转录"，跟"多少请求有积分"完全是
        两回事：之前把它当覆盖率显示，于是 4% 的真实覆盖被读成 100%。
        真正的覆盖率只能拿事件侧的请求数当分母。
        """
        w, ap = _acct_sql(acct)
        db = self.store.query(
            "SELECT COUNT(*) n, SUM(value) v, SUM(mapped) m FROM credits "
            "WHERE src='db' AND %s" % w, ap)
        js = self.store.query(
            "SELECT COUNT(*) n, SUM(value) v FROM credits "
            "WHERE src='jsonl' AND %s" % w, ap)
        dbn = (db[0]["n"] if db else 0) or 0
        dbv = (db[0]["v"] if db else 0) or 0
        dbm = (db[0]["m"] if db else 0) or 0
        jsn = (js[0]["n"] if js else 0) or 0
        jsv = (js[0]["v"] if js else 0) or 0

        # 分母：全部请求；分子：真的拿到了积分记录的请求
        req_total = self.store.scalar(
            "SELECT COUNT(DISTINCT COALESCE(conv_req,uid)) FROM events "
            "WHERE COALESCE(conv_req,'')<>'' AND %s" % w, ap) or 0
        req_cov = self.store.scalar(
            "SELECT COUNT(DISTINCT conv_req) FROM events "
            "WHERE COALESCE(conv_req,'')<>'' AND conv_req IN (SELECT key FROM credits) "
            "AND %s" % w, ap) or 0
        cov = _r(req_cov / req_total * 100) if req_total else None

        # 完全没有积分记录的会话数：一眼看出缺口是不是出在"会话没被记过"
        sess_gap = self.store.scalar(
            "SELECT COUNT(*) FROM (SELECT DISTINCT session_id FROM events "
            "WHERE COALESCE(session_id,'')<>'' AND %s) s WHERE s.session_id NOT IN "
            "(SELECT session_id FROM credits WHERE session_id IS NOT NULL)" % w,
            ap) or 0

        if dbn:
            ratio = _r(dbm / dbn * 100)
            mode = "db" if cov is not None and cov >= 90 else "db_partial"
            total, available = dbv, True
        elif jsn:
            ratio, mode, total, available = None, "jsonl_partial", jsv, True
        else:
            ratio, mode, total, available = None, "none", 0, False
        return {"available": available, "mode": mode, "total": _r(total, 2),
                "mapped": dbm, "unmapped": dbn - dbm, "records": dbn,
                "mapped_ratio": ratio, "jsonl_records": jsn,
                "jsonl_total": _r(jsv, 2),
                "reqs": req_total, "reqs_covered": req_cov, "coverage": cov,
                "sessions_without_credit": sess_gap}

    # ---------- 账号 ----------
    def accounts(self):
        """轻量段：只读 accounts 表 + 一次主键点查，不扫 events。

        bundle 每 1.5 秒就要一次，绝不能在这里做聚合。
        """
        rows = self.store.query(
            "SELECT user_id, COUNT(*) n, MAX(last_activity_at) last FROM accounts "
            "WHERE %s GROUP BY user_id ORDER BY n DESC" % _ACCT_ATTR)
        items = [{"user_id": r["user_id"], "short": self._label(r["user_id"]),
                  "sessions": r["n"], "last_activity_at": r["last"]} for r in rows]
        cur = self.resolve_session().get("sid")
        uid = self.store.account_of(cur) if cur else None
        un = self.store.scalar(
            "SELECT COUNT(*) FROM accounts WHERE user_id IS NULL OR user_id=''") or 0
        return {
            "available": bool(items),
            "items": items,
            "current": {"user_id": uid, "short": self._label(uid) if uid else None,
                        "session_id": cur},
            "unattributed_sessions": un,
        }

    def audit(self):
        """归属自检：各账号 + 未归属 == 全局总量。

        bucket_sum 与 tokens 必须恒等 —— 它只验证 SQL 拼对了没；
        真正的"漏归属"看 sessions_unknown / tokens_unattributed。
        """
        attr = ("COALESCE(session_id,'') IN "
                "(SELECT session_id FROM accounts WHERE %s)" % _ACCT_ATTR)
        r = self.store.query(
            "SELECT COUNT(*) n, COALESCE(SUM(total),0) t, "
            "COALESCE(SUM(CASE WHEN " + attr + " THEN 1 ELSE 0 END),0) na, "
            "COALESCE(SUM(CASE WHEN " + attr + " THEN total ELSE 0 END),0) ta "
            "FROM events")[0]
        buckets = self.store.query(
            "SELECT COALESCE(SUM(e.total),0) t FROM events e "
            "LEFT JOIN accounts a ON a.session_id=e.session_id GROUP BY "
            "COALESCE(NULLIF(a.user_id,''),'')")
        bsum = sum((b["t"] or 0) for b in buckets)
        unknown = self.store.scalar(
            "SELECT COUNT(*) FROM (SELECT DISTINCT session_id FROM events "
            "WHERE COALESCE(session_id,'')<>'' AND COALESCE(session_id,'') NOT IN "
            "(SELECT session_id FROM accounts))") or 0
        return {
            "accounts": self.store.scalar(
                "SELECT COUNT(DISTINCT user_id) FROM accounts WHERE %s" % _ACCT_ATTR) or 0,
            "events": r["n"] or 0,
            "events_attributed": r["na"] or 0,
            "events_unattributed": (r["n"] or 0) - (r["na"] or 0),
            "tokens": r["t"] or 0,
            "tokens_attributed": r["ta"] or 0,
            "tokens_unattributed": (r["t"] or 0) - (r["ta"] or 0),
            "sessions_unknown": unknown,
            "bucket_sum": bsum,
            "balanced": bsum == (r["t"] or 0),
        }

    # ---------- 汇总 ----------
    def summary(self, indexer=None, acct=None):
        t = self.today(acct)
        a = self.all_time(acct)
        s = self.session()
        return {
            # 不用时间戳当版本：它每次调用都不同，会让"数据没变"永远判不出来
            "v": self.store.seq(),
            "indexing": dict(indexer.progress) if indexer else {},
            "today": {k: t[k] for k in ("day", "tokens", "input", "output", "cache_read",
                                        "requests", "turns", "cache_hit_rate",
                                        "yesterday_same_time", "delta_vs_yesterday",
                                        "now_hour", "peak_hour", "peak_tokens")},
            "session": {k: s.get(k) for k in ("session_id", "title", "project", "model",
                                              "tokens", "turns", "requests", "duration_s",
                                              "active", "source", "rate_per_min",
                                              "delta_60s", "idle_s", "account",
                                              "account_short")},
            "all_time": a,
            "w30": self.window(30, acct),
            "top_sessions": self.top_sessions(1, 5, acct),
            "credit": self.credits(acct=acct),
            "totals": {"files": self.store.scalar("SELECT COUNT(*) FROM files") or 0,
                       "events": self.store.scalar("SELECT COUNT(*) FROM events") or 0},
        }

    def status(self, indexer=None):
        errs = self.store.query(
            "SELECT err, COUNT(*) n FROM files WHERE err IS NOT NULL AND err<>'' GROUP BY err")
        return {
            "files": self.store.scalar("SELECT COUNT(*) FROM files") or 0,
            "events": self.store.scalar("SELECT COUNT(*) FROM events") or 0,
            "projects": self.store.scalar("SELECT COUNT(DISTINCT project) FROM events") or 0,
            "errors": [{"err": e["err"], "count": e["n"]} for e in errs],
            "indexing": dict(indexer.progress) if indexer else {},
            "credit": self.credits(),
            "audit": self.audit(),
            "db_mb": round(_db_size(), 2),
        }


def _db_size():
    try:
        return os.path.getsize(config.INDEX_DB) / 1048576
    except OSError:
        return 0
