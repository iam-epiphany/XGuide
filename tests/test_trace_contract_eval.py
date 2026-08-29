"""结构化 Trace、Task Contract 与 Eval 关联的离线回归测试。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from agents.agent_orchestrator import AgentOrchestrator, AgentResponse, Request
from agents.profiles import ExecutionProfile, ProfileName
from agents.roles import TaskAgent
from agents.workflow import Task, verify_task_contract
from core.domains import IntentAction, IntentDomain
from core.tracing import begin_trace, current_trace, end_trace
from evaluation.evaluator import EndToEndEvaluator, QualityScores
from mcp.tool_manager import MCPToolManager, Tool, ToolEffect
from runtime.policy import ExecutionPolicy
from runtime.state import RunState


def _run(coro):
    return asyncio.run(coro)


def test_concurrent_runstate_traces_do_not_cross_requests():
    """contextvars + RunState 容器均按请求隔离，工具/决策不可串线。"""
    async def one(index: int):
        begin_trace(f"request-{index}")
        state = RunState(request_id=f"r-{index}")
        state.record_decision("intent", domain=f"domain-{index}")
        await asyncio.sleep(0.005 * (2 - index))
        state.record_tool_call(
            tool_name=f"tool-{index}", task_id=f"t-{index}", tool_round=1,
            success=True, result_count=index + 1, evidence_count=index,
        )
        record = end_trace().to_dict()
        return state, record

    async def both():
        return await asyncio.gather(one(0), one(1))

    first, second = _run(both())
    for index, (state, record) in enumerate((first, second)):
        assert state.tool_trace[0]["tool_name"] == f"tool-{index}"
        assert record["decision_trace"]["intent"]["domain"] == f"domain-{index}"
        assert [item["tool_name"] for item in record["tool_calls"]] == [f"tool-{index}"]


def test_tool_trace_records_runtime_result_shape():
    manager = MCPToolManager(api_key="sk-test-not-used")

    async def lookup(params, context):
        return [{"title": "证据", "content": params["query"]}]

    manager.register(Tool(
        name="lookup", description="lookup", handler=lookup,
        schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
        effect=ToolEffect.READ,
    ))
    profile = ExecutionProfile(ProfileName.FAST, "m", 128, False, 3, False, False)
    agent = TaskAgent(AsyncMock(), "m", None, manager, profile)
    req = Request(message="查资料", user_id="u", conv_id="c", task_id="t1")
    req.state = RunState(request_id="r1", policy=ExecutionPolicy())
    data, error = _run(agent._execute_tool("lookup", {"query": "选课"}, req))

    assert error is None
    assert data[0]["title"] == "证据"
    trace = req.state.tool_trace
    assert len(trace) == 1
    assert trace[0] == {
        "tool_name": "lookup", "task_id": "t1", "tool_round": 1,
        "success": True, "error": None, "latency_ms": trace[0]["latency_ms"],
        "cache_hit": False, "reranked": False, "fallback_used": False,
        "result_count": 1, "evidence_count": 1,
    }


def test_task_contract_is_rule_completed_and_deterministically_verified():
    orchestrator = AgentOrchestrator(api_key="sk-test-not-used")
    req = Request(
        message="帮我添加一个补办校园卡的待办", user_id="u", conv_id="c",
        domain=IntentDomain.PERSONAL, action=IntentAction.REQUEST,
    )
    plan = _run(orchestrator._planner.plan(req, req.domain, req.action))
    task = plan.tasks[0]
    assert task.inputs == ["用户请求"]
    assert task.risk_level == "high"
    assert "返回非空结果" in task.acceptance_criteria

    required = Task(
        task_id="write", domain=IntentDomain.PERSONAL, goal="g", message="m",
        action=IntentAction.REQUEST, required_tool="add_todo",
    )
    missing = verify_task_contract(required, AgentResponse(content="建议已给出", success=True))
    assert missing["passed"] is False
    assert missing["failed"] == ["required_tool_executed"]


def test_decision_trace_contains_intent_plan_profile_tasks_and_verification():
    orchestrator = AgentOrchestrator(api_key="sk-test-not-used")

    async def fake_execute(task_req, on_event=None):
        return AgentResponse(content="校园卡办理请查看服务大厅通知。", success=True, profile="fast")

    orchestrator._execute = fake_execute
    begin_trace("decision-test")
    req = Request(
        message="校园卡怎么办理", user_id="u", conv_id="c",
        domain=IntentDomain.AFFAIRS, action=IntentAction.QUERY,
    )
    result = _run(orchestrator.run(req))
    record = end_trace().to_dict()

    assert result.execution["tasks"][0]["contract_verification"]["passed"] is True
    assert {"intent", "planning", "profile", "tasks", "verification"} <= set(record["decision_trace"])
    assert record["decision_trace"]["planning"]["strategy"] == "fast_path"
    assert record["decision_trace"]["tasks"]["tasks"][0]["id"] == "t0"


def test_parallel_decision_trace_uses_final_task_execution_snapshot():
    orchestrator = AgentOrchestrator(api_key="sk-test-not-used")

    async def fake_execute(task_req, on_event=None):
        return AgentResponse(content=f"{task_req.task_id} ok", success=True, profile="deep")

    async def fake_synthesize(req, results):
        return "combined"

    orchestrator._execute = fake_execute
    orchestrator._synthesizer.synthesize = fake_synthesize
    begin_trace("parallel-decision-test")
    req = Request(
        message="食堂几点关门，顺便查下明天课表", user_id="u", conv_id="c",
        domain=IntentDomain.CAMPUS_LIFE, action=IntentAction.QUERY,
    )
    result = _run(orchestrator.run(req))
    record = end_trace().to_dict()

    assert result.execution["mode"] == "parallel"
    task_snapshot = record["decision_trace"]["tasks"]["tasks"]
    assert {task["status"] for task in task_snapshot} == {"success"}
    assert all("contract_verification" in task for task in task_snapshot)


def test_profile_decision_records_monitor_upgrade():
    orchestrator = AgentOrchestrator(api_key="sk-test-not-used")
    orchestrator.set_fast_health(False)
    req = Request(message="你好", user_id="u", conv_id="c")
    req.state = RunState(request_id="r")
    selected = orchestrator._select_profile(req, "single")

    assert selected == ProfileName.DEEP
    assert req.state.decision_trace["profile"]["policy_selected"] == "fast"
    assert req.state.decision_trace["profile"]["monitor_upgraded"] is True


def test_runtime_owned_trace_is_closed_after_direct_orchestrator_run():
    orchestrator = AgentOrchestrator(api_key="sk-test-not-used")

    async def fake_execute(task_req, on_event=None):
        return AgentResponse(content="ok", success=True, profile="fast")

    orchestrator._execute = fake_execute
    result = _run(orchestrator.run(Request(
        message="你好", user_id="u", conv_id="c",
        domain=IntentDomain.OTHER, action=IntentAction.GREETING,
    )))
    assert result.execution["runtime"]["trace_id"]
    assert current_trace() is None


def test_eval_failure_links_request_trace_and_stage():
    result = SimpleNamespace(
        request_id="request-eval-1",
        response="没有检索证据的答案",
        agent_type="campus_life",
        intent=SimpleNamespace(value="query"),
        tool_evidence=[],
        execution={
            "trace_id": "trace-eval-1",
            "runtime": {"trace_id": "trace-eval-1", "tool_trace": []},
            "verification": {"flags": ["expected_retrieval_missing"]},
        },
    )
    orchestrator = MagicMock()
    orchestrator.run = AsyncMock(return_value=result)
    evaluator = EndToEndEvaluator(orchestrator, None, api_key="sk-test-not-used")
    evaluator._judge.judge = AsyncMock(return_value=QualityScores(0.2, 0.2, 0.2, 0.2))

    item = _run(evaluator._evaluate_dialog_case({"question": "食堂几点关门？"}, 0))[0]
    assert item.passed is False
    assert item.metadata["request_id"] == "request-eval-1"
    assert item.metadata["trace_id"] == "trace-eval-1"
    assert item.failure_stage == "retrieval"
