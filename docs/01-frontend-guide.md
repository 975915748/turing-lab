# 前端开发指南

## 1. 前端技术形态

前端位于 static/，由原生 HTML、CSS 和 JavaScript 组成。当前没有 React、Vue、TypeScript、Vite 或其他前端构建流程。

| 文件 | 职责 |
|---|---|
| static/index.html | 页面外壳、顶部工具栏、主内容容器、提示弹窗和静态资源入口 |
| static/app.js | 登录状态、页面渲染、API 请求、WebSocket、表单和按钮交互 |
| static/style.css | 颜色变量、卡片、表单、网格布局、日间 / 深色主题和移动端样式 |

index.html 通过带版本查询参数的 URL 引入 CSS 和 JS；当前查询参数是 1.7.0。修改静态文件后，若浏览器仍使用旧缓存，应同步检查 index.html 的查询参数。

## 2. 页面和渲染入口

app.js 通过一个主容器 app 渲染页面，主要入口如下：

| 函数 | 页面 / 用途 |
|---|---|
| init() | 初始化偏好、请求当前用户并选择大厅或房间 |
| renderAuth() | 登录、注册和旧用户名登录切换 |
| renderLobby() | 大厅、创建房间、邀请码加入和房间列表 |
| loadAccounts() / renderAccounts() | 管理员用户管理 |
| openRoom() | 加载房间并建立 WebSocket |
| renderRoom() | 普通房间视图的主渲染分发 |
| renderJudgeRoom() | 真人裁判专用匿名视图 |
| judgeAdminPanel() | 房主的真人 / AI 裁判管理面板 |
| renderModelRoom() | 房间级模型 API 配置页面 |
| renderMultiResults() | 多裁判结果展示 |
| renderReadyRoom() | 比赛记录清空后的房间状态页 |
| makeRoster() | 参赛者名单和本轮提交状态 |

房间渲染不是由路由库驱动，而是由 state.room、state.roomView 和 state.selectedRound 等客户端状态决定。

## 3. 前端状态

文件顶部的 state 保存当前页面状态，主要字段：

- me：当前登录用户和大厅数据；
- room / roomId：当前房间数据及 ID；
- selectedRound：当前选中的历史轮次；
- ws / reconnect：当前 WebSocket 和重连定时器；
- roomView：房主侧的 match、judges、admin、models 子视图；
- drafts：当前页面生命周期中的表单草稿；
- typing：本轮正在输入的参赛者状态；
- accounts：管理员用户列表；
- packetPanels：裁判包折叠面板展开状态。

草稿以房间 ID、match_number、轮次和字段名组合为键，保存在 JavaScript 内存中；它不是服务器持久化数据，也不是 localStorage 数据。刷新页面后不要假定未提交草稿仍然存在。

### 浏览器本地偏好

- 主题：localStorage 中的 turing_theme；
- 字号：localStorage 中的 turing_font；
- 裁判就绪提示是否已显示：sessionStorage；
- 裁判包面板展开状态：当前页面内的 state.packetPanels。

不要将账号密码、Cookie、API Key 或其他敏感凭据写入浏览器本地存储。

## 4. HTTP API 封装

所有前端 API 请求通过 app.js 的 api() 函数发送。

该函数负责：

1. 根据请求方法和 body 生成 JSON 请求；
2. 非 GET 请求自动附带当前用户的 X-CSRF-Token；
3. 房间写请求在已有房间状态时附带 X-Match-Number；
4. 将服务端错误转换为可显示的异常消息；
5. 对 401 响应清理登录状态并回到登录页；
6. 对下载请求返回 Blob。

新增前端请求时优先复用 api()，不要在页面组件里散落独立 fetch 逻辑，否则容易漏掉 CSRF 和比赛场次头。

## 5. WebSocket 与实时刷新

房间连接地址为 /ws/rooms/{room_id}。

浏览器连接后：

- 服务端发送 connected；
- 房间数据变化时，服务端广播 room_updated，客户端调用 refreshRoom() 重新获取完整房间数据；
- 参赛者输入状态通过 typing_status 同步；
- WebSocket 断线后客户端约 3 秒重连；
- 页面重新获得焦点或从后台切回时会重新刷新房间；
- WebSocket 不可用时，客户端每 15 秒尝试刷新房间数据。

