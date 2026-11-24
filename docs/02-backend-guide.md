# 后端开发指南

## 1. 入口与模块边界

| 文件 | 职责 |
|---|---|
| app.py | FastAPI 应用、SQLite 初始化与迁移、认证、权限、业务逻辑、HTTP 路由和 WebSocket |
| model_api.py | OpenAI 兼容模型请求、API Key 加解密、Base URL 校验和网络连接安全 |
| railway_start.py | 生产启动前检查持久化数据库、首次创建管理员并启动单 worker Uvicorn |
| Dockerfile | 构建生产镜像 |
| railway.json | Railway Dockerfile 构建、单副本和健康检查配置 |

app.py 当前约 2,093 行，业务逻辑集中在单个模块中。新增业务时应优先沿用已有 helper 和事务模式，不要在路由中复制权限判断。

## 2. 应用初始化

app.py 导入时执行 create_app() 并创建全局 app。create_app() 会调用 init_db()，初始化或迁移 SQLite 数据库。

数据库路径：

- 默认：项目根目录下 data/game.sqlite3；
- 设置 DB_PATH 后：使用环境变量指定的路径；
- Railway 部署说明使用 /app/data/game.sqlite3。

SQLite 连接通过 db_open() 创建，当前设置：

- 外键约束开启；
- busy_timeout 为 20 秒；
- SQLite 连接 timeout 为 20 秒；
- 初始化时启用 WAL 模式。

测试通常通过 create_app(tmp_path) 使用临时数据库，不应连接真实生产数据。

## 3. 数据模型概览

当前 schema 主要由以下表组成：

| 分类 | 表 | 作用 |
|---|---|---|
| 账号 | users | UID、用户名、显示名、密码哈希、管理员标记、软删除时间 |
| 账号 | sessions | 登录会话 Token 哈希、CSRF Token、过期时间 |
| 账号 | login_attempts | 登录失败计数和时间窗口 |
| 房间 | rooms | 房间标题、邀请码、房主、状态、当前轮次、场次编号 |
| 参赛者 | members | 人类参赛者与匿名代号、加入和移出时间 |
| 参赛者 | ai_players | AI 参赛位、名称和模型配置关联 |
| 轮次 | rounds | 轮次编号、问题、状态和揭晓时间 |
| 回答 | answers | 每个匿名代号的回答、提交时间和编辑时间 |
| 互动 | suggestions | 玩家向主持人提交的问题或追问建议 |
| 审计 | events | 房间内的业务事件记录 |
| 真人裁判 | room_judges | 真人裁判与房间关系 |
| 真人裁判 | judge_questions | 真人裁判逐轮问题 |
| 真人裁判 | judge_verdicts | 真人裁判各自独立的最终判决 |
| AI 裁判 | ai_judges | AI 裁判席及模型配置关联 |
| AI 裁判 | ai_judge_questions | AI 裁判逐轮问题 |
| AI 裁判 | ai_judge_verdicts | AI 裁判独立最终判决 |
| 模型 | model_profiles | 房间级模型配置及加密 API Key |
| 修订 | ai_answer_revisions | AI 参赛者回答修订前后的历史 |
| 快照 | packets | 匿名裁判包内容、版本和 SHA256 |

历史迁移还会为既有表增加字段，例如 rooms.owner_id、rooms.match_number、rooms.records_deleted、users.deleted_at、members.removed_at、answers.edited_at，以及 AI 席位的 model_profile_id。

### 关键关系

- 一个房间有多名人类参赛者和 AI 参赛者；
- 一个房间有多轮比赛；
- 每轮有多条匿名回答；
- 一个房间可以关联多名真人裁判和 AI 裁判；
- 真人裁判的提问和判决按 UID 隔离；
- AI 裁判的提问和判决按 AI 裁判席 ID 隔离；
- 模型配置属于房间，可被多个 AI 参赛者或 AI 裁判席复用；
- 删除模型配置会将关联席位的 model_profile_id 置空，席位转为手动转录；
- 删除房间会通过外键级联删除该房间数据；删除用户则采用软删除，以保留历史关系和署名。

## 4. 认证与权限

### UID

UID 根据 users.id 生成，格式为 TL 加至少 8 位十进制数字，例如 TL00000001。UID 不是单独随机字段，而是由数据库用户主键稳定派生。

