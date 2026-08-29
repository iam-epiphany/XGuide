# EchoGuide 面试故事线（发现问题 → 解决问题）

> **用法**：面试前把每条弧线的「3 分钟版」读熟，讲的时候用自己的话 + 在白板/纸上画图。
> 「1 分钟版」用于项目介绍环节串联。
> 所有代码位置在 `agents/`、`runtime/`、`mcp/`、`memory/`、`api/` 下，面试前至少精读一遍每条弧线标注的核心文件。
> **不要背稿**——面试官追问细节时，背稿痕迹会被一眼看穿；要能讲到"我当时为什么这么想"。

---

## 故事线 0：项目概览（30 秒电梯陈述）

EchoGuide 是一个面向校园场景的自研 Agent Runtime：FastAPI 服务 + Task DAG 编排 + Agentic RAG + L0-L3 分层记忆 + MCP 风格工具框架，外加一套完整的控制面（Guard / 预算 / 熔断 / 重试 / 超时 / 结构化 Trace / 评测闭环）。

一句话定位：**"我做的不是调用 LLM 的聊天机器人，而是一个让 LLM 在受控边界内干活的运行时"**——业务 Agent 保持薄，可靠性全部由 Runtime 收口。

---

## 故事线 1：工具调用死循环与资源失控

**现象**：Agent 的 tool-calling 循环是"模型决定→执行工具→结果回填→再问模型"，模型是概率系统，没有任何硬性保证它会停下来——可能反复调用同一个工具（幻觉重试同参）、或在"调工具→拿结果→继续调"里打转，每转一圈烧一次模型调用。

**定位与根因**：只设"轮次上限"能止损但太粗（每轮都是真金白银的模型调用）；完全不设则理论上无限循环。真正的问题是：**循环的终止不能只靠模型自觉，必须由运行时强制**。

**方案（双保险 + 预算闸）**：
1. **分级轮次上限**：Fast 3 轮 / Deep 5 轮（复杂任务留余量），超限强制收尾——`agents/roles.py:495-502`，配置在 `runtime/policy.py:36-37`（`ECHOGUIDE_RUNTIME_MAX_TOOL_ROUNDS_*`）。
2. **无进展检测**：对每轮工具调用算签名（工具名 + 参数，参数排序无关），连续 2 轮签名完全重复 → 判定死循环 → 强制收尾——`agents/roles.py:593-606`（`_tool_round_signature`）。硬上限是兜底，签名检测是省预算。
3. **预算中间件**（第二道闸）：单请求模型调用次数 / 工具调用总次数上限，超限抛 `BudgetExceeded`，由 Runtime 统一收口返回拒绝文案——`runtime/middlewares.py:62-84`。
4. **收尾语义**：轮次用尽仍补占位 `tool_result` 走普通收尾，保证用户拿到最终答复而不是空响应——`agents/roles.py:617-645`。

**验证**：`tests/test_tool_rounds.py`（全离线确定性测试）。

**3 分钟版**：讲"双保险"的设计取舍——为什么不是只设上限（浪费预算），为什么不是只做重复检测（模型可能换着参数打转，检测不到），所以两者叠加，再加一层全请求预算闸。强调"终止是运行时职责，不是模型自觉"。

**1 分钟版**：Agent 循环有分级轮次上限 + 签名级无进展检测 + 全请求预算闸三层防护，超限后仍然收尾给用户答复而不是静默失败。

---

## 故事线 2：模型服务不稳定（重试 / 熔断 / 降级 / 监控反馈）

**现象**：LLM API 有瞬时失败（超时、429、5xx），供应商会故障，成本会失控。朴素写法是每个调用点各写各的 try/except——漏一个就裸奔。

**根因**：外部依赖不可靠是常态；而且"调用点分散"导致没法统一做重试、熔断、预算、统计。

