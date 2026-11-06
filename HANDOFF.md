# 开发交接记录

> 最后更新：2026-11-06 @ 公司笔记本
> 当前分支：main（尚未执行 git init）

## 1. 本次已完成

- 两台电脑统一路径已约定：`D:\dev\myproj\Token看板`
- 公司笔记本 2.1 基础配置、2.2 认证已完成（SSH over 443 验证通过）
- 已放入 4 个骨架文件：`.gitignore`、`.gitattributes`、`README.md`、`HANDOFF.md`

## 2. 未完成 / 进行中

- 项目代码尚未放入本目录
- 尚未执行 `git init`、首次提交、首次推送
- GitHub 上尚未创建 `token-dashboard` 仓库
- 家里台式机尚未做任何操作

## 3. 下一步建议

1. 执行 2.4 Step 2-3：`git init` → `git add -A` → 首次 commit
2. 在 GitHub 网页建空的 Private 仓库 `token-dashboard`（不要勾 README / .gitignore / license）
3. `git remote add origin` → `git push -u origin main`
4. 把项目代码放进本目录，补 `README.md` 的启动说明
5. 家里台式机按 2.5 执行 clone（末尾目录名写 `Token看板`）

## 4. 需要注意的坑

- 两台电脑路径必须完全一致，含中文目录名
- GitHub 地址用用户名 `1042407063`，不是显示名 `yyf`
- 家里台式机也要建一份 `~/.ssh/config`（SSH over 443）
- 不要在父目录 `D:\dev\myproj` 里 `git init`，它只是容器

## 5. 最近提交

- （首次提交后补上）
