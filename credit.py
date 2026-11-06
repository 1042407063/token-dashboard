# -*- coding: utf-8 -*-
"""credit 采集（可选增强）。

这是全项目唯一接触 workbuddy.db 的模块，原则：
  1. 只读，绝不写（PRAGMA query_only），连 WAL 都不主动碰
  2. 打开失败就降级，异常一律吞掉 —— token 统计绝不能因为 credit 挂掉
  3. credit 与 token 分区展示，永不混算

只读打开的三档顺序：mode=ro -> mode=ro&immutable=1 -> 放弃。
immutable 会读不到活跃 WAL，所以必须标记"可能滞后"。
"""

import datetime
import hashlib
import json
import logging
import sqlite3
import time

import config

log = logging.getLogger("credit")


def _uri(path, immutable=False):
    p = path.replace("\\", "/")
    return "file:%s?mode=ro%s" % (p, "&immutable=1" if immutable else "")


def open_readonly():
    """返回 (conn, lag)。lag=True 表示用的是 immutable，可能读不到最新 credit。"""
    if not config.WB_DB or not __import__("os").path.exists(config.WB_DB):
        return None, False
    for immutable in (False, True):
        try:
            conn = sqlite3.connect(_uri(config.WB_DB, immutable), uri=True, timeout=0.8)
            conn.execute("PRAGMA query_only=ON")
            conn.execute("SELECT 1 FROM session_usage LIMIT 1").fetchone()
            return conn, immutable
        except sqlite3.Error as e:
            log.debug("只读打开失败(%s): %s", "immutable" if immutable else "ro", e)
            continue
    return None, False


def _local(ts):
    try:
        dt = datetime.datetime.fromtimestamp(ts / 1000.0)
        return dt.strftime("%Y-%m-%d"), dt.hour
    except (OSError, ValueError, OverflowError):
        return None, None


# 聚合指纹：一条 SQL 覆盖"行数变 / 更新时间变 / credit_json 变长变短 / used、size 变"
# 53 行全在内存页里，耗时约 1-2ms
_PROBE_SQL = (
    "SELECT COUNT(*), COALESCE(MAX(updated_at),0), "
    "COALESCE(SUM(LENGTH(credit_json)),0), "
    "COALESCE(SUM(COALESCE(used,0)),0), COALESCE(SUM(COALESCE(size,0)),0) "
    "FROM session_usage")
_SESS_PROBE_SQL = (
    "SELECT COUNT(*), COALESCE(MAX(updated_at),0), "
    "COALESCE(SUM(LENGTH(user_id)),0), COUNT(DISTINCT user_id) "
    "FROM sessions WHERE deleted_at IS NULL")
# 旧版 WorkBuddy 没有 user_id 列：保持四列同构，让上层不用分支
_SESS_PROBE_SQL_NO_UID = (
    "SELECT COUNT(*), COALESCE(MAX(updated_at),0), 0, 0 "
    "FROM sessions WHERE deleted_at IS NULL")

# 会话 -> 账号。刻意不带 deleted_at 过滤：软删会话的 user_id 仍然有效，
# 而转录里那些会话的 token 还在，过滤掉就会永久掉进"未归属"。
_ACCT_SQL = ("SELECT id, user_id, COALESCE(NULLIF(custom_title,''), title), cwd, "
             "COALESCE(last_activity_at, updated_at) FROM sessions")
_ACCT_SQL_NO_UID = ("SELECT id, NULL, COALESCE(NULLIF(custom_title,''), title), cwd, "
                    "COALESCE(last_activity_at, updated_at) FROM sessions")

_HAS_UID = {"v": None}          # None=未探测 / True / False，只付一次异常代价


def _probe_all(conn):
    """返回 (probe_tuple, has_uid)。缺 user_id 列时降级，绝不让 credit 整体失败。"""
    if _HAS_UID["v"] is not False:
        try:
            p = (tuple(conn.execute(_PROBE_SQL).fetchone())
                 + tuple(conn.execute(_SESS_PROBE_SQL).fetchone()))
            _HAS_UID["v"] = True
            return p, True
        except sqlite3.OperationalError:
            # 只有第一次会付这个异常；之后直接走常量列分支
            _HAS_UID["v"] = False
    p = (tuple(conn.execute(_PROBE_SQL).fetchone())
         + tuple(conn.execute(_SESS_PROBE_SQL_NO_UID).fetchone()))
    return p, False


