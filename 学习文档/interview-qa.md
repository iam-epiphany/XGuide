# EchoGuide 面试 Mock 追问 Q&A

> **用法**：每条先自己答一遍，再对照"要点"补漏。回答只给要点，用自己的话展开。
> 「诚实边界」是**必须遵守的底线**：答不出来的宁可说"这块我当时没深究"，也不要编——面试官一追问就穿帮，比承认不知道严重得多。
> 数字口径一律见 `docs/interview-story-lines.md` 的核对表。

---

## 一、工具调用死循环

**Q1：Agent 调用工具陷入死循环怎么办？**
要点：
- 三层：① 分级轮次上限（Fast 3 / Deep 5）；② 签名级无进展检测（工具名+参数签名连续 2 轮重复 → 强制收尾）；③ 全请求预算闸（模型调用次数 / 工具调用总次数上限，`BudgetExceeded` 统一收口）。
- 为什么三层而不是一层：轮次上限止损但浪费预算；重复检测省预算但模型换着参数打转时检测不到；预算闸是兜底。终止是运行时的职责，不是模型的自觉。
- 收尾语义：轮次用尽仍补占位 tool_result 走普通收尾，用户拿到答复而不是空响应。
追问："重复"怎么定义？→ 参数先做排序无关归一化再比签名（`_tool_round_signature`），比如 `{"a":1,"b":2}` 和 `{"b":2,"a":1}` 算同一签名。

**Q2：轮次上限设 3/5 的依据？**
要点：Fast 是简单任务（知识查询/单工具），3 轮足够；Deep 是多任务/复杂链，5 轮留余量。可环境变量调。诚实边界：不是调参实验得出的最优值，是基于任务形态的工程判断——可以说"这是经验值，我在 benchmark 里没有观测到正常请求逼近上限，说明设计合理"。

**Q3：如果模型一直输出 tool_use 但就是不结束，最坏会怎样？**
要点：最坏就是烧到轮次上限（5 轮 Deep）+ 预算闸兜底，然后强制收尾给答复；不会无限循环。预算闸 0=仅计数不强限的默认值反而是最弱一环——面试时主动说这个已知取舍：生产环境应该显式设 `ECHOGUIDE_RUNTIME_MAX_MODEL_CALLS`，默认 0 是为了本地演示不误伤。

## 二、重试

**Q4：哪些异常会重试？怎么重试？**
要点：ModelGateway 统一网关内建退避重试（0.5s×attempts），只对**瞬时异常**重试；`GuardRejection`（注入/超长）和 `BudgetExceeded` **不重试**——重试拦截类失败等于重复攻击自己。默认 retries=0 不重试，由调用方显式开。

**Q5：重试会重复执行副作用吗（幂等性）？**
要点：重试发生在**模型调用**层（读性质），工具执行本身不因模型重试而重跑——每次工具调用是独立 await，重试的是"问模型"而不是"执行工具"。但写类工具（add_todo 等）本身没有幂等键，这是已知边界，值得主动提。

**Q6：Fast 失败后为什么用 Deep 重试而不是同模型重试？**
要点：Fast→Deep 是**降级策略**不是重试——Fast 模型更便宜更快但能力弱，失败大概率是模型能力问题不是网络抖动；换更强模型重试一次，比同模型反复重试成功率高。受 `max_retries=1` 约束，不会无限降级。工具 fallback 也是同思路：熔断后走降级回调而不是重试原调用。

## 三、熔断

**Q7：熔断器怎么设计的？参数为什么是 5 次/60 秒？**
要点：三态（CLOSED→OPEN→HALF_OPEN），每个工具独立实例；连续失败 5 次 OPEN，60s 后 HALF_OPEN 放行一次探测，成功即恢复。5/60 是工程默认值——太快误伤瞬时抖动，太慢故障期拖太久。诚实边界：没有做过参数敏感性实验，这是"够用的默认值"，生产应按监控数据调。

**Q8：熔断期间用户会看到什么？**
要点：不会看到报错——熔断后调用走工具自带 `fallback` 降级回调（如本地规则回答），fallback 也失败才如实返回 `ToolResult(success=False)`，结果回填给 LLM 时如实报错不谎报（`roles.py:585-589`），LLM 据此组织兜底话术。失败会被如实呈现，这是设计决策：不掩盖错误，但把错误翻译成用户可理解的结果。

