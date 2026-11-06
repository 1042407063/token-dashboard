# -*- coding: utf-8 -*-
"""索引库封装。

只碰 token-panel\\data\\index.db，绝不写 WorkBuddy 自己的 workbuddy.db。
每线程一个连接（sqlite3 连接不是线程安全的），WAL 模式让 HTTP 读不被索引写阻塞。
"""

import logging
import os
import sqlite3
import threading
import time

import config

log = logging.getLogger("store")

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    dev         INTEGER DEFAULT 0,
    ino         INTEGER DEFAULT 0,
    size        INTEGER DEFAULT 0,
    mtime_ns    INTEGER DEFAULT 0,
    offset      INTEGER DEFAULT 0,
    session_id  TEXT,
    project     TEXT,
    kind        TEXT,
    first_seen  INTEGER,
    last_scan   INTEGER,
    err         TEXT
);

CREATE TABLE IF NOT EXISTS events (
    rowid      INTEGER PRIMARY KEY AUTOINCREMENT,
    uid        TEXT,
    path       TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    day        TEXT NOT NULL,
    hour       INTEGER NOT NULL,
    session_id TEXT,
    project    TEXT,
    cwd        TEXT,
    model      TEXT,
    agent      TEXT,
    kind       TEXT,
    conv_req   TEXT,
    input      INTEGER DEFAULT 0,
    output     INTEGER DEFAULT 0,
    cache_read INTEGER DEFAULT 0,
    reasoning  INTEGER DEFAULT 0,
    total      INTEGER DEFAULT 0,
    requests   INTEGER DEFAULT 1,
    src        TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_events_uid   ON events(uid);
CREATE INDEX IF NOT EXISTS idx_events_day    ON events(day);
CREATE INDEX IF NOT EXISTS idx_events_sess   ON events(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_conv   ON events(conv_req);
CREATE INDEX IF NOT EXISTS idx_events_path   ON events(path);
CREATE INDEX IF NOT EXISTS idx_events_model  ON events(model);

CREATE TABLE IF NOT EXISTS credits (
    key        TEXT PRIMARY KEY,
    value      REAL NOT NULL,
    db_session TEXT,
    mapped     INTEGER DEFAULT 0,
    ts         INTEGER,
    day        TEXT,
    hour       INTEGER,
    session_id TEXT,
    project    TEXT,
    cwd        TEXT,
    model      TEXT,
    src        TEXT,
    updated_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_credits_day ON credits(day);

CREATE TABLE IF NOT EXISTS sessions_meta (
    session_id       TEXT PRIMARY KEY,
    cwd              TEXT,
    title            TEXT,
    model            TEXT,
    used             INTEGER,
    size             INTEGER,
    credit_total     REAL,
    db_updated_at    INTEGER,
    fetched_at       INTEGER
);

-- 会话 -> WorkBuddy 账号的映射。来源是 workbuddy.db 的 sessions.user_id。
-- 刻意不在 events 上开列：events 来自转录、写入时根本不知道账号，
-- 事后补列既要 ALTER 又要回填；这里用一张小表 join，随时可以整表刷新。
CREATE TABLE IF NOT EXISTS accounts (
    session_id       TEXT PRIMARY KEY,
    user_id          TEXT,
    title            TEXT,
    cwd              TEXT,
    last_activity_at INTEGER,
    fetched_at       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_accounts_user ON accounts(user_id);

-- 账号切换时间线：只在采集线程判定"最近活跃会话换了账号"时落一行
CREATE TABLE IF NOT EXISTS account_switches (
    ts         INTEGER NOT NULL,
    from_user  TEXT,
    to_user    TEXT,
    session_id TEXT,
    src        TEXT
);

CREATE INDEX IF NOT EXISTS idx_switches_ts ON account_switches(ts);
"""


MASK = 0x7FFFFFFFFFFFFFFF


def _i(v):
    """Windows 的 st_ino / st_dev 可能是 128 位，直接入库会 OverflowError。"""
    try:
        return int(v) & MASK
    except (TypeError, ValueError):
        return 0


class Store:
    def __init__(self, path=None):
        self.path = path or config.INDEX_DB
        self._local = threading.local()
        # 数据版本号：任何会改变统计结果的写入都 +1。
        # 种子用毫秒时间戳，进程重启后仍然单调，客户端只需比较"不相等"。
        self._seq = int(time.time() * 1000)
        self._seq_lock = threading.Lock()
        config.ensure_dirs()

    # ---------- 版本号 ----------
    def seq(self):
        """当前版本。单次属性读，CPython 下原子，不加锁。"""
        return self._seq

    def bump(self):
        """写入后调用。返回新版本，便于调用方日志。"""
        with self._seq_lock:          # 只保护读-改-写，防两个线程同时 +1 丢更新
            self._seq += 1
            return self._seq

    # ---------- 连接 ----------
    def conn(self):
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=5.0)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=5000")
            c.execute("PRAGMA temp_store=MEMORY")
            self._local.c = c
        return c

    def init_schema(self):
        c = self.conn()
        c.executescript(DDL)          # 全部 IF NOT EXISTS：老库跑一遍就补齐新表
        c.commit()

        raw = self.get_meta("schema_version")
        try:
            ver = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            ver = None                # 脏值当作"未知"，直接对齐当前版本

        if ver is None:
            self.set_meta("schema_version", str(config.SCHEMA_VERSION))
        elif ver < config.SCHEMA_VERSION:
            # 本次升级是纯增量（只加表/索引），DDL 上面已经幂等执行完，
            # 所以这里只推版本号：不重建、不清历史。
            self.set_meta("schema_version", str(config.SCHEMA_VERSION))
            log.info("schema %s -> %s（增量：accounts / account_switches）",
                     ver, config.SCHEMA_VERSION)
        elif ver > config.SCHEMA_VERSION:
            # 新库被老程序打开：不降级、不改版本号，只是读不到新表
            log.warning("索引库 schema=%s 高于程序 %s，请升级程序",
                        ver, config.SCHEMA_VERSION)

    def quick_check(self):
        """索引库损坏时只丢索引、不丢源数据，直接重建即可。"""
        try:
            row = self.conn().execute("PRAGMA quick_check").fetchone()
            return bool(row) and row[0] == "ok"
        except sqlite3.Error:
            return False

    # ---------- meta ----------
    def get_meta(self, k, default=None):
        row = self.conn().execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def set_meta(self, k, v):
        self.conn().execute(
            "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, str(v)),
        )
        self.conn().commit()

    # ---------- files ----------
    def get_file(self, path):
        row = self.conn().execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        return dict(row) if row else None

    def all_paths(self):
        return {r[0] for r in self.conn().execute("SELECT path FROM files")}

    def save_file(self, path, st, offset, session_id, project, kind, err=None):
        now = int(time.time() * 1000)
        self.conn().execute(
            """INSERT INTO files(path,dev,ino,size,mtime_ns,offset,session_id,project,kind,
                                 first_seen,last_scan,err)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                 dev=excluded.dev, ino=excluded.ino, size=excluded.size,
                 mtime_ns=excluded.mtime_ns, offset=excluded.offset,
                 session_id=excluded.session_id, project=excluded.project,
                 kind=excluded.kind, last_scan=excluded.last_scan, err=excluded.err""",
            (path, _i(getattr(st, "st_dev", 0)), _i(getattr(st, "st_ino", 0)),
             _i(st.st_size), _i(st.st_mtime_ns), _i(offset), session_id, project,
             kind, now, now, err),
        )
        self.conn().commit()

    def touch_file(self, path, mtime_ns):
        self.conn().execute(
            "UPDATE files SET mtime_ns=?, last_scan=? WHERE path=?",
            (mtime_ns, int(time.time() * 1000), path),
        )
        self.conn().commit()

    def mark_err(self, path, err):
        self.conn().execute("UPDATE files SET err=? WHERE path=?", (err, path))
        self.conn().commit()

    def delete_file(self, path):
        """文件消失：只删位点，保留 events —— 历史统计不该倒退。"""
        cur = self.conn().execute("DELETE FROM files WHERE path=?", (path,))
        n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        self.conn().commit()
        if n:
            self.bump()               # totals.files 变了
        return n

    def delete_events_by_path(self, path):
        """截断/重写：清掉该文件的事件。必须 bump，否则页面停在旧数字上。"""
        cur = self.conn().execute("DELETE FROM events WHERE path=?", (path,))
        n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        self.conn().commit()
        if n:
            self.bump()
        return n

    # ---------- events ----------
    def insert_events(self, rows):
        if not rows:
            return 0
        cols = ("uid", "path", "ts", "day", "hour", "session_id", "project", "cwd",
                "model", "agent", "kind", "conv_req", "input", "output",
                "cache_read", "reasoning", "total", "requests", "src")
        sql = ("INSERT OR IGNORE INTO events(%s) VALUES(%s)"
               % (",".join(cols), ",".join("?" * len(cols))))
        c = self.conn()
        cur = c.executemany(sql, [tuple(r.get(k) for k in cols) for r in rows])
        n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
        if n:
            self.bump()               # 版本号在这里涨，调用方漏了也不会停更
        return n

    # ---------- credits ----------
    def insert_credits(self, rows):
        if not rows:
            return 0
        cols = ("key", "value", "db_session", "mapped", "ts", "day", "hour",
                "session_id", "project", "cwd", "model", "src", "updated_at")
        sql = ("INSERT INTO credits(%s) VALUES(%s) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
               "mapped=excluded.mapped, ts=excluded.ts, day=excluded.day, "
               "hour=excluded.hour, session_id=excluded.session_id, "
               "project=excluded.project, cwd=excluded.cwd, model=excluded.model, "
               "src=excluded.src, updated_at=excluded.updated_at"
               % (",".join(cols), ",".join("?" * len(cols))))
        c = self.conn()
        c.executemany(sql, [tuple(r.get(k) for k in cols) for r in rows])
        c.commit()
        # credit.py 已用指纹判过"确实变了"才走到这里，所以直接 bump
        self.bump()
        return len(rows)

    # ---------- accounts ----------
    def upsert_accounts(self, rows, digest):
        """刷新会话 -> 账号映射。rows 来自 workbuddy.db 的 sessions 全量。

        返回 True 表示归属关系真的变了（调用方据此判断要不要日志）。
        digest 只由 (session_id, user_id) 决定：改个会话标题不该触发全站重算。
        """
        if not rows:
            return False
        now = int(time.time() * 1000)
        c = self.conn()
        c.executemany(
            "INSERT INTO accounts(session_id,user_id,title,cwd,last_activity_at,fetched_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
            "user_id=excluded.user_id, title=excluded.title, cwd=excluded.cwd, "
            "last_activity_at=excluded.last_activity_at, fetched_at=excluded.fetched_at",
            [(r.get("session_id"), r.get("user_id"), r.get("title"), r.get("cwd"),
              r.get("last_activity_at"), now) for r in rows if r.get("session_id")])
        # 本轮没写到的，就是源库里已经消失的会话（硬删/换库）。
        # 必须清掉，否则它会把事件错误地永远留在某个账号名下。
        c.execute("DELETE FROM accounts WHERE COALESCE(fetched_at,0)<?", (now,))
        c.commit()
        if digest and digest != self.get_meta("accounts_digest"):
            self.set_meta("accounts_digest", digest)
            self.bump()
            return True
        return False

    def account_of(self, session_id):
        if not session_id:
            return None
        return self.scalar("SELECT user_id FROM accounts WHERE session_id=?", (session_id,))

    def account_of_user(self, user_id):
        """校验一个 user_id 是否真实存在（用于 query string 白名单）。"""
        if not user_id:
            return None
        return self.scalar("SELECT user_id FROM accounts WHERE user_id=? LIMIT 1", (user_id,))

    # ---------- 账号切换时间线 ----------
    def insert_switch(self, ts, from_user, to_user, session_id=None, src=None):
        self.conn().execute(
            "INSERT INTO account_switches(ts,from_user,to_user,session_id,src) "
            "VALUES(?,?,?,?,?)", (ts, from_user, to_user, session_id, src))
        # 写入极少（一天最多几次），顺手裁掉 180 天前的不影响性能
        self.conn().execute("DELETE FROM account_switches WHERE ts<?",
                            (ts - 180 * 86400000,))
        self.conn().commit()
        self.bump()                   # 时间线要能立刻看到

    def recent_switches(self, limit=20):
        return self.query(
            "SELECT ts, from_user, to_user, session_id, src FROM account_switches "
            "ORDER BY ts DESC LIMIT ?", (limit,))

    def query(self, sql, params=()):
        return [dict(r) for r in self.conn().execute(sql, params)]

    def scalar(self, sql, params=()):
        row = self.conn().execute(sql, params).fetchone()
        return row[0] if row else None

    def checkpoint(self):
        try:
            self.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

    def close(self):
        c = getattr(self._local, "c", None)
        if c is not None:
            try:
                c.close()
            except sqlite3.Error:
                pass
            self._local.c = None