用户名允许重名；新登录流程使用 UID。旧账号兼容使用 legacy_username 登录。

### 密码与会话

- 密码使用 PBKDF2-HMAC-SHA256；
- 当前迭代次数为 310,000；
- 每个密码使用 16 字节随机盐；
- 浏览器 Cookie 名称为 turing_sid；
- 数据库只保存会话 Token 的 SHA256 哈希；
- 会话有效期为 7 天；
- Cookie 设置 HttpOnly、SameSite=Lax；只有 COOKIE_SECURE=1 时设置 Secure；
- 登录失败在 15 分钟窗口内达到 8 次后返回 429。

### 写请求保护

caller(request, db, mutate=True) 会执行：

1. Origin 校验；
2. Cookie 会话验证；
3. X-CSRF-Token 校验；
4. 如果请求带有 X-Match-Number，则校验它与当前房间场次编号一致。

当前 Origin 检查允许请求来源与 Host 一致，或位于 ALLOWED_ORIGINS 逗号分隔白名单中。没有 Origin 头时，当前 helper 会放行；不要把这一点误写为“所有请求都强制要求 Origin 头”。

### 权限 helper

| helper | 用途 |
|---|---|
| auth() | 根据 Cookie Token 哈希验证会话 |
| caller() | 统一认证，并在写请求时执行 Origin、CSRF 和可选场次校验 |
| permission() | 验证用户是否属于房间，并返回本人参赛代号 |
| can_manage_room() | 判断是否为房主或全站管理员 |
| require_room_manager() | 仅允许房主或管理员管理房间 |
| require_judge() | 仅允许当前房间的真人独立裁判操作裁判接口 |
| can_view_room() | 判断调用者是否有权查看指定房间，用于冲突信息脱敏 |

不要只依赖前端传来的 role、UID、user_id 或隐藏按钮判断权限。服务端必须根据当前会话 UID 和数据库关系做最终判断。

## 5. 房间和比赛状态

### 轮次状态

~~~text
draft
  |
  | 房主确认问题已提交并开启作答
  v
collecting
  |
  | 所有当前在场参赛者都提交回答
  v
revealed
  |
  | 房主开启下一轮
  v
下一轮 draft
~~~

当前轮次状态保存在 rounds.status。房间状态主要为 open 或 finished。

is_complete() 负责判断当前在场名单是否全部提交，并推进统一揭晓。不要在客户端自行判断所有人已提交后直接展示答案；服务端状态才是最终依据。

### 场次编号

rooms.match_number 区分同一个房间反复进行的不同比赛。房间复用时会递增场次编号。浏览器写请求附带 X-Match-Number，防止旧页面误写到新场次。

### 容量

app.py 当前常量：

- MAX_PLAYERS = 8；
- MAX_ROUNDS = 100；
- MAX_JUDGES = 12；
- DURATION_DAYS = 7。

参赛者上限是人类和 AI 合计的在场人数；裁判席上限单独计算，包含真人和 AI 裁判。

## 6. 真人裁判自助入房

PR #8 已将真人裁判自助加入流程合并到 main。

当前流程：

1. 用户自行注册，取得 UID；
2. 使用 UID 和密码登录；
3. 在大厅输入房间邀请码；
4. 将加入身份选择为真人裁判；
5. 后端根据当前认证会话 UID 写入 room_judges。

关键规则：

- 客户端不能指定要加入的 user_id 或 UID；
- 管理员不能担任独立真人裁判；
- 房主不能担任自己房间的独立真人裁判；
- 本房间参赛者不能同时担任本房间裁判；
- 同一账号在同一房间重复以裁判身份加入是幂等的；
- 同一 UID 不能同时担任两个未完赛房间的真人裁判；
- 每房间真人与 AI 裁判合计最多 12 位；
- 同一 UID 可以在不同房间担任玩家，但不能同时在两个未完赛房间担任真人裁判。

入房和重开冲突检查都在 SQLite 写事务中完成。涉及竞争条件的检查不得移到事务外。

### 并发与旧数据

check_no_other_active_judge_room() 检查同一 UID 是否在其他未完赛房间任职。

