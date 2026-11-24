# HTTP / WebSocket API 接口约定

本文按当前 app.py 路由整理。FastAPI 的 docs_url 和 redoc_url 均关闭，因此本项目没有公开 Swagger / ReDoc 页面。修改接口时必须同步更新本文件和相关测试。

## 1. 通用请求约定

### 认证

除健康检查、注册和登录外，接口需要有效的 turing_sid 登录 Cookie。

登录成功后服务端设置 HttpOnly Cookie。浏览器不应自行读取或保存会话 Token。

### 写请求

所有写请求应通过前端 api() 发送，并附带：

- Content-Type: application/json（有 JSON body 时）；
- X-CSRF-Token：来自 GET /api/me 返回的 user.csrf；
- Origin：浏览器同源请求；
- X-Match-Number：房间写请求附带当前场次编号。

服务端 caller(..., mutate=True) 会验证 Origin、登录会话和 CSRF。若提供 X-Match-Number，还会校验它与数据库中的当前场次一致。

部分破坏性操作还要求 body 明确提供 match_number 和 confirm: true。

### 常见错误状态

| 状态码 | 常见含义 |
|---|---|
| 401 | 未登录、会话失效或账号不可用 |
| 403 | 已登录但没有所需身份 / 权限，或 CSRF / Origin 校验失败 |
| 404 | 房间、账号、配置或席位不存在 |
| 409 | 当前比赛状态不允许操作、冲突或重复提交 |
| 422 | 请求字段格式、长度或业务数据不符合要求 |
| 429 | 登录失败次数达到限流阈值 |
| 502 | 模型连接或模型服务调用失败 |

## 2. 主要请求模型

| 模型 | 字段 | 说明 |
|---|---|---|
| LoginData | uid 或 username、password | uid 与 username 必须且只能提供一个；username 仅用于旧账号兼容登录 |
| RegisterData | username、password、display_name?、code? | 注册；code 可选，若提供则注册后直接以玩家身份加入该房间 |
| CodeData | code、role? | 加入房间；role 为 player 或 judge，省略时默认为 player |
| RoomData | title、judge? | 创建房间；title 最多 80 字，judge 默认“裁判” |
| MatchActionData | match_number、confirm=true | 删除房间、清空记录或重新开赛 |
| TextData | text | 文本输入；各路由还会施加更具体的长度限制 |
| AIData | name、model_profile_id? | 添加 AI 参赛者或 AI 裁判席 |
| ModelProfileData | name、base_url、model、api_key?、temperature、max_tokens、timeout_seconds | 创建或更新模型配置 |
| VerdictData | predictions、evidence、confidence、weakness?、possible_error? | 最终判断；predictions 的键必须覆盖全部匿名代号，值只能是 Human / AI |

ModelProfileData 当前约束：

- name：1–60 字；
- base_url：8–500 字；
- model：1–120 字；
- api_key：最多 2,000 字；创建配置时必填，更新时可留空以保留原密钥；
- temperature：0–2，默认 0.7；
- max_tokens：64–8192，默认 2048；
- timeout_seconds：10–120，默认 60。

VerdictData 当前约束：

- evidence 必填，最多 5,000 字；
- confidence 为 0–100 整数；
- weakness 和 possible_error 可选，最多 5,000 字。

## 3. 健康检查、账号和大厅

| 方法 | 路径 | 权限 | 用途 |
|---|---|---|---|
| GET | /api/health | 公开 | 健康检查，返回 ok |
| GET | /api/me | 登录 | 当前用户、房间列表和可清理的完赛房间列表 |
| POST | /api/register | 公开 | 注册新用户；可选 code 直接加入房间 |
| POST | /api/login | 公开 | UID 登录或旧用户名登录 |
| POST | /api/logout | 登录 + CSRF | 删除当前会话并清 Cookie |
| POST | /api/rooms | 登录 + CSRF | 创建房间，调用者成为房主 |
| POST | /api/join | 登录 + CSRF | 通过邀请码加入为玩家或真人裁判 |

### 注册与加入规则

- 注册无需邀请码；
- 注册成功后返回 UID；
- RegisterData.code 若提供，只支持按普通玩家流程加入；
- 通过 /api/join 加入真人裁判时，body.role 必须为 judge；
- 真人裁判身份来自当前会话 UID，服务端忽略客户端自报身份；
- 同一账号重复以真人裁判身份加入同一房间时返回已有关系；
- 同一 UID 不能同时在两个未完赛房间担任真人裁判。

### 管理员账号接口

