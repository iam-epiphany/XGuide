"""P0/P1 产品服务：Today 空闲时间、稳定画像与通知→计划闭环的底层保证。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
from types import SimpleNamespace

import httpx
import pytest

from api import state
from api.routers.memory import get_schedule
from campus.adapters import CPIPCCompetitionAdapter, HtmlNoticeAdapter, NoticeLink, NoticePage, PublicSourceAdapter
from campus.radar import CampusRadar
from mcp.tool_manager import MCPToolManager, Tool, ToolEffect
from personal.service import PersonalService
from personal.store import PersonalStore
from tools import with_service
from tools.campus_event_tool import get_campus_event_handler, query_campus_events_handler


@pytest.fixture(autouse=True)
def _no_semantic_layer(monkeypatch):
    """本模块固化纯关键词契约（嵌入模型不可用的世界线）。

    语义相关度加成/语义聚类是可选增强，其行为由 test_llm_product_features.py
    用确定性嵌入替身单独覆盖；在这里禁用后，断言不依赖本地模型缓存状态。
    """
    monkeypatch.setattr("campus.semantic.get_embedder", lambda: None)


class _AllowRobots:
    """robots 替身：测试不做真实网络请求，全部放行。"""

    async def allowed(self, client, url, user_agent):
        return True


def test_free_time_today_and_editable_todo(tmp_path):
    service = PersonalService(PersonalStore(str(tmp_path / "product.db")))
    asyncio.run(
        service.import_courses(
            "u",
            [
                {"course": "高数", "day_of_week": 0, "start_time": "09:00", "end_time": "10:30", "weeks": "1-16"},
                {"course": "英语", "day_of_week": 0, "start_time": "14:00", "end_time": "15:30", "weeks": "1-16"},
            ],
        )
    )
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
    radar = CampusRadar(store, robots=_AllowRobots())
    profile = asyncio.run(store.save_profile("u", {"education": "本科生", "interests": ["奖学金"], "college": ""}))
    url = "https://example.xidian.edu.cn/notice/1"
    inserted = radar._save_events_sync(
        [
            {
                "fingerprint": hashlib.sha256(url.encode()).hexdigest(),
                "title": "关于开展本科生国家奖学金申请工作的通知",
                "summary": "本科生请于 2026年9月15日 前提交申请材料。",
                "body": "本科生请于 2026年9月15日 前提交申请材料。",
                "source_name": "本科生院",
                "source_category": "academic",
                "source_url": url,
                "published_at": "2026-09-01",
                "deadline": "2026-09-15",
                "targets": ["本科生"],
                "actions": ["申请", "提交"],
            }
        ]
    )
    assert inserted == 1
    inbox = asyncio.run(radar.inbox("u", profile))
    assert len(inbox) == 1
    assert inbox[0]["source_url"] == url
    assert inbox[0]["deadline"] == "2026-09-15"
    assert "奖学金" in inbox[0]["reason"]
    assert asyncio.run(radar.set_status("u", inbox[0]["id"], "interested")) is True


def test_html_adapter_filters_notice_links_and_preserves_listing_date():
    html = """
    <a href="/tzgg.htm" title="通知公告">通知公告</a>
    <a href="/info/1012/23223.htm" title="关于第一学期选课的通知">关于第一学期选课的通知 <span>2026-09-01</span></a>
    <a href="/info/1012/23224.htm">2026-09-02 关于开展实验班选拔的通知</a>
    <a href="/info/1023/21373.htm" title="教改新闻">教改新闻 <span>2026-09-02</span></a>
    """

    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text=html))
        async with httpx.AsyncClient(transport=transport) as client:
            adapter = HtmlNoticeAdapter(
                name="本科生院",
                category="academic",
                listing_url="https://jwc.example/tzgg.htm",
                include_url_patterns=(r"/info/1012/\d+\.htm$",),
            )
            return await adapter.discover(client)

    links = asyncio.run(run())
    assert [(link.title, link.published_at) for link in links] == [
        ("关于第一学期选课的通知", "2026-09-01"),
        ("关于开展实验班选拔的通知", "2026-09-02"),
    ]


def test_cpipc_adapter_splits_public_schedule_into_competitions_with_deadlines():
    listing = '<a href="/pw/notice/detail/schedule">2026年度各主题赛事赛程一览</a>'
    schedule = """
    <h1>2026年度中国研究生创新实践系列大赛各主题赛事赛程一览</h1>
    <span>发布时间：2026-05-21</span>
    <table><tr><th>序号</th><th>主题赛事</th><th>报名时间</th><th>提交作品时间</th><th>决赛时间</th></tr>
    <tr><td>1</td><td>人工智能创新大赛</td><td>5月22日-8月25日</td><td>5月22日-9月1日</td><td>待定</td></tr>
    <tr><td>2</td><td>网络安全创新大赛</td><td>6月18日-10月18日</td><td>6月18日-10月22日</td><td>待定</td></tr></table>
    """

    async def run():
        def handler(request):
            return httpx.Response(200, text=listing if request.url.path.endswith("/list/1") else schedule)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = CPIPCCompetitionAdapter(
                listing_url="https://cpipc.example/pw/notice/list/1",
                fallback_schedule_url="https://cpipc.example/pw/notice/detail/fallback",
            )
            links = await adapter.discover(client)
            pages = [await adapter.fetch(client, link) for link in links]
            return links, pages

    links, pages = asyncio.run(run())
    assert [link.title for link in links] == ["中国研究生人工智能创新大赛", "中国研究生网络安全创新大赛"]
    assert [link.published_at for link in links] == ["2026-05-21", "2026-05-21"]
    assert "报名截止：2026年8月25日" in pages[0].body
    assert "报名截止：2026年10月18日" in pages[1].body
    assert all("#competition-" in link.url for link in links)


def test_inbox_does_not_deliver_events_after_their_deadline(tmp_path):
    store = PersonalStore(str(tmp_path / "expired-competition.db"))
    radar = CampusRadar(store, robots=_AllowRobots())
    today = datetime.now().astimezone().date()
    expired, active = (today - timedelta(days=1)).isoformat(), (today + timedelta(days=1)).isoformat()
    events = []
    for suffix, deadline in (("expired", expired), ("active", active)):
        url = f"https://cpipc.example/competition/{suffix}"
        events.append(
            {
                "fingerprint": hashlib.sha256(url.encode()).hexdigest(),
                "title": f"中国研究生人工智能创新大赛-{suffix}",
                "summary": f"研究生报名截止：{deadline.replace('-', '年', 1).replace('-', '月', 1)}日",
                "body": "研究生竞赛报名",
                "source_name": "中国研究生创新实践系列大赛",
                "source_category": "competition",
                "source_url": url,
                "published_at": today.isoformat(),
                "deadline": deadline,
                "targets": ["研究生"],
                "actions": ["报名"],
            }
        )
    assert radar._save_events_sync(events) == 2

    inbox = asyncio.run(radar.inbox("u", {"education": "研究生", "interests": ["竞赛"]}))

    assert [event["deadline"] for event in inbox] == [active]


def test_inbox_ttl_and_personal_deletion_do_not_recreate_notifications(tmp_path):
    store = PersonalStore(str(tmp_path / "inbox-ttl.db"))
    radar = CampusRadar(store, inbox_ttl_hours=48, robots=_AllowRobots())
    today = datetime.now().astimezone().date()
    url = "https://notice.example/ttl"
    radar._save_events_sync(
        [
            {
                "fingerprint": hashlib.sha256(url.encode()).hexdigest(),
                "title": "本科生奖学金申请",
                "summary": "本科生奖学金申请",
                "body": "本科生奖学金申请",
                "source_name": "本科生院",
                "source_category": "academic",
                "source_url": url,
                "published_at": today.isoformat(),
                "deadline": (today + timedelta(days=7)).isoformat(),
                "targets": ["本科生"],
                "actions": ["申请"],
            }
        ]
    )
    profile = {"education": "本科生", "interests": ["奖学金"]}
    event = asyncio.run(radar.inbox("u", profile))[0]
    assert event["expires_at"] > event["updated_at"]

    assert asyncio.run(radar.delete_inbox("u", [event["id"]])) == 1
    assert asyncio.run(radar.inbox("u", profile)) == []

    # TTL 已到期的记录即使仍可从公共源计算出相关性，也不能再次显示。
    with store._connect() as conn:
        conn.execute("UPDATE campus_inbox SET status='new', expires_at='2000-01-01 00:00:00' WHERE user_id='u'")
    assert asyncio.run(radar.inbox("u", profile)) == []


def test_empty_profile_receives_recent_public_notices(tmp_path):
    radar = CampusRadar(
        PersonalStore(str(tmp_path / "recent-notices.db")), adapters=[_FakeAdapter()], robots=_AllowRobots()
    )
    asyncio.run(radar.refresh())

    events = asyncio.run(radar.inbox("u", {}))

    assert len(events) == 1
    assert events[0]["relevance"] == 1
    assert "最近 24 小时" in events[0]["reason"]

    # 保存画像后，恢复既有的相关性阈值；不相关画像不接收该奖学金通知。
    filtered = asyncio.run(radar.inbox("u2", {"education": "研究生", "interests": ["出国"]}))
    assert filtered == []


def test_schedule_endpoint_keeps_full_schedule_outside_current_week(tmp_path, monkeypatch):
    service = PersonalService(PersonalStore(str(tmp_path / "schedule.db")))
    asyncio.run(
        service.import_courses(
            "u",
            [
                {
                    "course": "高等数学",
                    "day_of_week": 0,
                    "start_time": "08:30",
                    "end_time": "10:05",
                    "location": "B-101",
                    "weeks": "1-16",
                }
            ],
        )
    )
    monkeypatch.setattr(state, "_personal_service", service)

    response = asyncio.run(get_schedule(user=SimpleNamespace(id="u")))

    assert [course["course"] for course in response["courses"]] == ["高等数学"]
    assert response["total"] == 1
    assert "weekly_courses" in response


class _FakeAdapter(PublicSourceAdapter):
    name, category = "测试本科生院", "academic"

    def __init__(self):
        self.body = "本科生国家奖学金申请，截止 2026年9月15日。"

    async def discover(self, client):
        return [NoticeLink("https://notice.example/1", "国家奖学金申请", self.name, self.category)]

    async def fetch(self, client, link, *, etag=None, last_modified=None):
        return NoticePage(link, self.body, "v1", None)


class _PartiallyFailingAdapter(_FakeAdapter):
    async def discover(self, client):
        return [
            NoticeLink("https://notice.example/fail", "读取失败的通知", self.name, self.category),
            NoticeLink("https://notice.example/ok", "国家奖学金申请", self.name, self.category, "2026-09-01"),
        ]

    async def fetch(self, client, link, *, etag=None, last_modified=None):
        if link.url.endswith("/fail"):
            raise httpx.ReadTimeout("temporary")
        return NoticePage(link, self.body, "v1", None)


class _EvidenceExtractor:
    async def extract(self, title, body):
        return {
            "event_type": "奖学金",
            "summary": body,
            "deadline": "2026-09-15",
            "targets": ["本科生"],
            "requirements": ["本科生"],
            "materials": ["成绩证明", "获奖材料"],
            "actions": ["填写申请表", "提交申请"],
            "location": "学生工作处",
            "extraction": "test",
        }


def test_incremental_event_sync_and_action_plan_provenance(tmp_path):
    store = PersonalStore(str(tmp_path / "incremental.db"))
    adapter = _FakeAdapter()
    radar = CampusRadar(store, adapters=[adapter], extractor=_EvidenceExtractor(), robots=_AllowRobots())
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


def test_refresh_keeps_syncing_when_one_notice_page_fails(tmp_path):
    radar = CampusRadar(
        PersonalStore(str(tmp_path / "partial.db")), adapters=[_PartiallyFailingAdapter()], robots=_AllowRobots()
    )
    result = asyncio.run(radar.refresh())
    assert result["checked"] == 2
    assert result["new_events"] == 1
    assert result["failed"] == 1
    assert result["errors"] == [{"source": "测试本科生院", "message": "1 条通知读取失败"}]


def test_agent_tool_reads_same_event_store_as_inbox(tmp_path):
    store = PersonalStore(str(tmp_path / "tool.db"))
    radar = CampusRadar(store, adapters=[_FakeAdapter()], extractor=_EvidenceExtractor(), robots=_AllowRobots())
    asyncio.run(radar.refresh())
    personal = PersonalService(store)
    asyncio.run(store.save_profile("u", {"education": "本科生", "interests": ["奖学金"]}))
    manager = MCPToolManager(api_key="test", model="test")
    manager.register(
        Tool(
            name="query_campus_events",
            effect=ToolEffect.READ,
            cache_ttl=0,
            description="test",
            schema={"type": "object"},
            handler=with_service(query_campus_events_handler, campus_radar=radar, personal_service=personal),
        )
    )
    manager.register(
        Tool(
            name="get_campus_event",
            effect=ToolEffect.READ,
            cache_ttl=0,
            description="test",
            schema={"type": "object"},
            handler=with_service(get_campus_event_handler, campus_radar=radar),
        )
    )
    result = asyncio.run(manager.call("query_campus_events", {"query": "奖学金"}, {"user_id": "u"}))
    assert result.success
    assert result.data["events"][0]["title"] == "国家奖学金申请"
    event_id = result.data["events"][0]["id"]
    detail = asyncio.run(manager.call("get_campus_event", {"id": event_id}, {"user_id": "u"}))
    assert detail.success
    assert detail.data["event"]["source_url"] == "https://notice.example/1"