check_restart_judge_conflicts() 在重新开赛前检查保留的真人裁判是否仍占用其他未完赛房间。该检查必须在清空上一场记录之前执行。

当前代码不会自动修复历史上已经存在的多房间冲突关系。旧数据需要人工确认后处理。

冲突提示还会调用 can_view_room()：有权限查看冲突房间的人可以看到详细房间信息；无权限者只收到通用提示。

## 7. 模型 API

model_api.py 实现标准 OpenAI Chat Completions 兼容请求：

~~~text
POST {Base URL}/chat/completions
Authorization: Bearer {API Key}
Content-Type: application/json
~~~

### 密钥

- 使用 Fernet 对称加密；
- 密钥来自 MODEL_CREDENTIAL_KEY；
- SQLite 保存 api_key_encrypted；
- 房间查询只返回 has_api_key，不返回密文或明文；
- 更新配置时 API Key 留空会保留旧密钥；
- 删除模型配置会把 AI 席位切换为手动转录。

### 网络校验

- Base URL 必须为 HTTPS；
- 拒绝 localhost、.local 和非公网地址；
- 域名解析结果只校验一次；
- 实际 TCP 连接固定到该次已校验的公网 IP；
- TLS 仍使用原始域名进行 SNI 和证书校验；
- 模型调用不应在 SQLite 写事务中执行。

### 模型执行

- 单个应用进程同时最多执行 4 个模型请求；
- AI 参赛者只读取自己的历史回答和当前问题；
- AI 裁判只读取匿名代号、问题和回答；
- AI 裁判生成最终判决时计算匿名资料 SHA256；如果模型返回前比赛资料变化，则废弃陈旧判决；
- 模型错误、超时或无效 JSON 不应被当作成功回答或判决写入。

## 8. 数据库迁移

init_db() 负责创建表和兼容旧 schema。部分旧结构升级会先通过 SQLite Backup API 在数据库同目录创建迁移前备份，再执行迁移。

涉及表重建或主键迁移时，应保留当前代码的事务、回滚和外键检查策略。不要直接在生产数据库上手工删表或运行未经验证的迁移脚本。

修改 schema 时至少补充：

- 新数据库初始化测试；
- 从旧 schema 升级的测试；
- 迁移失败回滚测试；
- 账号、房间、历史回答和裁判关系保留测试。

## 9. 本地运行

建议使用与 CI 一致的 Python 3.13 环境进行测试。生产 Docker 镜像当前使用 Python 3.12。

~~~powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m uvicorn app:app --reload --host 127.0.0.1 --port 8000
~~~

访问 http://127.0.0.1:8000。

默认本地数据库位于项目根目录 data/game.sqlite3；该目录已加入 .gitignore。不要将生产数据库复制进开发目录并误提交。

本地需要测试管理员功能时，可使用 app.py 中的 seed_admin(path, username, display, password) 初始化一个仅供开发的管理员账号。不要复用生产密码。

## 10. Railway / Docker 部署

当前 Dockerfile：

- 基于 python:3.12-slim；
- 安装 requirements.txt；
- 复制 app.py、model_api.py、railway_start.py 和 static/；
- 启动 railway_start.py。

railway.json 当前配置：

- Dockerfile 构建；
- numReplicas = 1；
- 健康检查路径 /api/health；
- restartPolicyType = ALWAYS。

railway_start.py 在数据库不存在管理员时，要求：

- BOOTSTRAP_ADMIN_USERNAME：3–30 位 ASCII 字母、数字或下划线；
- BOOTSTRAP_ADMIN_PASSWORD：至少 12 位；
- BOOTSTRAP_ADMIN_DISPLAY：可选，默认“主持人”；
- DB_PATH：默认 /app/data/game.sqlite3；
- PORT：合法端口，默认 8000。

已有管理员时，启动脚本不会重置管理员密码。应用以单个 Uvicorn worker 启动，因为 WebSocket Hub 和连接集合保存在进程内。

生产部署必须挂载持久化卷到 /app/data。MODEL_CREDENTIAL_KEY 必须作为 Railway 私密变量配置并长期保持不变。不要将密钥写进仓库、Dockerfile、测试截图或日志。

完整部署参数仍应对照根目录 RAILWAY_部署说明.md，并以当前 Dockerfile、railway.json 和 railway_start.py 为准。
