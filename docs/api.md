# XGuide API

本文档描述当前 XGuide 校园个人 Agent 的 HTTP API。运行中的 OpenAPI/Swagger（`/docs`）是字段约束与完整 Schema 的唯一权威来源；本文说明产品语义、认证边界和常用调用方式。

## 基础约定

| 项目 | 本地开发 | Docker Compose |
|---|---|---|
| 后端 | `http://localhost:8100` | `http://localhost:8100` |
| 同源产品入口 | `http://localhost:8100`（静态托管开启时） | `http://localhost:8088` |
| API 前缀 | 前端/Nginx 使用 `/api/*`；后端路由本身为无前缀路径 | 同左 |
| OpenAPI | `/docs` | `http://localhost:8100/docs` 或 `http://localhost:8088/api/docs` |

浏览器前端应始终携带 Cookie（Fetch 使用 `credentials: 'include'`）。需要认证的接口由当前登录用户决定数据归属，客户端不应传递或伪造用户 ID。

## 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/auth/register` | 注册并写入会话 Cookie；可由 `ECHOGUIDE_ALLOW_REGISTRATION=0` 关闭 |
| POST | `/auth/login` | 登录并写入会话 Cookie |
| POST | `/auth/logout` | 清除当前会话 |
| GET | `/auth/me` | 读取当前登录用户 |
| POST | `/auth/password` | 修改当前用户密码 |

会话 Cookie 为 HttpOnly、SameSite=Lax，默认有效期为 7 天。生产环境必须配置非默认的 `JWT_SECRET_KEY`。

## Personal Hub

这些接口需要登录，数据仅限当前用户。Today 是供产品首页直接使用的聚合接口；课表和待办接口适合编辑页或导入流程。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/personal/today` | 当天课程、下一节课、今日待办、未来 7 天 DDL/考试与提醒 |
| GET | `/personal/reminders` | 今天/明天课程与近期 DDL、考试提醒 |
| GET | `/personal/free-time?when=今天` | 按日期表达式计算可用时间段 |
| POST | `/personal/schedule/import` | 用 JSON 导入课表 |
| POST | `/personal/schedule/import/file` | 上传 `.ics` 或支持的课表文件 |
| GET / DELETE | `/personal/schedule` | 读取或清空课表 |
| POST / GET | `/personal/todo` | 创建或读取待办、DDL、考试 |
| PATCH | `/personal/todo/{todo_id}` | 修改内容、类型或截止时间 |
| POST | `/personal/todo/{todo_id}/complete?done=true` | 标记完成或恢复 |
| DELETE | `/personal/todo/{todo_id}` | 删除事项 |
| GET | `/personal/overview` | 兼容既有客户端的个人概览 |

创建待办的核心字段示例：

```json
{
  "content": "提交竞赛报名材料",
  "kind": "ddl",
  "due_at": "2026-09-10 18:00"
}
```

`kind` 取值为 `todo`、`ddl` 或 `exam`。时间字段采用 `YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM`。

## Campus Radar、Inbox 与行动计划

Campus Radar 仅访问无需登录的官方公开页面。同步请求不会携带学生画像或登录 Cookie 给外部网站；每个事件保留 `source_url`，页面应让用户回到官方原文核验。除校内公开源外，已接入研创网（中国研究生创新实践系列大赛）的年度公开赛程：每项主题赛事（含赛程表中单列的子赛道）会被拆为独立事件，报名截止日未过时才会依据用户画像进入 Inbox。新投递的 Inbox 通知默认保留 48 小时；删除仅清除当前用户的收件箱记录，不会删除公共来源事件。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/student-profile` | 获取当前用户的通知筛选条件 |
| PUT | `/student-profile` | 保存学院、专业、年级、学历层次和关注方向 |
| POST | `/inbox/refresh` | 同步公开通知源，返回已检查/新增/更新/未变更数量及来源错误 |
| GET | `/inbox?status=active` | 读取个性化 Inbox；状态可为 `active`、`all`、`new`、`seen`、`interested`、`ignored` |
| GET | `/inbox/briefing` | 返回今日关注、个性化推荐、事务分类、其他动态及同主题通知时间线 |
| POST | `/inbox/{event_id}/status` | 设置为 `seen`、`interested` 或 `ignored` |
| DELETE | `/inbox` | 传入 `event_ids` 批量删除；传空数组或空请求体一键清空当前用户 Inbox |
| POST | `/inbox/{event_id}/add-to-plan` | 依据通知原文中已提取的材料/动作创建个人待办 |

学生画像请求体示例：

```json
{
  "college": "计算机科学与技术学院",
  "major": "软件工程",
  "grade": "2027届",
  "education": "本科生",
  "interests": ["竞赛", "保研"]
}
```

设置 Inbox 状态：

```json
{ "status": "interested" }
```

`add-to-plan` 的响应外层包含 `message` 和 `plan`；`plan` 内含 `plan_id`、来源事件、证据说明和已创建的 `items`。该接口只转换通知正文中可核验的材料和动作；不会凭空补写办理步骤、资格条件或截止日期。

```json
{
  "message": "已生成个人行动计划",
  "plan": {
    "plan_id": "abcd1234ef56",
    "event": {"id": 12, "source_url": "https://example.edu/notice/12"},
    "items": []
  }
}
```

## 对话与知识

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/chat` | 可选登录 | 非流式对话 |
| POST | `/chat/stream` | 可选登录 | SSE 流式对话 |
| POST | `/search` | 可选登录 | RAG 查询改写、召回与重排演示 |
| POST | `/knowledge/add` | 管理员 | 写入结构化知识文档 |
| POST | `/knowledge/upload` | 管理员 | 上传知识文档 |
| GET | `/knowledge/stats` | 管理员 | 知识库统计 |

最小对话请求：

```json
{ "message": "明天第一节课在哪里？" }
```

流式端点返回 SSE 事件。前端主要处理 `hello`、`meta`、`tool`、`delta`、`done` 与 `error`；`done` 包含最终回复和执行元数据。执行元数据不包含思维链、完整 Prompt 或个人上下文。

## 系统、观测与 MCP

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/health` | 公开 | 运行健康状态 |
| GET / POST | `/skills`、`/skills/reload` | 后者管理员 | 查看或重载 Skills |
| GET / POST | `/campus/info`、`/campus/reload` | 后者管理员 | 查询或重载本地结构化校园信息 |
| GET | `/monitor`、`/metrics` | 观测权限 | 运行摘要与 Prometheus 指标 |
| GET | `/traces`、`/traces/{trace_id}` | 观测权限 | 请求 Trace 列表和详情 |
| POST | `/eval/run` | 管理员 | 触发评测 |
| POST | `/mcp` | 按工具授权 | MCP Streamable HTTP tools 子集 |
| GET | `/mcp/info` | 公开 | MCP Server 元数据 |

MCP 使用 JSON-RPC 2.0，支持 `initialize`、`tools/list` 和 `tools/call`。登录用户的 Cookie 同样适用于其个人工具；未登录客户端只能使用公开工具。

## 错误处理

错误响应采用 FastAPI 标准格式：

```json
{ "detail": "错误说明" }
```

| 状态码 | 含义 |
|---|---|
| 400 | 参数、日期表达式或请求体无效 |
| 401 | 未登录或会话无效 |
| 403 | 权限不足 |
| 404 | 资源不存在，或不属于当前用户 |
| 409 | 注册用户名冲突 |
| 503 | 初始化中的服务组件不可用 |

## 相关文档

- [产品与运行说明](../README.md)
- [前端说明](../frontend/README.md)
- [技术架构](./architecture.md)
