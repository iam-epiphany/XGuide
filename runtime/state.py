"""
RunState —— Agent Runtime 的单次运行状态载体。

一次请求（/chat、/chat/stream、CLI）对应一个 RunState：身份信息、路由结果、
执行计数器、错误记录与 middleware 自由扩展的 meta。执行摘要由 summary() 输出，
合并进 execution 元数据，是可观测性的统一出口（debug 面板可见）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Dict, List, Optional


@dataclass
class RunState:
    """单次运行的执行状态（由 AgentRuntime.run 创建，贯穿整条请求链路）。"""

    request_id: str
    user_id: str = ""
    conv_id: str = ""
    trace_id: str = ""  # 与请求级 Trace 对齐（TraceMiddleware 回填）
    message: str = ""
    profile: str = ""  # fast / deep（路由后回填）
    complexity_mode: str = ""  # single / parallel / dependent（路由后回填）

    started_at: float = field(default_factory=time.monotonic)

    # 执行计数器（BudgetMiddleware 等中间件维护）
    step_count: int = 0  # 模型调用次数（before_model）
    tool_call_count: int = 0  # 工具调用次数（before_tool）
    tool_round_count: int = 0  # 工具调用轮次
    retry_count: int = 0  # 失败降级次数（Fast→Deep）
    input_tokens: int = 0
    output_tokens: int = 0

    # 错误记录（Guard 拒绝、预算超限、middleware 异常）
    errors: List[str] = field(default_factory=list)

    # middleware 自由扩展位（如 SkillMiddleware 的 skill_prompt_by_msg）
    meta: Dict[str, Any] = field(default_factory=dict)

    # 请求级结构化观测：只保存摘要，不保存工具参数/结果正文，避免敏感数据和
    # 大对象进入 trace。每个 RunState 独立持有，天然隔离并发请求。
    tool_trace: List[Dict[str, Any]] = field(default_factory=list)
    decision_trace: Dict[str, Any] = field(default_factory=dict)

    # 执行策略引用（BaseAgent 等执行层读取预算，缺省 None 时回落旧常量）
    policy: Optional[Any] = None

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.started_at) * 1000

    def add_error(self, message: str) -> None:
        self.errors.append(message)

    def record_decision(self, stage: str, **detail: Any) -> None:
        """记录本请求的决策快照，并同步写入当前 Trace（若已绑定）。"""
        snapshot = dict(detail)
        self.decision_trace[stage] = snapshot
        try:
            from core.tracing import current_trace

            trace = current_trace()
            if trace is not None:
                trace.record_decision(stage, snapshot)
        except Exception:
            # 观测不可影响业务；CLI/离线环境也允许没有 Trace。
            pass

    def record_tool_call(
        self,
        *,
        tool_name: str,
        task_id: str = "",
        tool_round: int = 0,
        success: bool,
        error: Optional[str] = None,
        latency_ms: float = 0.0,
        cache_hit: bool = False,
        reranked: bool = False,
        fallback_used: bool = False,
        result_count: int = 0,
        evidence_count: int = 0,
    ) -> None:
        """记录一次工具执行的统一摘要，供 Runtime、Trace 与 Eval 共用。"""
        detail = {
            "tool_name": tool_name,
            "task_id": task_id,
            "tool_round": tool_round,
            "success": bool(success),
            "error": str(error)[:200] if error else None,
            "latency_ms": round(float(latency_ms), 2),
            "cache_hit": bool(cache_hit),
            "reranked": bool(reranked),
            "fallback_used": bool(fallback_used),
            "result_count": max(0, int(result_count)),
            "evidence_count": max(0, int(evidence_count)),
        }
        self.tool_trace.append(detail)
        try:
            from core.tracing import current_trace

            trace = current_trace()
            if trace is not None:
                trace.record_tool_call(detail)
        except Exception:
            pass

    def summary(self) -> Dict[str, Any]:
        """运行摘要（execution meta 的纯增量字段，不透出敏感信息）。"""
        return {
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "steps": self.step_count,
            "tool_calls": self.tool_call_count,
            "tool_rounds": self.tool_round_count,
            "retries": self.retry_count,
            "errors": list(self.errors),
            "tool_trace": list(self.tool_trace),
            "decision_trace": dict(self.decision_trace),
        }
