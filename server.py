# -*- coding: utf-8 -*-
"""HTTP 服务：路由 + 静态文件 + SSE 推送 + 单实例。

只做分发，业务逻辑全在 aggregate / credit 里。
"""

import argparse
import json
import logging
import os
import queue
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import aggregate
import config
import credit
import indexer as indexer_mod
import store as store_mod

log = logging.getLogger("server")

# BrokenPipeError / ConnectionResetError / ConnectionAbortedError 的公共父类。
# 关页面、刷新、超时断开都会落在这里 —— 属正常，不该记成 ERROR 堆栈。
CLIENT_GONE = ConnectionError

STORE = None
INDEXER = None
AGG = None
HUB = None
START_TS = time.time()
CREDIT_STATE = {"ok": False, "reason": "尚未采集", "lag": False}
PORT = config.DEFAULT_PORT

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
    "/static/vendor/echarts.min.js": ("vendor/echarts.min.js",
                                      "application/javascript; charset=utf-8"),
}


# ---------------- SSE ----------------
class Hub:
    """SSE 客户端集合，同时也是"有没有人在看"的唯一事实来源。"""

    def __init__(self, on_change=None):
        self.clients = set()
        self.lock = threading.Lock()
        self._on_change = on_change      # 只允许 O(1) 非阻塞操作

    def active(self):
        with self.lock:
            return bool(self.clients)

    def count(self):
        with self.lock:
            return len(self.clients)

    def _notify(self):
        fn = self._on_change
        if fn:
            try:
                fn()
            except Exception:
                log.debug("active 状态回调失败", exc_info=True)

    def attach(self):
        q = queue.Queue(maxsize=8)
        with self.lock:
            if len(self.clients) >= config.MAX_SSE_CLIENTS:
                return None
            first = not self.clients
            self.clients.add(q)
        if first:
            self._notify()               # 必须在锁外调用，避免锁序反转
        return q

    def detach(self, q):
        with self.lock:
            self.clients.discard(q)
            last = not self.clients
        if last:
            self._notify()

    def publish(self, payload):
        with self.lock:
            targets = list(self.clients)
        for q in targets:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass                     # 慢客户端丢帧，不阻塞广播