**方案（控制面收口）**：
1. **ModelGateway 统一网关**：所有生产链路 LLM 调用（意图识别 / Agent 循环 / 合成 / 出口校验 / 记忆提炼 / 查询改写）都经 `gateway.call()` 进出——`runtime/model_gateway.py`。网关内建**瞬时失败退避重试**（0.5s×attempts），Guard/Budget 拦截不重试（`model_gateway.py:140-144`）。
2. **编排级降级**：Fast 执行失败 → 同任务用 Deep 重试一次（`max_retries=1`）——`agents/agent_orchestrator.py:756-773`。
3. **工具级三态熔断**：连续失败 5 次 → OPEN，60s 后 HALF_OPEN 放行一次探测，期间调用走工具自带 `fallback` 降级回调——`mcp/tool_manager.py:73-108,299-328`。
4. **Monitor 健康反馈**：Z-score 异常检测，Fast profile 不健康时编排器临时升级 Deep——`agents/agent_orchestrator.py:674-677`。
5. **HTTP 限流**：用户 30 次/分、IP 120 次/分——`echoguide_guard/integration.py:73-74`。

**验证**：`tests/test_model_gateway.py`（瞬时失败重试 / 默认不重试 / 拦截不重试）、`tests/test_tool_manager.py`（熔断状态迁移 CLOSED→OPEN→HALF_OPEN）。

**3 分钟版**：讲"为什么需要一个统一网关"——重试、熔断、预算、Trace、token 统计五个能力如果分散在各调用点，任何一个漏了就是故障点；收口后新增能力只需改一个文件。再讲"重试不是万能药"：拦截类异常不重试（重试也是浪费），Fast 挂了换 Deep（重试换个姿势），工具挂了走 fallback（别让工具失败变成用户可见失败）。

**1 分钟版**：所有模型调用经过统一网关，带退避重试；Fast 失败自动降级 Deep 重试；每个工具独立三态熔断 + fallback；监控发现 Fast 不健康还会临时升级 Deep。

---

## 故事线 3：上下文爆炸（分层记忆 + 上下文卸载）

**现象**：多轮对话历史 + 超长工具结果（一次检索几千 token）会撑爆上下文窗口；朴素"全量历史进 prompt"还会让记忆无限增长、成本失控、无关噪音干扰回答。

**根因**：上下文是稀缺资源，"什么都塞进去"不是工程方案——**该记什么、以什么形态记，是决策**。

**方案（L0-L3 分层记忆 + 卸载）**：
1. **分层**：L0 原文（SQLite，全量证据锚点）→ L1 原子事实（带来源轮次）→ L2 场景块（ChromaDB 跨会话检索）→ L3 画像（常驻注入，可回滚）——`memory/conversation_memory.py`。
2. **工作记忆压缩**：Redis TTL 24h，20 条上限、15 条触发 LLM 场景化压缩，只留最近 5 条——`conversation_memory.py:555`。
3. **上下文卸载**：超长工具结果（>1500 字符）落盘 refs 表，上下文只留摘要 + `refs/{id}` 索引——实测 9267 tokens → 591 tokens，**节省 93.6%**，refs 100% 可找回（`docs/benchmark-report.md:64`）。
4. **画像提炼信号门控**：只有声明偏好/背景的句子才触发 LLM 提炼（信号率 50%），增量水位防重复输入。

**验证**：`tests/test_layered_memory.py`、`evaluation/memory_benchmark.py`（确定性离线评测，可入 CI）。

**3 分钟版**：讲"记忆不是聊天记录"——什么进上下文、什么进向量库、什么落盘，由时间尺度和用途决定；重点讲卸载这个反直觉设计：把长结果"踢出"上下文反而让回答更稳（噪音少了），代价是 refs 找回机制要保证 100% 可追溯。

**1 分钟版**：L0-L3 分层记忆 + 长结果上下文卸载，节省 93.6% token 且 100% 可找回。

---

## 故事线 4：幻觉与引用错误（Grounding 升级）

**现象**：LLM 回答凭参数记忆作答、不带证据，或引用乱标（答非所引）——检索链路"召回了但没被使用"。

**根因**：模型参数记忆与检索证据混用，输出没有和证据绑定；只靠提示词"请引用来源"没有硬约束。

**方案（v7 claim 级 Grounding，设计文档 `docs/2026-08-27-grounding-hybrid-claim-aware.md`）**：
1. **Harness 自动预检索**（retrieval-first）：知识类请求由运行时先注入证据，不依赖模型自觉调工具——`agents/roles.py:787`。
2. **Claim 级校验**：回答按 claim 拆分 → 与全证据候选匹配 → **Hard Consistency Guards**（硬规则，区间/数值/否定必须与证据一致）+ **Soft Guards**（语义近似）→ **Entailment Judge** 兜底判定。
3. **效果**：引用正确率 0.18 → 1.0、忠实性 0.33 → 0.93（开发期实测基线）；当前可复现 benchmark 报告准确性 0.9357、引用正确率 1.0000（`docs/benchmark-report.md:38-43`）。