| 方法 | 路径 | 权限 | 用途 |
|---|---|---|---|
| GET | /api/admin/users | 全站管理员 | 获取未删除账号列表 |
| POST | /api/admin/users/{uid}/password | 全站管理员 | 重置账号密码并撤销其所有会话 |
| DELETE | /api/admin/users/{uid} | 全站管理员 | 软删除账号、撤销会话并处理未完赛参赛状态 |

删除最后一个可用管理员会被拒绝。

## 4. 房间读取与模型配置

| 方法 | 路径 | 权限 | 用途 |
|---|---|---|---|
| GET | /api/rooms/{room_id} | 房间成员、房主、管理员或该房间裁判 | 获取房间数据；真人裁判获得独立匿名视图 |
| POST | /api/rooms/{room_id}/models | 房主 / 管理员 | 创建模型配置 |
| PUT | /api/rooms/{room_id}/models/{profile_id} | 房主 / 管理员 | 更新模型配置 |
| DELETE | /api/rooms/{room_id}/models/{profile_id} | 房主 / 管理员 | 删除配置并将关联 AI 席位切换为手动转录 |
| POST | /api/rooms/{room_id}/models/{profile_id}/test | 房主 / 管理员 | 测试模型连接并返回延迟、预览和用量 |

GET /api/rooms/{room_id} 的响应按身份裁剪：

- 房主 / 管理员可获得管理字段、邀请码、模型配置元数据和完整参赛者映射；
- 普通参赛者获得参赛者视图，但未统一揭晓的其他回答正文不会返回；
- 真人裁判获得匿名代号、问题和已揭晓回答，不获得真实玩家身份、账号信息或其他裁判判决。

不要把一个角色的响应结构直接复用到另一个角色的前端视图。

## 5. 房间与账号管理

| 方法 | 路径 | 权限 | 用途 |
|---|---|---|---|
| DELETE | /api/rooms/{room_id} | 房主 / 管理员 | 永久删除房间及其关联数据 |
| DELETE | /api/rooms/{room_id}/records | 任意已登录用户 | 清空已完赛房间的比赛记录，房间保留 |
| POST | /api/rooms/{room_id}/restart | 房主 / 管理员 | 完赛后复用房间并开启新场次 |
| POST | /api/rooms/{room_id}/members/{user_id}/remove | 房主 / 管理员 | 将人类参赛者移出房间 |

注意：清空完赛记录的接口有意允许任意已登录用户调用，不是仅房主接口。服务端仍要求房间处于 finished，并校验当前 match_number 和 confirm。

永久删除房间则仅允许房主或管理员。

重新开赛时，服务端会先检查保留的真人裁判是否仍在其他未完赛房间任职；冲突时返回 409，并且不会先清空上一场记录。

## 6. 裁判管理接口

| 方法 | 路径 | 权限 | 用途 |
|---|---|---|---|
| POST | /api/rooms/{room_id}/judges | 房主 / 管理员 | 旧流程：由房主创建真人裁判账号并加入房间 |
| POST | /api/rooms/{room_id}/judges/assign | 房主 / 管理员 | 旧流程：按 UID 或旧用户名绑定已有账号 |
| POST | /api/rooms/{room_id}/ai-judges | 房主 / 管理员 | 添加 AI 裁判席 |
| POST | /api/rooms/{room_id}/ai-judges/questions/generate | 房主 / 管理员 | 让绑定模型的 AI 裁判自动出题 |
| POST | /api/rooms/{room_id}/ai-judges/{judge_id}/questions | 房主 / 管理员 | 手动转录 AI 裁判问题 |
| POST | /api/rooms/{room_id}/judge-questions | 当前房间真人裁判 | 提交自己的本轮问题 |

PR #8 移除了旧的“创建真人裁判账号”和“绑定已有账号”前端表单，但 app.py 中的两个旧 API 路由仍然存在。新用户流程应使用 /api/register、/api/login 和 /api/join 的 role=judge。

### 裁判约束

- 每房间真人和 AI 裁判合计最多 12 位；
- 真人裁判不能是本房间房主、管理员或本房间参赛者；
- 真人裁判每轮最多提交一次问题；
- 同一 UID 不能同时在两个未完赛房间担任真人裁判；
- 冲突提示只向有权查看冲突房间的人披露房间标题和编号。

## 7. 比赛轮次、回答和追问

