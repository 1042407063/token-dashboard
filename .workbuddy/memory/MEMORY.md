# Token 看板 · 长期约定

## 仓库与双机交接

- 项目全程由 AI 开发，家里电脑与公司电脑交接着开发。
- `.workbuddy/memory/` 下的每日工作日志**必须随仓库上传**，用来保证开发连贯性：
  不要忽略、不要删除，新日志也要记得 `git add`。
- 仓库只上传源码、文档、脚本、前端本地依赖与上述工作日志；
  不上传构建产物、发行包、运行时数据、索引库、日志、pid、缓存。
- `.gitignore` 覆盖：`dist/ build/ *.zip *.7z *.rar *.tar.gz data/*
  __pycache__ *.db *.log *.pid *.pyc`。

## 技术约定

- 后端只用 Python 标准库；`web/vendor/echarts.min.js` 随仓库保留（断网可用）。
- 服务只监听 `127.0.0.1`，不提供远程访问。
- 用户可改配置放 `data/settings.json`；账号名可在面板顶栏 `✎` 直接改。
- 平台相关代码集中在 `panel.py` 与 `.bat`；核心模块保持平台无关。
- 发行包用 `python tools/pack.py` 生成，包内文件名全 ASCII、不含 `data/`。