**验证**：`evaluation/grounding_eval.py`（claim 级 P/R/F1、FP/FN、阈值网格标定 `--grid --apply`）+ 38 个确定性单测。

**口径提醒**：0.18/0.33 是升级前基线、只记录在简历草稿里没有文档持久化——面试时这样答："升级前实测基线是 0.18 / 0.33（开发期测量），升级后当前可复现报告是 0.9357 / 1.0000，评估脚本在仓库里可以复现"，不要把 0.18/0.33 说成可复现数字。

**3 分钟版**：讲"反幻觉是工程问题不是提示词问题"——把输出拆成 claim、把每个 claim 对到证据、硬规则兜底语义判定，才叫 grounding。顺带讲评测闭环：阈值用网格标定回写，不是拍脑袋。

**1 分钟版**：回答按 claim 拆分逐条与证据校验，硬规则 + Entailment Judge 双层守卫，引用正确率从 0.18 提到 1.0。

---

## 故事线 5：请求级超时与用户中断（本次新增 —— 你亲手参与的第一条，重点打磨）

**现象（问题发现）**：整条链路没有**整体 deadline**——LLM 调用靠 SDK 默认 600s 超时，最坏情况一个请求挂 10 分钟；SSE 流式接口客户端断开后，服务端编排任务**继续跑完**（继续烧 LLM 调用 + 把半截结果写进记忆）。

**根因**：控制面只管了"单点"（工具 30s 超时、轮次上限、预算），没管两个生命周期边界：**"整次请求最久活多久"** 和 **"用户退出时任务怎么停"**。

**方案**：
1. **全局 deadline**：`ExecutionPolicy.request_timeout_s`（默认 120s，env `ECHOGUIDE_RUNTIME_REQUEST_TIMEOUT_S`，0=关闭）——`runtime/policy.py`。`AgentRuntime.run()` 用 `asyncio.wait_for` 包住整个 core——`runtime/runtime.py`。超时 → `CancelledError` 级联取消整条编排链（意图识别 / 工具循环 / 合成 / 校验全部被打断）→ 抛 `RequestTimeoutError`，走与 Guard/Budget **相同的拦截收口** → 用户拿到兜底文案"抱歉，请求处理超时（超过 120 秒），已自动终止。"（编排器 `agent_orchestrator.py:330-345` 统一包装）。`after_run` 恒执行，Trace 观测闭环不因超时中断。
2. **为什么放 Runtime 层而不是每个调用点**：一次封装全链路生效；工具层 30s 超时是局部保险丝，这是全局熔断——两层职责互补。
3. **SSE 断开取消**：事件队列循环里 0.5s 周期轮询 `request.is_disconnected()`，断开即 `task.cancel()` 并吞掉 `CancelledError`，**中断不写记忆**（防止半截回答污染用户记忆）——`api/routers/chat.py`。

**验证**：`tests/test_runtime_timeout.py`（正常路径不受影响 / 超时返回兜底文案 / 编排子任务确实被取消 / after_run 观测闭环不中断 / 0=关闭兼容）；`tests/test_chat_stream.py` 新增两个断开测试（断开即取消、静默期也能检测到断开）。

**3 分钟版**（这是你的"亲历"故事，讲法示例）：
> 我在 review 整条请求链路的时候发现，工具调用有 30 秒超时、Agent 循环有轮次上限，但**整次请求没有 deadline**——如果模型或外部依赖卡住，一个请求理论上可以挂很久；另外流式接口用户关掉页面后，服务端任务还会继续跑完并写记忆。这是两个真实的资源失控点。
> 我的做法是：把全局超时放在 Runtime 层，用 asyncio.wait_for 包住整个编排 core，超时就把整个任务树取消掉，走和 Guard、预算一样的拦截收口，用户拿到的是一条明确的兜底文案而不是无限等待。SSE 那边加 0.5 秒周期的断开轮询，断开就取消后台任务，并且故意设计成"中断不写记忆"——半截回答不该污染用户的长期记忆。
> 我还为这两个改动写了 8 个测试：验证正常路径不受影响、超时后子任务确实被取消而不是继续在后台跑、观测闭环不被破坏。全量 425 个测试通过，CI 的 ruff 也过了。