def _vframe(v):
    """SSE 只推"新鲜度"，不推数据。

    数据走 /api/bundle —— 因为 series 的天数、heatmap 的周数是客户端参数，
    服务端预知不了，广播完整快照必然会漏掉这几张图（这正是改造前的老问题）。
    """
    prog = INDEXER.progress if INDEXER else {}
    return json.dumps({
        "v": v,
        "at": int(time.time() * 1000),
        "i": {"a": bool(prog.get("active")), "p": prog.get("pct")},
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def broadcast_loop():
    last_v = None
    last_pct = None
    last_ping = 0.0
    last_day = time.strftime("%Y-%m-%d")
    last_sid = None
    last_ws = aggregate.workspace_names()[1]
    while True:
        t0 = time.monotonic()
        try:
            if HUB.active():
                # 跨日：没有新事件，"今日"也会从有数变成 0，必须让前端知道
                day = time.strftime("%Y-%m-%d")
                if day != last_day:
                    STORE.bump()
                    last_day = day

                # 项目空间改名只动 workspace-display-names.json，不写 index.db，
                # seq 不会涨；检测到变化就 bump，前端才会立刻重拉新标签
                ws = aggregate.workspace_names()[1]
                if ws != last_ws:
                    last_ws = ws
                    STORE.bump()

                # 会话切换：心跳可能先于新事件把"当前会话"顶上去
                sid = AGG.resolve_session().get("sid")
                if sid != last_sid:
                    last_sid = sid
                    STORE.bump()

                v = STORE.seq()
                pct = INDEXER.progress.get("pct")
                now = time.monotonic()
                # 变了立刻推；没变就按心跳间隔重发一次当前版本，丢帧的客户端能自愈
                if v != last_v or pct != last_pct or now - last_ping >= config.PING_INTERVAL:
                    HUB.publish(_vframe(v))
                    last_v, last_pct, last_ping = v, pct, now
        except Exception:
            log.exception("广播失败")
        time.sleep(max(0.2, config.TICK - (time.monotonic() - t0)))


def credit_loop():
    global CREDIT_STATE
    while True:
        try:
            st = credit.run_once(STORE)
            # 没变化时不必动版本号，否则每 30 秒白算一遍聚合
            if st.get("ok") and not st.get("skipped"):
                STORE.bump()             # credit 写库时已在 Store 里 bump 过，这里再兜一次
            if st.get("skipped"):
                log.debug("credit 未变化，跳过（%sms）", st.get("cost_ms"))
            elif not st.get("ok") and st.get("reason") != CREDIT_STATE.get("reason"):
                log.warning("credit 不可用：%s", st.get("reason"))   # 同样的原因只报一次
            CREDIT_STATE = st
            # 账号时间线：只读 events/accounts，与 credit 是否可用无关
            try:
                cur = AGG.resolve_session()
                sw = credit.track_switch(STORE, cur.get("sid"), cur.get("source"))
                if sw.get("logged"):
                    log.info("账号切换：%s -> %s",
                             (sw.get("from") or "")[:8], (sw.get("to") or "")[:8])
            except Exception:
                log.debug("账号时间线记录失败", exc_info=True)
        except Exception as e:          # credit 永远不许拖垮主流程
            CREDIT_STATE = {"ok": False, "reason": "采集异常：%s" % e, "lag": False}
            log.exception("credit 采集异常")
        time.sleep(30)


def build_bundle(rng="today", days=30, weeks=52, acct=None):
    """一次请求拿全整页需要的数据。

    v 在计算**之前**取：如果算到一半有新数据写进来，返回的 v 会偏小，
    客户端下次收到更大的 v 会再拉一次 —— 只会"迟一拍"，绝不会"报新版本配旧数据"。
    """
    acct = AGG.normalize_acct(acct)      # 白名单化：挡住随机参数把缓存 key 打爆
    v = STORE.seq()
    return {
        "v": v,
        "acct": acct,
        "accounts": AGG.accounts(),
        "summary": AGG.summary(INDEXER, acct),
        "today": AGG.today(acct),
        "session": AGG.session(),
        "period": AGG.period(rng, acct),
        "series": AGG.series(days, acct),
        "heat": AGG.heatmap(weeks, acct),
    }


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "token-panel"
    _client_gone = False       # 本次请求内客户端已走，后续一律不写
    _responded = False         # 响应头已发出，禁止再补 500

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- 写响应原语 --
    # 关页面/刷新/超时断开都属正常，不该记成 ERROR 堆栈，更不该在已发过 200 之后
    # 再往一个坏 socket 上补 500。所有写出口统一走这两个方法。
    def _drain_gone(self, exc):
        self._client_gone = True
        self.close_connection = True
        log.debug("客户端提前断开（%s）：%s", type(exc).__name__, self.path)

    def _begin(self, code, headers=()):
        try:
            self.send_response(code)
            for k, val in headers:
                self.send_header(k, val)
            self.end_headers()
        except CLIENT_GONE as e:
            self._drain_gone(e)
            return False
        self._responded = True
        return True

    def _body(self, body, flush=False):
        if self._client_gone:
            return
        try:
            self.wfile.write(body)
            if flush:
                self.wfile.flush()
        except CLIENT_GONE as e:        # 只吞连接族，其余照常上抛
            self._drain_gone(e)

    # -- 工具 --
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        if not self._begin(code, (("Content-Type", "application/json; charset=utf-8"),
                                  ("Content-Length", str(len(body))),
                                  ("Cache-Control", "no-store"))):
            return
        self._body(body)

    def _host_ok(self):
        """回环 + RFC1918 私有网段；公网 IP / 伪造域名一律 403。

        HOST 绑 0.0.0.0 之后，这层是挡 DNS rebinding 与非局域网来源的主要防线。
        """
        raw = (self.headers.get("Host") or "").strip().lower()
        if raw.startswith("["):                       # IPv6 形如 [::1]:8770
            host = raw[1:raw.index("]")] if "]" in raw else raw
        else:
            host = raw.split(":")[0]
        if host in ("127.0.0.1", "localhost", "::1"):
            return True
        parts = host.split(".")
        if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            return False
        a, b = int(parts[0]), int(parts[1])
        return a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31)

    def _static(self, rel, ctype):
        path = os.path.join(config.WEB_DIR, rel)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._json(404, {"error": "not_found", "path": rel})
            return
        if not self._begin(200, (("Content-Type", ctype),
                                 ("Content-Length", str(len(body))),
                                 ("Cache-Control", "max-age=604800"))):
            return
        self._body(body)

    def _page(self):
        """首页把首屏数据直接注进去，避免打开瞬间一片空白。"""
        path = os.path.join(config.WEB_DIR, "index.html")
        try:
            with open(path, "r", encoding="utf-8") as f:
                html = f.read()
        except OSError:
            self._json(404, {"error": "not_found", "path": "index.html"})
            return
        try:
            v0 = STORE.seq()          # 先取版本再算，宁可"迟一拍"也不"新版本配旧数据"
            boot = {
                "v": v0,
                "acct": None,
                "accounts": AGG.accounts(),
                "summary": AGG.summary(INDEXER),
                "today": AGG.today(),
                "session": AGG.session(),
                "period": AGG.period("today"),
                "series": AGG.series(30),
                "heat": AGG.heatmap(52),
            }
            blob = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
        except Exception:
            log.exception("首屏数据生成失败")
            blob = "null"
        body = html.replace("__BOOT__", blob).encode("utf-8")
        if not self._begin(200, (("Content-Type", "text/html; charset=utf-8"),
                                 ("Content-Length", str(len(body))),
                                 ("Cache-Control", "no-store"))):
            return
        self._body(body)
        INDEXER.wake()          # 首屏数据是请求时算的，唤醒索引尽快追上


    # -- 路由 --
    def do_GET(self):
        self._client_gone = False
        self._responded = False
        if not self._host_ok():
            self._json(403, {"error": "bad_host"})
            return
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)

        try:
            if path == "/healthz":
                self._json(200, {
                    "app": config.APP_NAME, "ok": True,
                    "pid": os.getpid(), "port": PORT,
                    "indexing": bool(INDEXER.progress.get("active")),
                    "uptime_s": int(time.time() - START_TS),
                    "credit": "ok" if CREDIT_STATE.get("ok") else "degraded",
                    # 下面四个是"降频是否生效"的仪表：不开浏览器也能核对
                    "clients": HUB.count(),
                    "scan_interval": INDEXER.progress.get("interval"),
                    "scans": INDEXER.progress.get("scans"),
                    "backlog": INDEXER.progress.get("backlog"),
                    # 版本号：静默时它不该变；在 WorkBuddy 里发一条消息它必须变
                    "seq": STORE.seq(),
                    "cache": AGG.cache_stats(),
                })
            elif path == "/api/summary":
                self._json(200, AGG.summary(INDEXER, _acct(qs)))
            elif path == "/api/today":
                self._json(200, AGG.today(_acct(qs)))
            elif path == "/api/series":
                days = _int(qs, "days", 30, (7, 365))
                self._json(200, {"days": days, "series": AGG.series(days, _acct(qs))})
            elif path == "/api/heatmap":
                weeks = _int(qs, "weeks", 52, (4, 104))
                self._json(200, {"weeks": weeks, "days": AGG.heatmap(weeks, _acct(qs))})
            elif path == "/api/breakdown":
                by = (qs.get("by") or ["model"])[0]
                by = by if by in ("model", "project", "account") else "model"
                days = _int(qs, "days", 0, (0, 365))
                self._json(200, {"by": by, "days": days or None,
                                 "items": AGG.breakdown(by, days or None, _acct(qs))})
            elif path == "/api/bundle":
                self._json(200, build_bundle(
                    (qs.get("range") or ["today"])[0],
                    _int(qs, "days", 30, (7, 365)),
                    _int(qs, "weeks", 52, (4, 104)),
                    _acct(qs),
                ))
            elif path == "/api/accounts":
                self._json(200, {
                    "ok": True,
                    "accounts": AGG.accounts(),
                    "switches": STORE.recent_switches(20),
                    "audit": AGG.audit(),
                    "credit": AGG.credits(),
                })
            elif path == "/api/session":
                self._json(200, AGG.session())
            elif path == "/api/period":
                self._json(200, AGG.period((qs.get("range") or ["today"])[0], _acct(qs)))
            elif path == "/api/credits":
                self._json(200, {"status": CREDIT_STATE,
                                 "stats": AGG.credits(acct=_acct(qs))})
            elif path == "/api/status":
                st = AGG.status(INDEXER)
                st["credit_state"] = CREDIT_STATE
                st["seq"] = STORE.seq()
                st["cache"] = AGG.cache_stats()
                self._json(200, st)
            elif path == "/api/stream":
                self._stream()
            elif path == "/favicon.ico":
                self._begin(204)
            elif path in ("/", "/index.html"):
                self._page()
            elif path in STATIC:
                rel, ctype = STATIC[path]
                self._static(rel, ctype)
            else:
                self._json(404, {"error": "not_found", "path": path})
        except CLIENT_GONE as e:                 # 必须排在 Exception 之前
            self._drain_gone(e)                  # 关页面/刷新/超时断开，属正常
        except Exception as e:
            log.exception("请求处理失败：%s", self.path)
            if not self._responded and not self._client_gone:
                try:
                    self._json(500, {"error": "internal", "detail": str(e)})
                except Exception:
                    pass

    def _stream(self):
        q = HUB.attach()
        if q is None:
            self._json(503, {"error": "too_many_clients"})
            return
        try:
            if not self._begin(200, (
                    ("Content-Type", "text/event-stream; charset=utf-8"),
                    ("Cache-Control", "no-store"),
                    ("Connection", "keep-alive"),
                    ("X-Accel-Buffering", "no"))):
                return
            self.close_connection = False
            self._body(b": hello\n\n", flush=True)
            # 一连上就报当前版本，否则新客户端要等下一次数据变化
            self._body(b"event: v\ndata: " + _vframe(STORE.seq()) + b"\n\n", flush=True)
            while not self._client_gone:
                try:
                    data = q.get(timeout=15)
                    self._body(b"event: v\ndata: " + data + b"\n\n", flush=True)
                except queue.Empty:
                    self._body(b": ping\n\n", flush=True)
        finally:
            HUB.detach(q)                        # 无论怎么退出都必须 detach
            self.close_connection = True


