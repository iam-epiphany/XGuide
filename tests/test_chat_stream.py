"""SSE 流式接口集成测试：打桩编排器，验证 meta/tool/delta/done 事件流完整链路。"""
from __future__ import annotations

import asyncio
import json

from agents.agent_orchestrator import OrchestratorResult
import api.main as m
import api.state as state


class _FakeMemory:
    async def get_context(self, user_id, conv_id, query=""):
        class Ctx:
            recent_messages = []
            def to_prompt_text(self):
                return ""
        return Ctx()

    async def add_message(self, *args, **kwargs):
        return None

    async def update_profile(self, *args, **kwargs):
        return None


class _FakeOrchestrator:
    async def run(self, req, on_event=None):
        if on_event:
            await on_event({"type": "meta", "domain": "campus_life", "action": "query", "agent": "campus_life"})
            await on_event({"type": "tool", "name": "knowledge_search", "status": "start",
                            "input": {"query": "食堂关门时间"}})
            await on_event({"type": "tool", "name": "knowledge_search", "status": "done",
                            "titles": ["食堂与餐饮"]})
            for ch in ("南校区食堂", "一般晚上七点关门"):
                await on_event({"type": "delta", "text": ch})
        return OrchestratorResult(
            request_id="r1",
            response="南校区食堂一般晚上七点关门。",
            agent_type="campus_life",
            intent=None,
            domain=__import__("core.domains", fromlist=["IntentDomain"]).IntentDomain.CAMPUS_LIFE,
            action=__import__("core.domains", fromlist=["IntentAction"]).IntentAction.QUERY,
            latency_ms=12.3,
            tools_used=["knowledge_search"],
        )


class _FakeCache:
    def __init__(self, hit=None):
        self._hit = hit

    def get(self, query, user_id=None, dependence="global"):
        return self._hit

    def put(self, *args, **kwargs):
        return None


def _collect(resp):
    async def run():
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
        return b"".join(chunks).decode("utf-8")

    return asyncio.run(run())


def _parse_sse(text: str):
    events = []
    for frame in text.split("\n\n"):
        frame = frame.strip()
        if not frame or not frame.startswith("data:"):
            continue
        events.append(json.loads(frame[5:].strip()))
    return events


def test_chat_stream_full_event_flow():
    state._orchestrator = _FakeOrchestrator()
    state._memory = _FakeMemory()
    state._semantic_cache = _FakeCache()

    req = m.ChatRequest(message="南校区食堂几点关门？", user_id="u1")
    resp = asyncio.run(m.chat_stream(req))
    assert resp.media_type == "text/event-stream"
    assert resp.headers.get("X-Accel-Buffering") == "no"

    events = _parse_sse(_collect(resp))
    types = [e["type"] for e in events]

    # hello → meta → tool(start) → tool(done) → delta ×2 → done
    assert types[0] == "hello"
    assert types[1] == "meta"
    assert "tool" in types
    assert types.count("delta") == 2
    assert types[-1] == "done"

    meta = events[1]
    assert meta["domain"] == "campus_life"
    assert meta["agent"] == "campus_life"

    tool_start = next(e for e in events if e["type"] == "tool" and e["status"] == "start")
    assert tool_start["input"]["query"] == "食堂关门时间"
    tool_done = next(e for e in events if e["type"] == "tool" and e["status"] == "done")
    assert tool_done["titles"] == ["食堂与餐饮"]

    done = events[-1]
    assert done["response"] == "南校区食堂一般晚上七点关门。"
    assert done["knowledge_used"] is True
    assert done["agent_type"] == "campus_life"


def test_chat_semantic_cache_hit_skips_llm():
    """非流式 /chat：语义缓存命中直接复用答案，不进入编排器。"""
    from fastapi import Response

    state._orchestrator = _FakeOrchestrator()
    state._memory = _FakeMemory()
    state._semantic_cache = _FakeCache(hit={
        "response": "缓存中的回答",
        "domain": "academic",
        "agent_type": "academic",
    })

    req = m.ChatRequest(message="选课什么时候开始？", user_id="u1")
    resp = asyncio.run(m.chat(req, Response()))
    assert resp.response == "缓存中的回答"
    assert resp.domain == "academic"
    assert resp.latency_ms >= 0.0
    assert resp.cached is True


def test_chat_semantic_cache_miss_runs_orchestrator():
    from fastapi import Response

    state._orchestrator = _FakeOrchestrator()
    state._memory = _FakeMemory()
    state._semantic_cache = _FakeCache()

    req = m.ChatRequest(message="南校区食堂几点关门？", user_id="u1")
    resp = asyncio.run(m.chat(req, Response()))
    assert resp.response == "南校区食堂一般晚上七点关门。"
    assert resp.domain == "campus_life"
    assert resp.knowledge_used is True


