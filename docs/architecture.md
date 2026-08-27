# EchoGuide 技术架构图

本文档包含 EchoGuide 系统的核心架构设计图，展示各组件之间的关系和数据流向。

## 目录

1. [系统整体架构](#系统整体架构)
2. [Fast/Deep 双路径架构](#fastdeep-双路径架构)
3. [分层长程记忆架构](#分层长程记忆架构)
4. [Agentic RAG 架构](#agentic-rag-架构)
5. [执行时 Runtime 架构](#执行时-runtime-架构)
6. [MCP Server 架构](#mcp-server-架构)

---

## 系统整体架构

```mermaid
flowchart TB
    subgraph "前端层"
        UI[Vue3 学生界面]
        Chat[对话交互]
        Monitor[监控面板]
    end

    subgraph "API 层 (FastAPI)"
        ChatAPI[/chat, /chat/stream]
        PersonalAPI[/personal/*]
        KnowledgeAPI[/knowledge/*]
        MonitorAPI[/monitor, /metrics]
        MCPAPI[/mcp]
        AuthAPI[/auth/*]
        SystemAPI[/health, /skills]
    end

    subgraph "Runtime 层 (Harness)"
        Orchestrator[编排器<br/>Task DAG 执行]
        TaskAgent[TaskAgent<br/>唯一执行体]
        Intent[级联意图识别]
        Planner[Planner<br/>ExecutionPlan]
        Profile[Profile<br/>Fast/Deep]
    end

    subgraph "记忆层"
        Redis[Redis<br/>工作记忆]
        SQLite[(SQLite<br/>L0/L1/L3)]
        Chroma[(ChromaDB<br/>L2/向量)]
    end

    subgraph "知识层"
        KB[知识库<br/>文档分块]
        Embedding[BGE Embedding<br/>本地推理]
        Rerank[BGE Reranker<br/>本地重排]
    end

    subgraph "工具层"
        Tools[工具管理器<br/>熔断/缓存/降级]
        PersonalTools[个人工具]
        CampusTools[校园工具]
        KnowledgeTools[知识检索工具]
    end

    subgraph "可观测层"
        Tracing[分布式追踪<br/>X-Trace-Id]
        Prometheus[Prometheus 指标]
        MonitorService[监控服务<br/>异常检测]
    end

    subgraph "外部服务"
        DeepSeek[DeepSeek API<br/>V4 Flash/Pro]
        QWeather[和风天气<br/>Open-Meteo 兜底]
    end

    UI --> ChatAPI
    Monitor --> MonitorAPI

    ChatAPI --> Orchestrator
    PersonalAPI --> PersonalTools
    KnowledgeAPI --> KB
    MCPAPI --> Tools
    AuthAPI --> SystemAPI

    Orchestrator --> Intent
    Intent --> Planner
    Planner --> Profile
    Profile --> TaskAgent

    TaskAgent --> Redis
    TaskAgent --> SQLite
    TaskAgent --> Chroma
    TaskAgent --> Tools
    TaskAgent --> KB
    TaskAgent --> DeepSeek

    Tools --> PersonalTools
    Tools --> CampusTools
    Tools --> KnowledgeTools

    KnowledgeTools --> Embedding
    KnowledgeTools --> Rerank

    Orchestrator --> Tracing
    Tools --> Prometheus
    TaskAgent --> MonitorService

    CampusTools --> QWeather

    style Orchestrator fill:#e1f5ff
    style TaskAgent fill:#fff4e1
    style Intent fill:#e8f5e9
    style Planner fill:#f3e5f5
    style Profile fill:#fce4ec
```

---

## Fast/Deep 双路径架构

```mermaid
flowchart LR
    User[学生请求] --> Intent[级联意图识别<br/>domain × action]

    Intent -->|Pattern ≥ 0.90<br/>Embedding 双确认| PatternFree[免费直返]
    Intent -->|Pattern 未达高置信<br/>双确认失败<br/>方向分歧| LLM[LLM 分类/仲裁<br/>携带最近对话]

    PatternFree --> Planner[Planner<br/>ExecutionPlan]
    LLM --> Planner

    Planner -->|1 个 task<br/>single| Fast[Fast<br/>V4 Flash]
    Planner -->|RAG<br/>低置信度| Deep[Deep<br/>V4 Pro]
    Planner -->|多领域<br/>依赖<br/>parallel/dependent| DAG[Harness<br/>分波执行]

    DAG --> Deep

    Fast --> TaskAgent[TaskAgent<br/>READ_ONLY]
    Deep --> TaskAgent

    TaskAgent --> Tools[工具层]

    Tools --> Verifier[Verifier<br/>Grounding]
    Verifier --> Response[回答 + 执行元数据]

    style Fast fill:#fff3e0
    style Deep fill:#e8eaf6
    style LLM fill:#c8e6c9
    style PatternFree fill:#ffecb3
```

**核心设计原则：**

- **级联意图识别**：Pattern 匹配优先，LLM 兜底，双确认机制
- **最小权限原则**：Fast 路径只读，Deep 路径允许写操作
- **成本控制**：Fast 路径无思考模式，Token 预算 768
- **复杂任务路由**：多任务依赖时自动升级 Deep

---

## 分层长程记忆架构

```mermaid
flowchart TB
    subgraph "Redis 工作记忆"
        WorkingMemory[会话近期上下文<br/>不计入 L0-L3]
    end

    subgraph "SQLite 长期记忆"
        L0[(L0 Raw<br/>原始对话全量)]
        L1[(L1 Atom<br/>结构化原子事实<br/>带证据链)]
        L3[(L3 Persona<br/>用户画像历史<br/>版本可回滚)]
        ExtractMarks[extract_marks<br/>增量提炼水位]
        Refs[(refs<br/>上下文卸载)]
    end

    subgraph "ChromaDB 向量记忆"
        L2[(L2 Scenario<br/>场景块向量)]
        ProfileVectors[profile<br/>画像向量]
    end

    subgraph "提炼流程"
        Conversation[对话输入] --> Signal[信号检测]
        Signal --> LLM提炼[LLM 提炼]
        LLM提炼 --> 双产出[双产出机制]
        双产出 --> 画像[L3 画像]
        双产出 --> 事实[L1 事实]
    end

    WorkingMemory --> Conversation
    L0 --> 证据链[证据链溯源]
    事实 --> 证据链
    证据链 --> L0

    Conversation --> WorkingMemory
    WorkingMemory --> 上下文构建[上下文构建]
    L3 --> 上下文构建
    L1 --> 上下文构建
    L2 --> 上下文构建
    Refs --> 上下文构建

    上下文构建 --> Response[响应生成]

    style L0 fill:#ffebee
    style L1 fill:#e8f5e9
    style L2 fill:#e3f2fd
    style L3 fill:#fff3e0
    style WorkingMemory fill:#f3e5f5
```

**记忆层级职责：**

| 层级 | 内容 | 存储 | 写入时机 |
|------|------|------|----------|
| Redis | 会话近期上下文 | Redis | 每轮对话 |
| L0 | 原始对话全量 | SQLite `raw_messages` | 每条消息 |
| L1 | 原子事实 + 证据链 | SQLite `facts` | 信号触发时 |
| L2 | 场景块向量 | ChromaDB `layer=scenario` | 记忆压缩时 |
| L3 | 用户画像历史 | ChromaDB + SQLite | 信号触发时 |

**核心特性：**

- **增量提炼**：`extract_marks` 水位记录，只提炼新消息
- **证据链溯源**：任意高层结论可下钻到 L0 原文
- **上下文卸载**：长工具结果落 `refs`，上下文只留摘要
- **一次双产出**：画像 + 事实零额外成本

---

## Agentic RAG 架构

```mermaid
flowchart TB
    Query[用户查询] --> Rewrite[查询改写<br/>LLM 扩写多角度子查询]

    Rewrite --> 并行召回[并行召回<br/>Top-K]
    并行召回 --> 向量搜索[ChromaDB 向量搜索<br/>BGE Embedding]

    向量搜索 --> 去重[去重]
    去重 --> 重排[重排<br/>BGE Reranker<br/>毫秒级本地]
    重排 -->|min_signal ≥ 0.7| LLM兜底[LLM 兜底重排]
    重排 -->|无判别信号| 保持顺序[保持召回顺序]

    LLM兜底 --> TopK[Top-K 结果]
    保持顺序 --> TopK

    TopK --> Agent[Agent 工具调用]
    Agent --> 引用链路[Grounding 链路<br/>retrieval-first +<br/>sentence-level citation]

    引用链路 --> 证据注入[证据注入<br/>编号证据 [i]]
    证据注入 --> 回答生成[回答生成]

    回答生成 --> 引用检查[引用正确性检查<br/>Dice + BGE 余弦]
    引用检查 --> 最终回答[最终回答 + 可核验来源]

    style Rewrite fill:#c8e6c9
    style 向量搜索 fill:#bbdefb
    style 重排 fill:#ffecb3
    style LLM兜底 fill:#fff3e0
    style 引用链路 fill:#f8bbd0
```

**RAG 优化链路：**

1. **查询改写**：LLM 扩写成多角度子查询
2. **并行召回**：ChromaDB 向量搜索，BGE Embedding 本地推理
3. **重排序**：BGE Reranker cross-encoder 毫秒级，无信号时保持原序
4. **Grounding**：检索优先，句级引用，证据链完整

---

## 执行时 Runtime 架构

```mermaid
flowchart TB
    subgraph "Runtime 核心组件"
        RunState[RunState<br/>单次运行状态]
        ExecutionPolicy[ExecutionPolicy<br/>执行预算控制]
        ModelGateway[ModelGateway<br/>统一模型入口]
        AgentRuntime[AgentRuntime<br/>运行入口]
    end

    subgraph "中间件链"
        Trace[TraceMiddleware<br/>分布式追踪]
        Guard[GuardMiddleware<br/>消息长度 +<br/>注入检测]
        Budget[BudgetMiddleware<br/>step/tool 计数]
        Skill[SkillMiddleware<br/>消息指纹缓存]
    end

    subgraph "执行策略"
        ReadOnly[READ_ONLY<br/>公共工具层 - 写工具]
        WriteAllowed[WRITE_ALLOWED<br/>公共工具层 ∩ 写工具]
    end

    subgraph "权限门禁"
        ToolEffect[工具副作用声明<br/>ToolEffect.WRITE]
        ActionPolicy[动作权限策略<br/>request → 写工具]
        PersonaAction[Persona 动作策略<br/>domain × action]
        TaskCapability[Task 最小权限<br/>allowed_tools 白名单]
    end

    subgraph "质量保证"
        Verifier[Verifier<br/>出口校验]
        Grounding[Grounding<br/>引用检查]
    end

    AgentRuntime --> 中间件链
    中间件链 --> Orchestrator[编排器核心]

    ModelGateway --> Orchestrator
    RunState --> ModelGateway
    ExecutionPolicy --> RunState

    Orchestrator --> TaskAgent[TaskAgent Run]
    TaskAgent --> 执行策略

    执行策略 --> 权限门禁
    权限门禁 --> Tools[工具调用]

    Tools --> Verifier
    Verifier --> Grounding
    Grounding --> Response[响应返回]

    Trace --> ModelGateway
    Budget --> Orchestrator

    style RunState fill:#e8eaf6
    style ExecutionPolicy fill:#fff3e0
    style ModelGateway fill:#c8e6c9
    style Guard fill:#ffcdd2
```

**Runtime 核心组件：**

- **RunState**：单次运行状态、trace_id、计数器、错误记录
- **ExecutionPolicy**：协作目标上限、工具轮次分级、无进展检测
- **ModelGateway**：统一 LLM 调用入口，Token 统计、降级次数
- **AgentRuntime**：运行入口、中间件链编排

**四层权限门禁：**

1. **注册级**：`Tool.effect` 副作用声明
2. **Run 级**：非 request 动作一律 READ_ONLY
3. **Action 级**：动作类型权限过滤
4. **Task 级**：任务最小权限白名单

---

## MCP Server 架构

```mermaid
flowchart TB
    subgraph "MCP 协议层"
        Server[MCPServer<br/>JSON-RPC 2.0]
        Protocol[Streamable HTTP<br/>tools 子集]
        Methods[initialize<br/>tools/list<br/>tools/call]
    end

    subgraph "工具管理层"
        ToolManager[ToolManager<br/>熔断/缓存/降级]
        ToolRegistry[工具注册表<br/>副作用声明]
        WriteTools[写工具集合<br/>自动推导]
    end

    subgraph "内置工具"
        KnowledgeTools[knowledge_search<br/>查询改写/重排]
        PersonalTools[query_schedule<br/>query_todo<br/>add_todo<br/>complete_todo<br/>query_ddl]
        CampusTools[query_campus_info<br/>diagnose_it_issue<br/>query_affairs_process]
        WeatherTools[get_weather]
        MiscTools[calculate_weighted_score]
    end

    subgraph "外部 MCP 工具源"
        External[外部 MCP Client<br/>默认关闭]
        GitHub[GitHub 官方 server<br/>只读工具]
        Proxy[代理支持<br/>国内网络]
    end

    subgraph "权限控制"
        UserAuth[用户认证<br/>Cookie 鉴权]
        ToolVisibility[工具可见性<br/>agent_exposed]
        AccessControl[访问控制<br/>个人工具拒绝]
    end

    Protocol --> Server
    Methods --> Server

    Server --> UserAuth
    Server --> ToolManager

    ToolManager --> ToolRegistry
    ToolManager --> WriteTools

    ToolRegistry --> 内置工具
    内置工具 --> KnowledgeTools
    内置工具 --> PersonalTools
    内置工具 --> CampusTools
    内置工具 --> WeatherTools
    内置工具 --> MiscTools

    ToolManager --> External
    External --> GitHub
    GitHub --> Proxy

    UserAuth --> AccessControl
    ToolVisibility --> AccessControl
    AccessControl --> ToolsCall[工具调用]

    style Server fill:#e3f2fd
    style ToolManager fill:#fff3e0
    style External fill:#f8bbd0
```

**MCP 协议特性：**

- **Streamable HTTP**：JSON-RPC 2.0 标准，SSE 支持
- **工具 Schema 映射**：JSON Schema ↔ MCP inputSchema
- **权限控制**：登录用户才能访问个人工具
- **外部工具源**：可选集成远程 MCP server
- **前缀隔离**：外部工具 `github_*` 前缀避免冲突

---

## 总结

EchoGuide 的核心架构优势：

1. **分层长程记忆**：L0-L3 四层记忆，证据链完整溯源
2. **Fast/Deep 双路径**：成本与质量的最优平衡
3. **级联意图识别**：Pattern + Embedding 双确认 + LLM 兜底
4. **Agentic RAG**：本地向量模型，完整的检索优化链路
5. **Runtime Harness**：统一执行控制，四层权限门禁
6. **MCP 标准协议**：工具生态扩展，可选外部工具源

所有设计都遵循"宁紧勿松"的安全原则，在保证功能完整性的前提下，最大限度降低误判风险。