## 四、预算 / 超时

**Q9：单请求最多能烧多少 token？**
要点：有两层控制——每层模型调用有 max_tokens（Fast 768 / Deep 1536 / 合成 1024），这是单次调用的输出上限；跨调用的总量靠 `max_model_calls`/`max_tool_calls` 预算闸（默认 0 仅计数不强限，生产应显式设置）。诚实边界：没有做"单请求总 token 硬上限"的中间件，这是遗留的已知取舍。

**Q10：全局超时怎么实现的？取消安全吗？**
要点：`AgentRuntime.run()` 用 `asyncio.wait_for` 包住整个编排 core（`runtime/runtime.py`），默认 120s；超时 → CancelledError 级联进整条链路（意图识别/工具循环/合成/校验），抛 `RequestTimeoutError` 走与 Guard/Budget 同一收口，用户拿到兜底文案。取消安全是因为：全程 asyncio 协程、无裸 except 吞 CancelledError（Python 3.8+ 它是 BaseException）、`after_run` 恒执行保证 Trace 闭环。
追问：为什么不做在每个调用点？→ 一次封装全链路生效；工具层 30s 超时是局部保险丝，全局 deadline 是整次请求的熔断，两层互补。

**Q11：LLM 调用本身有超时吗？**
要点：SDK 默认 600s 兜底（这是历史遗留，已在计划中）；工具执行有显式 30s `asyncio.wait_for`，外部 HTTP 15-30s，改写/重排 15s。诚实边界：LLM 主链路显式 timeout 还没补——如果面试官追问，大方承认并说出补法（Anthropic SDK 支持 timeout 参数，网关层统一注入）。

## 五、SSE 用户中断

**Q12：用户关掉页面后，服务端任务会怎样？**
要点：事件队列循环 0.5s 周期轮询 `request.is_disconnected()`，断开即 `task.cancel()`；取消后**不写记忆**——半截回答不该污染用户长期记忆。设计上有意为之：中断是正常事件，不是错误，所以不发 error 事件，直接静默收尾（日志留痕）。

**Q13：取消和"跑完再丢弃"相比有什么权衡？**
要点：取消省模型调用（钱）、省资源、不污染记忆；代价是中断点不可控（可能已经写了一半状态）。取舍依据：SSE 场景用户已明确放弃本次会话，服务端继续跑是纯浪费。非流式 /chat 不受影响（请求是原子的，返回即完成）。

## 六、评测口径

**Q16a：评测失败后，怎样快速定位是意图、规划还是工具出了问题？**

要点：每个端到端请求都有独立 `request_id` 和 `trace_id`。`RunState` 同时保存 Decision Trace 和 Tool Trace：前者按顺序记录 intent（最终 domain/action/confidence/stage）、Planner 的 strategy/DAG/reason、Fast/Deep 与 Monitor 升级、每个 Task 的 Contract/状态/校验结果和 Verifier；后者记录工具所属 task、轮次、成功/错误、耗时、缓存、重排、fallback、结果/证据数。评测不通过时，结果 metadata 自动附带这两个 id 和 `failure_stage`（intent/planning/routing/retrieval/tool/generation/grounding/verification/unknown）。阶段归因优先读确定性信号：Verifier 的 `expected_retrieval_missing` → retrieval、工具 Trace 失败 → tool、Grounding flag/faithfulness 低 → grounding、Task Contract 未满足 → verification；没有证据才标 generation 或 unknown。这样排障路径是 `Intent → Planner → Profile → Task → Tool/Model → Grounding/Verifier`，不是重新人工复现。

**Q16b：Task Contract 为什么不完全让 LLM 判？**

要点：Contract 的目的是把“任务做没做完”从提示词语义变成可验证边界。Task 包含 inputs、expected_output、acceptance_criteria、risk_level；Fast Path 由规则补全，LLM 只能补充描述且字段类型、风险枚举、DAG、工具名都做硬校验。执行后优先用确定性规则验证非空输出与 required_tool 等硬条件，再由 Verifier 汇总 `task_contract_failed`。LLM 只保留给 Grounding 的语义辅助，避免“模型自己说自己完成了”的循环论证。

