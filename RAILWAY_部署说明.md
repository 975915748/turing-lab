# 图灵实验室 v1.2 — Railway 云部署版

保留原本 v1.2 网站全部核心功能。增加 `railway_start.py`，**首次启动时仅从 Railway 私密环境变量创建管理员**；若没设置符合要求的管理员密码，则拒绝启动，避免暴露没有管理员的空网站。SQLite 数据库放在挂载卷 `/app/data`，单进程运行 WebSocket。

## 1. 源码

此文件夹中的文件是网站根目录。创建一个 GitHub 仓库，将 **本目录内部的文件** 上传到仓库根目录（不要嵌套一层 `turing-lab-railway`）。如使用 ChatGPT 的 GitHub 连接，可授权其创建并提交该仓库。

**严禁上传** Windows 上的 `data/` 数据库、`.env` 文件、管理员密码、服务器 Token。此源码 ZIP 不包含这些信息。

## 2. Railway 设置

在 Railway 项目 `turing-lab` 中，连接 GitHub 仓库创建网站 service，配置：

- Build: Dockerfile（仓库根目录）
- Volume: 挂载到 **`/app/data`**（必需，必须先挂卷再首次正式部署）
- `DB_PATH=/app/data/game.sqlite3`
- `COOKIE_SECURE=1`（公网 HTTPS 登录 cookie）
- `BOOTSTRAP_ADMIN_USERNAME=` 你的管理员登录名（3–30 位英文/数字/下划线）
- `BOOTSTRAP_ADMIN_PASSWORD=` **随机生成并安全保存的强密码**，至少 12 个字符
- `BOOTSTRAP_ADMIN_DISPLAY=主持人`（可选）
- `PORT=8000`（如平台已为服务提供 PORT，则使用其既有值）
- single replica, WebSocket requires a single worker (Docker image already sets --workers 1)
- Healthcheck `/api/health`

管理员账号仅在数据库中**还没有管理员**时自动初始化。后续重新部署不会覆盖原密码。即使删掉初始化环境变量，已存在管理员也能继续登录。

## 3. 公开网址

使用 Railway 的 Generate Domain 生成 `*.up.railway.app` HTTPS 网址；在浏览器访问 `/api/health` 看是否返回 `{"ok":true}`，随后登录并用两部设备测试 WebSocket 和匿名裁判权限。

Railway 服务器通常不在中国大陆，**大陆直连速度和稳定性不能保证**。首次上线后应让多位玩家分别在电信、联通、移动网络中测试。

## 4. 历史数据迁移

本 ZIP 为**空数据部署**，不会自动包含电脑 `E:\AI多人图灵测试网站\data\game.sqlite3` 的现有账号、房间、比赛记录。若要迁移，应先在本地停止网站，用 SQLite Backup API 做一致性备份，再通过有权限的工具把备份放到 Railway volume 中 `/app/data/game.sqlite3`，在完成前不要用新网站录入需要保留的数据。不要将含有玩家资料的数据库上传至公开 GitHub 仓库。

## 5. 费用与安全

Railway 云资源和卷可能产生费用，请在 Railway 控制台核实计划、费用/预算提醒。公共互联网访问前确认邀请码仅给受邀者，管理员使用强密码，定期备份数据库。

本项目适用于可信玩家的小型比赛；正式长期运行仍建议补做安全审计、注册速率限制、备份和监控。