class PanelServer(ThreadingHTTPServer):
    """socketserver 默认会把任何异常直接打到 stderr。
    连接中断（关页面、断网）属于正常，降为 debug；真异常仍完整记录。"""
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, CLIENT_GONE):
            log.debug("连接中断（%s）：%s", client_address, exc)
            return
        log.exception("连接处理异常：%s", client_address)


def _acct(qs):
    """query string 里的 acct -> 白名单化后的值（None / __none__ / 真实 user_id）。

    不白名单化的话，随机参数会不断进入 Aggregator 的 memo key，而 memo 从不淘汰。
    """
    raw = (qs.get("acct") or [None])[0]
    return AGG.normalize_acct(raw) if isinstance(raw, str) else None


def _int(qs, key, default, bounds):
    try:
        v = int((qs.get(key) or [default])[0])
    except (TypeError, ValueError):
        return default
    lo, hi = bounds
    return max(lo, min(hi, v))


# ---------------- 启动 ----------------
def _healthy(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=0.4) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == config.APP_NAME
    except Exception:
        return False


def _bindable(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((config.HOST, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_port(preferred):
    for p in range(preferred, preferred + config.PORT_SPAN):
        if _healthy(p):
            return "reuse", p
        if _bindable(p):
            return "free", p
    return "free", preferred


def build(refresh=False):
    global STORE, INDEXER, AGG
    STORE = store_mod.Store()
    if refresh or not STORE.quick_check():
        for sfx in ("", "-wal", "-shm"):
            try:
                os.remove(config.INDEX_DB + sfx)
            except OSError:
                pass
    STORE.init_schema()
    INDEXER = indexer_mod.Indexer(STORE)
    AGG = aggregate.Aggregator(STORE)


def main(argv=None):
    global PORT, HUB
    ap = argparse.ArgumentParser(description="WorkBuddy Token 实时面板")
    ap.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    ap.add_argument("--open", action="store_true", help="启动后打开浏览器")
    ap.add_argument("--once", action="store_true", help="只建索引并打印摘要，不起服务")
    ap.add_argument("--rebuild", action="store_true", help="丢弃索引重建")
    ap.add_argument("--no-credit", action="store_true", help="不读 workbuddy.db")
    args = ap.parse_args(argv)

    config.setup_console()
    config.ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler(config.LOG_PATH, encoding="utf-8"),
        ],
    )
    try:
        logging.getLogger().addHandler(logging.StreamHandler(sys.stderr))
    except Exception:
        pass

    build(refresh=args.rebuild)

    if args.once:
        t0 = time.time()
        INDEXER.scan_once()
        if not args.no_credit:
            credit.run_once(STORE)
        s = AGG.summary(INDEXER)
        print("首次索引 %.2fs / %d 文件 / %d 条记录" % (
            time.time() - t0, s["totals"]["files"], s["totals"]["events"]))
        print("累计 token : {:,}".format(s["all_time"]["tokens"]))
        print("输入       : {:,}（其中缓存 {:,}）".format(
            s["all_time"]["input"], s["all_time"]["cache_read"]))
        print("输出       : {:,}".format(s["all_time"]["output"]))
        print("轮次       : {:,} 轮 / {:,} 次模型调用".format(
            s["all_time"]["turns"], s["all_time"]["requests"]))
        print("今日       : {:,} token / {} 轮".format(
            s["today"]["tokens"], s["today"]["turns"]))
        print("本会话     : {:,} token / {} 轮 ({})".format(
            s["session"]["tokens"] or 0, s["session"]["turns"] or 0,
            s["session"].get("source")))
        print("缓存命中率 : {}%".format(s["all_time"]["cache_hit_rate"]))
        c = s["credit"]
        print("credit     : {} ({})".format(
            c["total"] if c["available"] else "不可用", c["mode"]))
        au = AGG.audit()
        rate = (100.0 * au["tokens_attributed"] / au["tokens"]) if au["tokens"] else 0.0
        print("账号       : {} 个 / 归属 {:.1f}% / 未知会话 {} / 守恒 {}".format(
            au["accounts"], rate, au["sessions_unknown"],
            "ok" if au["balanced"] else "FAIL"))
        return 0

    action, port = pick_port(args.port)
    if action == "reuse":
        url = "http://127.0.0.1:%d/" % port
        print("面板已在运行：%s" % url)
        if args.open:
            webbrowser.open(url)
        return 0

    PORT = port
    STORE.set_meta("port", port)
    with open(config.PID_PATH, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    HUB = Hub(on_change=INDEXER.wake)    # 有人连上/断开时立刻唤醒索引线程
    INDEXER.bind_active(HUB.active)      # 注入"有没有人在看"，Indexer 不反向依赖 server
    INDEXER.start()
    threading.Thread(target=broadcast_loop, daemon=True, name="bcast").start()
    if not args.no_credit:
        threading.Thread(target=credit_loop, daemon=True, name="credit").start()

    httpd = PanelServer((config.HOST, port), Handler)
    url = "http://127.0.0.1:%d/" % port
    log.info("面板已启动：%s", url)
    print("面板已启动：%s" % url)

    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("收到中断，退出")
    finally:
        INDEXER.stop()
        httpd.shutdown()
        try:
            os.remove(config.PID_PATH)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