**Q14：LLM 给自己打分，可信吗？**
要点：Judge 固定 temperature=0、失败显式标记、可独立模型（`EVAL_JUDGE_*`）；它只做主观四维评分（相关性/准确性/完整性/有用性），**分类指标全部确定性计算**（Accuracy/Macro-F1/per-class P/R/F1 由脚本按标注集算）。评测集 202 条意图 + 58 条检索 + 18 组端到端场景，用例从知识库/skills 系统化派生、可审计，held-out 20% 未见用例。

**Q15：96.4% 通过率是怎么来的？**
要点：28 个真实 HTTP 场景经 Orchestrator 真实执行（`evaluation/demo_benchmark.py`），LLM-as-Judge 判定通过。口径文档（`docs/benchmark-methodology.md`）写明：Judge 主观分波动、模型行为波动都如实记录，5% 回归检测 + 基线持久化，不隐藏不利结果。

**Q16：为什么不是 99%？**
要点：评测集刻意包含挑战样本（无主题词、追问省略句、关键词误配、相似主题竞争），27 条误分类逐条可复盘（`evaluation/error_analysis/`），是继续迭代的输入。诚实比好看重要——这本身就是工程态度。

## 七、身份与所有权（必考）

**Q17：这项目是你一个人写的吗？用 AI 了吗？**
推荐框架（诚实 + 有分量）：
- 大方承认：**"大部分代码是在 AI 辅助下写的，但架构和关键决策是我的"**。
- 然后立刻把话题拉回你能掌控的层：架构图你画的、控制面设计（Runtime 收口、双保险护栏、评测闭环）是你定的、代码你逐行 review 过、所有测试你跑过、所有数字你能复现。
- 举具体证据：今天这条超时/中断的链路，从问题发现、方案设计到实现、测试都是你做的（故事线 5）；你还能现场改代码、现场跑测试。
- 讲清楚 AI 在你工作流里的角色：**AI 是高效的编码器，决策者是你**——这和"AI 写了我背稿"有本质区别，面试官要的就是这个认知。

**Q18：让你现场改一行代码/加一个功能，你能做到吗？**
准备动作（面试前必须完成）：
- 能画全链路图：HTTP → Guard → 语义缓存 → 记忆读取 → 意图识别 → Planner → Harness（Agent 循环）→ Grounding → 记忆写入。
- 能指出每个控制点在哪：`runtime/runtime.py`（拦截收口）、`runtime/model_gateway.py`（重试/预算）、`mcp/tool_manager.py`（熔断/超时）、`runtime/middlewares.py`（预算中间件）、`agents/roles.py`（轮次/无进展检测）。
- 能现场跑：`python -m pytest`（425 个）、`python -m ruff check`、评测脚本。

**Q19：这个项目最大的缺点是什么？**
诚实清单（挑 1-2 个说，别全倒）：
- LLM 主调用无显式 timeout（SDK 默认 600s）——已识别，待补。
- 预算闸默认 0（仅计数不强限）——本地演示友好，生产必须显式配置。
- 无并发信号量，并行工具调用没有上限——`asyncio.gather` 无界并行，压测高峰有资源风险。
- 无覆盖率门禁、无 pre-commit。
说完补一句："这些我都列在已知边界里，优先级和方案都有（xxx），下一步会做"——**能说出缺点和补法，比项目完美更有说服力**。

**Q20：如果让你重做一遍，哪里会不一样？**
要点：
- 一开始就引入统一网关（现在是历史演进出来的，早期调用点分散）。
- 评测先于实现（现在是功能先行、评测补齐，应该反过来——指标驱动开发）。
- 控制面（超时/并发/预算）在设计期就进架构图，而不是上线前补。
这套回答同时展示反思能力和工程判断。

---

## 收尾提醒

1. 面试前把 `docs/interview-story-lines.md` 的 5 条弧线 + 本文件 Q17/Q18 过一遍，其余按"答得出要点"标准即可。
2. 所有"我知道"的代码位置，**打开文件指给面试官看**（项目在本地、随时可跑）——现场演示比任何背书都有说服力。
3. 面试官最想验证的不是"你会不会写 Agent"，而是"你能不能让它不乱跑、出事了怎么办"——把回答重心始终拉回控制面。
