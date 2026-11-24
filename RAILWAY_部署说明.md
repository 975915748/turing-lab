# 图灵实验室 Railway 部署与升级说明

本文说明 Railway 首次部署和现有生产环境升级。**已经运行的网站应优先阅读第 1 节；不要把首次安装步骤当成生产环境升级步骤。** 当前网站的账号与比赛使用方法见 [README.md](README.md)。

## 1. 现有生产环境升级（已有 Railway 服务）

升级时复用原 Railway 项目、原网站 service 和原持久化数据卷。正常升级不需要创建替代 service、重新初始化管理员或迁移数据库。

### 1.1 升级前：确认服务并备份

1. 确认本次准备部署的 GitHub 仓库和目标提交（commit SHA），并选择没有比赛正在进行的维护时段。
2. 打开原 Railway 项目中的网站 service，确认它仍连接到正确的 GitHub 仓库和部署分支。
3. 确认原有 Volume 仍挂载在 **`/app/data`**，并且服务变量仍为 `DB_PATH=/app/data/game.sqlite3`。不要删除、卸载或新建替代数据卷，也不要把数据库路径改到容器临时文件系统。
4. 在 Railway 对原 Volume / 数据库执行一次可恢复的备份或快照，等待备份成功；确认自己知道对应的恢复入口。升级前备份必须来自当前生产数据卷，而不是旧电脑上的历史路径。
5. 记录并保留现有 Variables。尤其是 `MODEL_CREDENTIAL_KEY` 必须原样保留；不要重新生成，不要清空或替换管理员、Cookie、数据库等已有配置。不要把环境变量、密码、API Key 或备份数据库提交到 GitHub。

### 1.2 部署指定提交

1. 将需要上线的文档或代码提交到 Railway 已连接的目标分支；如果 service 配置为跟随 `main` 自动部署，提交进入 `main` 后 Railway 会按该配置启动部署。
2. 确认这次部署使用的是预期的 commit SHA。不要因为仓库已有新提交，就默认 Railway 已经部署到该提交。
3. 等待 Railway 部署完成，查看该 service 的部署日志。确认应用启动完成（例如出现 `Application startup complete`），并检查数据库迁移、备份或启动阶段没有异常；仅仅构建成功不代表应用已正常启动。

### 1.3 健康检查与公网验收

1. 访问现有公网 HTTPS 域名下的 `/api/health`，确认返回：

   ```json
   {"ok":true}
   ```

2. 使用已有管理员和普通账号登录，确认 UID 登录正常，既有房间和历史数据仍在。
3. 使用独立测试房间验收：邀请码加入、参赛者与真人裁判身份选择、问题提交、统一作答、回答公开、裁判独立判断和整场 Log 下载。
4. 如果使用模型 API，再测试模型连接、AI 裁判出题、AI 参赛者回答及最终判断。确认模型 API 配置仍能读取；不要为了测试而更换 `MODEL_CREDENTIAL_KEY`。
5. 检查 WebSocket 实时同步。建议使用不同浏览器或设备登录测试账号，避免只验证单个页面的本地状态。

### 1.4 升级失败与回退

如果启动、健康检查或公网验收失败，先停止继续操作并保留日志、目标 commit SHA 和升级前备份。不要删除 Volume，也不要直接创建一个空数据库替代生产数据库。

**不要只把代码降级后就让旧版继续写入升级后的数据库。** 如果本次版本包含数据库迁移，回退时应评估迁移兼容性；必要时在维护窗口内，将匹配的旧版代码与升级前数据库备份配套恢复。恢复前先另存升级后的数据库，并明确处理升级后产生的新比赛数据，避免无意覆盖。

## 2. 首次部署（尚无生产服务和数据卷）

### 2.1 准备源码

仓库根目录就是应用源码目录。将 GitHub 仓库连接到 Railway 时，使用仓库根目录构建，不要再嵌套一层项目目录。

**严禁上传** Windows 本地 `data/` 数据库、`.env` 文件、管理员密码、服务器 Token、模型 API Key 或 `MODEL_CREDENTIAL_KEY`。源码仓库不应包含生产数据和私密配置。

### 2.2 创建 Railway service 与 Volume