输入状态只面向当前房间的人类参赛者；裁判、AI 和管理员观察视图不接收参赛者输入状态。

WebSocket 仅负责实时通知和输入状态，不承担比赛数据的最终存储。真实业务数据仍以 HTTP API 和 SQLite 为准。

## 6. 事件委托与表单

主容器通过事件委托处理交互：

- 按钮使用 data-action；
- 表单使用 data-form；
- 输入草稿使用 data-draft；
- AI 回复编辑选择器使用 data-ai-edit-select / data-ai-edit-text。

新增按钮时，在统一 click 事件处理器中增加对应 action；新增表单时，在统一 submit 事件处理器中增加对应 form 分支。不要给每次 render() 产生的按钮重复绑定全局监听器。

当前常见 data-action：

- open、back、sync、round、room-view；
- tab-login、tab-register、toggle-legacy-login；
- copy-uid、copy-code；
- start-round、next-round、reveal；
- generate-ai-questions、generate-ai-answers、generate-ai-verdicts；
- packet、view-packet、mark-sent；
- remove-human、restart-match、delete-room、delete-records；
- test-model、delete-model；
- manage-users、delete-account、toggle-password-reset。

当前常见 data-form：

- login、register、join、room；
- answer、question、suggestion、ai-answer；
- judge-question、judge-verdict、ai-judge-question、ai-judge-verdict；
- add-ai、add-ai-judge、edit-ai-answer；
- model-profile、edit-model-profile；
- verdict、reset-password。

这些名称来自当前 app.js；新增或删除交互时应同步更新本指南。

## 7. PR #8 后的前端行为

### 账号

- 注册不需要邀请码，注册成功后展示唯一 UID；
- 默认使用 UID 登录；
- 旧账号可以切换到旧用户名登录；
- 用户可在大厅复制自己的 UID。

### 加入房间

大厅加入表单新增身份选择：

- 参赛者（默认）；
- 真人裁判。

管理员界面不显示真人裁判选项。服务端仍会拒绝管理员以独立真人裁判身份加入，因此不能把前端隐藏选项当作唯一权限控制。

### 真人裁判管理

当前 UI 不再显示“创建真人裁判登录账号”和“绑定已有账号”表单。真人裁判应自行注册、使用 UID 登录，再通过邀请码选择“真人裁判”加入。

后端仍保留旧的创建账号 / 绑定账号 API 路由，见 API 指南。不要因为页面上没有表单就删除旧接口。

### 模型配置

模型配置表单当前使用“采样温度”文案；字段名仍为 temperature，范围 0–2，步长 0.1，默认 0.7。

当服务端没有配置 MODEL_CREDENTIAL_KEY 时，页面显示“模型 API 暂不可用，请联系管理员。”并禁用保存按钮。页面不展示密钥内容，只展示是否已保存。

## 8. 前端安全注意事项

app.js 使用 innerHTML 生成大量动态页面。所有来自用户、数据库或模型的文本，在进入 HTML 字符串前都应通过 esc() 转义。

特别注意：

- 用户名、显示名、房间标题、裁判问题、回答、建议和模型名称；
- 裁判包内容应通过 textContent 展示，不要直接拼入 HTML；
- 不要将 API Key 或其他凭据放进 DOM、错误消息或 console；
- 不要把客户端传来的 role、UID 或 user_id 当作可信授权依据；
- 不要在前端实现“隐藏按钮即可限制权限”的安全逻辑。

## 9. 前端测试

当前 CI 对 app.js 执行 node --check，并运行 Playwright 浏览器验收。相关脚本包括：

- tests/uid_ui.cjs：UID 账号和真人裁判自助加入相关浏览器流程；
- tests/host_ui.cjs：房主功能和移动端布局；
- tests/room_delete_ui.cjs：房间删除、记录清空等操作；
- tests/test_host_ui.py、tests/test_uid_ui.py：Python 侧测试入口。

修改 UI 后应检查至少以下场景：

1. 登录、注册、UID 登录和旧用户名登录；
2. 普通玩家与真人裁判的身份选择；
3. 房主、玩家、裁判三种房间视图；
4. 390px 左右移动端宽度；
5. 日间和深色主题；
6. WebSocket 重连和页面重新获得焦点；
7. 未提交草稿、历史轮次和新场次切换；
8. API 失败时是否显示明确错误。