def _load_accounts(conn, has_uid):
    """读出全部会话的归属关系，返回 (rows, digest)。

    digest 只含 (session_id, user_id)：会话标题、cwd 变了不该触发全站重算。
    """
    sql = _ACCT_SQL if has_uid else _ACCT_SQL_NO_UID
    rows = [{"session_id": r[0],
             "user_id": (r[1] or "").strip() or None,
             "title": r[2], "cwd": r[3], "last_activity_at": r[4]}
            for r in conn.execute(sql)]
    src = sorted("%s|%s" % (r["session_id"], r["user_id"] or "") for r in rows)
    digest = hashlib.sha1("\n".join(src).encode("utf-8")).hexdigest() if rows else ""
    return rows, digest



_STATE = {"probe": None, "last_ok": None, "last_full": 0.0}


def run_once(store, force=False):
    """刷新 credits 表与 sessions_meta。返回状态字典，绝不抛异常。

    db 没变化时只做一次聚合探测就返回（约 1-2ms），省掉 392 次点查 + 392 行 upsert。
    注意：判断"有没有变化"不用文件 mtime —— WAL 模式下主库时间戳会滞后，
    NTFS 对正在被打开的文件时间戳也是惰性更新，读到旧时间会让 credit 永久卡住。
    """
    t0 = time.monotonic()
    conn, lag = open_readonly()
    if conn is None:
        return {"ok": False, "reason": "workbuddy.db 只读打开失败（可能被占用或拒绝访问）",
                "lag": False}

    try:
        try:
            probe, has_uid = _probe_all(conn)
        except sqlite3.Error as e:
            log.warning("读取 workbuddy.db 失败：%s", e)
            return {"ok": False, "reason": "读取失败：%s" % e, "lag": lag}

        # lag=True 用的是 immutable，根本读不到活跃 WAL：此时"未变化"是假的，
        # 而且必须尽快重试 mode=ro 才能恢复，所以永不跳过。
        ceiling = 60 if lag else config.CREDIT_REFRESH_CEIL
        fresh = (time.monotonic() - _STATE["last_full"]) < ceiling
        if (not force and not lag and fresh and _STATE["last_ok"]
                and _STATE["probe"] == probe):
            return dict(_STATE["last_ok"], ok=True, lag=lag, skipped=True,
                        cost_ms=int((time.monotonic() - t0) * 1000))

        credits, sessions, metas = [], {}, []
        for sid, cj, used, size, upd in conn.execute(
                "SELECT session_id, credit_json, used, size, updated_at FROM session_usage"):
            d = {}
            if cj:
                try:
                    d = json.loads(cj) or {}
                except ValueError:
                    d = {}
            for k, v in d.items():
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                key = str(k).replace("-", "").lower()
                credits.append({"key": key, "value": v, "db_session": sid,
                                "upd": upd})
            metas.append({"session_id": sid, "used": used, "size": size, "upd": upd})

        for row in conn.execute(
                "SELECT id, cwd, COALESCE(NULLIF(custom_title,''), title), model, "
                "last_activity_at, updated_at FROM sessions WHERE deleted_at IS NULL"):
            sessions[row[0]] = {"cwd": row[1], "title": row[2], "model": row[3],
                                "last": row[4] or row[5]}

        # ---- 账号归属：独立于 credits，失败也绝不能拖累已有数字 ----
        try:
            arows, adigest = _load_accounts(conn, has_uid)
        except sqlite3.Error as e:
            log.warning("读取 sessions.user_id 失败：%s", e)
            arows, adigest = [], ""
    except sqlite3.Error as e:
        log.warning("读取 workbuddy.db 失败：%s", e)
        return {"ok": False, "reason": "读取失败：%s" % e, "lag": lag}
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    # 用 conv_req 精确对齐到转录；对不上的退回会话更新时间，并标记 mapped=0
    mapped = 0
    rows = []
    for c in credits:
        hit = store.query(
            "SELECT session_id, project, cwd, model, ts FROM events "
            "WHERE conv_req=? ORDER BY ts LIMIT 1", (c["key"],))
        if hit:
            h = hit[0]
            day, hour = _local(h["ts"])
            mapped += 1
            rows.append({"key": c["key"], "value": c["value"], "db_session": c["db_session"],
                         "mapped": 1, "ts": h["ts"], "day": day, "hour": hour,
                         "session_id": h["session_id"], "project": h["project"],
                         "cwd": h["cwd"], "model": h["model"], "src": "db",
                         "updated_at": c["upd"]})
        else:
            day, hour = _local(c["upd"] or 0)
            rows.append({"key": c["key"], "value": c["value"], "db_session": c["db_session"],
                         "mapped": 0, "ts": c["upd"], "day": day, "hour": hour,
                         "session_id": c["db_session"], "project": None, "cwd": None,
                         "model": sessions.get(c["db_session"], {}).get("model"),
                         "src": "db", "updated_at": c["upd"]})

    if rows:
        store.insert_credits(rows)

    # 会话标题（纯展示用，拿不到也不影响任何数字）
    now = int(time.time() * 1000)
    for m in metas:
        s = sessions.get(m["session_id"], {})
        store.conn().execute(
            "INSERT INTO sessions_meta(session_id,cwd,title,model,used,size,"
            "db_updated_at,fetched_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET cwd=excluded.cwd, title=excluded.title, "
            "model=excluded.model, used=excluded.used, size=excluded.size, "
            "db_updated_at=excluded.db_updated_at, fetched_at=excluded.fetched_at",
            (m["session_id"], s.get("cwd"), s.get("title"), s.get("model"),
             m["used"], m["size"], m["upd"], now))
    store.conn().commit()

    # 账号映射：只有 (session_id, user_id) 真的变了才 bump，改标题不触发重算
    acct_rows = 0
    if arows:
        store.upsert_accounts(arows, adigest)
        acct_rows = len(arows)

    total = sum(c["value"] for c in credits) or 0
    result = {"ok": True, "lag": lag, "records": len(credits), "mapped": mapped,
              "total": round(total, 2), "skipped": False,
              "accounts": {"rows": acct_rows, "uid": has_uid},
              "cost_ms": int((time.monotonic() - t0) * 1000)}
    # 只有整个流程成功才记指纹：失败必须下一轮重试
    _STATE["probe"] = probe
    _STATE["last_full"] = time.monotonic()
    _STATE["last_ok"] = result
    return result


