# -*- coding: utf-8 -*-
"""增量索引器。

只做一件事：把 JSONL 里新增的字节变成 events 行。

四条边界必须同时成立，否则数字会错：
  1. 新文件 / 被替换（inode 变）  -> 清空该文件事件，从 0 重读
  2. 文件变短（截断 / 重写）      -> 清空该文件事件，从 0 重读，防止旧记录残留翻倍
  3. 尺寸不变只有 mtime 变        -> 只是被 touch，不重读
  4. 末尾半行未写完               -> offset 不动，下一拍再读（绝不消费残缺行）
"""

import datetime
import hashlib
import json
import logging
import os
import threading
import time

import config

log = logging.getLogger("indexer")


def _num(v):
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _first(d):
    """从 [{cached_tokens: n}] 这种结构里取第一个值。"""
    if isinstance(d, list) and d and isinstance(d[0], dict):
        return d[0]
    return {}


def extract_event(rec, raw, path, project, kind, sid, fallback_ts):
    """把一条转录记录变成 events 行；不是 usage 记录则返回 None。"""
    pd = rec.get("providerData")
    if not isinstance(pd, dict):
        pd = {}
    msg = rec.get("message")
    if not isinstance(msg, dict):
        msg = {}

    usage = pd.get("usage")
    src = "providerData"
    if not isinstance(usage, dict):
        usage = msg.get("usage")
        src = "message"
    if not isinstance(usage, dict):
        return None

    inp = _num(usage.get("inputTokens")) or _num(usage.get("input_tokens")) \
        or _num(usage.get("prompt_tokens"))
    out = _num(usage.get("outputTokens")) or _num(usage.get("output_tokens")) \
        or _num(usage.get("completion_tokens"))

    cache = _num(usage.get("cache_read_input_tokens")) or _num(usage.get("cacheReadInputTokens"))
    if not cache:
        cache = _num(_first(usage.get("inputTokensDetails")).get("cached_tokens"))
    if not cache:
        cache = _num(_first(usage.get("prompt_tokens_details")).get("cached_tokens"))

    reasoning = _num(usage.get("reasoning_tokens"))
    if not reasoning:
        reasoning = _num(_first(usage.get("outputTokensDetails")).get("reasoning_tokens"))

    total = _num(usage.get("totalTokens")) or _num(usage.get("total_tokens"))
    if not total:
        total = inp + out

    ts = rec.get("timestamp")
    if isinstance(ts, str) and ts.strip().isdigit():
        ts = int(ts.strip())
    if isinstance(ts, float):
        ts = int(ts)
    if not isinstance(ts, int):
        ts = 0
    if 0 < ts < 100000000000:      # 秒级时间戳 -> 毫秒
        ts *= 1000
    if not (config.TS_MIN <= ts <= config.TS_MAX):
        ts = fallback_ts

    dt = datetime.datetime.fromtimestamp(ts / 1000.0)

    uid = rec.get("id")
    if not uid:
        uid = "h:" + hashlib.sha1(raw).hexdigest()

    conv = pd.get("conversationRequestId") or msg.get("conversationRequestId") or ""
    conv = str(conv).replace("-", "").lower() if conv else ""

    return {
        "uid": str(uid),
        "path": path,
        "ts": ts,
        "day": dt.strftime("%Y-%m-%d"),
        "hour": dt.hour,
        "session_id": rec.get("sessionId") or sid,
        "project": project,
        "cwd": rec.get("cwd") or "",
        "model": pd.get("model") or msg.get("model") or "未知",
        "agent": pd.get("agent") or "",
        "kind": kind,
        "conv_req": conv,
        "input": inp,
        "output": out,
        "cache_read": cache,
        "reasoning": reasoning,
        "total": total,
        "requests": _num(usage.get("requests")) or 1,
        "src": src,
    }


def extract_credit(rec, fallback_ts):
    """转录里的 credit-usage 记录（团队调用），db 不可读时的兜底。"""
    if rec.get("type") != "credit-usage":
        return None
    pd = rec.get("providerData")
    if not isinstance(pd, dict):
        return None
    val = pd.get("credit")
    try:
        val = float(val)
    except (TypeError, ValueError):
        return None

    ts = rec.get("timestamp")
    if isinstance(ts, str) and ts.strip().isdigit():
        ts = int(ts.strip())
    if not isinstance(ts, int) or not (config.TS_MIN <= ts <= config.TS_MAX):
        ts = fallback_ts
    dt = datetime.datetime.fromtimestamp(ts / 1000.0)

    key = pd.get("conversationRequestId") or pd.get("messageId") or ""
    key = str(key).replace("-", "").lower()
    if not key:
        return None
    return {
        "key": key,
        "value": val,
        "db_session": None,
        "mapped": 0,
        "ts": ts,
        "day": dt.strftime("%Y-%m-%d"),
        "hour": dt.hour,
        "session_id": rec.get("sessionId"),
        "project": None,
        "cwd": None,
        "model": None,
        "src": "jsonl",
        "updated_at": ts,
        "source": pd.get("source"),
    }


