"""P0/P1 产品服务：Today 空闲时间、稳定画像与通知→计划闭环的底层保证。"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import date

from campus.radar import CampusRadar
from personal.service import PersonalService
from personal.store import PersonalStore


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