# ---------------- 账号切换时间线 ----------------
_TRACK_KEY = "account_track"


def track_switch(store, sid, src=None):
    """每个采集周期调用一次：判定"当前会话的账号"是否真的变了。

    放在 30 秒一轮的采集线程里，而不是 HTTP GET 路径上 —— GET 只该读，
    而且 broadcast_loop 在没人看页面时不跑，会漏记。

    三条规则：
      1. 首次运行只写基线，不落行（否则会出现 from=None 的假记录）
      2. 需要连续两拍一致才确认，防 A→B→A 抖动刷出两条
      3. 解析不到账号时不动状态，宁可不记也不错记
    """
    if not sid:
        return {"logged": False, "reason": "no_session"}
    uid = (store.account_of(sid) or "").strip()
    if not uid:
        return {"logged": False, "reason": "no_account"}

    raw = store.get_meta(_TRACK_KEY)
    try:
        st = json.loads(raw) if raw else {}
    except ValueError:
        st = {}
    if not isinstance(st, dict):
        st = {}

    if not st.get("prev"):                       # 首次：只建基线
        store.set_meta(_TRACK_KEY, json.dumps(
            {"prev": uid, "pending": None, "n": 0}, ensure_ascii=False))
        return {"logged": False, "reason": "baseline"}

    if st["prev"] == uid:                        # 没变，清掉待确认
        if st.get("pending"):
            store.set_meta(_TRACK_KEY, json.dumps(
                {"prev": uid, "pending": None, "n": 0}, ensure_ascii=False))
        return {"logged": False, "reason": "same"}

    n = (st.get("n") or 0) + 1 if st.get("pending") == uid else 1
    if n < 2:                                    # 第一拍：只记候选，等下一拍确认
        store.set_meta(_TRACK_KEY, json.dumps(
            {"prev": st["prev"], "pending": uid, "n": n}, ensure_ascii=False))
        return {"logged": False, "reason": "pending"}

    store.insert_switch(int(time.time() * 1000), st["prev"], uid, sid, src)
    store.set_meta(_TRACK_KEY, json.dumps(
        {"prev": uid, "pending": None, "n": 0}, ensure_ascii=False))
    return {"logged": True, "from": st["prev"], "to": uid}