**1 分钟版**：补上了两个兜底缺口——请求级全局超时（120s 强制取消整条编排链并返回兜底文案）和 SSE 客户端断开即取消任务（中断不写记忆）。

---

## 故事线 6：从“有日志”到“可沿链路定位”的可观测性闭环

**现象**：以前已有 request/span 和 task 状态，但遇到“回答错了”仍要在意图、规划、Profile、工具、Grounding 之间人工拼日志；尤其是工具的缓存、重排、fallback、证据数量和任务归属没有统一事实来源。

**方案**：把结构化观测收口到每请求独立的 `RunState`，并同步投影到 `core/tracing.py` 的 Trace：

1. **Tool Trace**：每次实际工具执行记录 `tool_name / task_id / tool_round / success / error / latency / cache_hit / reranked / fallback_used / result_count / evidence_count`。Agent 工具循环、自动预检索和协作任务的补写都经过同一 Runtime 工具边界，因此预算、Trace 与工具统计口径一致。
2. **Task Contract**：`Task` 现在显式携带 inputs、expected_output、acceptance_criteria、risk_level。Fast Path 用确定性规则补全，LLM 规划只可细化且需通过类型/枚举硬校验；TaskExecutor 在任务结束后用非空结果、必需工具等规则校验 Contract，Verifier 再将失败汇总为 `task_contract_failed`，不让 LLM 重新猜完成状态。
3. **Decision Trace**：同一 Trace 顺序保存 intent（domain/action/confidence/stage）、planning（strategy/DAG/reason/contract）、profile（原策略、Monitor 健康升级）、tasks（状态/工具/Contract 校验）和 verification。排障路径固定为 `Intent → Planner → Profile → Task → Tool/Model → Grounding/Verifier`。
4. **Eval 关联**：端到端评测失败带上 `request_id`、`trace_id` 和确定性优先的 `failure_stage`（retrieval/tool/grounding/verification/generation 等）；失败报告不再只是一条样本文字，可直接下钻 Trace。

**取舍**：Trace 只记录工具结果摘要和计数，不保存工具参数或结果正文，避免把个人数据与大对象塞进观测系统；失败阶段是轻量归因，不确定时显式给 `unknown`，不伪造根因。

**验证**：`tests/test_trace_contract_eval.py` 覆盖并发 Trace 隔离、Tool Trace 字段、Task Contract 生成/校验、Decision Trace 完整性和 Eval→Trace 关联；原有 Tool Loop、RAG、Grounding、Monitor 测试继续回归。

**1 分钟版**：我把一次请求的决策和工具执行统一写到 RunState/Trace：评测发现失败后会给出 request_id、trace_id 和失败阶段，可以从意图一路追到工具、Grounding 和 Verifier，不需要靠人工复现拼日志。

---

## 数字口径核对表（面试前必跑）

| 数字 | 出处 | 复现方式 | 口径提醒 |
|---|---|---|---|
| 425 个测试通过 | `tests/` | `python -m pytest -q` | CI 也跑（`.github/workflows/ci.yml`） |
| 上下文卸载节省 93.6%（9267→591 tokens） | `docs/benchmark-report.md:64` | `evaluation/memory_benchmark.py` | 3 个长工具结果样例 |
| 引用正确率 1.0000、忠实性 0.9278、准确性 0.9357 | `docs/benchmark-report.md:38-43` | `evaluation/grounding_eval.py` | 18 组端到端场景，LLM-as-Judge 四维 |
| 0.18 / 0.33（升级前基线） | `docs/resume-bullets.md` | 无脚本复现 | 开发期实测，只用于讲"提升"故事，别当可复现数字 |
| 28 个 HTTP 场景通过率 96.4% | `README.md:302-327` | `evaluation/demo_benchmark.py` | 真实 HTTP 压测，120s 上限 |

**通用口径原则**（照抄 resume-bullets.md 的立场）：所有数字要能答出"怎么算、数据来源、怎么复现"；不隐藏不利结果；评测集包含刻意挑战样本，所以不是 99%。