| 方法 | 路径 | 权限 | 用途 |
|---|---|---|---|
| POST | /api/rooms/{room_id}/start | 房主 / 管理员 | 汇总本轮裁判问题并开启统一作答 |
| POST | /api/rooms/{room_id}/question | 房主 / 管理员 | 旧单裁判模式下录入裁判问题 |
| POST | /api/rooms/{room_id}/ai | 房主 / 管理员 | 添加 AI 参赛位 |
| POST | /api/rooms/{room_id}/suggestions | 房间成员（真人裁判除外） | 提交问题或追问建议 |
| POST | /api/rooms/{room_id}/answers | 当前房间人类参赛者 | 提交自己的回答 |
| POST | /api/rooms/{room_id}/ai/{alias}/answer | 房主 / 管理员 | 手动录入 AI 参赛者回答 |
| POST | /api/rooms/{room_id}/ai/answers/generate | 房主 / 管理员 | 自动生成绑定模型的 AI 参赛者回答 |
| PUT | /api/rooms/{room_id}/ai/{alias}/answer | 房主 / 管理员 | 修订当前轮 AI 回答并保存历史版本 |
| POST | /api/rooms/{room_id}/next | 房主 / 管理员 | 当前轮揭晓后开启下一轮 |

问题、建议和回答均有服务端长度校验。玩家不能代替其他玩家提交回答；每个参赛代号每轮只能提交一次。

AI 回答修订仅限当前轮，并受比赛状态和裁判判决锁定状态约束。旧文本写入 ai_answer_revisions。

## 8. 日志、裁判包和最终判决

| 方法 | 路径 | 权限 | 用途 |
|---|---|---|---|
| GET | /api/rooms/{room_id}/rounds/{number}/log | 房间可见成员 | 下载指定轮次 Log |
| GET | /api/rooms/{room_id}/log | 房间可见成员；比赛必须 finished | 下载整场 Log |
| POST | /api/rooms/{room_id}/packets | 房主 / 管理员 | 为已完成轮次生成匿名裁判包快照 |
| GET | /api/rooms/{room_id}/packets/{packet_id} | 房主 / 管理员 | 读取并校验快照 SHA256 |
| POST | /api/rooms/{room_id}/packets/{packet_id}/sent | 房主 / 管理员 | 记录主持人已手动发送快照 |
| POST | /api/rooms/{room_id}/verdict | 房主 / 管理员 | 无独立裁判时锁定传统单裁判结论 |
| POST | /api/rooms/{room_id}/judge-verdict | 当前真人裁判 | 锁定自己的独立判决 |
| POST | /api/rooms/{room_id}/ai-judges/verdicts/generate | 房主 / 管理员 | 让模型 AI 裁判自动判断并锁定 |
| POST | /api/rooms/{room_id}/ai-judges/{judge_id}/verdict | 房主 / 管理员 | 手动转录 AI 裁判最终判断 |
| POST | /api/rooms/{room_id}/reveal | 房主 / 管理员 | 所有裁判完成锁定后揭晓身份 |

整场 Log 只允许在比赛结束且记录未被清空时下载。

裁判包是固定内容快照。服务端不会把快照自动发送到外部聊天窗口；sent 接口只记录主持人确认已手动发送。

## 9. WebSocket

连接地址：/ws/rooms/{room_id}

客户端可发送的消息：

~~~json
{
  "type": "typing",
  "active": true,
  "round": 1,
  "match_number": 1
}
~~~

服务端主要推送：

~~~json
{
  "type": "room_updated",
  "room_id": 1,
  "reason": "round_revealed",
  "match_number": 1
}
~~~

以及：

~~~json
{
  "type": "typing_status",
  "room_id": 1,
  "round": 1,
  "match_number": 1,
  "states": {
    "A": true
  }
}
~~~

room_updated 的 reason 对应业务事件，例如 joined、judge_added、judge_question_proposed、round_revealed、new_round、verdict_locked、identity_revealed、match_restarted、records_deleted、room_deleted、model_profile_changed 等。

WebSocket 连接会验证 Origin、Cookie 会话和房间权限。输入状态只向当前房间的人类参赛者发送，不向裁判、AI 或管理员观察视图发送。

## 10. 修改接口时的要求

- 修改请求体时同步更新 Pydantic 模型、前端 payload 和测试。
- 修改权限时同步检查 permission、require_room_manager、require_judge 和 public_room。
- 修改裁判数据响应时，必须确认没有把真实身份或其他裁判判决泄露给裁判视图。
- 修改房间生命周期时，必须考虑 match_number、记录清空、重开和删除。
- 修改模型调用时，不能把网络等待放进 SQLite 写锁中。
- 新增路由必须补充权限测试和正常流程测试。
