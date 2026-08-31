"""P0/P1 产品服务：Today 空闲时间、稳定画像与通知→计划闭环的底层保证。"""
from __future__ import annotations

import asyncio
import hashlib

from campus.adapters import NoticeLink, NoticePage, PublicSourceAdapter
from campus.radar import CampusRadar
from mcp.tool_manager import MCPToolManager, Tool, ToolEffect
from personal.service import PersonalService
from personal.store import PersonalStore
from tools import with_service
from tools.campus_event_tool import get_campus_event_handler, query_campus_events_handler


def test_free_time_today_and_editable_todo(tmp_path):
    service = PersonalService(PersonalStore(str(tmp_path / "product.db")))
    asyncio.run(service.import_courses("u", [
        {"course": "高数", "day_of_week": 0, "start_time": "09:00", "end_time": "10:30", "weeks": "1-16"},
        {"course": "英语", "day_of_week": 0, "start_time": "14:00", "end_time": "15:30", "weeks": "1-16"},
    ]))
    free = asyncio.run(service.free_time("u", "2026-09-07"))
    assert free["free_periods"] == [
        {"start_time": "08:00", "end_time": "09:00"},
        {"start_time": "10:30", "end_time": "14:00"},
        {"start_time": "15:30", "end_time": "22:00"},
    ]
    todo = asyncio.run(service.add_todo("u", "交初稿", "ddl", "2026-09-10"))
    changed = asyncio.run(service.update_todo("u", todo["id"], content="交终稿", due_at="2026-09-12"))
    assert changed["content"] == "交终稿"
    assert changed["due_at"] == "2026-09-12"


def test_profile_filters_public_event_and_keeps_source(tmp_path):
    store = PersonalStore(str(tmp_path / "radar.db"))
    radar = CampusRadar(store)
    profile = asyncio.run(store.save_profile("u", {"education": "本科生", "interests": ["奖学金"], "college": ""}))
    url = "https://example.xidian.edu.cn/notice/1"
    inserted = radar._save_events_sync([{
        "fingerprint": hashlib.sha256(url.encode()).hexdigest(),
        "title": "关于开展本科生国家奖学金申请工作的通知",
        "summary": "本科生请于 2026年9月15日 前提交申请材料。",
        "body": "本科生请于 2026年9月15日 前提交申请材料。",
        "source_name": "本科生院", "source_category": "academic", "source_url": url,
        "published_at": "2026-09-01", "deadline": "2026-09-15", "targets": ["本科生"], "actions": ["申请", "提交"],
    }])
    assert inserted == 1
    inbox = asyncio.run(radar.inbox("u", profile))
    assert len(inbox) == 1
    assert inbox[0]["source_url"] == url
    assert inbox[0]["deadline"] == "2026-09-15"
    assert "奖学金" in inbox[0]["reason"]
    assert asyncio.run(radar.set_status("u", inbox[0]["id"], "interested")) is True


class _FakeAdapter(PublicSourceAdapter):
    name, category = "测试本科生院", "academic"

    def __init__(self):
        self.body = "本科生国家奖学金申请，截止 2026年9月15日。"

    async def discover(self, client):
        return [NoticeLink("https://notice.example/1", "国家奖学金申请", self.name, self.category)]

    async def fetch(self, client, link, *, etag=None, last_modified=None):
        return NoticePage(link, self.body, "v1", None)


class _EvidenceExtractor:
    async def extract(self, title, body):
        return {
            "event_type": "奖学金", "summary": body, "deadline": "2026-09-15", "targets": ["本科生"],
            "requirements": ["本科生"], "materials": ["成绩证明", "获奖材料"],
            "actions": ["填写申请表", "提交申请"], "location": "学生工作处", "extraction": "test",
        }


def test_incremental_event_sync_and_action_plan_provenance(tmp_path):
    store = PersonalStore(str(tmp_path / "incremental.db"))
    adapter = _FakeAdapter()
    radar = CampusRadar(store, adapters=[adapter], extractor=_EvidenceExtractor())
    first = asyncio.run(radar.refresh())
    assert first["new_events"] == 1
    second = asyncio.run(radar.refresh())
    assert second["unchanged"] == 1
    adapter.body = "本科生国家奖学金申请，截止 2026年9月20日。请准备成绩证明和获奖材料。"
    changed = asyncio.run(radar.refresh())
    assert changed["updated_events"] == 1

    profile = asyncio.run(store.save_profile("u", {"education": "本科生", "interests": ["奖学金"]}))
    event = asyncio.run(radar.inbox("u", profile))[0]
    personal = PersonalService(store)
    plan = asyncio.run(radar.create_action_plan("u", event["id"], personal))
    assert [item["content"] for item in plan["items"]] == ["准备成绩证明", "准备获奖材料", "填写申请表", "提交申请"]
    assert plan["items"][-1]["due_at"] == "2026-09-15"
    assert all(item["source_event_id"] == event["id"] for item in plan["items"])
    assert all(item["source_url"] == "https://notice.example/1" for item in plan["items"])


def test_agent_tool_reads_same_event_store_as_inbox(tmp_path):
    store = PersonalStore(str(tmp_path / "tool.db"))
    radar = CampusRadar(store, adapters=[_FakeAdapter()], extractor=_EvidenceExtractor())
    asyncio.run(radar.refresh())
    personal = PersonalService(store)
    asyncio.run(store.save_profile("u", {"education": "本科生", "interests": ["奖学金"]}))
    manager = MCPToolManager(api_key="test", model="test")
    manager.register(Tool(
        name="query_campus_events", effect=ToolEffect.READ, cache_ttl=0,
        description="test", schema={"type": "object"},
        handler=with_service(query_campus_events_handler, campus_radar=radar, personal_service=personal),
    ))
    manager.register(Tool(
        name="get_campus_event", effect=ToolEffect.READ, cache_ttl=0,
        description="test", schema={"type": "object"},
        handler=with_service(get_campus_event_handler, campus_radar=radar),
    ))
    result = asyncio.run(manager.call("query_campus_events", {"query": "奖学金"}, {"user_id": "u"}))
    assert result.success
    assert result.data["events"][0]["title"] == "国家奖学金申请"
    event_id = result.data["events"][0]["id"]
    detail = asyncio.run(manager.call("get_campus_event", {"id": event_id}, {"user_id": "u"}))
    assert detail.success
    assert detail.data["event"]["source_url"] == "https://notice.example/1"