1. 在 Railway 项目中连接 GitHub 仓库，创建网站 service。
2. 使用仓库根目录的 `Dockerfile` 构建。
3. 在首次正式部署前创建持久化 Volume，并挂载到 **`/app/data`**。
4. 设置 `DB_PATH=/app/data/game.sqlite3`。必须先挂载 Volume 再进行首次正式部署，避免数据库写入临时容器文件系统。

### 2.3 环境变量

| 变量 | 设置 |
|---|---|
| `DB_PATH` | `/app/data/game.sqlite3` |
| `COOKIE_SECURE` | `1`，用于公网 HTTPS 登录 Cookie |
| `BOOTSTRAP_ADMIN_USERNAME` | 首次初始化管理员登录名，3～30 位英文、数字或下划线 |
| `BOOTSTRAP_ADMIN_PASSWORD` | 随机强密码，至少 12 个字符；安全保存 |
| `BOOTSTRAP_ADMIN_DISPLAY` | 可选，例如 `主持人` |
| `MODEL_CREDENTIAL_KEY` | 使用模型 API 时必需；生成方式见第 5 节 |
| `PORT` | 通常由 Railway 提供；平台未提供时可设为 `8000` |

管理员只会在数据库中**尚不存在管理员账号**时初始化。后续部署不会覆盖现有管理员密码；删除初始化变量也不会删除数据库中已有的管理员。

### 2.4 运行配置与首次检查

- 使用单个 replica、单个 worker；WebSocket 依赖单进程运行，仓库 Docker 配置已使用单 worker。
- Healthcheck 路径设置为 `/api/health`。
- 部署后生成 Railway HTTPS 公网域名，访问 `/api/health`，确认返回 `{"ok":true}`。
- 使用初始化管理员登录，再创建独立测试房间，验证邀请码、WebSocket 和真人裁判权限。

## 3. 数据库与数据迁移

生产 SQLite 数据库位于持久化 Volume 的 `/app/data/game.sqlite3`。容器重新构建或重新部署时应继续使用同一个 Volume；**不要把 Volume 当作可随部署重建的临时目录**。

从本地电脑迁移数据库到 Railway 是单独的数据迁移任务，不属于普通版本升级。迁移时应先停止本地网站，使用 SQLite Backup API 创建一致性备份，再通过受控且有权限的方式将备份放入目标 Volume。确认目标文件和权限无误后再启动服务。不要把包含玩家资料的数据库上传到公开 GitHub，也不要依赖旧文档中的个人电脑绝对路径。

## 4. 公开访问与运行限制

- Railway 服务器通常不在中国大陆，大陆直连速度和稳定性不能保证；上线后应使用不同网络和设备做实际测试。
- 保持单实例、单 worker，避免多个进程同时运行同一 SQLite 数据库或造成 WebSocket 状态不一致。
- 邀请码只分享给预期参与者；管理员使用强密码。
- 定期备份生产 Volume，并确认备份可恢复；备份和回退流程应在非比赛时段演练。
- Railway 云资源和 Volume 可能产生费用，请在 Railway 控制台核实计划和费用提醒。

## 5. 模型 API 加密密钥

模型 API Key 使用 Fernet 加密后存入 SQLite。首次启用模型 API 前，在本地生成一次密钥：

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

将输出设置为 Railway service 的 `MODEL_CREDENTIAL_KEY`。

**该密钥必须长期固定并妥善备份。** 不要每次部署重新生成，也不要提交到 GitHub。更换或丢失密钥会导致数据库中已经保存的模型 API Key 无法解密，需要重新录入各房间的 API Key。

每次 Railway 启动时，`railway_start.py` 会先验证密钥格式，再逐条尝试解密数据库中已保存的模型 API Key：

- 没有已保存模型配置时，可以不设置密钥，网站仍可使用手动转录模式。
- 已有模型配置但没有设置密钥时，启动会被拒绝。
- 密钥格式错误、与原密钥不匹配，或数据库中的加密凭据损坏时，启动会被拒绝，并在部署日志中给出不包含密钥明文的错误信息。
- 修复 Railway Variables 中的密钥后重新部署即可；该检查不会修改、重新加密或删除数据库中的 API Key。

这样可以在服务上线前发现密钥配置错误，而不是等到下一次 AI 调用才暴露问题。
