# -*- coding: utf-8 -*-
"""把用量摘要推到企业微信群机器人。

设计前提：
  * 只发出站 HTTPS 请求，不监听任何端口 —— 没有公网入口，也就没有被扫描的风险。
  * 数字与面板完全同源：复用 store / aggregate，不另写一套统计。
  * 面板没在跑时先补一次扫描，保证推的数字不是几小时前的。

用法：
  python notify.py --set-webhook https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
  python notify.py --test          # 发一条测试消息，确认能收到
  python notify.py --now           # 立刻发今天的真实摘要
  python notify.py --loop          # 每天定时发（默认 22:00）
  python notify.py --at 21:30 --loop
  python notify.py --titles off    # 不推送会话标题（默认关，避免客户名外泄）
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

import aggregate
import config
import indexer
import store as store_mod

CFG_PATH = os.path.join(config.DATA_DIR, "notify.json")
DEFAULTS = {"webhook": "", "hour": 22, "minute": 0, "include_titles": False}


# ---------------- 配置 ----------------
def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f) or {})
    except (OSError, ValueError):
        pass
    return cfg


def save_cfg(cfg):
    config.ensure_dirs()
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------- 取数 ----------------
def panel_alive():
    import urllib.request as u
    try:
        with u.urlopen("http://127.0.0.1:%d/healthz" % config.DEFAULT_PORT, timeout=0.4) as r:
            return r.status == 200
    except Exception:
        return False


def build_agg():
    st = store_mod.Store()
    # 面板在跑就不用重复扫；没在跑补一次，避免推几小时前的旧数
    if not panel_alive():
        try:
            indexer.Indexer(st).scan_once(budget_ms=3000)
        except Exception as e:
            print("补扫失败（继续用现有索引）：%s" % e, file=sys.stderr)
    return aggregate.Aggregator(st)


def fmt(n):
    n = float(n or 0)
    if n >= 1e8:
        return "%.2f 亿" % (n / 1e8)
    if n >= 1e4:
        return "%.1f 万" % (n / 1e4)
    return "%d" % n


# ---------------- 组装消息 ----------------
def build_message(agg, cfg):
    s = agg.summary()
    t, w, c = s["today"], s["w30"], s["credit"]
    lines = []

    lines.append("**WorkBuddy 用量日报**  %s" % t["day"])
    lines.append("> 今日 <font color=\"info\">%s</font> token · %d 轮 · 缓存命中 %.1f%%"
                 % (fmt(t["tokens"]), t["turns"], t.get("cache_hit_rate") or 0))

    peak = t.get("peak_hour")
    delta = t.get("delta_vs_yesterday")
    extra = []
    if peak is not None:
        extra.append("峰值 %d 时" % peak)
    if delta is not None:
        # delta_vs_yesterday 本身就是百分数（如 -97.7），不要再乘 100
        extra.append("较昨日同期 %+.1f%%" % delta)
    if extra:
        lines.append("> " + " · ".join(extra))

    lines.append("")
    lines.append("**近 30 天** 累计 %s（%d 天有记录），活跃日日均 %s"
                 % (fmt(w["tokens"]), w["active_days"], fmt(w["avg_active"])))

    tops = s.get("top_sessions") or []
    if tops and cfg.get("include_titles"):
        lines.append("**最贵会话** %s（%s）" % (tops[0]["title"] or "未命名", fmt(tops[0]["tokens"])))

    if c.get("available"):
        ratio = c.get("mapped_ratio")
        lines.append("**credit** %s（%s）" % (c.get("total"),
                                              ("覆盖 %s%%" % ratio) if ratio is not None else c.get("mode")))
    else:
        lines.append("**credit** 暂不可用")

    lines.append("")
    lines.append("<font color=\"comment\">数据来自本机转录，仅在本机汇总，未上传内容</font>")
    return "\n".join(lines)


# ---------------- 发送 ----------------
def send(webhook, content):
    body = json.dumps({"msgtype": "markdown", "markdown": {"content": content}},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook, data=body, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        obj = json.loads(raw)
    except ValueError:
        return False, "返回不是 JSON：%s" % raw[:200]
    if obj.get("errcode") == 0:
        return True, "ok"
    return False, "errcode=%s errmsg=%s" % (obj.get("errcode"), obj.get("errmsg"))


def require_webhook(cfg):
    url = (cfg.get("webhook") or "").strip()
    if not url:
        print("还没配置 webhook。先执行：\n"
              "  python notify.py --set-webhook https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",
              file=sys.stderr)
        sys.exit(2)
    if "qyapi.weixin.qq.com" not in url:
        print("这看起来不是企业微信机器人地址，仍然继续。", file=sys.stderr)
    return url


# ---------------- 定时 ----------------
def run_loop(cfg):
    h, m = int(cfg["hour"]), int(cfg["minute"])
    print("已进入定时推送：每天 %02d:%02d。Ctrl+C 退出。" % (h, m))
    url = require_webhook(cfg)
    while True:
        now = datetime.datetime.now()
        nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if nxt <= now:
            nxt += datetime.timedelta(days=1)
        wait = (nxt - now).total_seconds()
        print("下次推送：%s（%.1f 小时后）" % (nxt.strftime("%m-%d %H:%M"), wait / 3600))
        time.sleep(max(30, wait))
        try:
            msg = build_message(build_agg(), cfg)
            ok, info = send(url, msg)
            print("[%s] 推送%s（%s）" % (datetime.datetime.now().strftime("%H:%M:%S"),
                                        "成功" if ok else "失败", info))
        except Exception as e:
            print("推送异常：%s" % e, file=sys.stderr)
        time.sleep(65)   # 跨过这一分钟，避免重复触发


# ---------------- 入口 ----------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="把 token 用量推到企业微信群机器人")
    ap.add_argument("--set-webhook", metavar="URL", help="保存机器人 webhook 地址")
    ap.add_argument("--at", metavar="HH:MM", help="定时时间，默认 22:00")
    ap.add_argument("--titles", choices=["on", "off"], help="是否推送会话标题（默认 off）")
    ap.add_argument("--test", action="store_true", help="发一条测试消息")
    ap.add_argument("--now", action="store_true", help="立刻发送今天的摘要")
    ap.add_argument("--loop", action="store_true", help="每天定时推送")
    ap.add_argument("--show", action="store_true", help="查看当前配置")
    args = ap.parse_args(argv)

    cfg = load_cfg()

    if args.set_webhook:
        cfg["webhook"] = args.set_webhook.strip()
        save_cfg(cfg)
        print("已保存 webhook 到 %s" % CFG_PATH)
    if args.at:
        hh, _, mm = args.at.partition(":")
        cfg["hour"], cfg["minute"] = int(hh), int(mm or 0)
        save_cfg(cfg)
        print("已设置每天 %02d:%02d 推送" % (cfg["hour"], cfg["minute"]))
    if args.titles:
        cfg["include_titles"] = (args.titles == "on")
        save_cfg(cfg)
        print("会话标题推送：%s" % ("开" if cfg["include_titles"] else "关"))

    if args.show:
        shown = dict(cfg)
        if shown.get("webhook"):
            shown["webhook"] = shown["webhook"][:52] + "…"
        print(json.dumps(shown, ensure_ascii=False, indent=2))
        return 0

    if args.test:
        url = require_webhook(cfg)
        ok, info = send(url, "**测试消息**\n> 如果你在手机上看到这条，说明群机器人配好了。")
        print("发送%s：%s" % ("成功" if ok else "失败", info))
        return 0 if ok else 1

    if args.now:
        url = require_webhook(cfg)
        msg = build_message(build_agg(), cfg)
        ok, info = send(url, msg)
        print("发送%s：%s" % ("成功" if ok else "失败", info))
        if not ok:
            print("--- 消息内容 ---\n%s" % msg)
        return 0 if ok else 1

    if args.loop:
        run_loop(cfg)
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
