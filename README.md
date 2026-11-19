# token-panel · WorkBuddy Token 用量面板

一个只在本机运行的看板：读取 WorkBuddy 的会话转录，实时统计 token 与积分消耗，在浏览器里展示。
纯 Python 标准库，不装依赖、不联网、不改动 WorkBuddy 的任何数据。

## 快速开始

前置条件：这台电脑装过 WorkBuddy 并聊过天；有 Python 3.7+（WorkBuddy 自带一份）。

| 系统 | 启动 | 停止 |
|---|---|---|
| Windows | 双击 `start-panel.bat` | 双击 `stop-panel.bat`，或在启动器里按 `S` |
| macOS / Linux | `python3 panel.py start` | `python3 panel.py stop` |

任何系统都可以直接用统一入口（Windows 用 `python`，macOS / Linux 用 `python3`）：

```
python panel.py start      # 后台启动并打开浏览器
python panel.py status     # 查看运行状态
python panel.py once       # 不启服务，只建索引并打印摘要
python panel.py stop       # 停止，数据与索引保留
```

面板地址 <http://127.0.0.1:8770/>，**只监听本机回环**，别的机器访问不到。
端口被占用时会自动顺延到 8771–8779。

开机自启：Windows 双击 `install-autostart.bat`，撤销用 `uninstall-autostart.bat`。

## 数据从哪来

| 来源 | 路径 | 用途 | 必需 |
|---|---|---|---|
| 会话转录 | `~/.workbuddy/projects/**/*.jsonl` | 全部 token 数字 | 是 |
| 会话心跳 | `~/.workbuddy/sessions/*.json` | 识别当前会话 | 否 |
| 本地库 | `~/.workbuddy/workbuddy.db` | 积分与账号归属 | 否 |

全部只读。索引写在项目内的 `data/index.db`，删掉会自动重建；`data/` 不进版本库、也不进发行包。

数据源不在默认位置时：

```
python panel.py start --workbuddy-dir /path/to/.workbuddy
```

也可以设置环境变量 `WORKBUDDY_DIR`。

## 统计口径

- token 优先取 `providerData.usage`，缺失才退回 `message.usage`，同一条只算一次。
- 轮数按 `conversationRequestId` 去重；一次提问可能包含多次模型调用，仍算一轮。
- 缓存命中率单独展示：累计里绝大部分是缓存读取，不必被总数吓到。
- 积分来自 `workbuddy.db` 的 `session_usage`，与 token 是两套口径，不互相换算；没记录显示 `—`，不显示 `0.00`。
- 切日切时按本机时区，在建索引时就写进库。

## 账号名称

点顶栏的 `✎` 可以直接改每个账号（含"未归属"）的显示名，留空恢复默认。
保存后整页立即刷新：顶栏、账号筛选、账号占比饼图、会话卡、切换时间线里的
名字会一起更新，不需要重启或刷新浏览器。

也可以手改 `data/settings.json`（不存在就用默认值），升级替换代码不会丢：

```json
{
  "port": 8770,
  "account_name_template": "账号 {short}",
  "account_aliases": { "1a2b3c4d": "工作号" }
}
```

`{short}` 是账号 UUID 前 8 位，完整 UUID 可在面板上悬停账号名看到。
两个账号配了同一个昵称时，后端会自动补短码区分。

## 发给别人

```
python tools/pack.py
```

生成 `dist/token-panel-<版本>.zip`。包里没有 `data/`，解压到任意目录即可运行。
升级时只替换代码文件，保留 `data/`。详见 `docs/PORTING.md`。

## 出问题了

| 现象 | 处理 |
|---|---|
| 双击没反应 | 看 `data/panel.log`；或 `python panel.py once` 直接看报错 |
| 打开是空白页 | 先跑 `python tools/selftest.py`，再看日志 |
| 数字不涨 | 确认 WorkBuddy 正在对话，页面右上角应显示"实时" |
| 积分显示不可用 | `workbuddy.db` 被占用，属正常降级，token 统计不受影响 |
| 想清空重来 | 删掉 `data/index.db*`，重启即可 |

## 目录

```
config.py      路径、端口、扫描预算与用户配置
store.py       SQLite 索引读写
indexer.py     增量读取转录
aggregate.py   统计查询与版本缓存
credit.py      只读积分与账号归属
server.py      HTTP 与 SSE
panel.py       统一命令行入口
web/           前端（本地 ECharts，断网可用）
tools/         启动器、打包、自检、性能基线
docs/          移植与升级说明
data/          运行时生成：索引、日志、pid、配置
```
