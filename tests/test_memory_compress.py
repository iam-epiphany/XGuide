"""工作记忆压缩（_compress）的并发安全与失败降级测试。

MemoryManager.__init__ 依赖真实 Redis/ChromaDB/Embedding，过重；
这里用 object.__new__ 跳过构造器，只装配 add_message/_compress 路径
实际触达的协作者（内存版 Redis / 假 gateway / 假 L0 层）。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from memory.conversation_memory import MemoryManager, MsgRole


class FakeRedis:
    """内存版 Redis：仅实现 add_message / _compress 用到的命令子集。"""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.strings: dict[str, str] = {}

    async def lpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).insert(0, value)

    async def llen(self, key: str) -> int:
        return len(self.lists.get(key, []))

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        lst = self.lists.get(key, [])
        return lst[start : stop + 1]

    async def delete(self, key: str) -> None:
        self.lists.pop(key, None)

    async def expire(self, key: str, ttl: int) -> None:
        return None

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.strings[key] = value


class FakeGateway:
    """统一模型入口假件：可注入延迟/异常，记录调用次数。"""

    def __init__(self, *, delay: float = 0.0, fail: bool = False) -> None:
        self.calls = 0
        self.delay = delay
        self.fail = fail

    async def call(self, **kwargs) -> SimpleNamespace:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("gateway down")
        return SimpleNamespace(response=SimpleNamespace(content=[SimpleNamespace(text=f"摘要-{self.calls}")]))


class FakeLayered:
    def __init__(self) -> None:
        self.raw: list[tuple] = []

    async def append_raw(self, user_id, conv_id, role, content, meta=None) -> int:
        self.raw.append((user_id, conv_id, role, content))
        return len(self.raw)


def _make_manager(gateway: FakeGateway) -> tuple[MemoryManager, FakeRedis, list]:
    mm = object.__new__(MemoryManager)
    redis = FakeRedis()
    episodic_calls: list[tuple] = []

    async def fake_store_episodic(user_id, conv_id, text, summary, layer="segment") -> None:
        episodic_calls.append((user_id, conv_id, text, summary, layer))

    mm._redis = redis
    mm._gateway = gateway
    mm._client = None
    mm._model = "test-model"
    mm.llm_call_count = 0
    mm._extract_locks = {}
    mm._layered = FakeLayered()
    mm._store_episodic = fake_store_episodic  # type: ignore[method-assign]
    return mm, redis, episodic_calls


async def _add(mm: MemoryManager, user: str, conv: str, text: str) -> None:
    await mm.add_message(user, conv, MsgRole.USER, text)


def test_concurrent_add_message_compresses_once_without_loss():
    """并发跨阈值：锁内二次检查保证只压缩一次，消息零丢失、L2 只写一块。"""
    gateway = FakeGateway(delay=0.01)  # 制造压缩窗口，放大并发交错
    mm, redis, episodic_calls = _make_manager(gateway)

    async def run():
        # 预置 14 条（差 1 条到 COMPRESS_AT=15）
        for i in range(14):
            await _add(mm, "u", "c", f"历史消息 {i}")
        # 两条并发写入同时跨过阈值
        await asyncio.gather(_add(mm, "u", "c", "并发 A"), _add(mm, "u", "c", "并发 B"))
        return await redis.llen(MemoryManager._wm_key("u", "c"))

    final_len = asyncio.run(run())

    # 压缩恰好触发一次：保留 5 条，两轮压缩不发生（第二把锁内 llen 已回落）
    assert final_len == 5
    assert gateway.calls == 1
    assert len(episodic_calls) == 1
    # 被压缩的消息有 L0 兜底，一条不丢
    assert len(mm._layered.raw) == 16
    # 成功调用才计数
    assert mm.llm_call_count == 1


def test_compress_llm_failure_skips_l2_no_placeholder_pollution():
    """摘要生成失败：不写占位场景块进 L2，工作记忆照常截断（L0 兜底）。"""
    gateway = FakeGateway(fail=True)
    mm, redis, episodic_calls = _make_manager(gateway)

    async def run():
        for i in range(16):
            await _add(mm, "u", "c", f"消息 {i}")
        return await redis.llen(MemoryManager._wm_key("u", "c"))

    final_len = asyncio.run(run())

    assert gateway.calls == 1
    assert episodic_calls == []  # 失败摘要绝不入 L2
    # 16 条顺序写入：第 15 条触发压缩（截到 5），第 16 条再进 → 6 条。
    # 若压缩没有发生（截断被跳过），这里会是 16。
    assert final_len == 6
    # 摘要缓存也不写占位内容
    assert MemoryManager._summary_key("u", "c") not in redis.strings


def test_compress_keeps_latest_five_in_order():
    """压缩后工作记忆是时序上最新的 5 条，且 JSON 结构完好可解析。"""
    gateway = FakeGateway()
    mm, redis, _ = _make_manager(gateway)

    async def run():
        for i in range(15):
            await _add(mm, "u", "c", f"消息 {i}")

    asyncio.run(run())

    raws = redis.lists[MemoryManager._wm_key("u", "c")]
    contents = [json.loads(r)["content"] for r in raws]
    assert set(contents) == {f"消息 {i}" for i in range(10, 15)}


def test_conv_lock_cache_is_bounded():
    """锁字典有界：超过上限清理未持有的锁，持锁中的锁绝不被清理。"""
    gateway = FakeGateway()
    mm, _, _ = _make_manager(gateway)

    async def hold_lock():
        hot = mm._conv_lock("u", "hot-conv")
        await hot.acquire()
        return hot

    hot = asyncio.run(hold_lock())  # 事件循环关闭后锁仍保持 locked 状态

    for i in range(600):
        mm._conv_lock("u", f"conv-{i}")

    assert len(mm._extract_locks) <= 512
    assert "u:hot-conv" in mm._extract_locks  # 持锁中的键不被驱逐
    hot.release()
