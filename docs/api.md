# EchoGuide API 详细文档

本文档详细描述 EchoGuide 系统的所有 API 端点、请求/响应格式、使用示例和注意事项。

## 目录

1. [API 概览](#api-概览)
2. [认证 API](#认证-api)
3. [对话 API](#对话-api)
4. [个人数据 API](#个人数据-api)
5. [知识库 API](#知识库-api)
6. [监控与评测 API](#监控与评测-api)
7. [MCP 协议 API](#mcp-协议-api)
8. [系统 API](#系统-api)
9. [错误处理](#错误处理)
10. [认证机制](#认证机制)

---

## API 概览

### 基础信息

| 项目 | 说明 |
|------|------|
| Base URL | `http://localhost:8100` (本地) / `http://localhost:8088` (Docker) |
| API 前缀 | `/api/*` (前端同源托管，API 路由自动剥离前缀) |
| 认证方式 | Cookie 签名 (JWT) |
| 响应格式 | JSON |
| 字符编码 | UTF-8 |

### 全局响应头

| 响应头 | 说明 |
|--------|------|
| `X-Trace-Id` | 全链路追踪 ID，用于问题排查和监控 |
| `Content-Type` | `application/json` |

---

## 认证 API

### 注册用户

**端点**：`POST /api/auth/register`

**请求体**：
```json
{
  "username": "student001",
  "password": "password123"
}
```

**参数说明**：
| 参数 | 类型 | 必填 | 约束 | 说明 |
|------|------|------|------|------|
| username | string | 是 | 3-32 字符，字母数字._- | 用户名，不区分大小写 |
| password | string | 是 | 6-128 字符 | 密码 |

**响应**：
```json
{
  "authenticated": true,
  "user": {
    "id": 1,
    "username": "student001",
    "role": "user",
    "created_at": "2026-08-27T10:30:00"
  }
}
```

**状态码**：
- `201 Created`：注册成功
- `409 Conflict`：用户名已存在
- `403 Forbidden`：未开放注册 (`ECHOGUIDE_ALLOW_REGISTRATION=0`)

**Cookie**：
- `SESSION_COOKIE`：7 天有效，HttpOnly，SameSite=lax

---

### 登录

**端点**：`POST /api/auth/login`

**请求体**：
```json
{
  "username": "student001",
  "password": "password123"
}
```

**响应**：
```json
{
  "authenticated": true,
  "user": {
    "id": 1,
    "username": "student001",
    "role": "user",
    "created_at": "2026-08-27T10:30:00"
  }
}
```

**状态码**：
- `200 OK`：登录成功
- `401 Unauthorized`：用户名或密码错误

---

### 登出

**端点**：`POST /api/auth/logout`

**响应**：
```json
{
  "authenticated": false
}
```

**状态码**：`200 OK`

---

### 获取当前用户信息

**端点**：`GET /api/auth/me`

**响应**（已登录）：
```json
{
  "authenticated": true,
  "user": {
    "id": 1,
    "username": "student001",
    "role": "user",
    "created_at": "2026-08-27T10:30:00"
  }
}
```

**响应**（未登录）：
```json
{
  "authenticated": true,
  "user": null
}
```

**状态码**：`200 OK`

---

### 修改密码

**端点**：`POST /api/auth/password`

**需要认证**：是

**请求体**：
```json
{
  "current_password": "password123",
  "new_password": "newpassword456"
}
```

**响应**：
```json
{
  "message": "密码已修改"
}
```

**状态码**：
- `200 OK`：修改成功
- `400 Bad Request`：当前密码错误或密码格式错误
- `401 Unauthorized`：未登录

---

## 对话 API

### 主对话接口

**端点**：`POST /api/chat`

**请求体**：
```json
{
  "message": "明天第一节课在哪里？",
  "user_id": "student001",
  "conv_id": "conv_123456"
}
```

**参数说明**：
| 参数 | 类型 | 必填 | 约束 | 说明 |
|------|------|------|------|------|
| message | string | 是 | 1-2000 字符 | 用户消息 |
| user_id | string | 否 | 1-64 字符 | 用户标识，默认 "anonymous" |
| conv_id | string | 否 | 最多 80 字符 | 会话 ID，自动生成 |

**响应**：
```json
{
  "conv_id": "conv_123456",
  "response": "明天第一节课（08:30-10:05）在北校区教学楼 A302。",
  "intent": "personal",
  "domain": "personal",
  "action": "query",
  "agent_type": "Fast",
  "latency_ms": 1250.5,
  "knowledge_used": false,
  "cached": false,
  "execution": {
    "mode": "adaptive",
    "profile": "fast",
    "classifier_stage": "pattern",
    "complexity_reason": "单任务确定性查询",
    "agents": ["Fast"],
    "tools": ["query_schedule"],
    "tasks": [],
    "model": "deepseek-v4-flash",
    "trace_id": "trace_abc123",
    "input_tokens": 450,
    "output_tokens": 120,
    "memory_trace": {
      "l3_profile_hits": 1,
      "l1_facts_hits": 0,
      "refs_used": 0
    }
  }
}
```

**响应头**：
- `X-Trace-Id`：全链路追踪 ID

**状态码**：
- `200 OK`：成功
- `503 Service Unavailable`：服务未就绪

---

### 流式对话接口（SSE）

**端点**：`POST /api/chat/stream`

**事件序列**：
```javascript
// 1. hello 事件
data: {"type":"hello","conv_id":"conv_123456"}

// 2. meta 事件
data: {"type":"meta","domain":"personal","action":"query","agent":"Fast","cached":false}

// 3. tool 事件
data: {"type":"tool","name":"query_schedule","status":"running"}

// 4. delta 事件（多个）
data: {"type":"delta","text":"明天"}
data: {"type":"delta","text":"第一节课"}

// 5. done 事件
data: {
  "type":"done",
  "conv_id":"conv_123456",
  "response":"明天第一节课在 A302...",
  "intent":"personal",
  "agent_type":"Fast",
  "latency_ms":1250.5,
  "knowledge_used":false,
  "cached":false,
  "execution":{...}
}

// 6. error 事件（出错时）
data: {"type":"error","message":"工具调用超时"}
```

**响应头**：
- `Content-Type: text/event-stream`
- `Cache-Control: no-cache`
- `Connection: keep-alive`
- `X-Accel-Buffering: no`

**状态码**：
- `200 OK`：成功（SSE 流）
- `503 Service Unavailable`：服务未就绪

---

### RAG 检索演示

**端点**：`POST /api/search`

**查询参数**：
- `query` (必填)：检索查询，1-500 字符
- `top_k` (可选)：返回数量，1-20，默认 5

**请求示例**：
```bash
POST /api/search?query=选课流程&top_k=5
```

**响应**：
```json
{
  "query": "选课流程",
  "results": [
    {
      "title": "西电本科生选课指南",
      "content": "西电选课通过教务系统进行...",
      "domain": "academic",
      "source_url": "https://xxx.xidian.edu.cn/guide",
      "updated_at": "2026-08-01",
      "score": 0.92
    }
  ],
  "reranked": true
}
```

**状态码**：
- `200 OK`：成功
- `503 Service Unavailable`：工具管理器未初始化

---

## 个人数据 API

> 所有个人数据 API 都需要认证

### 导入课程表

**端点**：`POST /api/personal/schedule/import`

**请求体**（JSON 格式）：
```json
{
  "user_id": "student001",
  "courses": [
    {
      "course": "高等数学",
      "day_of_week": 1,
      "start_time": "08:30",
      "end_time": "10:05",
      "location": "A302",
      "weeks": "1-16",
      "note": ""
    }
  ]
}
```

**请求体**（ICS 格式）：
```json
{
  "user_id": "student001",
  "ics_text": "BEGIN:VCALENDAR..."
}
```

**参数说明**：
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| user_id | string | 是 | 用户标识 |
| courses | array | 二选一 | JSON 课表列表 |
| ics_text | string | 二选一 | ICS 文本内容 |

**响应**：
```json
{
  "message": "课表导入成功，共 1 门课程",
  "courses": 1
}
```

---

### 上传课表文件

**端点**：`POST /api/personal/schedule/import/file`

**请求格式**：`multipart/form-data`

**表单字段**：
- `file` (必填)：.ics 或 .json 文件，最大 5MB
- `user_id` (可选)：用户标识，默认 "anonymous"

**响应**：
```json
{
  "message": "文件 schedule.ics 导入成功",
  "courses": 8
}
```

**状态码**：
- `200 OK`：成功
- `413 Payload Too Large`：文件超过 5MB
- `400 Bad Request`：文件格式不支持或解析失败

---

### 查看课表

**端点**：`GET /api/personal/schedule`

**响应**：
```json
{
  "user_id": "student001",
  "week_num": 3,
  "monday": "2026-08-25",
  "courses": [
    {
      "course": "高等数学",
      "day_of_week": 1,
      "start_time": "08:30",
      "end_time": "10:05",
      "location": "A302",
      "weeks": "1-16",
      "note": ""
    }
  ],
  "total": 8
}
```

---

### 清空课表

**端点**：`DELETE /api/personal/schedule`

**响应**：
```json
{
  "message": "课表已清空"
}
```

---

### 添加待办

**端点**：`POST /api/personal/todo`

**请求体**：
```json
{
  "user_id": "student001",
  "content": "提交实验报告",
  "kind": "ddl",
  "due_at": "2026-08-30 23:59"
}
```

**参数说明**：
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| user_id | string | 是 | 用户标识 |
| content | string | 是 | 事项内容，1-500 字符 |
| kind | string | 否 | 类型：todo/ddl/exam，默认 "todo" |
| due_at | string | 否 | 截止时间，格式 "YYYY-MM-DD" 或 "YYYY-MM-DD HH:MM" |

**响应**：
```json
{
  "message": "已记录",
  "todo": {
    "id": 1,
    "content": "提交实验报告",
    "kind": "ddl",
    "due_at": "2026-08-30 23:59",
    "done": 0,
    "created_at": "2026-08-27T10:30:00",
    "completed_at": null
  }
}
```

---

### 查看待办

**端点**：`GET /api/personal/todo?status=open`

**查询参数**：
- `status` (可选)：状态，open/done/all，默认 "open"

**响应**：
```json
{
  "user_id": "student001",
  "status": "open",
  "todos": [
    {
      "id": 1,
      "content": "提交实验报告",
      "kind": "ddl",
      "due_at": "2026-08-30 23:59",
      "done": 0,
      "created_at": "2026-08-27T10:30:00",
      "completed_at": null
    }
  ],
  "total": 1
}
```

---

### 标记待办完成

**端点**：`POST /api/personal/todo/{todo_id}/complete?done=true`

**路径参数**：
- `todo_id`：待办 ID

**查询参数**：
- `done` (可选)：true/false，默认 true

**响应**：
```json
{
  "message": "已标记完成",
  "todo": {
    "id": 1,
    "content": "提交实验报告",
    "kind": "ddl",
    "due_at": "2026-08-30 23:59",
    "done": 1,
    "created_at": "2026-08-27T10:30:00",
    "completed_at": "2026-08-27T10:35:00"
  }
}
```

**状态码**：
- `200 OK`：成功
- `404 Not Found`：待办不存在或不属于该用户

---

### 删除待办

**端点**：`DELETE /api/personal/todo/{todo_id}`

**响应**：
```json
{
  "message": "已删除"
}
```

---

### 当日汇总

**端点**：`GET /api/personal/overview`

**响应**：
```json
{
  "today_courses": [
    {
      "course": "高等数学",
      "time": "08:30-10:05",
      "location": "A302"
    }
  ],
  "today_todos": [
    {
      "id": 1,
      "content": "提交实验报告",
      "due_at": "2026-08-27 23:59",
      "kind": "ddl"
    }
  ],
  "upcoming_ddls": [
    {
      "days_left": 3,
      "title": "提交实验报告",
      "due_at": "2026-08-30"
    }
  ]
}
```

---

## 知识库 API

> 所有知识库 API 都需要管理员权限

### 批量导入文档

**端点**：`POST /api/knowledge/add`

**请求体**：
```json
{
  "documents": [
    {
      "title": "西电选课指南",
      "content": "西电选课通过教务系统进行，分预选、正选、退改选阶段...",
      "domain": "academic",
      "source_url": "https://xxx.xidian.edu.cn/guide",
      "updated_at": "2026-08-01T00:00:00",
      "valid_from": "2026-08-01T00:00:00",
      "source_status": "official"
    }
  ]
}
```

**参数说明**：
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| documents | array | 是 | 文档列表，1-100 条 |
| title | string | 是 | 文档标题，1-200 字符 |
| content | string | 是 | 文档内容，1-100,000 字符 |
| domain | string | 否 | 领域：academic/campus_life/affairs/it_help/general |
| source_url | string | 否 | 来源 URL，最多 1000 字符 |
| updated_at | datetime | 否 | 更新时间 |
| valid_from | datetime | 否 | 生效时间 |
| source_status | string | 否 | 状态：official/unverified/sample/stale |

**响应**：
```json
{
  "message": "成功导入 3 个文档片段",
  "added_chunks": 3,
  "total_chunks": 156
}
```

---

### 上传文件导入

**端点**：`POST /api/knowledge/upload`

**请求格式**：`multipart/form-data`

**表单字段**：
- `file` (必填)：文件，最大 10MB

**支持格式**：
- 文本：.txt, .md
- JSON：.json, .jsonl
- 文档：.pdf, .doc, .docx, .ppt, .pptx, .xls, .xlsx, .odt, .odp, .rtf, .epub, .csv

**响应**：
```json
{
  "message": "文件 guide.pdf 导入成功",
  "added_chunks": 8,
  "total_chunks": 164
}
```

**状态码**：
- `200 OK`：成功
- `413 Payload Too Large`：文件超过 10MB
- `400 Bad Request`：文件解析失败或无文本层

---

### 知识库统计

**端点**：`GET /api/knowledge/stats`

**响应**：
```json
{
  "total_chunks": 164
}
```

---

## 监控与评测 API

### 实时监控摘要

**端点**：`GET /api/monitor`

**需要权限**：observability（观测凭据）

**响应**：
```json
{
  "success_rate": 0.964,
  "avg_latency_ms": 7594,
  "p50_latency_ms": 6500,
  "p95_latency_ms": 83517,
  "in_flight_requests": 2,
  "profile_stats": {
    "fast": {
      "success_rate": 0.95,
      "avg_latency_ms": 1200,
      "in_flight": 1
    },
    "deep": {
      "success_rate": 0.98,
      "avg_latency_ms": 8500,
      "in_flight": 1
    }
  },
  "alerts": [
    {
      "level": "warning",
      "message": "Fast 路径成功率低于阈值",
      "timestamp": "2026-08-27T10:30:00"
    }
  ],
  "verification": {
    "total_runs": 28,
    "flags_count": 1,
    "citation_missing": 0,
    "write_claim_without_tool": 1,
    "retrieval_missing": 0
  }
}
```

---

### Prometheus 指标

**端点**：`GET /api/metrics`

**说明**：Prometheus 指标入口，无认证要求，仅包含非敏感数据

**响应格式**：Prometheus 文本格式

```
# HELP echoguide_request_total Total number of requests
# TYPE echoguide_request_total counter
echoguide_request_total{method="POST",endpoint="/api/chat",status="200"} 1523

# HELP echoguide_latency_seconds Request latency in seconds
# TYPE echoguide_latency_seconds histogram
echoguide_latency_seconds_bucket{le="0.1"} 100
echoguide_latency_seconds_bucket{le="1.0"} 800
echoguide_latency_seconds_bucket{le="+Inf"} 1000
```

---

### 最近 Trace 列表

**端点**：`GET /api/traces?limit=20`

**需要权限**：observability

**查询参数**：
- `limit` (可选)：返回数量，1-200，默认 20

**响应**：
```json
{
  "traces": [
    {
      "trace_id": "trace_abc123",
      "request": "明天第一节课在哪里？",
      "user_id": "student001",
      "domain": "personal",
      "action": "query",
      "agent_type": "Fast",
      "latency_ms": 1250.5,
      "timestamp": "2026-08-27T10:30:00",
      "status": "success"
    }
  ]
}
```

---

### Trace 详情

**端点**：`GET /api/traces/{trace_id}`

**需要权限**：observability

**响应**：
```json
{
  "trace_id": "trace_abc123",
  "spans": [
    {
      "name": "memory_read",
      "start_ms": 0,
      "duration_ms": 15,
      "metadata": {}
    },
    {
      "name": "intent_recognition",
      "start_ms": 20,
      "duration_ms": 120,
      "metadata": {
        "stage": "pattern",
        "confidence": 0.95
      }
    },
    {
      "name": "tool_call",
      "start_ms": 150,
      "duration_ms": 800,
      "metadata": {
        "tool": "query_schedule",
        "success": true
      }
    }
  ],
  "total_duration_ms": 1250.5,
  "timestamp": "2026-08-27T10:30:00"
}
```

**状态码**：
- `200 OK`：成功
- `404 Not Found`：trace 不存在

---

### 运行评测

**端点**：`POST /api/eval/run`

**需要权限**：管理员

**请求体**（可选）：
```json
{
  "intent_cases": [
    {
      "message": "我要查课表",
      "expected_intent": "personal",
      "context": {}
    }
  ],
  "dialog_cases": [
    {
      "question": "西电选课流程是什么？",
      "golden_answer": "西电选课通过教务系统进行，分预选、正选、退改选阶段..."
    }
  ],
  "routing_cases": [],
  "retrieval_cases": [],
  "promote_baseline": false
}
```

**响应**：
```json
{
  "pass_rate": 0.964,
  "total": 28,
  "passed": 27,
  "avg_scores": {
    "intent_accuracy": 0.962,
    "dialog_relevance": 0.9,
    "retrieval_precision": 1.0,
    "retrieval_recall": 0.95
  },
  "regressions": [],
  "recommendations": [
    "建议增加校车班次查询的测试用例"
  ],
  "retrieval": {
    "hit_rate@5": 1.0,
    "recall@5": 0.95,
    "mrr": 0.84
  },
  "provenance": {
    "citation_correct": 1.0,
    "evidence_chain_complete": 1.0
  },
  "judge": "llm_evaluator_v1",
  "results": [
    {
      "test_id": "test_001",
      "passed": true,
      "scores": {
        "intent_accuracy": 1.0,
        "dialog_relevance": 0.95
      },
      "detail": "意图识别正确，回答相关性强",
      "metadata": {}
    }
  ]
}
```

---

## MCP 协议 API

### MCP Streamable HTTP 端点

**端点**：`POST /api/mcp`

**请求头**：
```
Content-Type: application/json
Accept: text/event-stream
MCP-Protocol-Version: 2025-11-25
```

**请求体**（JSON-RPC 2.0）：
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/list"
}
```

**支持方法**：
- `initialize`：初始化会话
- `tools/list`：列出可用工具
- `tools/call`：调用工具

**响应**：
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "tools": [
      {
        "name": "query_schedule",
        "description": "查询用户课程表",
        "inputSchema": {
          "type": "object",
          "properties": {
            "when": {"type": "string"}
          }
        }
      }
    ]
  }
}
```

**状态码**：
- `200 OK`：成功
- `202 Accepted`：通知请求
- `400 Bad Request`：非法 UTF-8 或不支持版本
- `405 Method Not Allowed`：GET 请求返回 405

---

### MCP 信息端点

**端点**：`GET /api/mcp/info`

**响应**：
```json
{
  "server": "echoguide-mcp",
  "protocolVersion": "2025-11-25",
  "tools": [
    {
      "name": "knowledge_search",
      "description": "知识库检索工具",
      "inputSchema": {...}
    }
  ],
  "note": "POST /mcp 为 MCP Streamable HTTP tools 子集；GET /mcp 明确返回 405。"
}
```

---

## 系统 API

### 健康检查

**端点**：`GET /api/health`

**响应**：
```json
{
  "status": "ok",
  "agents": {
    "total_calls": 1523,
    "success_rate": 0.964
  },
  "verification": {
    "total_runs": 28,
    "flags_count": 1
  }
}
```

**状态码**：
- `200 OK`：服务正常
- `503 Service Unavailable`：服务未就绪

---

### Skills 摘要

**端点**：`GET /api/skills`

**响应**：
```json
{
  "loaded_skills": 5,
  "skills": [
    {
      "name": "academic_selection",
      "domain": "academic",
      "triggers": ["选课", "课程"],
      "loaded": true,
      "error": null
    }
  ]
}
```

**状态码**：
- `200 OK`：成功
- `503 Service Unavailable`：Skills 未初始化

---

### 重新加载 Skills

**端点**：`POST /api/skills/reload`

**需要权限**：管理员

**响应**：
```json
{
  "loaded_skills": 6,
  "skills": [...]
}
```

---

### 查询校园信息

**端点**：`GET /api/campus/info?category=shuttle&keyword=北校区`

**查询参数**：
- `category` (可选)：shuttle/buildings/venues/library，默认 "shuttle"
- `keyword` (可选)：搜索关键词

**响应**：
```json
{
  "category": "shuttle",
  "keyword": "北校区",
  "results": [
    {
      "line": "北校区线",
      "next_bus": "10:35",
      "stops": ["南校区北门", "北校区南门"]
    }
  ]
}
```

---

### 热加载校园信息

**端点**：`POST /api/campus/reload`

**需要权限**：管理员

**响应**：
```json
{
  "status": "success",
  "loaded_count": 156
}
```

---

## 错误处理

### 标准错误响应格式

```json
{
  "detail": "错误信息"
}
```

### 常见错误码

| 状态码 | 说明 | 示例 |
|--------|------|------|
| 400 Bad Request | 请求参数错误 | 无效的请求体格式 |
| 401 Unauthorized | 未认证 | 缺少或无效的 Cookie |
| 403 Forbidden | 权限不足 | 非管理员访问知识库 |
| 404 Not Found | 资源不存在 | Trace ID 不存在 |
| 409 Conflict | 资源冲突 | 用户名已存在 |
| 413 Payload Too Large | 请求体过大 | 文件超过大小限制 |
| 503 Service Unavailable | 服务未就绪 | 组件未初始化 |

### Verifier Flags

当响应中包含 `verification` 字段时，可能包含以下 flag：

| Flag | 说明 |
|------|------|
| `citation_missing` | 回答有 `[n]` 引用但无对应证据 |
| `write_claim_without_tool` | 声称写操作但无成功工具调用 |
| `retrieval_missing` | 意图判定需要知识但执行链无检索证据 |

---

## 认证机制

### Cookie 签名认证

**Cookie 名称**：`SESSION_COOKIE`

**Cookie 属性**：
- `HttpOnly`：防止 XSS 攻击
- `SameSite=lax`：防止 CSRF 攻击
- `Secure`：仅 HTTPS 传输（生产环境）

**Token 内容**：
```json
{
  "user_id": 1,
  "username": "student001",
  "role": "user",
  "exp": 1732584600
}
```

**密钥配置**：
- 开发环境：`SECRET_KEY=change_this_in_production`
- 生产环境：`JWT_SECRET_KEY` 必须设置，否则拒绝启动

### 权限等级

| 权限 | 说明 | 可用 API |
|------|------|----------|
| `anonymous` | 匿名用户 | 公共工具，知识检索 |
| `user` | 普通用户 | 个人数据，专属工具 |
| `admin` | 管理员 | 知识库管理，评测 |
| `observability` | 观测权限 | 监控，Trace 查看 |

### 装饰器说明

| 装饰器 | 说明 |
|--------|------|
| `optional_user` | 可选用户认证，未登录返回 `user=None` |
| `require_user` | 必须用户认证，未登录返回 401 |
| `require_admin` | 必须管理员，非管理员返回 403 |
| `require_observability` | 需要观测权限，权限不足返回 403 |

---

## 使用示例

### Python 客户端

```python
import httpx

base_url = "http://localhost:8100"

# 登录
response = httpx.post(
    f"{base_url}/api/auth/login",
    json={"username": "student001", "password": "password123"}
)
cookies = response.cookies

# 查询课表
response = httpx.get(
    f"{base_url}/api/personal/schedule",
    cookies=cookies
)
schedule = response.json()

# 对话
response = httpx.post(
    f"{base_url}/api/chat",
    json={
        "message": "明天第一节课在哪里？",
        "user_id": "student001"
    },
    cookies=cookies
)
answer = response.json()
```

### JavaScript 客户端（Fetch API）

```javascript
const baseUrl = 'http://localhost:8100';

// 登录
const loginResponse = await fetch(`${baseUrl}/api/auth/login`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  credentials: 'include',
  body: JSON.stringify({
    username: 'student001',
    password: 'password123'
  })
});

// 查询课表
const scheduleResponse = await fetch(`${baseUrl}/api/personal/schedule`, {
  method: 'GET',
  credentials: 'include'
});
const schedule = await scheduleResponse.json();

// 对话
const chatResponse = await fetch(`${baseUrl}/api/chat`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  credentials: 'include',
  body: JSON.stringify({
    message: '明天第一节课在哪里？',
    user_id: 'student001'
  })
});
const answer = await chatResponse.json();
```

---

## 最佳实践

### 错误处理

```python
import httpx

try:
    response = httpx.post(
        f"{base_url}/api/chat",
        json={"message": "test"},
        timeout=30.0
    )
    response.raise_for_status()
    data = response.json()
except httpx.HTTPStatusError as e:
    print(f"HTTP 错误: {e.response.status_code}")
    print(f"详细信息: {e.response.json()}")
except httpx.TimeoutException:
    print("请求超时")
except httpx.RequestError as e:
    print(f"网络错误: {e}")
```

### 重试策略

```python
import httpx
import time

def call_with_retry(url, max_retries=3):
    for attempt in range(max_retries):
        try:
            response = httpx.post(url, json={"message": "test"})
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500 and attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # 指数退避
                continue
            raise
```

### 流式响应处理

```python
import httpx

def stream_chat(message):
    with httpx.stream(
        'POST',
        f"{base_url}/api/chat/stream",
        json={"message": message},
        timeout=None
    ) as response:
        for line in response.iter_lines():
            if line.startswith('data: '):
                data = json.loads(line[6:])
                handle_sse_event(data)
```

---

## 注意事项

1. **Cookie 携带**：所有需要认证的 API 都需要携带 Cookie（`credentials: 'include'`）
2. **用户隔离**：所有个人数据 API 都会自动使用认证用户 ID，无需手动传递
3. **Token 预算**：注意 API 响应中的 `input_tokens` 和 `output_tokens`，避免超限
4. **超时处理**：对话请求建议设置 30 秒超时，复杂任务可能更长
5. **缓存利用**：相同查询可能命中缓存，关注 `cached` 字段
6. **监控利用**：生产环境使用 `/api/metrics` 进行监控集成

---

## 版本说明

| 版本 | 发布日期 | 主要变更 |
|------|----------|----------|
| 1.0.0 | 2026-08-27 | 初始版本，完整 API 文档 |

---

## 相关文档

- [技术架构图](./architecture.md)
- [数据库 ER 图](./database-er.md)
- [项目 README](../README.md)