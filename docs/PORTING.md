# 移植与升级

## 打包

    python tools/pack.py

生成 `dist/token-panel-<版本>.zip`。包里只有源码、`web/`、`tools/` 和文档，
不含 `data/`（索引、日志、配置属于单台机器）。包内文件名全部是 ASCII，
常见解压工具都不会乱码。

## 在另一台电脑运行

前置：装过 WorkBuddy 并聊过天；Python 3.7+（WorkBuddy 自带一份）。

    Windows         双击 start-panel.bat
    macOS / Linux   python3 panel.py start（或 sh panel.sh start）

后端只用标准库，不需要 `pip install`，不需要数据库服务，不需要 npm；
前端 ECharts 已随包提供，断网可用。平台相关代码只在 `panel.py`
和几个 Windows 批处理里，核心模块不含任何平台判断。

数据源不在默认的 `~/.workbuddy` 时：

    python panel.py start --workbuddy-dir /path/to/.workbuddy

也可以设置环境变量 `WORKBUDDY_DIR`。

## 升级

把新包里的代码文件覆盖到旧目录，**保留 `data/`**（索引与 `settings.json`）。
索引库带 schema 版本号，结构变化会自动增量升级，不用手工删库。

## 限制

面板只读本机的 `~/.workbuddy`，两台电脑的数据不会合并；各自统计各自的用量。
