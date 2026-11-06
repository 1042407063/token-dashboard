# 开发交接记录

> 最后更新：2026-11-06 @ 公司笔记本
> 当前分支：main（已绑定 origin/main）

## 1. 本次已完成

- 两台电脑统一路径：`D:\dev\myproj\Token看板`
- 2.1 基础配置、2.2 认证完成（SSH over 443 验证通过）
- GitHub 仓库 `token-dashboard` 已建，`main` 已推送
- 项目代码已从 `D:\WorkBuddy\WorkBuddy实时token看板` 迁入本仓库
- `.gitignore` 已补运行时数据规则（`data/*`、`*.db*`、`*.pid`、`.workbuddy/tmp/`）
- 3 份文档里写死的旧路径 `D:\WorkBuddy\个人开发\token-panel` 已替换为新路径
- 项目 README 末尾补上双机交接信息

## 2. 未完成 / 进行中

- 本次「代码迁入 + 忽略规则 + 路径修正」尚未 commit / push
- 家里台式机尚未做任何操作（装 Git、2.1、2.2、建目录、clone）
- 新目录下尚未启动服务验证（旧目录服务已停）

## 3. 下一步建议

1. `git status -sb` 核对改动，确认没有 `.db` / `.pyc` / `.zip` / `.log` 被跟踪
2. `git add -A` → `git commit -m "feat: 迁入 Token 看板项目代码"` → `git push`
3. 在新目录跑一次 `tools/selftest.py` 验证，再 `start-panel.bat` 起服务
4. 确认无误后再清理旧目录 `D:\WorkBuddy\WorkBuddy实时token看板`（先别删）
5. 家里台式机按手册 2.5 配置并 clone（末尾目录名写 `Token看板`）

## 4. 需要注意的坑

- 两台电脑路径必须完全一致，含中文目录名
- GitHub 地址用用户名 `1042407063`，不是显示名 `yyf`
- `data/` 是运行时数据，已忽略；另一台 clone 后是空目录，程序首次运行会自行建库
- 家里台式机也要建一份 `~/.ssh/config`（SSH over 443）
- 启动脚本依赖 WorkBuddy 自带的 Python 解释器路径，另一台需同版本或改路径
- 不要在父目录 `D:\dev\myproj` 里 `git init`，它只是容器

## 5. 最近提交

- 9bedaae docs: 修正 README 中的可见性标注
- 4330b9f chore: 初始化仓库骨架
