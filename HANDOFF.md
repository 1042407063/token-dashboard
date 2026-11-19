# 项目状态

当前版本见 `VERSION`。工作区改动以 `git status` 为准。

## 约定

- 后端只用 Python 标准库，不引入第三方依赖
- 对 `~/.workbuddy` 全程只读；索引与运行时状态只写 `data/`
- 服务只监听 `127.0.0.1`，不提供远程访问
- 平台相关代码集中在 `panel.py` 与 `.bat`；核心模块保持平台无关
- 用户可改配置放 `data/settings.json`，不要写死在 `config.py`
- 改前端资源时同步 bump `index.html` 里的 `?v=`，避免浏览器吃缓存
- `data/` 已被 `.gitignore` 忽略，不进仓库、不进发行包

## 验证

```
python tools/selftest.py     # 边界用例
python tools/bench.py        # 性能基线
python tools/pack.py         # 发行包
```

## 仓库

`git@github.com:1042407063/token-dashboard.git`（用户名 1042407063）。
