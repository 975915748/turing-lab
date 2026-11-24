# 开发与贡献流程

## 1. 协作目标

协作者应能够独立完成以下流程：

1. 克隆仓库并建立本地开发环境；
2. 理解自己负责的前端、后端或测试模块；
3. 在独立分支实现小而明确的改动；
4. 使用临时数据库运行相关回归测试；
5. 提交 PR，说明改动、测试和可能影响的业务规则；
6. 由项目维护者审查并决定是否合并、部署。

本仓库当前没有强制的分支命名规范、PR 模板或自动格式化工具。下面的流程是团队建议，不应误认为已经由 GitHub 强制执行。

## 2. 克隆与环境

~~~powershell
git clone https://github.com/975915748/turing-lab.git
cd turing-lab

py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
~~~

CI 当前使用 Python 3.13。生产 Docker 镜像基于 Python 3.12-slim；两者版本不同，涉及运行时兼容性时应同时留意。

启动本地开发服务：

~~~powershell
python -m uvicorn app:app --reload --host 127.0.0.1 --port 8000
~~~

访问 http://127.0.0.1:8000。

本地默认数据库位于 data/game.sqlite3。该目录已在 .gitignore 中排除。不要把生产数据库、线上用户数据或真实 API Key 复制进测试提交。

## 3. 测试命令

### Python 回归测试

~~~powershell
python -m pytest -q
~~~

测试使用临时目录或临时 SQLite 数据库。新增测试时应保持这一原则，避免访问真实生产数据。

### JavaScript 语法检查

~~~powershell
node --check static/app.js
node --check tests/uid_ui.cjs
node --check tests/host_ui.cjs
node --check tests/room_delete_ui.cjs
~~~

CI 使用 Node.js 22。

### Playwright 浏览器测试

CI 当前安装浏览器测试依赖的方式：

~~~powershell
npm install --no-save playwright
npx playwright install chromium
~~~

Linux CI 还会通过 --with-deps 安装 Chromium 系统依赖。浏览器验收脚本可能会输出截图到 artifacts/；该目录已被 .gitignore 排除，截图由 CI Artifact 保存。

### Docker 镜像检查

~~~powershell
docker build --pull -t turing-lab:ci .
docker run --rm --entrypoint python turing-lab:ci -c "import app, model_api, railway_start; print('Docker runtime imports passed')"
~~~

Docker 检查用于防止生产镜像遗漏运行时 Python 模块。

## 4. 现有测试分布

| 测试文件 | 主要覆盖 |
|---|---|
| tests/test_flow.py | 基础比赛流程、房间权限和跨房间隐私 |
| tests/test_uid_accounts.py | UID、注册、旧账号迁移、登录限流和账号唯一性 |
| tests/test_room_hosts.py | 房主权限、账号管理、退赛、重开和历史数据 |
| tests/test_multi_judge.py | 多真人裁判、问题与判决隔离 |
| tests/test_mixed_judges.py | 真人与 AI 裁判混合模式、旧数据库迁移 |
| tests/test_human_judge_self_service.py | 真人裁判自助入房、并发冲突、重开冲突及提示脱敏 |
| tests/test_model_api.py | API Key 加密、模型请求、DNS 固定、AI 回答和判决 |
| tests/test_room_delete.py | 房间删除、清空记录、房间 ID 迁移 |
| tests/test_docker_package.py | Docker 镜像是否复制所有运行时 Python 模块 |
| tests/*_ui.py、tests/*.cjs | Playwright 浏览器流程和界面验收 |

CI 工作流位于 .github/workflows/tests.yml，当前包含：

1. Python 3.13 环境；
2. Node.js 22；
3. Python 和 Playwright 依赖安装；
4. JavaScript 语法检查；
5. pytest 全量测试；
6. Docker 生产镜像构建；
7. 容器内运行时模块导入检查；
8. 上传浏览器验收截图 Artifact。

## 5. 建议分支流程

每项功能或修复使用独立分支，例如：

~~~text
feat/<short-feature-name>
fix/<short-bug-name>
test/<short-test-name>
docs/<short-doc-name>
~~~

推荐流程：

~~~powershell
git switch main
git pull --ff-only
git switch -c feat/your-change

# 修改代码并运行相关测试
python -m pytest -q

git status
git add <changed-files>
git commit -m "feat: describe the change"
git push -u origin feat/your-change
~~~

然后创建目标分支为 main 的 PR。

提交前确认：

- 只提交本次任务需要的文件；
- 没有 data/、SQLite 数据库、.env、API Key、密码或 Token；
- 没有把本地临时日志、截图或模型输出误提交；
- 没有顺手改动与任务无关的业务逻辑。

## 6. PR 描述建议

建议每个 PR 包含：

- **目标**：要解决什么问题；
- **改动范围**：修改了哪些模块；
- **业务规则**：是否改变角色、权限、匿名或比赛状态；
- **数据影响**：是否改 schema、是否需要迁移、是否保留历史数据；
- **测试**：执行的命令和结果；
- **风险**：部署、兼容性、并发或数据删除风险；
- **截图**：涉及 UI 时附桌面和移动端验收截图。

## 7. 代码修改注意事项

### 权限和隐私

- 前端隐藏按钮不构成权限控制；
- 后端必须验证会话、房间关系和当前角色；
- 真人裁判只能读取匿名代号和已公开回答；
- 裁判不能读取其他裁判的判决；
- 跨房间冲突提示不得泄露无权查看的房间标题、编号或 UID；
- 任何客户端传入的 UID、user_id 或 role 都不能直接作为授权依据。

### SQLite 和并发

- 多步骤写操作使用现有 connection(immediate=True) 事务模式；
- 不要在 SQLite 写事务中执行 DNS 解析或等待模型 API；
- 对入房、重开、删除、揭晓等竞争敏感操作增加并发回归测试；
- 修改 schema 时提供迁移前备份、回滚和外键检查；
- 保持房间 match_number 校验，避免旧页面误写新场次。

### 模型 API

- API Key 必须由服务端加密保存；
- 不要把密钥返回给浏览器；
- Base URL 仍需 HTTPS、公网地址和 DNS 固定校验；
- 模型返回失败或资料已变化时，不得写入伪造回答或陈旧判决；
- 模型调用费用由对应 API 服务商计费，测试时使用独立测试配置。

### 前端

- 复用 app.js 的 api()；
- 新增动态文本时使用 esc()；
- 保持 data-action / data-form 事件委托结构；
- 检查 390px 左右移动端布局、日间和深色主题；
- 修改静态文件后检查 index.html 的缓存查询参数。

## 8. 部署边界

开发分支和 PR 测试不应修改 Railway 生产环境。

生产部署会影响真实账号、房间和比赛数据。涉及以下操作时，应由项目维护者明确确认：

- 合并生产代码；
- 修改 Railway Variables；
- 更换 MODEL_CREDENTIAL_KEY；
- 修改或迁移持久化卷；
- 恢复数据库备份；
- 清空生产比赛记录或删除生产房间。

不要通过本地测试脚本直接连接或修改生产数据库。

## 9. 当前文档分支

本轮协作文档位于：

~~~text
docs/team-handover-20261124-current
~~~

该分支以 PR #8 合并后的 main 提交 e3b7c10757bcd6c24d3eae12140054cfa83d7d7c 为起点。文档变更应通过 PR 合并，不应直接写入 main。