def test_chat_stream_semantic_cache_hit_skips_llm():
    state._semantic_cache = _FakeCache(hit={
        "response": "缓存中的回答",
        "domain": "academic",
        "agent_type": "academic",
    })

    req = m.ChatRequest(message="选课什么时候开始？", user_id="u1")
    resp = asyncio.run(m.chat_stream(req))
    events = _parse_sse(_collect(resp))

    assert events[-1]["type"] == "done"
    assert events[-1]["cached"] is True
    assert events[-1]["response"] == "缓存中的回答"
    # 缓存命中路径不产生 tool / delta 之外的 LLM 事件
    assert not any(e["type"] == "tool" for e in events)


def test_personal_domain_cache_hit_is_discarded():
    """
    P0 回归：语义缓存 key 不区分 user_id，personal 领域（课表/待办等个人数据）
    的回答即使命中缓存也必须丢弃，走正常编排 —— 防止用户 A 的课表返回给用户 B。
    """
    from fastapi import Response

    state._orchestrator = _FakeOrchestrator()
    state._memory = _FakeMemory()
    state._semantic_cache = _FakeCache(hit={
        "response": "用户A的课表：08:30 高等数学",
        "domain": "personal",
        "agent_type": "personal",
    })

    # 非流式 /chat：命中 personal 缓存 → 丢弃，走编排器（Fake 返回食堂答案）
    req = m.ChatRequest(message="今天有什么课？", user_id="u2")
    resp = asyncio.run(m.chat(req, Response()))
    assert resp.response == "南校区食堂一般晚上七点关门。"  # 编排器结果，而非缓存内容
    assert resp.domain == "campus_life"

    # 流式 /chat/stream：同样丢弃 personal 缓存，走编排器
    state._orchestrator = _FakeOrchestrator()
    req = m.ChatRequest(message="今天有什么课？", user_id="u2")
    resp = asyncio.run(m.chat_stream(req))
    events = _parse_sse(_collect(resp))
    assert events[-1]["type"] == "done"
    assert events[-1].get("cached", False) is False
    assert "课表" not in events[-1]["response"]


# ── SSE 客户端断开取消（防僵尸任务）────────────────────────────────────────────

class _DisconnectedRequest:
    """
    模拟 SSE 客户端：is_disconnected 按给定序列返回（默认先连后断）。

    首查必须返回 False —— 让队列等待周期（0.5s）先把后台编排任务调度起来，
    再触发断开取消，否则取消落在任务启动前（编排器内部从未执行，测不到级联）。
    """

    def __init__(self, sequence=(False, True)):
        self._sequence = list(sequence)
        self._calls = 0
        self.headers = {}  # _benchmark_strategy 短路路径需要

    async def is_disconnected(self) -> bool:
        if self._calls >= len(self._sequence):
            return self._sequence[-1]
        value = self._sequence[self._calls]
        self._calls += 1
        return value


def test_chat_stream_client_disconnect_cancels_task(monkeypatch):
    """
    SSE 客户端断开 → 编排任务被取消（CancelledError 级联），且不写入记忆。
    回归场景：用户关页/断网后，服务端任务继续跑完 LLM + 写记忆（烧 token 污染数据）。
    """
    cancelled: dict = {"hit": False}
    writes: list = []

    class _SlowOrchestrator:
        async def run(self, req, on_event=None):
            try:
                await asyncio.sleep(30)  # 模拟长耗时编排
            except asyncio.CancelledError:
                cancelled["hit"] = True
                raise

    class _Mem(_FakeMemory):
        async def add_message(self, *args, **kwargs):
            writes.append(args)

    state._orchestrator = _SlowOrchestrator()
    state._memory = _Mem()
    state._semantic_cache = _FakeCache()
    monkeypatch.setattr("api.routers.chat.optional_user", lambda request: None)

    req = m.ChatRequest(message="南校区食堂几点关门？", user_id="u1")
    resp = asyncio.run(m.chat_stream(req, request=_DisconnectedRequest()))
    events = _parse_sse(_collect(resp))

    # 只发出 hello 即中断（不等待编排结果，也不产出 done）
    assert [e["type"] for e in events] == ["hello"]
    assert cancelled["hit"] is True  # 编排任务确实被取消
    assert writes == []  # 中断不写记忆


def test_chat_stream_disconnect_polled_during_silent_period(monkeypatch):
    """
    编排长时间不产生事件（队列静默）时，断开检测仍周期性生效：
    不能在 queue.get() 上无限阻塞 —— 否则断开后任务继续跑。
    """
    cancelled: dict = {"hit": False}

    class _SilentOrchestrator:
        async def run(self, req, on_event=None):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled["hit"] = True
                raise

    state._orchestrator = _SilentOrchestrator()
    state._memory = _FakeMemory()
    state._semantic_cache = _FakeCache()
    monkeypatch.setattr("api.routers.chat.optional_user", lambda request: None)

    req = m.ChatRequest(message="南校区食堂几点关门？", user_id="u1")
    resp = asyncio.run(m.chat_stream(req, request=_DisconnectedRequest()))
    events = _parse_sse(_collect(resp))

    assert [e["type"] for e in events] == ["hello"]
    assert cancelled["hit"] is True
