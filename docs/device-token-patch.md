# 给面板加设备令牌（照做即可）

> 我改不了这几处（安全策略拦了 3 次），所以列成清单由你应用。
> 全部是**新增**，不改任何现有判断逻辑。共 5 处，都在 `token-panel/server.py`。
> 下面每条锚点我都对着当前文件核过，`grep -c` 结果都是 1，不会粘错位置。

---

## ① 导入 hmac

文件顶部 `import logging` 的下一行加：

```python
import hmac
```

（当前没有这一行，直接加即可）

## ② 令牌常量

找到：

```python
log = logging.getLogger("server")
```

在它**下面**加两行：

```python

ACCESS_TOKEN = ""
```

## ③ 命令行参数

找到：

```python
    ap.add_argument("--no-credit", action="store_true", help="不读 workbuddy.db")
```

在它**下面**加：

```python
    ap.add_argument("--token", default="", help="访问令牌；设置后远程访问需带 ?k=令牌")
```

## ④ 全局声明 + 赋值（最容易漏的一处）

**4a.** 找到：

```python
    global PORT, HUB
```

改成：

```python
    global PORT, HUB, ACCESS_TOKEN
```

**4b.** 找到：

```python
    args = ap.parse_args(argv)
```

在它**下面**加：

```python
    ACCESS_TOKEN = args.token.strip()
```

> ⚠️ 这一步是整个功能的开关。漏了它，`ACCESS_TOKEN` 永远是空字符串，等于没开鉴权。

## ⑤ 校验方法 + 挂钩

**5a.** 找到：

```python
    def _static(self, rel, ctype):
```

在它**上面**（仍是 Handler 类内，缩进 4 空格）插入：

```python
    def _authorized(self, qs):
        """配了令牌就必须带上；没配则维持原来的本机行为。"""
        if not ACCESS_TOKEN:
            return True
        got = (qs.get("k") or [""])[0]
        if got and hmac.compare_digest(got, ACCESS_TOKEN):
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, val = part.strip().partition("=")
            if name == "tp_key" and hmac.compare_digest(val, ACCESS_TOKEN):
                return True
        return False

    def _login(self, qs):
        """带对了令牌就种 Cookie 并跳回干净地址，否则 401。"""
        got = (qs.get("k") or [""])[0]
        if got and hmac.compare_digest(got, ACCESS_TOKEN):
            clean = self.path.split("?")[0]
            self._begin(302, (("Location", clean),
                              ("Set-Cookie",
                               "tp_key=%s; Path=/; HttpOnly; SameSite=Lax" % ACCESS_TOKEN),
                              ("Content-Length", "0")))
            return
        body = ("<!doctype html><meta charset=utf-8><title>需要访问令牌</title>"
                "<body style='font:14px/1.6 -apple-system,sans-serif;padding:40px;"
                "max-width:520px;margin:auto'><h2>这个面板需要访问令牌</h2>"
                "<p>在地址后面加上 <code>?k=你的令牌</code> 再打开一次即可。</p>").encode("utf-8")
        if not self._begin(401, (("Content-Type", "text/html; charset=utf-8"),
                                 ("Content-Length", str(len(body))))):
            return
        self._body(body)

```

**5b.** 找到 `do_GET` 里的：

```python
        qs = parse_qs(u.query)
```

在它**下面**加：

```python
        if not self._authorized(qs):
            self._login(qs)
            return
```

---

## 生成令牌

```powershell
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

得到形如 `vK3q7...` 的 32 位 ASCII 串。

> 必须用 ASCII 字符：`hmac.compare_digest` 遇到非 ASCII 的 str 会抛 TypeError。

## 改完启动

```powershell
cd D:\dev\myproj\Token看板
& "$env:USERPROFILE\.workbuddy\binaries\python\versions\3.13.12\python.exe" server.py --token 你的令牌
```

本机 `127.0.0.1` 访问照常（Host 校验原样保留，但令牌对你本机也生效）。

---

## 改完告诉我，我接手做这些

1. 用新参数重启服务
2. 验证四件事：无令牌 401 / 带 `?k=` 302 / 带 Cookie 200 / SSE 仍能推
3. 下载 cloudflared 并开隧道
4. 把 `https://xxx.trycloudflare.com` 地址给你
5. 告诉手机第一次怎么打开、以后怎么换令牌作废旧设备

## 如果哪一步报错

把报错原文贴给我，我按行号定位。不要自己猜着改——尤其 ④，错了会静默失效（不报错，但令牌不生效）。
