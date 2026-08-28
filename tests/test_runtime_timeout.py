"""
请求级全局超时（deadline）测试：core 超过时限被强制取消，
以 RequestTimeoutError 走与 Guard/Budget 同一拦截收口（用户可见兜底文案）。
"""

from __future__ import annotations

import asyncio

from runtime import AgentRuntime, ExecutionPolicy, RequestTimeoutError, RunState


def test_runtime_run_within_deadline_succeeds():
    runtime = AgentRuntime(middlewares=[], policy=ExecutionPolicy(request_timeout_s=5))
    state = RunState(request_id="r1")

    async def core(ctx):
        await asyncio.sleep(0.01)
        return "ok"

    result = asyncio.run(runtime.run(state, core))
    assert result == "ok"
    assert state.errors == []


def test_runtime_run_timeout_rejects_with_fallback_copy():
    runtime = AgentRuntime(middlewares=[], policy=ExecutionPolicy(request_timeout_s=0.05))
    state = RunState(request_id="r1")
    cancelled: list = []

    async def core(ctx):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    result = asyncio.run(runtime.run(state, core))
    # 与 Guard/Budget 同语义：返回 None，编排器据此产出兜底文案
    assert result is None
    assert "超时" in state.meta["reject_message"]
    assert "已自动终止" in state.meta["reject_message"]
    assert any(e.startswith("timeout:") for e in state.errors)
    assert cancelled == [True]  # 编排子任务确实被取消（不是继续在后台跑）


def test_runtime_run_timeout_zero_disables_deadline():
    runtime = AgentRuntime(middlewares=[], policy=ExecutionPolicy(request_timeout_s=0))
    state = RunState(request_id="r1")

    async def core(ctx):
        await asyncio.sleep(0.01)
        return "ok"

    result = asyncio.run(runtime.run(state, core))
    assert result == "ok"


def test_runtime_run_timeout_after_run_still_fires():
    """超时取消后观测闭环不中断：after_run 恒执行一次。"""
    fired: list = []

    class _Rec:
        name = "rec"

        async def after_run(self, ctx):
            fired.append(True)

    runtime = AgentRuntime(middlewares=[_Rec()], policy=ExecutionPolicy(request_timeout_s=0.05))
    state = RunState(request_id="r1")

    async def core(ctx):
        await asyncio.sleep(10)

    result = asyncio.run(runtime.run(state, core))
    assert result is None
    assert fired == [True]


def test_request_timeout_error_carries_reason():
    ex = RequestTimeoutError(2.5)
    assert "2.5" in ex.reason
    assert "超时" in ex.reason
    assert str(ex) == ex.reason


def test_policy_timeout_env_override(monkeypatch):
    monkeypatch.setenv("ECHOGUIDE_RUNTIME_REQUEST_TIMEOUT_S", "3.5")
    assert ExecutionPolicy.from_env().request_timeout_s == 3.5

    monkeypatch.setenv("ECHOGUIDE_RUNTIME_REQUEST_TIMEOUT_S", "abc")  # 非法值回落默认
    assert ExecutionPolicy.from_env().request_timeout_s == 120.0