class Indexer(threading.Thread):
    def __init__(self, store, is_active=None):
        super().__init__(daemon=True, name="indexer")
        self.store = store
        self._stop = threading.Event()
        self._wake = threading.Event()       # 只用来打断等待，不承载"有没有人看"的状态
        self._is_active = is_active          # Callable[[], bool] | None
        self._backlog = False                # 上一拍预算用尽、没追平
        # 所有键必须在这里一次性建好：HTTP 线程会 dict(progress) 拷贝，
        # 运行期新增键会触发 "dictionary changed size during iteration"
        self.progress = {
            "active": False, "pct": 100, "files": 0,
            "last_cost_ms": 0, "last_scan": 0, "truncated": 0, "long_line": 0,
            "interval": config.SCAN_INTERVAL, "scans": 0, "backlog": 0,
        }
        self._first = store.get_meta("first_index_done") != "1"
        # 目录枚举缓存：dirpath -> (mtime_ns, [子目录], [.jsonl])
        self._dir_cache = {}
        self._scan_rounds = 0

    # ---------- 有人在看吗 ----------
    def bind_active(self, fn):
        """注入"现在有没有客户端连着"。只存函数，不存 Hub 对象，避免反向依赖 server。"""
        self._is_active = fn
        self.wake()

    def wake(self):
        """任意线程可调：O(1)、不阻塞、不抢锁。"""
        self._wake.set()

    def _active(self):
        fn = self._is_active
        if fn is None:
            return True                      # 未注入 = 恒实时（selftest / bench / --once）
        try:
            return bool(fn())
        except Exception:
            return True                      # 判定失败宁可多扫，绝不悄悄降低实时性

    def _next_interval(self):
        # 上拍没追平说明有积压：先按 1s 追上，别在待机时越落越远
        if self._backlog:
            return config.SCAN_INTERVAL
        return config.SCAN_INTERVAL if self._active() else config.IDLE_INTERVAL

    def _wait_next(self):
        """等待下一拍。clear() 必须先于读状态 —— 这是"不丢唤醒"的关键。"""
        self._wake.clear()
        interval = self._next_interval()
        self.progress["interval"] = interval
        deadline = time.monotonic() + interval
        while not self._stop.is_set():
            left = deadline - time.monotonic()
            if left <= 0 or self._wake.is_set():
                return
            # 分片等待：即便将来有人漏调 wake()，切回实时也不超过一拍
            self._wake.wait(min(left, config.WAIT_SLICE))

    # ---------- 发现 ----------
    def discover(self):
        """找出所有 .jsonl 文件。

        原来每拍都完整 os.walk 一遍：153 个目录、2263 个目录项，实测 36ms；
        而 stat 74 个文件只要 1ms —— 成本几乎全在"枚举目录"本身。

        改成按目录 mtime 增量枚举：增删文件一定会改变所在目录的 mtime，
        所以新文件照样能立刻发现；追加内容由每个文件自己的 size/mtime 判断，
        与本缓存无关（文件列表不变，但仍逐个 stat）。
        每 30 拍强制全量重扫一次，兜住 mtime 精度、网络盘之类的意外。
        """
        self._scan_rounds += 1
        force = (self._scan_rounds % 30 == 0)
        out = []
        for full in self._scan_paths(force):
            try:
                st = os.stat(full)
            except OSError:
                continue
            out.append((full, st, self._project_of(full), self._kind_of(full),
                        self._session_of(full)))
        out.sort(key=lambda x: x[1].st_mtime_ns, reverse=True)  # 活跃文件优先
        return out

    def _scan_paths(self, force):
        root = config.PROJECTS_DIR
        if not os.path.isdir(root):
            self._dir_cache.clear()
            return []
        out, seen = [], set()
        stack = [root]
        while stack:
            d = stack.pop()
            if d in seen:
                continue
            seen.add(d)
            try:
                mtime = os.stat(d).st_mtime_ns
            except OSError:
                continue
            hit = None if force else self._dir_cache.get(d)
            if hit and hit[0] == mtime:
                subdirs, files = hit[1], hit[2]
            else:
                subdirs, files = [], []
                try:
                    with os.scandir(d) as it:
                        for ent in it:
                            try:
                                if ent.is_dir(follow_symlinks=False):
                                    subdirs.append(ent.path)
                                elif ent.name.endswith(".jsonl"):
                                    files.append(ent.path)
                            except OSError:
                                continue
                except OSError:
                    continue
                self._dir_cache[d] = (mtime, subdirs, files)
            out.extend(files)
            stack.extend(subdirs)
        # 目录被删掉了就把它的缓存一起清掉，免得越积越多
        for d in [k for k in self._dir_cache if k not in seen]:
            self._dir_cache.pop(d, None)
        return out

    @staticmethod
    def _project_of(path):
        try:
            rel = os.path.relpath(path, config.PROJECTS_DIR)
        except ValueError:
            return "未知"
        head = rel.split(os.sep)[0]
        return head if head and not head.endswith(".jsonl") else "根目录"

    @staticmethod
    def _kind_of(path):
        return "subagent" if (os.sep + "subagents" + os.sep) in path else "main"

    @staticmethod
    def _session_of(path):
        parts = path.split(os.sep)
        if "subagents" in parts:
            i = parts.index("subagents")
            if i > 0:
                return parts[i - 1]
        return os.path.splitext(os.path.basename(path))[0]

    # ---------- 单文件同步 ----------
    def _sync_one(self, path, st, project, kind, sid, deadline=None):
        row = self.store.get_file(path)
        ino = int(getattr(st, "st_ino", 0) or 0) & 0x7FFFFFFFFFFFFFFF

        # 边界 1：新文件，或被替换（inode 变了 = 同名换了个文件）
        if row is None or (ino and row.get("ino") and ino != row["ino"]):
            if row is not None:
                self.store.delete_events_by_path(path)
            return self._read_from(path, st, 0, project, kind, sid, deadline=deadline)

        offset = row["offset"]

        # 边界 2：文件变短，被截断或重写。必须清空该文件的事件，否则旧记录残留会翻倍
        if st.st_size < offset:
            self.store.delete_events_by_path(path)
            self.progress["truncated"] += 1
            log.warning("文件变短，重建：%s (%d -> %d)", path, offset, st.st_size)
            return self._read_from(path, st, 0, project, kind, sid, err="TRUNCATED", deadline=deadline)

        # 边界 3：尺寸不变，只有 mtime 变 —— 只是被 touch
        if st.st_size == offset:
            if st.st_mtime_ns != row["mtime_ns"]:
                self.store.touch_file(path, st.st_mtime_ns)
            return 0

        return self._read_from(path, st, offset, project, kind, sid, deadline=deadline)

    def _read_from(self, path, st, offset, project, kind, sid, err=None, deadline=None):
        """从 offset 起循环读到文件末尾（或预算用完）。大文件必须循环，不能只读一块。"""
        try:
            fh = open(path, "rb")
        except (PermissionError, FileNotFoundError, OSError) as e:
            self.store.mark_err(path, "LOCKED")
            log.debug("暂时读不到 %s: %s", path, e)
            return 0

        consumed = 0
        try:
            while True:
                want = min(max(0, st.st_size - offset), config.CHUNK)
                if want <= 0:
                    self.store.save_file(path, st, offset, sid, project, kind, err)
                    break

                try:
                    fh.seek(offset)
                    buf = fh.read(want)
                except OSError as e:
                    self.store.mark_err(path, "READ_ERROR")
                    log.debug("读取失败 %s: %s", path, e)
                    break

                nl = buf.rfind(b"\n")

                # 边界 4：没有换行符
                if nl < 0:
                    if len(buf) < config.CHUNK:
                        # 正常：最后一行还在写。offset 不动，下一拍重试
                        self.store.save_file(path, st, offset, sid, project, kind, err)
                        break
                    # 异常：单行超过 4MB，跳到下一个换行，整段丢弃
                    skip = self._skip_line(fh, offset + len(buf), st.st_size)
                    offset = skip
                    self.progress["long_line"] += 1
                    self.store.save_file(path, st, offset, sid, project, kind, "LONG_LINE")
                    log.warning("超长行已跳过：%s @%d", path, offset)
                    if deadline and time.monotonic() > deadline:
                        break
                    continue

                complete = buf[:nl + 1]
                new_offset = offset + nl + 1
                self._consume(complete, path, project, kind, sid, st)
                consumed += new_offset - offset
                offset = new_offset
                self.store.save_file(path, st, offset, sid, project, kind, err)

                if offset >= st.st_size:
                    break
                if deadline and time.monotonic() > deadline:
                    break                       # 下一拍接着读，反复几次就追平
        finally:
            fh.close()
        return consumed

    def _consume(self, complete, path, project, kind, sid, st):
        """解析一段完整的行集合，写库。"""
        fallback_ts = int(st.st_mtime_ns / 1_000_000)
        events, credits = [], []
        for raw in complete.split(b"\n"):
            if not raw:
                continue
            if b'"usage"' not in raw and b'"credit-usage"' not in raw:
                continue                      # 子串预筛，省掉大部分 json.loads
            if raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                continue                      # 坏行跳过，不影响后续
            if not isinstance(rec, dict):
                continue
            ev = extract_event(rec, raw, path, project, kind, sid, fallback_ts)
            if ev:
                events.append(ev)
            cr = extract_credit(rec, fallback_ts)
            if cr:
                credits.append(cr)

        if events:
            self.store.insert_events(events)
        if credits:
            self.store.insert_credits(credits)

    @staticmethod
    def _skip_line(fh, pos, size):
        """从 pos 向后找换行，最多走 MAX_LINE，找不到就硬切。"""
        end = min(size, pos + config.MAX_LINE)
        while pos < end:
            fh.seek(pos)
            chunk = fh.read(min(config.CHUNK, end - pos))
            if not chunk:
                break
            i = chunk.find(b"\n")
            if i >= 0:
                return pos + i + 1
            pos += len(chunk)
        return min(pos, size)

    # ---------- 主循环 ----------
    def scan_once(self, budget_ms=None):
        t0 = time.monotonic()
        deadline = (t0 + budget_ms / 1000.0) if budget_ms else None
        entries = self.discover()
        self.progress["files"] = len(entries)
        seen = set()
        complete = True

        for path, st, project, kind, sid in entries:
            if deadline and time.monotonic() > deadline:
                complete = False            # 没走完，绝不能清理"没见到"的文件
                break
            seen.add(path)
            try:
                self._sync_one(path, st, project, kind, sid, deadline)
            except Exception:               # 单文件异常不该拖垮整个索引
                log.exception("索引失败：%s", path)

        if complete:
            for p in self.store.all_paths() - seen:
                self.store.delete_file(p)

        cost = int((time.monotonic() - t0) * 1000)
        self.progress["last_cost_ms"] = cost
        self.progress["last_scan"] = int(time.time() * 1000)
        self.progress["scans"] += 1
        self._backlog = not complete
        self.progress["backlog"] = 0 if complete else 1
        return cost

    def run(self):
        if not os.path.isdir(config.PROJECTS_DIR):
            log.error("数据目录不存在：%s", config.PROJECTS_DIR)
            self.progress["active"] = False
            return

        if self._first:
            self.progress["active"] = True
            log.info("首次索引开始 …")
            t0 = time.monotonic()
            total_files = len(self.discover())
            done = 0
            for path, st, project, kind, sid in self.discover():
                try:
                    self._sync_one(path, st, project, kind, sid)
                except Exception:
                    log.exception("索引失败：%s", path)
                done += 1
                self.progress["pct"] = int(done * 100 / max(1, total_files))
                self.progress["active"] = done < total_files
            self.store.set_meta("first_index_done", "1")
            # 首次索引改了 files 表（totals.files 用它），补一次版本
            self.store.bump()
            self.progress["pct"] = 100
            self.progress["active"] = False
            log.info("首次索引完成：%d 文件，%.1fs", total_files, time.monotonic() - t0)

        self.progress["active"] = False
        last_ckpt = time.monotonic()
        while not self._stop.is_set():
            try:
                self.scan_once(budget_ms=config.SCAN_BUDGET_MS)
            except Exception:
                log.exception("扫描异常")
            now = time.monotonic()
            # 必须按时间而不是按拍数：待机 5s 一拍时 tick % 300 会变成 25 分钟一次
            if now - last_ckpt >= config.CHECKPOINT_INTERVAL:
                self.store.checkpoint()
                last_ckpt = now
            self._wait_next()

    def stop(self):
        self._stop.set()
        self._wake.set()          # 别让退出卡在 5 秒的等待里
