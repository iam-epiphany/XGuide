"""Campus Radar 健壮性回归：聚类词序、兴趣计分、伪日期、编码嗅探、robots、并发刷新。"""

from __future__ import annotations

import asyncio

import httpx

from campus.adapters import decode_response_text, html_text
from campus.radar import CampusRadar, _date_in

# ── 聚类词序 ─────────────────────────────────────────────────────────────────


def test_cluster_key_prefers_longer_subject():
    """更具体的主题词必须先命中："国家奖学金"不能被"奖学金"抢走分组。"""
    event = {"title": "关于开展2026年国家奖学金评选的通知", "summary": ""}
    assert CampusRadar._cluster_key(event).startswith("国家奖学金")


def test_cluster_key_falls_back_to_title():
    event = {"title": "关于调整图书馆开放区域的通知", "summary": ""}
    assert CampusRadar._cluster_key(event).startswith("title:")


# ── 兴趣关联计分 ──────────────────────────────────────────────────────────────


def test_attention_interest_scores_matched_count_not_list_size():
    """兴趣分按命中数计：配 4 个兴趣但事件只命中 1 个 → 3 分而非满分 10。"""
    event = {"title": "奖学金申请通知", "summary": "", "relevance": 3}
    profile = {"interests": ["奖学金", "竞赛", "就业", "考研"]}
    _, factors = CampusRadar._attention(event, profile, "opportunity")
    assert factors["兴趣关联"] == 3

    none_matched = CampusRadar._attention({"title": "讲座通知", "summary": "", "relevance": 3}, profile, "campus_life")
    assert none_matched[1]["兴趣关联"] == 0


# ── 日期解析 ─────────────────────────────────────────────────────────────────


def test_date_in_rejects_invalid_calendar_dates():
    assert _date_in("2024年13月32日开会") is None
    assert _date_in("2024年0月5日") is None
    assert _date_in("2026年9月20日截止") == "2026-09-20"


# ── 编码嗅探 ─────────────────────────────────────────────────────────────────


def test_decode_response_text_sniffs_gbk_from_meta():
    """GB2312 老站点（无 charset 响应头）按 meta 解码，不再整页乱码静默 0 条。"""
    html = '<html><head><meta charset="gb2312"></head><body>关于奖学金评选的通知</body></html>'
    response = httpx.Response(200, content=html.encode("gb2312"), headers={"content-type": "text/html"})
    assert response.charset_encoding is None  # 响应头未声明 → 走 meta 嗅探
    decoded = decode_response_text(response)
    assert "奖学金" in decoded
    assert "奖学金" in html_text(decoded)


def test_decode_response_text_honors_header_charset():
    response = httpx.Response(200, content="通知".encode(), headers={"content-type": "text/html; charset=utf-8"})
    assert decode_response_text(response) == "通知"


# ── robots 与并发刷新 ─────────────────────────────────────────────────────────


class _DisallowRobots:
    async def allowed(self, client, url, user_agent):
        return False


class _AllowRobotsStub:
    async def allowed(self, client, url, user_agent):
        return True


class _NoFetchAdapter:
    name, category = "测试", "academic"
    listing_url = "https://notice.example/list"

    async def discover(self, client):
        raise AssertionError("robots 禁止时不应发起列表请求")

    async def fetch(self, client, link, *, etag=None, last_modified=None):  # pragma: no cover
        raise AssertionError


class _NeverAdapter(_NoFetchAdapter):
    async def discover(self, client):
        return []


def test_refresh_skips_source_disallowed_by_robots(tmp_path):
    from personal.store import PersonalStore

    radar = CampusRadar(
        PersonalStore(str(tmp_path / "robots.db")), adapters=[_NoFetchAdapter()], robots=_DisallowRobots()
    )
    result = asyncio.run(radar.refresh())
    assert result["skipped_by_robots"] == 1
    assert result["checked"] == 0


def test_refresh_returns_in_progress_when_already_running(tmp_path):
    """一轮 refresh 进行中的重复调用不重复抓取，直接返回进行中标记。"""
    from campus.adapters import NoticeLink, NoticePage
    from personal.store import PersonalStore

    class _SlowAdapter(_NeverAdapter):
        started = asyncio.Event()
        release = asyncio.Event()

        async def discover(self, client):
            _SlowAdapter.started.set()
            await _SlowAdapter.release.wait()
            return [NoticeLink("https://notice.example/1", "通知标题足够长了吧", self.name, self.category)]

        async def fetch(self, client, link, *, etag=None, last_modified=None):
            return NoticePage(link, "正文", None, None)

    adapter = _SlowAdapter()
    radar = CampusRadar(PersonalStore(str(tmp_path / "lock.db")), adapters=[adapter], robots=_AllowRobotsStub())

    async def run():
        first = asyncio.create_task(radar.refresh())
        await asyncio.wait_for(adapter.started.wait(), timeout=2)
        second = await radar.refresh()
        adapter.release.set()
        return await first, second

    async def runner():
        return await asyncio.wait_for(run(), timeout=5)

    first, second = asyncio.run(runner())
    assert "in_progress" in second
    assert first["checked"] == 1


# ── published_at 不再用正文日期兜底 ──────────────────────────────────────────


def test_upsert_keeps_listing_date_without_body_fallback(tmp_path):
    """列表页日期缺失时 published_at 为空，而不是误取正文里的历史日期。"""
    from campus.extractor import CampusEventExtractor
    from personal.store import PersonalStore

    store = PersonalStore(str(tmp_path / "pubdate.db"))
    radar = CampusRadar(store, extractor=CampusEventExtractor())

    async def seed():
        await store._run(
            radar._upsert_event_sync,
            {
                "url": "https://notice.example/legacy",
                "title": "奖学金评选通知",
                "summary": "",
                "body": "该活动始于 2020年10月1日，今年继续举办。",
                "source_name": "测试",
                "source_category": "academic",
                "published_at": None,
                "content_hash": "x",
            },
        )
        return await store._run(radar._event_sync, "https://notice.example/legacy")

    event = asyncio.run(seed())
    assert event["published_at"] is None
